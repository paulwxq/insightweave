"""doc 15 统一日志管理：运行配置装载、模块注册表与上下文传播的测试。

会执行 ``dictConfig()`` 的用例一律放到干净子进程（probe 模式，沿用
``tests/unit/test_db_step.py``），避免重配 pytest 进程的全局 logging；
进程内用例只覆盖 dictConfig 之前就会失败的校验路径与纯函数逻辑。
"""

import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 合成配置（结构与 configs/logging.yaml 的对应段落保持一致）
# ---------------------------------------------------------------------------


def _api_profile_yaml() -> dict:
    return {
        "formatters": {
            "api_console": {
                "format": (
                    "%(asctime)s %(levelname)s request_id=%(request_id)s "
                    "app_module=%(app_module)s %(name)s - %(message)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "api_access": {
                "format": (
                    "%(asctime)s %(levelname)s request_id=%(request_id)s "
                    "app_module=%(app_module)s method=%(method)s route=%(route)s "
                    "status_code=%(status_code)s outcome=%(outcome)s "
                    "duration_ms=%(duration_ms)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "api_detailed": {
                "format": (
                    "%(asctime)s %(levelname)s request_id=%(request_id)s "
                    "app_module=%(app_module)s %(name)s "
                    "[%(filename)s:%(lineno)d] - %(message)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "uvicorn_stderr": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
            },
        },
        "filters": {
            "api_context": {"()": "services.logging_manager.ApiContextFilter"},
        },
        "handlers": {
            "api_console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "api_console",
                "stream": "ext://sys.stdout",
                "filters": ["api_context"],
            },
            "api_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "INFO",
                "formatter": "api_access",
                "filename": "logs/api.log",
                "maxBytes": 10485760,
                "backupCount": 5,
                "encoding": "utf8",
                "filters": ["api_context"],
            },
            "uvicorn_error_stderr": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "uvicorn_stderr",
            },
        },
        "loggers": {
            "platform.http.access": {
                "level": "INFO",
                "handlers": ["api_file"],
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["uvicorn_error_stderr"],
                "propagate": False,
            },
        },
        "root": {"level": "INFO", "handlers": ["api_console"]},
        "backend_handler_template": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "DEBUG",
            "formatter": "api_detailed",
            "maxBytes": 10485760,
            "backupCount": 5,
            "encoding": "utf8",
            "delay": True,
        },
    }


def _default_modules() -> dict:
    return {
        "platform": {"backend_file": "logs/backend/platform.log"},
        "metaweave": {
            "backend_file": "logs/backend/metaweave.log",
            "api_route_prefixes": ["/api/data-sources", "/api/data-assets"],
            "logger_prefixes": [
                "metaweave",
                "backend.publishing",
                "backend.profile_loading",
            ],
        },
    }


def _script_section() -> dict:
    """顶层脚本配置的最小形态（含步骤分流依赖的 formatter/filter）。"""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(name)s - [%(step)s] - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "detailed": {
                "format": "%(asctime)s - %(name)s - [%(step)s] - [%(filename)s:%(lineno)d] - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "filters": {
            "db_filter": {
                "()": "metaweave.utils.logger.StepFilter",
                "allowed_steps": ["db"],
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
            "db_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "detailed",
                "filename": "logs/db.log",
                "maxBytes": 10485760,
                "backupCount": 5,
                "encoding": "utf8",
                "filters": ["db_filter"],
            },
            "loader_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "detailed",
                "filename": "logs/loader.log",
                "maxBytes": 10485760,
                "backupCount": 5,
                "encoding": "utf8",
            },
        },
        "loggers": {
            "metaweave": {"level": "DEBUG", "handlers": [], "propagate": True},
            "metaweave.loader": {
                "level": "DEBUG",
                "handlers": ["loader_file", "console"],
                "propagate": False,
            },
        },
        "root": {"level": "INFO", "handlers": ["console", "db_file"]},
    }


def _write_yaml(base: Path, *, modules: dict | None = None) -> Path:
    cfg = _script_section()
    cfg["x_profiles"] = {"api": _api_profile_yaml()}
    cfg["x_modules"] = modules if modules is not None else _default_modules()
    path = base / "logging.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def _mutate_api_profile(base: Path, mutate) -> None:
    """改写合成 YAML 的 x_profiles.api 段（契约校验类测试用）。"""
    path = base / "logging.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(cfg["x_profiles"]["api"])
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


def _write_script_yaml(base: Path, *, script_log_dir: str = "probe-logs") -> Path:
    """顶层脚本配置（相对 filename，供 script 运行配置装载）。"""
    cfg = _script_section()
    for name, handler in cfg["handlers"].items():
        if "filename" in handler:
            handler["filename"] = f"{script_log_dir}/{name.removesuffix('_file')}.log"
    cfg["handlers"]["loader_file"]["filename"] = f"{script_log_dir}/loader.log"
    cfg["handlers"]["db_file"]["filename"] = f"{script_log_dir}/db.log"
    path = base / "logging.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def _test_registry():
    from services.logging_manager import ModuleRegistration, ModuleRegistry

    return ModuleRegistry(
        (
            ModuleRegistration(
                module_id="platform", backend_file=Path("/unused/platform.log")
            ),
            ModuleRegistration(
                module_id="metaweave",
                backend_file=Path("/unused/metaweave.log"),
                api_route_prefixes=("/api/data-sources", "/api/data-assets"),
                logger_prefixes=("metaweave", "backend.publishing"),
            ),
            ModuleRegistration(
                module_id="nl2sql",
                backend_file=Path("/unused/nl2sql.log"),
                api_route_prefixes=("/api/nl2sql",),
                logger_prefixes=("nl2sql",),
            ),
        )
    )


