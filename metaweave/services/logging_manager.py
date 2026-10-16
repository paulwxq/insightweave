"""统一日志管理共享入口（doc 15）。

API 与 CLI 两个运行配置共用一个 ``configs/logging.yaml``：顶层
``formatters``/``filters``/``handlers``/``loggers``/``root`` 是 script
运行配置的原始形态，由 ``script`` 分支整段沿用；``x_profiles.api`` 是
API 专用模板，``x_modules`` 是后端模块注册表，只有 ``api`` 分支读取。
业务模块仍通过 ``logging.getLogger(...)`` 写日志，本模块只负责初始化
配置、识别模块归属与补充运行上下文，不安装脚本步骤记录工厂。
"""

import contextvars
import logging
import logging.config
import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

from metaweave.utils.file_utils import resolve_project_path

API_PROFILE = "api"
SCRIPT_PROFILE = "script"
PLATFORM_MODULE_ID = "platform"
ACCESS_LOGGER_NAME = "platform.http.access"
ACCESS_FILE_HANDLER_ID = "api_file"

_MODULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class LoggingConfigError(ValueError):
    """日志配置或模块注册表非法（doc 15 §3.2：装载时校验失败）。"""


# ---------------------------------------------------------------------------
# 模块注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleRegistration:
    """一个后端模块的注册项（doc 15 §3.2）。"""

    module_id: str
    backend_file: Path
    api_route_prefixes: tuple[str, ...] = ()
    logger_prefixes: tuple[str, ...] = ()


class ModuleRegistry:
    """校验后的模块注册表：按请求路径或 logger 名解析归属模块。"""

    def __init__(self, modules: Iterable[ModuleRegistration]):
        self._modules = tuple(modules)
        self._by_id = {m.module_id: m for m in self._modules}
        self._route_index: dict[str, str] = {}
        self._logger_index: dict[str, str] = {}
        for module in self._modules:
            for prefix in module.api_route_prefixes:
                self._route_index[prefix] = module.module_id
            for prefix in module.logger_prefixes:
                self._logger_index[prefix] = module.module_id

    @property
    def module_ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def resolve_request_module(self, route_path: str) -> Optional[str]:
        """按完整路径段边界匹配注册的最长 api_route_prefixes。"""
        best: Optional[str] = None
        best_length = -1
        for prefix, module_id in self._route_index.items():
            if len(prefix) > best_length and _path_prefix_matches(route_path, prefix):
                best = module_id
                best_length = len(prefix)
        return best

    def resolve_logger_module(self, logger_name: str) -> Optional[str]:
        """按完整名称或 `.` 分隔子 logger 匹配最长 logger_prefixes。"""
        best: Optional[str] = None
        best_length = -1
        for prefix, module_id in self._logger_index.items():
            if len(prefix) > best_length and _logger_name_matches(logger_name, prefix):
                best = module_id
                best_length = len(prefix)
        return best


def _path_prefix_matches(route_path: str, prefix: str) -> bool:
    if prefix == "/":
        return True
    if route_path == prefix:
        return True
    stripped = prefix.rstrip("/")
    return bool(stripped) and route_path.startswith(stripped + "/")


def _logger_name_matches(logger_name: str, prefix: str) -> bool:
    return logger_name == prefix or logger_name.startswith(prefix + ".")


