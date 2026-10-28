"""文件操作工具函数"""

import json
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional
import yaml

logger = logging.getLogger("metaweave.file_utils")


def ensure_dir(directory: str | Path) -> Path:
    """确保目录存在，如果不存在则创建

    Args:
        directory: 目录路径

    Returns:
        目录的 Path 对象
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"创建目录: {dir_path}")
    return dir_path


def clear_dir_contents(directory: str | Path) -> Path:
    """递归删除目录下的文件和子目录（保留目录本身）。

    此函数不是 metadata/db 的 ``--clean`` 语义；步骤清理应使用
    ``clean_top_level_files()``，只删除顶层文件并检查项目根边界。

    Args:
        directory: 目录路径

    Returns:
        目录的 Path 对象
    """
    dir_path = ensure_dir(directory)
    for child in dir_path.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"清空目录失败 ({child}): {e}")
            raise
    return dir_path


def clean_top_level_files(directory: str | Path) -> int:
    """metadata/db 的 clean 语义：只删顶层文件（含隐藏文件），保留子目录。

    目标必须是项目根内的严格子目录；与递归清理的
    ``clear_dir_contents()`` 不同。
    """
    target = ensure_path_in_project_root(Path(directory))
    target.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in target.iterdir():
        if child.is_file():
            child.unlink()
            removed += 1
    return removed


def save_json(data: Any, file_path: str | Path, indent: int = 2) -> bool:
    """保存数据为 JSON 文件

    Args:
        data: 要保存的数据
        file_path: 文件路径
        indent: 缩进空格数

    Returns:
        是否保存成功
    """
    try:
        file_path = Path(file_path)
        ensure_dir(file_path.parent)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)

        logger.debug(f"保存 JSON 文件: {file_path}")
        return True
    except Exception as e:
        logger.error(f"保存 JSON 文件失败 ({file_path}): {e}")
        return False


def atomic_write_json(data: Any, file_path: str | Path, indent: int = 2) -> Path:
    """将 JSON 写入同目录唯一临时文件，再原子替换目标文件。

    替换已有文件时保留原权限；新文件使用 0644。任一步骤失败都会清理
    临时文件并向上抛出，已有目标文件保持不变。
    """
    target = Path(file_path)
    ensure_dir(target.parent)
    target_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, target_mode)
        os.replace(temp_path, target)
        return target
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def load_json(file_path: str | Path) -> Any:
    """加载 JSON 文件

    Args:
        file_path: 文件路径

    Returns:
        加载的数据，如果失败返回 None
    """
    try:
        file_path = Path(file_path)
        if not file_path.exists():
            logger.warning(f"JSON 文件不存在: {file_path}")
            return None

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        logger.debug(f"加载 JSON 文件: {file_path}")
        return data
    except Exception as e:
        logger.error(f"加载 JSON 文件失败 ({file_path}): {e}")
        return None


def save_text(text: str, file_path: str | Path) -> bool:
    """保存文本到文件

    Args:
        text: 文本内容
        file_path: 文件路径

    Returns:
        是否保存成功
    """
    try:
        file_path = Path(file_path)
        ensure_dir(file_path.parent)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text)

        logger.debug(f"保存文本文件: {file_path}")
        return True
    except Exception as e:
        logger.error(f"保存文本文件失败 ({file_path}): {e}")
        return False


def load_text(file_path: str | Path) -> str:
    """加载文本文件

    Args:
        file_path: 文件路径

    Returns:
        文本内容，如果失败返回空字符串
    """
    try:
        file_path = Path(file_path)
        if not file_path.exists():
            logger.warning(f"文本文件不存在: {file_path}")
            return ""

        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        logger.debug(f"加载文本文件: {file_path}")
        return text
    except Exception as e:
        logger.error(f"加载文本文件失败 ({file_path}): {e}")
        return ""


def load_yaml(file_path: str | Path) -> Dict[str, Any]:
    """加载 YAML 配置文件

    Args:
        file_path: 文件路径

    Returns:
        配置字典，如果失败返回空字典
    """
    try:
        file_path = Path(file_path)
        if not file_path.exists():
            logger.error(f"YAML 文件不存在: {file_path}")
            return {}

        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        logger.debug(f"加载 YAML 文件: {file_path}")
        return data if data else {}
    except Exception as e:
        logger.error(f"加载 YAML 文件失败 ({file_path}): {e}")
        return {}


def save_yaml(data: Dict[str, Any], file_path: str | Path) -> bool:
    """保存数据为 YAML 文件

    Args:
        data: 要保存的数据
        file_path: 文件路径

    Returns:
        是否保存成功
    """
    try:
        file_path = Path(file_path)
        ensure_dir(file_path.parent)

        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

        logger.debug(f"保存 YAML 文件: {file_path}")
        return True
    except Exception as e:
        logger.error(f"保存 YAML 文件失败 ({file_path}): {e}")
        return False


def get_project_root() -> Path:
    """获取项目根目录

    Returns:
        项目根目录的 Path 对象
    """
    # 优先从当前工作目录向上查找（支持从任意子目录运行 CLI）
    # - 新版独立项目：pyproject.toml / uv.lock / configs/config.yaml
    # - 兼容旧结构：configs/metaweave/metadata_config.yaml 等
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        if (parent / "pyproject.toml").exists():
            return parent
        if (parent / "uv.lock").exists():
            return parent
        if (parent / "configs" / "config.yaml").exists():
            return parent
        if (parent / "configs" / "metadata_config.yaml").exists():
            return parent

    # 其次从当前文件向上查找（适用于源码直接运行的场景）
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists():
            return parent
        if (parent / "uv.lock").exists():
            return parent
        if (parent / "configs" / "config.yaml").exists():
            return parent
        if (parent / "configs" / "metadata_config.yaml").exists():
            return parent

    # 如果找不到：
    # - 独立项目结构：metaweave/metaweave/utils -> project_root（parents[2]）
    # - 旧项目结构：nl2sql_v3/src/metaweave/utils -> nl2sql_v3（parents[3]）
    if len(current.parents) >= 3:
        candidate = current.parents[2]
        if (candidate / "configs").exists():
            return candidate
    return current.parents[3]


def require_mapping(node: Any, path: str) -> Dict[str, Any]:
    """配置节点必须是 mapping；否则抛带完整路径的 ValueError（清晰失败）。

    null 也视为类型错误（键存在但无内容与键缺失应区分——
    "键缺失"由调用方以默认值处理）。
    """
    if not isinstance(node, dict):
        raise ValueError(f"{path} 必须是对象（mapping），实际为 {type(node).__name__}")
    return node


def get_optional_str(container: Dict[str, Any], key: str, path: str) -> Optional[str]:
    """读取可选字符串值：**键缺失** → None；键存在时必须为非空字符串。

    `null`、非字符串（列表/布尔/数字/对象）与空字符串均抛带完整路径的
    ValueError——区分"键缺失"（缺省语义）与"键存在但值非法"
    （用户审核第三轮第 1 条 + 第四轮第 2 条：`ddl_directory: null` 曾回退
    默认目录、`neo4j.database: null` 曾使预警与驱动实际目标不一致）。
    """
    if key not in container:
        return None
    value = container.get(key)
    if value is None:
        raise ValueError(
            f"{path} 为 null 非法（键存在时必须提供非空字符串；"
            f"如需缺省语义请删除该键）"
        )
    if not isinstance(value, str):
        raise ValueError(f"{path} 必须是字符串，实际为 {type(value).__name__}")
    if not value.strip():
        raise ValueError(
            f"{path} 不能为空字符串（键存在时必须提供非空值；"
            f"如需缺省语义请删除该键或置为 null）"
        )
    return value


def resolve_project_path(path_like: "str | Path") -> Path:
    """共享项目路径 resolver（doc 26 §6.2）：相对路径统一基于项目根目录解析。

    全仓所有语义为文件/目录的相对路径字段（生成输出、依赖输入、loader 输入、
    clean 目标等）必须经此函数解析，不得在各模块自行使用 os.getcwd()、
    Path.cwd() 或直接拼接 get_project_root()。绝对路径原样返回。
    """
    path = Path(path_like)
    if path.is_absolute():
        return path
    return get_project_root() / path


def ensure_path_in_project_root(path: Path) -> Path:
    """clean 安全防护：目标路径必须是项目根目录内的**严格子目录**（doc 26 §6.2）。

    原 metadata_cli._ensure_in_project_root 上移为共享实现。涉及 clean 的
    文件目标受两条限制：
    1) 必须位于项目根目录内；
    2) **不能是项目根目录本身**——clean 会删除目标目录的顶层文件，
       若允许根目录，``ddl_directory: "."``、``output_dir: ""`` 等配置
       将删除 pyproject.toml、README、.env 等根级文件。``"."``、空字符串
       及其他解析后等于项目根的取值在此一并拒绝。
    """
    project_root = get_project_root().resolve()
    resolved = path.resolve()
    if resolved == project_root:
        raise ValueError(
            f"目标路径不能是项目根目录本身: {resolved}"
            f"（clean 目标必须是项目根内的子目录，不能为空、'.' 或项目根路径）"
        )
    if project_root not in resolved.parents:
        raise ValueError(f"目标路径不在项目根目录内: {resolved}")
    return resolved


def resolve_domains_yaml_path(
    config: dict, cli_value: Optional[str], project_root: Optional[Path] = None
) -> Path:
    """解析 db_domains.yaml 有效路径（metadata / sql-rag / pipeline 三处 CLI 共用）。

    优先级：
    1) CLI --domains-config 显式传入（cli_value 非 None 即视为显式传入）
    2) output.domains_directory / db_domains.yaml
    3) output.output_dir / domains / db_domains.yaml（domains_directory 缺键时回退）
    4) output/domains/db_domains.yaml（output 段整体缺省时兜底）

    相对路径统一基于 project_root 解析。
    """
    project_root = project_root if project_root is not None else get_project_root()
    if cli_value is not None:
        p = Path(cli_value)
        return p if p.is_absolute() else project_root / p

    config = config or {}
    output_cfg = (
        require_mapping(config.get("output"), "output") if "output" in config else {}
    )
    domains_dir = get_optional_str(
        output_cfg, "domains_directory", "output.domains_directory"
    )
    if domains_dir:
        d = Path(domains_dir)
        if not d.is_absolute():
            d = project_root / d
        return d / "db_domains.yaml"

    output_dir = get_optional_str(output_cfg, "output_dir", "output.output_dir")
    if output_dir:
        d = Path(output_dir)
        if not d.is_absolute():
            d = project_root / d
        return d / "domains" / "db_domains.yaml"

    return project_root / "output" / "domains" / "db_domains.yaml"


def resolve_dim_tables_yaml_path(
    config: dict, cli_value: Optional[str], project_root: Optional[Path] = None
) -> Path:
    """解析 dim_tables.yaml 有效路径（metadata --generate-dim-config / pipeline / dim_value_loader 共用）。

    优先级：
    1) CLI --dim-config 显式传入（cli_value 非 None 即视为显式传入）
    2) loaders.dim_loader.config_file（完整文件路径，读取方 dim_value_loader 同源）
    3) output/dim_config/dim_tables.yaml（缺键兜底）

    相对路径统一基于 project_root 解析。
    """
    project_root = project_root if project_root is not None else get_project_root()
    if cli_value is not None:
        p = Path(cli_value)
        return p if p.is_absolute() else project_root / p

    config = config or {}
    loaders_cfg = (
        require_mapping(config.get("loaders"), "loaders") if "loaders" in config else {}
    )
    dim_loader_cfg = (
        require_mapping(loaders_cfg.get("dim_loader"), "loaders.dim_loader")
        if "dim_loader" in loaders_cfg
        else {}
    )
    config_file = get_optional_str(
        dim_loader_cfg, "config_file", "loaders.dim_loader.config_file"
    )
    if config_file:
        p = Path(config_file)
        return p if p.is_absolute() else project_root / p

    return project_root / "output" / "dim_config" / "dim_tables.yaml"