# ---------------------------------------------------------------------------
# 注册表校验（进程内：校验失败发生在 dictConfig 之前）
# ---------------------------------------------------------------------------


def test_api_profile_requires_platform_module(tmp_path):
    from services.logging_manager import LoggingConfigError, configure_logging

    _write_yaml(
        tmp_path,
        modules={
            "metaweave": {
                "backend_file": "logs/backend/metaweave.log",
                "logger_prefixes": ["metaweave"],
            }
        },
    )
    with pytest.raises(LoggingConfigError, match="platform"):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_invalid_module_id(tmp_path):
    from services.logging_manager import LoggingConfigError, configure_logging

    modules = _default_modules()
    modules["Meta-Weave"] = {"backend_file": "logs/backend/mw.log"}
    _write_yaml(tmp_path, modules=modules)
    with pytest.raises(LoggingConfigError, match="Meta-Weave"):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_duplicate_backend_file(tmp_path):
    from services.logging_manager import LoggingConfigError, configure_logging

    modules = _default_modules()
    modules["metaweave"]["backend_file"] = "logs/backend/platform.log"
    _write_yaml(tmp_path, modules=modules)
    with pytest.raises(LoggingConfigError, match="backend_file"):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_backend_file_path_aliases(tmp_path, monkeypatch):
    """`..` 等路径别名指向同一实际文件时必须按规范化路径判重。

    monkeypatch dictConfig 为 no-op：当前实现若放行该配置会继续走到
    dictConfig 污染 pytest 全局 logging；校验正确时在装载前即抛错，
    patch 不会被触发。
    """
    from services import logging_manager as lm

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    modules = _default_modules()
    modules["alias"] = {"backend_file": "logs/backend/../backend/platform.log"}
    _write_yaml(tmp_path, modules=modules)
    with pytest.raises(lm.LoggingConfigError, match="backend_file"):
        lm.configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_backend_file_matching_api_file(tmp_path, monkeypatch):
    """backend_file 指向 api_file 同一文件必须拒绝（别名形式证明按规范化路径比较）。

    两个独立 RotatingFileHandler 写并轮转同一文件，会使请求摘要与业务
    日志混杂、轮转互相干扰。monkeypatch dictConfig 同上：校验正确时
    在装载前即抛错。
    """
    from services import logging_manager as lm

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    modules = _default_modules()
    modules["nl2sql"] = {
        "backend_file": "logs/backend/../api.log",
        "logger_prefixes": ["nl2sql"],
    }
    _write_yaml(tmp_path, modules=modules)
    with pytest.raises(lm.LoggingConfigError, match="api_file"):
        lm.configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_duplicate_template_handler_files(tmp_path, monkeypatch):
    """模板内两个 handler 指向同一文件必须拒绝。

    复制 api_file 改名挂 root 时，注册表方向的判重（x_modules 内部、
    backend_file↔模板）都不覆盖"模板内部"这一组合；同一文件被两个
    RotatingFileHandler 写并轮转属于同一类冲突，装载期统一拒绝。
    """
    from services import logging_manager as lm

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)

    def add_duplicate_template_handler(api):
        api["handlers"]["api_file_2"] = deepcopy(api["handlers"]["api_file"])
        api["root"]["handlers"] = ["api_console", "api_file_2"]

    _write_yaml(tmp_path)
    _mutate_api_profile(tmp_path, add_duplicate_template_handler)
    with pytest.raises(lm.LoggingConfigError, match="同一文件"):
        lm.configure_logging("api", str(tmp_path / "logging.yaml"))


def test_script_profile_rejects_duplicate_handler_files(tmp_path, monkeypatch):
    """脚本段两个 handler 指向同一文件同样拒绝（`..` 别名形式）。

    目录/可写检查（``_prepare_log_dirs``）两个运行配置都跑，文件判重
    也应一致：同一文件被两个 RotatingFileHandler 写并轮转的冲突与
    api 侧同类。monkeypatch dictConfig 同上：校验正确时在装载前即
    抛错，patch 不会被触发。
    """
    from services import logging_manager as lm

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    _write_script_yaml(tmp_path)
    path = tmp_path / "logging.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    # 与 db_file 指向同一实际文件的别名：按规范化路径判重须拒绝
    cfg["handlers"]["loader_file"]["filename"] = "probe-logs/../probe-logs/db.log"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(lm.LoggingConfigError, match="同一文件"):
        lm.configure_logging("script", str(path))


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受权限位约束")
def test_api_profile_existing_readonly_backend_file_rejected(tmp_path, monkeypatch):
    """已存在的后端日志文件不可写须在装载时拒绝（doc 15 §3.3）。

    delay: true 使 dictConfig 不打开文件；只检查父目录的话，运行期
    每次写入都丢记录（stderr traceback），API 带病运行。校验在
    dictConfig 之前，进程内无 logging 污染。
    """
    from services.logging_manager import LoggingConfigError, configure_logging

    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    _write_yaml(tmp_path)
    target = tmp_path / "logs" / "backend" / "metaweave.log"
    target.parent.mkdir(parents=True)
    target.write_text("旧记录\n", encoding="utf-8")
    target.chmod(0o444)
    try:
        with pytest.raises(LoggingConfigError, match=re.escape(str(target))):
            configure_logging("api", str(tmp_path / "logging.yaml"))
    finally:
        target.chmod(0o644)