def _build_registry(x_modules: Any) -> ModuleRegistry:
    """校验 x_modules 并构造注册表；冲突/非法时抛 LoggingConfigError。"""
    if not isinstance(x_modules, dict) or not x_modules:
        raise LoggingConfigError("x_modules 必须是非空 mapping")

    if PLATFORM_MODULE_ID not in x_modules:
        raise LoggingConfigError(
            "x_modules 必须注册兜底模块 'platform'（接收未归属的项目/共享代码记录）"
        )

    registrations: list[ModuleRegistration] = []
    seen_backend_files: dict[str, str] = {}
    seen_route_prefixes: dict[str, str] = {}
    seen_logger_prefixes: dict[str, str] = {}

    for module_id, module_cfg in x_modules.items():
        if not isinstance(module_id, str) or not _MODULE_ID_PATTERN.match(module_id):
            raise LoggingConfigError(
                f"x_modules 模块 ID 非法: {module_id!r}"
                "（只允许小写字母、数字、下划线且以字母开头）"
            )
        if not isinstance(module_cfg, dict):
            raise LoggingConfigError(f"x_modules.{module_id} 必须是 mapping")

        backend_file = module_cfg.get("backend_file")
        if not isinstance(backend_file, str) or not backend_file.strip():
            raise LoggingConfigError(
                f"x_modules.{module_id}.backend_file 必须是非空字符串"
            )
        # 按规范化后的实际目标路径判重与存储：`..` 等别名指向同一文件时
        # 必须拒绝，否则两个 RotatingFileHandler 会写并轮转同一文件
        resolved_backend = str(resolve_project_path(backend_file).resolve())
        if resolved_backend in seen_backend_files:
            raise LoggingConfigError(
                f"x_modules.{module_id}.backend_file 与模块 "
                f"{seen_backend_files[resolved_backend]!r} 重复: {backend_file}"
            )
        seen_backend_files[resolved_backend] = module_id

        route_prefixes = tuple(
            _validated_prefixes(
                module_cfg.get("api_route_prefixes"),
                f"x_modules.{module_id}.api_route_prefixes",
                require_leading_slash=True,
            )
        )
        for prefix in route_prefixes:
            normalized = prefix.rstrip("/") or "/"
            if normalized in seen_route_prefixes:
                raise LoggingConfigError(
                    f"x_modules.{module_id}.api_route_prefixes 前缀 {prefix!r} "
                    f"已注册给模块 {seen_route_prefixes[normalized]!r}"
                )
            seen_route_prefixes[normalized] = module_id

        logger_prefixes = tuple(
            _validated_prefixes(
                module_cfg.get("logger_prefixes"),
                f"x_modules.{module_id}.logger_prefixes",
                require_leading_slash=False,
            )
        )
        for prefix in logger_prefixes:
            if prefix in seen_logger_prefixes:
                raise LoggingConfigError(
                    f"x_modules.{module_id}.logger_prefixes 前缀 {prefix!r} "
                    f"已注册给模块 {seen_logger_prefixes[prefix]!r}"
                )
            seen_logger_prefixes[prefix] = module_id

        registrations.append(
            ModuleRegistration(
                module_id=module_id,
                backend_file=Path(resolved_backend),
                api_route_prefixes=route_prefixes,
                logger_prefixes=logger_prefixes,
            )
        )

    return ModuleRegistry(registrations)


def _validated_prefixes(
    value: Any, path: str, *, require_leading_slash: bool
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LoggingConfigError(f"{path} 必须是列表")
    prefixes: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LoggingConfigError(f"{path} 的每一项都必须是非空字符串: {item!r}")
        if item != item.strip():
            raise LoggingConfigError(f"{path} 的项不得带首尾空白: {item!r}")
        if require_leading_slash and not item.startswith("/"):
            raise LoggingConfigError(f"{path} 的项必须以 '/' 开头: {item!r}")
        prefixes.append(item)
    return prefixes


# ---------------------------------------------------------------------------
# 请求上下文与模块归属
# ---------------------------------------------------------------------------

_REQUEST_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "metaweave_request_id", default=None
)
_REQUEST_MODULE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "metaweave_request_module", default=None
)

# api 运行配置装载成功后生效；script 运行配置不使用归属判定
_ACTIVE_REGISTRY: Optional[ModuleRegistry] = None


def set_request_id(value: Optional[str]):
    """设置当前请求 ID（由 ASGI 包装器调用），返回可 reset 的 token。"""
    return _REQUEST_ID.set(value)


def reset_request_id(token) -> None:
    # 用 var.reset(token) 而非 token.reset()：本项目的 CPython 构建里
    # Token 未暴露 reset 方法
    _REQUEST_ID.reset(token)