def test_api_profile_existing_directory_backend_file_rejected(tmp_path, monkeypatch):
    """已存在的日志目标是目录必须在装载期拒绝（doc 15 §3.3）。

    目录"存在且可写"，可写检查拦不住；delay: true 使启动时不打开
    目标，运行期首条记录 IsADirectoryError 被 logging 内部 handleError
    吞掉，业务诊断静默丢失而服务看似正常。校验在 dictConfig 之前。
    """
    from services.logging_manager import LoggingConfigError, configure_logging

    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    _write_yaml(tmp_path)
    target = tmp_path / "logs" / "backend" / "metaweave.log"
    target.parent.mkdir(parents=True)
    target.mkdir()
    with pytest.raises(LoggingConfigError, match=re.escape(str(target))):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_conflicting_route_prefix(tmp_path):
    from services.logging_manager import LoggingConfigError, configure_logging

    modules = _default_modules()
    modules["nl2sql"] = {
        "backend_file": "logs/backend/nl2sql.log",
        "api_route_prefixes": ["/api/data-sources"],
    }
    _write_yaml(tmp_path, modules=modules)
    with pytest.raises(LoggingConfigError, match="/api/data-sources"):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_conflicting_logger_prefix(tmp_path):
    from services.logging_manager import LoggingConfigError, configure_logging

    modules = _default_modules()
    modules["nl2sql"] = {
        "backend_file": "logs/backend/nl2sql.log",
        "logger_prefixes": ["metaweave"],
    }
    _write_yaml(tmp_path, modules=modules)
    with pytest.raises(LoggingConfigError, match="metaweave"):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_rejects_malformed_route_prefix(tmp_path):
    from services.logging_manager import LoggingConfigError, configure_logging

    modules = _default_modules()
    modules["metaweave"]["api_route_prefixes"] = ["api/data-sources"]
    _write_yaml(tmp_path, modules=modules)
    with pytest.raises(LoggingConfigError, match="api_route_prefixes"):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def _drop_access_logger(api):
    api["loggers"].pop("platform.http.access")


def _enable_access_propagate(api):
    api["loggers"]["platform.http.access"]["propagate"] = True


def _access_console_only(api):
    api["loggers"]["platform.http.access"]["handlers"] = ["api_console"]


def _access_with_backend_handler(api):
    api["loggers"]["platform.http.access"]["handlers"] = [
        "api_file",
        "backend_metaweave_file",
    ]


def _access_on_other_file_handler(api):
    # 换成其它文件 handler：摘要会被接到别的文件、留下空的 api.log，
    # 比文件不存在更具误导性——按 handler 身份严格拒绝
    api["handlers"]["other_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "level": "INFO",
        "formatter": "api_access",
        "filename": "logs/other.log",
        "maxBytes": 10485760,
        "backupCount": 5,
        "encoding": "utf8",
        "filters": ["api_context"],
    }
    api["loggers"]["platform.http.access"]["handlers"] = ["other_file"]


def _access_logger_error_level(api):
    api["loggers"]["platform.http.access"]["level"] = "ERROR"


def _access_file_error_level(api):
    api["handlers"]["api_file"]["level"] = "ERROR"


def _access_file_int_warning_level(api):
    # 数字写法（30 == WARNING）同样须归一化后拒绝
    api["handlers"]["api_file"]["level"] = 30


def _access_inherits_error_root(api):
    # 摘要 logger 未声明 level 时代码继承 root：root=ERROR 同样丢 INFO
    del api["loggers"]["platform.http.access"]["level"]
    api["root"]["level"] = "ERROR"


def _access_inherits_error_platform(api):
    # 未声明 level 时按 getEffectiveLevel 沿层级继承：platform=ERROR 的
    # 有效级别是 ERROR（不是 root 的 INFO），INFO 摘要在 Logger 判定处
    # 被滤掉、api.log 零行——只查 root 的校验拦不住
    del api["loggers"]["platform.http.access"]["level"]
    api["loggers"]["platform"] = {"level": "ERROR"}


def _access_and_root_no_level(api):
    # 两者都未声明：dictConfig 不设 root 级别，进程默认 WARNING 丢 INFO
    del api["loggers"]["platform.http.access"]["level"]
    del api["root"]["level"]


def _access_null_level_no_root_level(api):
    # level: null 与未声明同义（dictConfig 对 None 的语义）：root 也未
    # 声明时仍按"进程默认 WARNING 丢 INFO"拒绝
    api["loggers"]["platform.http.access"]["level"] = None
    del api["root"]["level"]


def _api_file_on_root(api):
    # 误用：摘要专用文件 handler 挂给 root，业务记录逐条格式化失败
    api["root"]["handlers"] = ["api_console", "api_file"]


def _api_file_on_other_logger(api):
    # 误用：摘要专用文件 handler 被其它 logger 引用
    api["loggers"]["some.business"] = {"handlers": ["api_file"]}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(_drop_access_logger, "必须配置请求摘要 logger", id="missing"),
        pytest.param(_enable_access_propagate, "propagate", id="propagate-true"),
        pytest.param(_access_console_only, "恰好", id="no-file-handler"),
        pytest.param(_access_with_backend_handler, "恰好", id="backend-handler"),
        pytest.param(_access_on_other_file_handler, "恰好", id="other-file-handler"),
        pytest.param(_access_logger_error_level, "INFO 摘要", id="logger-error-level"),
        pytest.param(_access_file_error_level, "INFO 摘要", id="handler-error-level"),
        pytest.param(
            _access_file_int_warning_level, "INFO 摘要", id="handler-int-level"
        ),
        pytest.param(_access_inherits_error_root, "INFO 摘要", id="inherit-root-level"),
        pytest.param(
            _access_inherits_error_platform, "INFO 摘要", id="inherit-platform-level"
        ),
        pytest.param(_access_and_root_no_level, "INFO 摘要", id="no-level-at-all"),
        pytest.param(
            _access_null_level_no_root_level, "INFO 摘要", id="null-level-no-root"
        ),
        pytest.param(_api_file_on_root, "不得被", id="file-handler-on-root"),
        pytest.param(
            _api_file_on_other_logger, "不得被", id="file-handler-on-other-logger"
        ),
    ],
)
def test_api_profile_validates_access_logger_contract(tmp_path, mutate, message):
    """摘要 logger 契约装载期校验（doc 15 §3.1/§3.2/§8.1）。

    改名或漏配时摘要记录经 root 传播分流进后端文件、api.log 一条
    没有，服务却看似正常。校验在 dictConfig 之前，进程内无污染。
    """
    from services.logging_manager import LoggingConfigError, configure_logging

    _write_yaml(tmp_path)
    _mutate_api_profile(tmp_path, mutate)
    with pytest.raises(LoggingConfigError, match=message):
        configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_accepts_info_allowing_levels(tmp_path, monkeypatch):
    """DEBUG/NOTSET（含数字写法）等放行 INFO 的级别合法，校验不误伤。"""
    from services import logging_manager as lm

    def loosen(api):
        api["loggers"]["platform.http.access"]["level"] = "DEBUG"
        api["handlers"]["api_file"]["level"] = 0  # NOTSET

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    _write_yaml(tmp_path)
    _mutate_api_profile(tmp_path, loosen)
    lm.configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_treats_null_level_as_undeclared(tmp_path, monkeypatch):
    """level: null 视同未声明（与 dictConfig 对 None 的语义一致）。

    dictConfig 在 logger 与 handler 两侧都是 `level is not None` 才
    生效：None 等价于不写该键（logger 继承 root、handler 落 NOTSET），
    都不丢 INFO——装载不得拒绝这种写法。
    """
    from services import logging_manager as lm

    def null_levels(api):
        api["loggers"]["platform.http.access"]["level"] = None
        api["handlers"]["api_file"]["level"] = None

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    _write_yaml(tmp_path)
    _mutate_api_profile(tmp_path, null_levels)
    lm.configure_logging("api", str(tmp_path / "logging.yaml"))


def test_api_profile_intermediate_ancestor_level_wins(tmp_path, monkeypatch):
    """继承沿 logger 层级取最近的声明，不是任一祖辈/root 的 ERROR 都拒。

    摘要 logger 未声明 level、platform=INFO（放行）、root=ERROR 时，
    getEffectiveLevel 是 INFO：只查 root 的实现会把这种放行配置
    误拒（与漏掉 platform=ERROR 是同一处缺陷的两面）。
    """
    from services import logging_manager as lm

    def platform_info_root_error(api):
        del api["loggers"]["platform.http.access"]["level"]
        api["loggers"]["platform"] = {"level": "INFO"}
        api["root"]["level"] = "ERROR"

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    _write_yaml(tmp_path)
    _mutate_api_profile(tmp_path, platform_info_root_error)
    lm.configure_logging("api", str(tmp_path / "logging.yaml"))


def test_script_profile_clears_stale_active_registry(tmp_path, monkeypatch):
    """同进程"先 api 后 script"装载不留过期注册表（script 不做归属判定）。

    生产中两个运行配置不共进程，此处钉住装载器语义：script 装载成功后
    进程内不得残留 api 注册表。monkeypatch dictConfig 为 no-op，文件
    落在临时项目根。
    """
    from services import logging_manager as lm

    monkeypatch.setattr(lm.logging.config, "dictConfig", lambda cfg: None)
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    monkeypatch.setattr(lm, "_ACTIVE_REGISTRY", _test_registry())
    _write_script_yaml(tmp_path)
    lm.configure_logging("script", str(tmp_path / "logging.yaml"))
    assert lm._ACTIVE_REGISTRY is None