def get_request_id() -> Optional[str]:
    return _REQUEST_ID.get()


def set_request_module(value: Optional[str]):
    """设置当前请求的归属模块 ID，返回可 reset 的 token。"""
    return _REQUEST_MODULE.set(value)


def reset_request_module(token) -> None:
    _REQUEST_MODULE.reset(token)


def get_request_module() -> Optional[str]:
    return _REQUEST_MODULE.get()


def resolve_record_module(record: logging.LogRecord) -> str:
    """共享归属判定（doc 15 §3.2 优先级）：请求上下文 > logger 前缀 > platform。"""
    module = _REQUEST_MODULE.get()
    if module:
        return module
    if _ACTIVE_REGISTRY is not None:
        found = _ACTIVE_REGISTRY.resolve_logger_module(record.name)
        if found:
            return found
    return PLATFORM_MODULE_ID


class ApiContextFilter(logging.Filter):
    """为记录补齐 request_id 与 app_module（api_file/console 用）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _REQUEST_ID.get() or "-"
        record.app_module = resolve_record_module(record)
        return True


class ApiModuleFilter(logging.Filter):
    """补齐两个字段，并只放行归属本模块的记录（每个后端文件 handler 用）。"""

    def __init__(self, target_module: str):
        super().__init__()
        self.target_module = target_module

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _REQUEST_ID.get() or "-"
        record.app_module = resolve_record_module(record)
        return record.app_module == self.target_module


# ---------------------------------------------------------------------------
# 线程池上下文传播（doc 15 §3.2 优先级 2）
# ---------------------------------------------------------------------------


def submit_with_context(
    executor: ThreadPoolExecutor, func: Callable, /, *args: Any, **kwargs: Any
) -> Future:
    """提交线程池任务时为该任务单独复制当前上下文并以其运行。

    ``ThreadPoolExecutor.submit`` 不会自动传播 ContextVar；每个任务独立
    ``copy_context()``（同一个 Context 不能并发供多个任务运行）。
    """
    context = contextvars.copy_context()
    return executor.submit(context.run, func, *args, **kwargs)


# ---------------------------------------------------------------------------
# 运行配置装载
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoggingSetup:
    profile: str
    config_path: Path
    modules: ModuleRegistry


def configure_logging(
    profile: str, config_path: "str | Path | None" = None
) -> LoggingSetup:
    """按运行配置初始化项目日志（doc 15 §3.1）。

    Args:
        profile: ``api``（FastAPI/Uvicorn 进程）或 ``script``（CLI 进程）。
        config_path: 日志配置文件路径；缺省为 ``configs/logging.yaml``，
            相对路径统一经 ``resolve_project_path()`` 基于项目根解析。

    Raises:
        FileNotFoundError: 配置文件不存在。
        ValueError / LoggingConfigError: 配置文件非法。
    """
    if profile not in (API_PROFILE, SCRIPT_PROFILE):
        raise ValueError(f"未知日志运行配置 profile: {profile!r}")
    resolved = _resolve_config_file(config_path)
    try:
        cfg = _load_yaml_config(resolved)
        if profile == API_PROFILE:
            return _configure_api(cfg, resolved)
        return _configure_script(cfg, resolved)
    except OSError as exc:
        # 装载边界的文件系统错误（配置不可读、日志目录/文件创建失败）
        # 统一转成带配置路径的明确错误；LoggingConfigError 是 ValueError
        # 子类，CLI 现有分支会转成 exit 2（doc 15 §3.1"存在、可读、普通文件"）
        raise LoggingConfigError(
            f"日志配置装载失败（无法读取配置或创建日志文件）: {resolved} ({exc})"
        ) from exc


def _resolve_config_file(config_path: "str | Path | None") -> Path:
    if config_path is None:
        config_path = "configs/logging.yaml"
    path = resolve_project_path(config_path)
    # 先完成两次 stat 再走语义分支：无权限父目录下 Path.exists()/is_file()
    # 的 stat 本身抛 EACCES（不会返回 False），须转成带路径的明确错误；
    # 而"不存在"单独保留 FileNotFoundError，默认配置缺失时 CLI 仍走
    # 警告 + basicConfig 回退（doc 15 §3.3），不能在 try 内抛出后被转换
    try:
        path_exists = path.exists()
        path_is_file = path.is_file() if path_exists else False
    except OSError as exc:
        raise LoggingConfigError(f"日志配置文件不可访问: {path} ({exc})") from exc
    if not path_exists:
        raise FileNotFoundError(f"日志配置文件不存在: {path}")
    if not path_is_file:
        raise LoggingConfigError(f"日志配置文件不是普通文件: {path}")
    return path


def _load_yaml_config(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise LoggingConfigError(f"日志配置文件不是合法 YAML: {path} ({exc})") from exc
    if not isinstance(cfg, dict):
        raise LoggingConfigError(f"日志配置必须是 mapping: {path}")
    return cfg


def _configure_script(cfg: dict, config_path: Path) -> LoggingSetup:
    """script 运行配置：整段沿用顶层标准配置，只解析相对日志路径。"""
    script_cfg = {
        key: value
        for key, value in cfg.items()
        if key not in ("x_profiles", "x_modules")
    }
    handlers = script_cfg.get("handlers") or {}
    _resolve_handler_filenames(handlers)
    # 与 api 分支同一套文件目标判重（Path.resolve()，`..` 别名等价）：
    # 同一文件不得被两个 RotatingFileHandler 写并轮转。目录/可写检查
    # 两个分支本就共用，判重不应只在 api 侧生效
    _reject_duplicate_handler_files(handlers)
    _prepare_log_dirs(handlers)
    logging.config.dictConfig(script_cfg)
    # 装载成功后清空可能的 api 残留注册表：script 进程不做模块归属
    # 判定，"先 api 后 script"的同进程序列不得留下过期归属映射
    global _ACTIVE_REGISTRY
    _ACTIVE_REGISTRY = None
    return LoggingSetup(SCRIPT_PROFILE, config_path, ModuleRegistry(()))


def _configure_api(cfg: dict, config_path: Path) -> LoggingSetup:
    """api 运行配置：按 x_profiles.api 模板 + x_modules 注册表组装并裁剪。"""
    profiles = cfg.get("x_profiles")
    if not isinstance(profiles, dict) or not isinstance(profiles.get("api"), dict):
        raise LoggingConfigError("x_profiles.api 模板缺失或不是 mapping")
    template = profiles["api"]
    registry = _build_registry(cfg.get("x_modules"))

    for required in ("formatters", "handlers", "root", "backend_handler_template"):
        if not isinstance(template.get(required), dict):
            raise LoggingConfigError(f"x_profiles.api.{required} 缺失或不是 mapping")

    dict_cfg: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": deepcopy(template["formatters"]),
        "filters": deepcopy(template.get("filters") or {}),
        "handlers": deepcopy(template["handlers"]),
        "loggers": deepcopy(template.get("loggers") or {}),
        "root": deepcopy(template["root"]),
    }
    if not isinstance(dict_cfg["root"].get("handlers"), list):
        raise LoggingConfigError("x_profiles.api.root.handlers 必须是列表")

    backend_template = template["backend_handler_template"]

    # 先解析模板文件 handler 并校验后端目标不与其冲突，再组装模块
    # handler：此时 handlers 只含模板项，模块 backend_file 不会与自己比较
    _resolve_handler_filenames(dict_cfg["handlers"])
    _reject_backend_file_collisions(registry, dict_cfg["handlers"])

    for module in registry._modules:
        filter_id = f"api_module_{module.module_id}"
        handler_id = f"backend_{module.module_id}_file"
        dict_cfg["filters"][filter_id] = {
            "()": f"{__name__}.ApiModuleFilter",
            "target_module": module.module_id,
        }
        handler = deepcopy(backend_template)
        # backend_file 在构建注册表时已 resolve() 为绝对路径
        handler["filename"] = str(module.backend_file)
        handler["filters"] = [filter_id]
        dict_cfg["handlers"][handler_id] = handler
        dict_cfg["root"]["handlers"] = [*dict_cfg["root"]["handlers"], handler_id]

    # 组装之后校验摘要 logger 契约（doc 15 §3.1：handlers 恰好
    # [api_file]、propagate: false、级别放行 INFO、api_file 不被复用）
    _validate_access_logger(dict_cfg["loggers"], dict_cfg["handlers"], dict_cfg["root"])
    # 全量文件目标判重兜底：模板内部、模板↔注册表、注册表内部三个
    # 组合中任何两个 handler 指向同一实际文件都在此拒绝
    _reject_duplicate_handler_files(dict_cfg["handlers"])
    _prepare_log_dirs(dict_cfg["handlers"])

    logging.config.dictConfig(dict_cfg)
    # dictConfig 成功后才激活注册表：装载失败时不留"注册表已换、配置
    # 未生效"的不一致状态；filter 最早在首条日志记录时读取，时序安全
    global _ACTIVE_REGISTRY
    _ACTIVE_REGISTRY = registry
    return LoggingSetup(API_PROFILE, config_path, registry)


def _validate_access_logger(loggers: dict, handlers: dict, root: dict) -> None:
    """校验请求摘要 logger 契约（doc 15 §3.1：只写 api_file、propagate: false）。

    dictConfig 不校验该契约：改名或漏配时摘要记录经 root 传播分流进
    后端文件、api.log 一条没有，服务却看似正常（doc 15 §8.1"恰好一条"
    与 §3.3"不静默退回"均被破坏）。handlers 按 handler 身份严格校验
    （必须恰好 ``[api_file]``）：换成其它文件 handler 会把摘要接到
    别的文件、留下空的 api.log，比文件不存在更具误导性；摘要文件的
    路径与文件名在 ``api_file.filename`` 配置。
    """
    entry = loggers.get(ACCESS_LOGGER_NAME)
    if not isinstance(entry, dict):
        raise LoggingConfigError(
            f"x_profiles.api.loggers 必须配置请求摘要 logger "
            f"{ACCESS_LOGGER_NAME!r}（只写 api_file、propagate: false）"
        )
    if entry.get("propagate") is not False:
        raise LoggingConfigError(
            f"请求摘要 logger {ACCESS_LOGGER_NAME!r} 必须 propagate: false，"
            "否则摘要会经 root 传播进后端文件"
        )
    if entry.get("handlers") != [ACCESS_FILE_HANDLER_ID]:
        raise LoggingConfigError(
            f"请求摘要 logger {ACCESS_LOGGER_NAME!r} 的 handlers 必须恰好为 "
            f"['{ACCESS_FILE_HANDLER_ID}']（doc 15 §3.1：只写 api_file；摘要"
            "文件的路径与文件名在 api_file.filename 配置，不得接到其它"
            "文件 handler）"
        )
    api_file = handlers.get(ACCESS_FILE_HANDLER_ID)
    if not isinstance(api_file, dict) or not api_file.get("filename"):
        raise LoggingConfigError(
            "api_file 必须是带 filename 的文件 handler（接收请求摘要），"
            "后端文件 handler 也不处理摘要（doc 15 §3.2）"
        )

    # 级别校验：logger/handler 任一处的 WARNING+ 都会把 <500 的 INFO 摘要
    # 在 Logger/Handler 判定处静默滤掉（doc 15 §4 级别表、§8.1 恰好一条）。
    # 未声明 level 时按 Logger.getEffectiveLevel() 的同一规则沿 logger
    # 层级继承（platform.http → platform → root 中最近的声明），不是只
    # 看 root：中间祖辈的 ERROR 同样决定有效级别。level: null 与 NOTSET
    # 都视同未声明——getEffectiveLevel() 对 NOTSET 继续向上
    inherited_owner: str | None = None
    inherited_level: Any = None
    if _level_is_undeclared(entry.get("level")):
        for ancestor in _ancestor_logger_names(ACCESS_LOGGER_NAME):
            ancestor_entry = loggers.get(ancestor)
            if not isinstance(ancestor_entry, dict):
                continue
            ancestor_level = ancestor_entry.get("level")
            if not _level_is_undeclared(ancestor_level):
                inherited_owner, inherited_level = (
                    f"logger {ancestor!r}",
                    ancestor_level,
                )
                break
        if inherited_level is None:
            if _level_is_undeclared(root.get("level")):
                raise LoggingConfigError(
                    f"请求摘要 logger {ACCESS_LOGGER_NAME!r} 的全部祖辈与 root "
                    "都未声明 level：root 进程默认 WARNING，会丢弃 INFO 摘要"
                )
            inherited_owner, inherited_level = "root", root["level"]
        _level_allows_info(
            inherited_level, f"{inherited_owner}（摘要 logger 未声明 level 时继承）"
        )
    else:
        _level_allows_info(entry["level"], f"请求摘要 logger {ACCESS_LOGGER_NAME!r}")
    if api_file.get("level") is not None:
        _level_allows_info(
            api_file["level"], f"文件 handler {ACCESS_FILE_HANDLER_ID!r}"
        )

    # 摘要文件 handler 不得被 root/其它 logger 复用：其 formatter 需要
    # method/route 等摘要字段，业务记录进入时逐条 `--- Logging error ---`
    # 打 stderr 并被该 handler 丢弃。无 filename 的 handler（如
    # api_console 同时挂 root）复用不受限（doc 15 §3.1）
    consumers: list[tuple[str, Any]] = [("root", root)]
    consumers += [
        (name, other)
        for name, other in loggers.items()
        if name != ACCESS_LOGGER_NAME and isinstance(other, dict)
    ]
    for consumer, consumer_entry in consumers:
        if ACCESS_FILE_HANDLER_ID in (consumer_entry.get("handlers") or []):
            raise LoggingConfigError(
                f"摘要文件 handler {ACCESS_FILE_HANDLER_ID!r} 不得被 {consumer!r} "
                "引用（其 formatter 只接受请求摘要记录，业务记录会逐条格式化失败）"
            )


def _level_is_undeclared(raw: Any) -> bool:
    """level: null / NOTSET（名字或 0）与未声明同义。

    ``Logger.getEffectiveLevel()`` 对 NOTSET 继续向上找最近声明，
    dictConfig 对 None 则不设该键（同样保持 NOTSET）——两者语义一致，
    校验时都按"未声明 → 继承"处理，不按非法写法拒绝。非法级别名不
    视同未声明，交由 ``_level_allows_info`` 明确报错。
    """
    if raw is None:
        return True
    if isinstance(raw, str):
        raw = logging.getLevelNamesMapping().get(raw)
    return raw == logging.NOTSET


def _ancestor_logger_names(logger_name: str) -> tuple[str, ...]:
    """logger 名的祖辈链，由近到远、不含自身与 root（按点分层级）。"""
    parts = logger_name.split(".")
    return tuple(".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1))


def _level_allows_info(raw: Any, owner: str) -> None:
    """级别写法（级别名字符串/整数）必须放行 INFO。

    名字经 ``logging.getLevelNamesMapping()`` 归一化（标准入口；
    ``getLevelName`` 的名字→数字方向已标记 deprecated），非法级别名在
    此明确报错，而不是留给 dictConfig 的报错缺上下文。
    """
    if isinstance(raw, str):
        number = logging.getLevelNamesMapping().get(raw)
        if number is None:
            raise LoggingConfigError(f"{owner} 的 level 写法非法: {raw!r}")
    else:
        number = raw
    if not isinstance(number, int) or isinstance(number, bool):
        raise LoggingConfigError(f"{owner} 的 level 必须是级别名或整数: {raw!r}")
    if number > logging.INFO:
        raise LoggingConfigError(
            f"{owner} 的 level {raw!r} 会丢弃 INFO 摘要"
            "（doc 15 §4：<500 记 INFO、§8.1 每请求恰好一条）"
        )


def _reject_backend_file_collisions(registry: ModuleRegistry, handlers: dict) -> None:
    """后端文件目标不得与 API 模板的文件 handler（api_file 等）指向同一文件。

    与 x_modules 内部判重同一套规范化规则（`Path.resolve()`，`..` 等别名
    等价）：两个独立 RotatingFileHandler 写并轮转同一文件，会使请求摘要
    与业务日志混杂、轮转互相干扰。
    """
    template_targets: dict[str, str] = {}
    for handler_id, handler in handlers.items():
        filename = handler.get("filename") if isinstance(handler, dict) else None
        if filename:
            template_targets[str(Path(filename).resolve())] = handler_id
    for module in registry._modules:
        resolved = str(module.backend_file)  # 构建注册表时已 resolve()
        if resolved in template_targets:
            raise LoggingConfigError(
                f"x_modules.{module.module_id}.backend_file 与 API 文件 handler "
                f"{template_targets[resolved]!r} 指向同一文件: {resolved}"
            )


def _resolve_handler_filenames(handlers: dict) -> None:
    """把每个文件 handler 的相对 filename 解析到项目根（绝对路径原样）。"""
    for handler in handlers.values():
        if isinstance(handler, dict) and handler.get("filename"):
            handler["filename"] = str(resolve_project_path(handler["filename"]))


def _reject_duplicate_handler_files(handlers: dict) -> None:
    """全部文件 handler 的 filename 按规范化路径判重。

    同一文件被两个 ``RotatingFileHandler`` 写并轮转会互相干扰，与
    x_modules 内部、backend_file↔模板 handler 判重同一套
    ``Path.resolve()`` 规则（``..`` 等别名等价）；此处是组装后的
    全量兜底，额外覆盖"模板内部两个 handler 指向同一文件"的组合
    （如复制 api_file 改名另挂）。
    """
    seen: dict[str, str] = {}
    for handler_id, handler in handlers.items():
        filename = handler.get("filename") if isinstance(handler, dict) else None
        if not filename:
            continue
        resolved = str(Path(filename).resolve())
        if resolved in seen:
            raise LoggingConfigError(
                f"文件 handler {seen[resolved]!r} 与 {handler_id!r} "
                f"指向同一文件: {resolved}"
            )
        seen[resolved] = handler_id


def _prepare_log_dirs(handlers: dict) -> None:
    """创建父目录并检查已存在目标文件是普通文件且可写（doc 15 §3.3）。

    delay 创建的文件无法靠打开失败暴露目录问题；已存在的目标文件
    （如被 chmod 只读的旧文件）不可写时，delay handler 会在运行期每条
    记录写入失败丢失。目录等非普通文件"存在且可写"，可写检查拦不住，
    运行期首条记录 IsADirectoryError 被 logging 内部吞掉、业务诊断
    静默丢失——统一在装载时明确失败，不存在的文件保持各自的创建
    时机（后端文件 delay 首写创建）。
    """
    seen_dirs: set[Path] = set()
    for handler in handlers.values():
        if not isinstance(handler, dict) or not handler.get("filename"):
            continue
        filename = Path(handler["filename"])
        if filename.parent not in seen_dirs:
            seen_dirs.add(filename.parent)
            filename.parent.mkdir(parents=True, exist_ok=True)
            if not os.access(filename.parent, os.W_OK):
                raise LoggingConfigError(f"日志目录不可写: {filename.parent}")
        if filename.exists():
            if not filename.is_file():
                raise LoggingConfigError(
                    f"日志目标不是普通文件（无法写入记录）: {filename}"
                )
            if not os.access(filename, os.W_OK):
                raise LoggingConfigError(f"日志文件不可写: {filename}")