def test_configure_logging_rejects_unknown_profile(tmp_path):
    from services.logging_manager import configure_logging

    _write_yaml(tmp_path)
    with pytest.raises(ValueError, match="profile"):
        configure_logging("worker", str(tmp_path / "logging.yaml"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受权限位约束")
def test_configure_logging_unreadable_config_raises_config_error(tmp_path):
    """配置文件不可读（open PermissionError）须转成带配置路径的明确错误。

    装载在 dictConfig 之前失败，进程内无 logging 污染。
    """
    from services.logging_manager import LoggingConfigError, configure_logging

    cfg = tmp_path / "logging.yaml"
    cfg.write_text("version: 1\n", encoding="utf-8")
    cfg.chmod(0o000)
    with pytest.raises(LoggingConfigError, match=re.escape(str(cfg))):
        configure_logging("script", cfg)


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受权限位约束")
def test_configure_logging_inaccessible_config_dir_raises_config_error(tmp_path):
    """无权限父目录下 Path.exists() 的 stat 直接抛 EACCES（不是返回 False）。

    须转成带配置路径的明确错误，而不是让 PermissionError 冒泡成 exit 1。
    """
    from services.logging_manager import LoggingConfigError, configure_logging

    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    cfg = cfg_dir / "logging.yaml"
    cfg.write_text("version: 1\n", encoding="utf-8")
    cfg_dir.chmod(0o000)
    try:
        with pytest.raises(LoggingConfigError, match=re.escape(str(cfg))):
            configure_logging("script", cfg)
    finally:
        cfg_dir.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受权限位约束")
def test_script_profile_log_dir_creation_denied_raises_config_error(
    tmp_path, monkeypatch
):
    """日志目录在不可写父目录下且不存在（mkdir PermissionError）同样统一转换。"""
    from services.logging_manager import LoggingConfigError, configure_logging

    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    cfg = _write_script_yaml(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("probe-logs/", "locked/logs/"),
        encoding="utf-8",
    )
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        # mkdir 在 dictConfig 之前失败，进程内无 logging 污染
        with pytest.raises(LoggingConfigError, match=re.escape(str(cfg))):
            configure_logging("script", cfg)
    finally:
        locked.chmod(0o755)


# ---------------------------------------------------------------------------
# 归属判定（纯函数）
# ---------------------------------------------------------------------------


def test_resolve_request_module_matches_segment_boundaries():
    registry = _test_registry()

    assert registry.resolve_request_module("/api/data-sources") == "metaweave"
    assert (
        registry.resolve_request_module("/api/data-sources/abc/collection-jobs")
        == "metaweave"
    )
    # 段边界：前缀字符串相同但不是完整段，不得匹配
    assert registry.resolve_request_module("/api/data-sources-extra") is None
    assert registry.resolve_request_module("/api/nl2sql-extra") is None
    assert registry.resolve_request_module("/api/nl2sql") == "nl2sql"
    assert registry.resolve_request_module("/api/nl2sql/q/1") == "nl2sql"
    assert registry.resolve_request_module("/api/unknown") is None


def test_resolve_request_module_longest_prefix_wins():
    from services.logging_manager import ModuleRegistration, ModuleRegistry

    registry = ModuleRegistry(
        (
            ModuleRegistration(
                module_id="platform", backend_file=Path("/unused/p.log")
            ),
            ModuleRegistration(
                module_id="metaweave",
                backend_file=Path("/unused/m.log"),
                api_route_prefixes=("/api",),
            ),
            ModuleRegistration(
                module_id="nl2sql",
                backend_file=Path("/unused/n.log"),
                api_route_prefixes=("/api/nl2sql",),
            ),
        )
    )
    assert registry.resolve_request_module("/api/nl2sql/x") == "nl2sql"
    assert registry.resolve_request_module("/api/data-sources") == "metaweave"


def test_resolve_logger_module_matches_name_segments():
    registry = _test_registry()

    assert registry.resolve_logger_module("metaweave") == "metaweave"
    assert registry.resolve_logger_module("metaweave.connector") == "metaweave"
    assert (
        registry.resolve_logger_module("backend.publishing.publish_service")
        == "metaweave"
    )
    # 非 `.` 分隔的字符串前缀不算子 logger
    assert registry.resolve_logger_module("metaweavex.core") is None
    assert registry.resolve_logger_module("backend") is None
    assert registry.resolve_logger_module("uvicorn.error") is None


def test_resolve_record_module_prefers_request_context(monkeypatch):
    from services import logging_manager as lm

    monkeypatch.setattr(lm, "_ACTIVE_REGISTRY", _test_registry())
    record = logging.LogRecord(
        name="metaweave.connector",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    assert lm.resolve_record_module(record) == "metaweave"
    token = lm.set_request_module("nl2sql")
    try:
        # 请求上下文优先：共享方法随调用者归属（doc 15 §3.2 优先级 2）
        assert lm.resolve_record_module(record) == "nl2sql"
    finally:
        lm.reset_request_module(token)
    assert lm.resolve_record_module(record) == "metaweave"


def test_api_context_filter_fills_fields(monkeypatch):
    from services import logging_manager as lm

    monkeypatch.setattr(lm, "_ACTIVE_REGISTRY", _test_registry())
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    assert lm.ApiContextFilter().filter(record) is True
    assert record.request_id == "-"
    assert record.app_module == "platform"

    rid = lm.set_request_id("rid-123")
    mod = lm.set_request_module("metaweave")
    try:
        record2 = logging.LogRecord(
            name="anything",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="x",
            args=(),
            exc_info=None,
        )
        assert lm.ApiContextFilter().filter(record2) is True
        assert record2.request_id == "rid-123"
        assert record2.app_module == "metaweave"
    finally:
        lm.reset_request_module(mod)
        lm.reset_request_id(rid)


# ---------------------------------------------------------------------------
# 线程池上下文传播（doc 15 §3.2 优先级 2）
# ---------------------------------------------------------------------------


def test_submit_with_context_snapshots_context_per_task():
    from services.logging_manager import (
        get_request_id,
        get_request_module,
        reset_request_id,
        reset_request_module,
        set_request_id,
        set_request_module,
        submit_with_context,
    )

    def work():
        return get_request_id(), get_request_module()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = []
        for rid, mod in [
            ("r-mw-1", "metaweave"),
            ("r-nl-2", "nl2sql"),
            ("r-mw-1", "metaweave"),
        ]:
            t1 = set_request_id(rid)
            t2 = set_request_module(mod)
            try:
                futures.append(((rid, mod), submit_with_context(executor, work)))
                # 对照：裸 submit 不复制上下文，任务内拿不到请求字段
                plain = executor.submit(work)
            finally:
                reset_request_module(t2)
                reset_request_id(t1)
        for (rid, mod), future in futures:
            assert future.result(timeout=10) == (rid, mod)
        assert plain.result(timeout=10) == (None, None)


# ---------------------------------------------------------------------------
# 子进程 probe：api 运行配置装载与分流
# ---------------------------------------------------------------------------


def _api_probe(base_dir: str) -> None:
    from pathlib import Path

    base = Path(base_dir)
    from metaweave.utils import file_utils

    file_utils.get_project_root = lambda: base

    import logging

    from services.logging_manager import configure_logging

    setup = configure_logging("api", str(base / "logging.yaml"))
    assert sorted(setup.modules.module_ids) == ["metaweave", "platform"]

    logs = base / "logs"
    assert (logs / "api.log").is_file(), "api.log 应在装载时创建（不 delay）"
    assert not (logs / "backend" / "metaweave.log").exists(), "后端文件应 delay 创建"
    assert not (logs / "backend" / "platform.log").exists()
    assert not (logs / "db.log").exists(), "API 装载不得创建脚本日志文件"
    assert not (logs / "loader.log").exists()

    logging.getLogger("backend.publishing").error("probe-mw-backend-publishing")
    logging.getLogger("totally.unknown.logger").warning("probe-platform-unknown")

    mw_text = (logs / "backend" / "metaweave.log").read_text(encoding="utf-8")
    assert "probe-mw-backend-publishing" in mw_text
    assert "request_id=-" in mw_text
    platform_text = (logs / "backend" / "platform.log").read_text(encoding="utf-8")
    assert "probe-platform-unknown" in platform_text
    assert "probe-mw-backend-publishing" not in platform_text

    import uvicorn.logging

    uv_err = logging.getLogger("uvicorn.error")
    assert uv_err.propagate is False
    assert len(uv_err.handlers) == 1
    handler = uv_err.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert isinstance(handler.formatter, uvicorn.logging.DefaultFormatter)

    access = logging.getLogger("platform.http.access")
    assert access.propagate is False
    assert len(access.handlers) == 1

    logging.shutdown()
    print("PROBE_OK")


def _nl2sql_probe(base_dir: str) -> None:
    from pathlib import Path

    base = Path(base_dir)
    from metaweave.utils import file_utils

    file_utils.get_project_root = lambda: base

    import logging

    from services.logging_manager import (
        configure_logging,
        reset_request_id,
        reset_request_module,
        set_request_id,
        set_request_module,
    )

    setup = configure_logging("api", str(base / "logging.yaml"))
    assert "nl2sql" in setup.modules.module_ids
    logs = base / "logs"

    # 无请求上下文：按 logger 前缀
    logging.getLogger("nl2sql.core").info("probe-nl2sql-by-prefix")
    logging.getLogger("metaweave.connector").info("probe-mw-by-prefix")

    nl_text = (logs / "backend" / "nl2sql.log").read_text(encoding="utf-8")
    mw_text = (logs / "backend" / "metaweave.log").read_text(encoding="utf-8")
    assert "probe-nl2sql-by-prefix" in nl_text
    assert "probe-mw-by-prefix" in mw_text
    assert "probe-nl2sql-by-prefix" not in mw_text

    # 请求上下文优先：共享 metaweave logger 在 NL2SQL 请求里归 nl2sql
    t1 = set_request_id("probe-rid-nl")
    t2 = set_request_module("nl2sql")
    try:
        logging.getLogger("metaweave.connector").info("probe-shared-in-nl2sql")
    finally:
        reset_request_module(t2)
        reset_request_id(t1)
    logging.getLogger("metaweave.connector").info("probe-shared-outside")

    nl_text = (logs / "backend" / "nl2sql.log").read_text(encoding="utf-8")
    mw_text = (logs / "backend" / "metaweave.log").read_text(encoding="utf-8")
    assert "probe-shared-in-nl2sql" in nl_text
    assert "request_id=probe-rid-nl" in nl_text
    assert "probe-shared-in-nl2sql" not in mw_text
    assert "probe-shared-outside" in mw_text

    logging.shutdown()
    print("PROBE_OK")


def _real_yaml_api_probe(base_dir: str) -> None:
    from pathlib import Path

    base = Path(base_dir)
    from metaweave.utils import file_utils

    file_utils.get_project_root = lambda: base

    import logging

    from services.logging_manager import configure_logging

    setup = configure_logging(
        "api", str(Path(__file__).resolve().parents[2] / "configs" / "logging.yaml")
    )
    assert "platform" in setup.modules.module_ids
    assert "metaweave" in setup.modules.module_ids
    assert (base / "logs" / "api.log").is_file()
    assert not (base / "logs" / "db.log").exists()

    # 真实配置的结构断言：合成配置测试镜像了 YAML 结构，只改真实 YAML
    # （换前缀、增删模块）时它们仍绿——用注册表公开 API 钉住真实注册表，
    # 防止归属静默漂移（doc 15 §3.2）
    assert set(setup.modules.module_ids) == {"platform", "metaweave"}
    assert setup.modules.resolve_request_module("/api/data-sources/x") == "metaweave"
    assert setup.modules.resolve_request_module("/api/data-assets/y") == "metaweave"
    assert setup.modules.resolve_request_module("/api/nl2sql") is None
    assert setup.modules.resolve_logger_module("backend.publishing") == "metaweave"
    assert setup.modules.resolve_logger_module("backend.profile_loading") == "metaweave"
    assert setup.modules.resolve_logger_module("unknown.module") is None

    # 摘要 logger 恰好一个 handler 且指向 api.log（doc 15 §3.1"只写 api_file"；
    # 装载校验只保证"至少一个文件 handler"，多挂 handler 须在此钉住）
    access = logging.getLogger("platform.http.access")
    assert access.propagate is False
    assert len(access.handlers) == 1
    assert Path(access.handlers[0].baseFilename) == base / "logs" / "api.log"
    # 级别契约：logger 与文件 handler 都显式 INFO，放行 <500 的 INFO 摘要
    # （WARNING+ 会在装载期被拒，此处钉住真实 YAML 不漂移）
    assert access.level == logging.INFO
    assert access.handlers[0].level == logging.INFO
    logging.shutdown()
    print("PROBE_OK")


def _run_probe(func_name: str, base_dir: Path) -> subprocess.CompletedProcess:
    probe = (
        f"from tests.unit.test_logging_profiles import {func_name}; "
        f"import sys; {func_name}(sys.argv[1])"
    )
    return subprocess.run(
        [sys.executable, "-c", probe, str(base_dir)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_api_profile_files_and_routing(tmp_path):
    _write_yaml(tmp_path)
    run = _run_probe("_api_probe", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PROBE_OK" in run.stdout


def test_api_profile_new_module_only_yaml(tmp_path):
    """只改 YAML 注册 nl2sql：分流与共享 logger 归属随之生效（doc 15 §8.7）。"""
    modules = _default_modules()
    modules["nl2sql"] = {
        "backend_file": "logs/backend/nl2sql.log",
        "api_route_prefixes": ["/api/nl2sql"],
        "logger_prefixes": ["nl2sql"],
    }
    _write_yaml(tmp_path, modules=modules)
    run = _run_probe("_nl2sql_probe", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PROBE_OK" in run.stdout


def test_real_config_api_profile_loads(tmp_path):
    run = _run_probe("_real_yaml_api_probe", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PROBE_OK" in run.stdout


# ---------------------------------------------------------------------------
# 阶段 2：script 运行配置（doc 15 §6/§10）
# ---------------------------------------------------------------------------


def _script_probe(base_dir: str) -> None:
    """script 运行配置：整段沿用顶层，相对 filename 解析到项目根。"""
    from pathlib import Path

    base = Path(base_dir)
    from metaweave.utils import file_utils

    file_utils.get_project_root = lambda: base

    import logging

    from metaweave.utils.logger import set_current_step, setup_metaweave_logging

    setup_metaweave_logging(str(base / "logging.yaml"))

    set_current_step("db")
    logging.getLogger("metaweave.probe").debug("probe-mw-debug-in-db")
    set_current_step("cql")
    logging.getLogger("metaweave.probe").warning("probe-mw-warn-in-cql")
    logging.getLogger("metaweave.loader").info("probe-loader-line")

    db_text = (base / "probe-logs" / "db.log").read_text(encoding="utf-8")
    assert db_text.count("probe-mw-debug-in-db") == 1
    # 未命中步骤与 loader logger 的记录不进步骤文件（StepFilter / propagate false）
    assert "probe-mw-warn-in-cql" not in db_text
    assert "probe-loader-line" not in db_text

    loader_text = (base / "probe-logs" / "loader.log").read_text(encoding="utf-8")
    assert "probe-loader-line" in loader_text

    # script 装载不创建 API 文件或目录
    assert not (base / "probe-logs" / "api.log").exists()
    assert not (base / "probe-logs" / "backend").exists()
    # root 只有脚本配置声明的 handler（console + db_file），无后端 handler
    assert len(logging.getLogger().handlers) == 2
    logging.shutdown()
    print("PROBE_OK")


def test_script_profile_uses_top_level_config(tmp_path):
    _write_script_yaml(tmp_path)
    run = _run_probe("_script_probe", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PROBE_OK" in run.stdout


def _real_script_paths_probe(base_dir: str) -> None:
    """真实 configs/logging.yaml 的 script 装载：12 个文件都在 logs/script/。"""
    from pathlib import Path

    base = Path(base_dir)
    from metaweave.utils import file_utils

    file_utils.get_project_root = lambda: base

    import logging
    from logging.handlers import RotatingFileHandler

    from metaweave.utils.logger import set_current_step, setup_metaweave_logging
    from metaweave.core.metadata.execution import set_operation_step

    setup_metaweave_logging(str(PROJECT_ROOT / "configs" / "logging.yaml"))

    file_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    # root 只挂 11 个步骤 handler（doc 21.1 增 domain_file）；loader_file 由
    # loader logger 显式引用
    assert len(file_handlers) == 11, [
        str(handler.baseFilename) for handler in file_handlers
    ]
    loader_handlers = [
        handler
        for handler in logging.getLogger("metaweave.loader").handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(loader_handlers) == 1
    for handler in [*file_handlers, *loader_handlers]:
        filename = Path(handler.baseFilename)
        assert filename.parent == base / "logs" / "script", filename
        # 脚本 handler 保持启动时创建文件语义（不加 delay）
        assert filename.is_file(), filename
    assert loader_handlers[0].baseFilename.endswith("/loader.log")

    # script 装载不创建 API 文件（api.log/backend 均不在）
    assert not (base / "logs" / "api.log").exists()
    assert not (base / "logs" / "backend").exists()

    # metaweave.* DEBUG 与五个 loader logger 隔离（doc 15 §8.7）
    assert logging.getLogger("metaweave").level == logging.DEBUG
    for name in (
        "metaweave.loader",
        "metaweave.core.loaders",
        "metaweave.dim_value.loader",
        "metaweave.table_schema.loader",
        "metaweave.core.sql_rag.loader",
    ):
        module_logger = logging.getLogger(name)
        assert module_logger.propagate is False, name
        assert len(module_logger.handlers) == 2, name

    # 真实配置的内容分流（doc 15 §8.4：内容在 logs/script/ 对应同名文件，
    # 未命中步骤不进）：位置验证（上方）之外，再用真实 YAML 的
    # StepFilter/loader logger 配置各写一条记录核对归属。test_db_step.py
    # 的 db 分流把 filename 重写到临时目录，不覆盖这里的组合
    set_current_step("rel")
    logging.getLogger("metaweave.probe").info("real-config-rel-step")
    set_current_step("ddl")
    logging.getLogger("metaweave.probe").info("real-config-ddl-step")
    set_current_step("domain")
    logging.getLogger("metaweave.probe").info("real-config-domain-step")
    set_current_step("generate-domains")
    logging.getLogger("metaweave.probe").info("real-config-cli-domains-step")
    # doc 22.1 A11b：CLI 与 Web 两个 operation 键都经 OPERATION_STEP_MAP
    # 解析为 dim_config 标签（用 set_operation_step 走完整映射链）
    set_operation_step("generate-dim-config")
    logging.getLogger("metaweave.probe").info("real-config-cli-dim-config-step")
    set_operation_step("dim_config")
    logging.getLogger("metaweave.probe").info("real-config-dim-config-step")
    # doc 23.1 §5.1/A11b：Web 步骤 rel 经 OPERATION_STEP_MAP 完整映射链
    # （受管服务调用路径）复用既有 rel_filter/rel_file，logging.yaml 零改动
    set_operation_step("rel")
    logging.getLogger("metaweave.probe").info("real-config-operation-rel-step")
    logging.getLogger("metaweave.loader").info("real-config-loader-line")

    script_dir = base / "logs" / "script"
    rel_text = (script_dir / "rel.log").read_text(encoding="utf-8")
    ddl_text = (script_dir / "ddl.log").read_text(encoding="utf-8")
    domain_text = (script_dir / "domain.log").read_text(encoding="utf-8")
    cli_domains_text = (script_dir / "generate_domains.log").read_text(
        encoding="utf-8"
    )
    loader_text = (script_dir / "loader.log").read_text(encoding="utf-8")
    assert "real-config-rel-step" in rel_text
    assert "real-config-ddl-step" not in rel_text
    assert "real-config-ddl-step" in ddl_text
    # doc 21.1 §5.1：Web 步骤 domain 与 CLI generate-domains 各归各的文件
    assert "real-config-domain-step" in domain_text
    assert "real-config-domain-step" not in ddl_text
    assert "real-config-domain-step" not in cli_domains_text
    assert "real-config-cli-domains-step" in cli_domains_text
    assert "real-config-cli-domains-step" not in domain_text
    # doc 22.1 §5.1/A11b：Web 步骤 dim_config 与 CLI generate-dim-config 经
    # OPERATION_STEP_MAP 映射到同一步骤名，复用既有 dim_config_filter/file
    dim_config_text = (script_dir / "dim_config.log").read_text(encoding="utf-8")
    assert "real-config-dim-config-step" in dim_config_text
    assert "real-config-cli-dim-config-step" in dim_config_text
    assert "real-config-dim-config-step" not in ddl_text
    assert "real-config-dim-config-step" not in rel_text
    assert "real-config-operation-rel-step" in rel_text
    assert "real-config-operation-rel-step" not in ddl_text
    assert "real-config-operation-rel-step" not in domain_text
    assert "real-config-loader-line" in loader_text
    # loader logger propagate: false，不进步骤文件
    assert "real-config-loader-line" not in rel_text
    logging.shutdown()
    print("PROBE_OK")


def test_real_config_script_paths_moved_to_script_dir(tmp_path):
    run = _run_probe("_real_script_paths_probe", tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PROBE_OK" in run.stdout


# ---------------------------------------------------------------------------
# CLI：--log-config 存在性检查时机与 exit 2（doc 15 §3.1/§3.3）
# ---------------------------------------------------------------------------


def _invoke_cli(args):
    from click.testing import CliRunner

    from metaweave.cli.main import cli

    return CliRunner().invoke(cli, args)


def test_cli_explicit_log_config_missing_exits_2(tmp_path):
    result = _invoke_cli(
        ["--log-config", str(tmp_path / "nope.yaml"), "metadata", "--help"]
    )
    assert result.exit_code == 2, result.output
    assert "--log-config" in result.output


def test_cli_explicit_log_config_directory_exits_2(tmp_path):
    result = _invoke_cli(["--log-config", str(tmp_path), "metadata", "--help"])
    assert result.exit_code == 2, result.output
    assert "--log-config" in result.output


def test_cli_explicit_log_config_invalid_yaml_exits_2(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: {broken", encoding="utf-8")
    result = _invoke_cli(["--log-config", str(bad), "metadata", "--help"])
    assert result.exit_code == 2, result.output
    assert "--log-config" in result.output


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受权限位约束")
def test_cli_explicit_log_config_unreadable_exits_2(tmp_path):
    """配置存在但不可读：doc 15 §3.3 显式配置非法须 exit 2，不得 exit 1。"""
    cfg = tmp_path / "secret.yaml"
    cfg.write_text("version: 1\n", encoding="utf-8")
    cfg.chmod(0o000)
    result = _invoke_cli(["--log-config", str(cfg), "metadata", "--help"])
    assert result.exit_code == 2, result.output
    assert "--log-config" in result.output
    assert str(cfg) in result.output


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受权限位约束")
def test_cli_explicit_log_config_inaccessible_dir_exits_2(tmp_path):
    """配置在无权限目录内：stat 阶段即失败，同样须 exit 2 而非 exit 1。"""
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    cfg = cfg_dir / "secret.yaml"
    cfg.write_text("version: 1\n", encoding="utf-8")
    cfg_dir.chmod(0o000)
    try:
        result = _invoke_cli(["--log-config", str(cfg), "metadata", "--help"])
        assert result.exit_code == 2, result.output
        assert "--log-config" in result.output
        assert str(cfg) in result.output
    finally:
        cfg_dir.chmod(0o755)


def test_cli_default_config_missing_warns_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    result = _invoke_cli(["metadata", "--help"])
    assert result.exit_code == 0, result.output
    assert "日志配置文件不存在" in result.output


def test_cli_default_config_invalid_exits_2(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "logging.yaml").write_text(
        "version: {broken", encoding="utf-8"
    )
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    result = _invoke_cli(["metadata", "--help"])
    assert result.exit_code == 2, result.output
    assert "logging.yaml" in result.output
