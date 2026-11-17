"""clean 目标解析（doc 26 §6.4/§6.2）。

计划阶段（pipeline 清理预警）与运行期复核（共享执行层在 clean 副作用前
比较）共用同一组 resolver 与规范化规则；不得各维护一套。

每个目标包含：
- kind: "dir" | "milvus" | "neo4j"
- display: 展示文本（预警输出；外部目标已脱敏）
- identity: 规范化身份字段（比较用，可哈希元组/字符串）
  - 文件：规范绝对路径字符串
  - Milvus：(host 小写去空白, port 无前导零十进制字符串, database, collection)
  - Neo4j：(脱敏 URI, database)
- note: 清理范围说明

规范化规则（doc 26 §6.2）：host 去首尾空白转小写；port 拒绝布尔并规范为
无前导零十进制字符串（YAML 整数 19530 / 字符串 "19530" / 环境变量展开同值
字符串 → 同一身份）；database 与 collection 去首尾空白但保留大小写。
解析只读取配置，不连接任何外部服务。
"""

from pathlib import Path
from typing import Any, Dict, Optional

from metaweave.utils import file_utils
from metaweave.utils.file_utils import (
    ensure_path_in_project_root,
    get_optional_str,
    require_mapping,
    resolve_dim_tables_yaml_path,
    resolve_domains_yaml_path,
    resolve_project_path,
)
from metaweave.utils.source_id import (
    normalize_source_id,
    normalize_workspace_id,
)

# 受管采集/发布步骤目录名（doc 6 §2）：dim_config/ 更名 dim/，sql-rag/ 更名 sql/。
STEP_DIR_NAMES = {
    "db": "db",
    "ddl": "ddl",
    "json": "json",
    "md": "md",
    "domain": "domain",
    "dim": "dim",
    "rel": "rel",
    "sql": "sql",
    "cql": "cql",
}


def resolve_workspace_root(source_id, workspace_id) -> Path:
    """工作区根目录 `output/<source_id>/<workspace_id>/`（doc 6 §2）。

    `source_id` 为 `db_src_1` 等 text 短 ID，`workspace_id` 为 `ws_1` 等
    短 ID。逐级拒绝符号链接、项目根边界与预期路径核对。
    """
    source = normalize_source_id(source_id)
    workspace = normalize_workspace_id(workspace_id)
    target = resolve_project_path(Path("output") / source / workspace)
    for part in (target.parents[2], target.parents[1], target.parent, target):
        if part.is_symlink():
            raise ValueError("工作区目录路径包含符号链接")
    guarded = ensure_path_in_project_root(target)
    expected = file_utils.get_project_root().resolve() / "output" / source / workspace
    if guarded != expected:
        raise ValueError("工作区目录路径与预期不一致")
    return guarded


def resolve_step_output_dir(source_id, workspace_id, step_name: str) -> Path:
    """受管步骤产物目录 `output/<source_id>/<workspace_id>/<step>/`。

    各步骤 resolver 的共用实现：短 ID 校验、逐级拒绝符号链接、项目根
    边界与预期路径核对；受管清理、写入、marker 与 manifest 共用。
    """
    if step_name not in STEP_DIR_NAMES:
        raise ValueError(f"未知的受管步骤目录: {step_name!r}")
    root = resolve_workspace_root(source_id, workspace_id)
    target = root / STEP_DIR_NAMES[step_name]
    for part in (target.parent, target):
        if part.is_symlink():
            raise ValueError("步骤产物目录路径包含符号链接")
    guarded = ensure_path_in_project_root(target)
    if guarded != target.resolve() or guarded.parent != root:
        raise ValueError("步骤产物目录路径与预期不一致")
    return guarded


def resolve_db_output_dir(source_id, workspace_id) -> Path:
    """db 产物目录：`output/<source_id>/<workspace_id>/db/`。"""
    return resolve_step_output_dir(source_id, workspace_id, "db")


def resolve_ddl_output_dir(source_id, workspace_id) -> Path:
    """受管 DDL 目录：`output/<source_id>/<workspace_id>/ddl/`。"""
    return resolve_step_output_dir(source_id, workspace_id, "ddl")


def resolve_json_output_dir(source_id, workspace_id) -> Path:
    """受管 JSON 目录：`output/<source_id>/<workspace_id>/json/`。"""
    return resolve_step_output_dir(source_id, workspace_id, "json")


def resolve_md_output_dir(source_id, workspace_id) -> Path:
    """受管 MD 目录：`output/<source_id>/<workspace_id>/md/`。"""
    return resolve_step_output_dir(source_id, workspace_id, "md")


def resolve_domain_output_dir(source_id, workspace_id) -> Path:
    """受管 Domain 目录：`output/<source_id>/<workspace_id>/domain/`。"""
    return resolve_step_output_dir(source_id, workspace_id, "domain")


def resolve_dim_output_dir(source_id, workspace_id) -> Path:
    """受管 Dim 目录（原 dim_config 更名）：`.../dim/`，产物 dim_tables.yaml。"""
    return resolve_step_output_dir(source_id, workspace_id, "dim")


def resolve_rel_output_dir(source_id, workspace_id) -> Path:
    """受管 Rel 目录：`output/<source_id>/<workspace_id>/rel/`。"""
    return resolve_step_output_dir(source_id, workspace_id, "rel")


def resolve_sql_output_dir(source_id, workspace_id) -> Path:
    """受管 SQL 样例目录（原 sql-rag 更名）：`.../sql/`。"""
    return resolve_step_output_dir(source_id, workspace_id, "sql")


def resolve_cql_output_dir(source_id, workspace_id) -> Path:
    """受管 cql 产物目录：`output/<source_id>/<workspace_id>/cql/`。

    cql 产物由 rel 发布过程创建，无采集步骤行；受管清理与发布生成共用。
    """
    return resolve_step_output_dir(source_id, workspace_id, "cql")


def resolve_source_workspace_dir(source_id) -> Path:
    """数据源全部工作区的根 `output/<source_id>/`。符号链接时拒绝。"""
    source = normalize_source_id(source_id)
    located = resolve_project_path(Path("output") / source)
    output_dir = located.parent
    if output_dir.is_symlink() or located.is_symlink():
        raise ValueError("数据源工作区路径包含符号链接")
    guarded = ensure_path_in_project_root(located)
    expected = output_dir.parent.resolve() / "output" / source
    if guarded != expected:
        raise ValueError("数据源工作区路径与预期不一致")
    return guarded


_METADATA_STEP_DIR_KEYS = {
    "ddl": "ddl_directory",
    "md": "markdown_directory",
    "json": "json_directory",
    "rel": "rel_directory",
    "cql": "cql_directory",
}

_RAG_COLLECTION_KEYS = {
    # sql 已随 doc 25.2 移除 CLI 入口、dim_value 已随 doc 22.2 移除——两条派发
    # 均不再可达；table_schema 条目经 CLI 跳过 clean 解析后同为不可达死代码，
    # 本篇不扩大改动面予以保留（彻底清理 milvus 子分支另立小任务，见 100 备忘录）
    "table_schema": "table_schema_loader",
}


# Neo4j 官方驱动支持的协议（bolt/neo4j 及其 TLS 变体）
_ALLOWED_NEO4J_SCHEMES = (
    "bolt",
    "bolt+s",
    "bolt+ssc",
    "neo4j",
    "neo4j+s",
    "neo4j+ssc",
)


_require_mapping = require_mapping  # 兼容既有调用点（实现见 file_utils）


def _get_mapping(container: dict, key: str, path: str) -> dict:
    """读取配置子节点：**键缺失** → {}（合法缺省）；键存在则必须为 mapping。

    不得使用 ``container.get(key) or {}``——那会把 []、""、false、0、null
    等假值静默转成缺省（用户审核第三轮第 1 条：output: [] 会被当作未配置，
    clean 目标解析到默认目录）。
    """
    if key not in container:
        return {}
    return require_mapping(container.get(key), path)


def normalize_neo4j_uri(uri: Any) -> str:
    """解析并校验 Neo4j URI，返回去除 userinfo/查询参数后的规范形式。

    （用户审核第 2 条）只允许驱动支持的协议；host 非空；显式 port 须在
    1..65535；拒绝格式错误。clean 计划与 identity 只使用本函数的输出。
    **错误消息一律不回显原始 URI**（原文可能含凭据）。

    Raises:
        ValueError: URI 为空、格式错误、协议不受支持或 host/port 非法。
    """
    from urllib.parse import urlsplit

    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("Neo4j URI 为空（必须提供 bolt:// 或 neo4j:// 形式的地址）")
    try:
        parts = urlsplit(uri.strip())
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Neo4j URI 格式错误（{type(e).__name__}）")

    if parts.scheme not in _ALLOWED_NEO4J_SCHEMES:
        detected = parts.scheme or "<无协议>"
        raise ValueError(
            f"Neo4j URI 协议不受支持（检测到 {detected}；"
            f"允许: {', '.join(_ALLOWED_NEO4J_SCHEMES)}）"
        )

    # 产品边界（用户审核第四轮第 1 条）：
    # - userinfo：Neo4j 驱动直接拒绝（"Username is not supported in the URI"），
    #   凭据只允许经 loaders.cql_loader.neo4j.user/password 提供；
    # - query：neo4j:// 的 query 被驱动解析为 routing context（driver.py
    #   parse_routing_context），具业务语义——v1 不支持，必须拒绝而非剥离
    #   （剥离会让不同 routing context 产生相同 identity，破坏"预警目标=
    #   实际清理目标"保证）；
    # - path / fragment：驱动不使用，拒绝以避免歧义。
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "Neo4j URI 不允许包含用户名/密码（userinfo）；"
            "凭据请通过 loaders.cql_loader.neo4j.user/password 配置"
        )
    if parts.query:
        raise ValueError(
            "Neo4j URI 不允许查询参数（neo4j:// 的 query 为 routing context，"
            "v1 未支持；如需 routing 请使用独立的 neo4j 配置项）"
        )
    if parts.path not in ("", "/"):
        raise ValueError("Neo4j URI 不允许路径（仅支持 scheme://host[:port]）")
    if parts.fragment:
        raise ValueError("Neo4j URI 不允许 fragment")

    host = parts.hostname
    if not host:
        raise ValueError("Neo4j URI 缺少主机名")
    try:
        port = parts.port
    except ValueError:
        # 不附加 urlsplit 原始异常（其消息会回显端口位置的任意文本，
        # 可能含敏感片段；用户审核第六轮第 2 条）
        raise ValueError(
            "Neo4j URI 端口非法（必须是 1–65535 的整数，其余内容一律拒绝）"
        ) from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(f"Neo4j URI 端口超出合法范围 1..65535（检测到 {port}）")

    host_fmt = f"[{host}]" if ":" in host else host
    netloc = host_fmt.lower()
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}"


_URI_IN_TEXT_RE = None


def redact_uris_in_text(text: str) -> str:
    """对任意文本中的 Neo4j URI 做脱敏（用户审核第五轮第 1 条）。

    驱动异常常含完整原始 URI（如 "Given URI: 'bolt://h:7687?token=xxx'"）。
    命中 URI 模式后替换为规范化形式（可规范化时）或 ``<redacted-uri>``，
    确保日志/终端输出不泄露 userinfo 与查询参数值。
    """
    global _URI_IN_TEXT_RE
    if _URI_IN_TEXT_RE is None:
        import re as _re

        _URI_IN_TEXT_RE = _re.compile(r"(?:bolt|neo4j)(?:\+s|\+ssc)?://[^\s'\"<>]+")
    if not isinstance(text, str):
        text = str(text)

    def _sub(m):
        try:
            return normalize_neo4j_uri(m.group(0))
        except ValueError:
            return "<redacted-uri>"

    return _URI_IN_TEXT_RE.sub(_sub, text)


def _normalize_host(host: Any) -> str:
    """Milvus host：只接受非空字符串（不做 str() 强转，第四轮第 2 条）。

    []/false/0/字典等被强转成 "[]"/"false"/"0" 会静默形成荒谬 endpoint。
    """
    if not isinstance(host, str):
        raise ValueError(f"Milvus host 必须是字符串，实际为 {type(host).__name__}")
    text = host.strip().lower()
    if not text:
        raise ValueError("Milvus host 为空（必须是非空主机名或地址）")
    return text


def _normalize_port(port: Any) -> str:
    if isinstance(port, bool):
        raise ValueError(f"Milvus port 非法（布尔值不受理）: {port!r}")
    if isinstance(port, int):
        value = port
    elif isinstance(port, str):
        text = port.strip()
        if not text:
            raise ValueError("Milvus port 为空")
        try:
            value = int(text)
        except ValueError as e:
            raise ValueError(f"Milvus port 不是合法整数: {port!r}") from e
    else:
        raise ValueError(f"Milvus port 类型非法: {port!r}")
    if not (1 <= value <= 65535):
        raise ValueError(f"Milvus port 超出合法范围 1..65535: {port!r}")
    return str(value)


def _dir_target(directory: str) -> Dict[str, Any]:
    try:
        abs_dir = ensure_path_in_project_root(resolve_project_path(directory))
    except ValueError as e:
        raise ValueError(f"目标目录不合法: {e}")
    return {
        "kind": "dir",
        "display": str(abs_dir),
        "identity": str(abs_dir),
        "note": "删除目录顶层全部文件，保留子目录",
    }


def _milvus_target(main_config: dict, collection_key: str) -> Dict[str, Any]:
    # 目标必须与 loader 实际传给 MilvusClient 的配置完全一致：三个 loader 均
    # 传入 vector_database.providers.milvus 子配置（不含根级 vector_database.database，
    # 该键无任何消费者）；MilvusClient 读子配置的 host/port/database，
    # database 缺省表示不切换（Milvus 默认库 "default"）。
    vector_db = _get_mapping(main_config, "vector_database", "vector_database")
    # 与 table_schema/sql 加载器同一约束（用户审核第八轮第 1 条）：active 必须
    # 为 milvus，providers.milvus 必须存在且非空——否则清理目标会静默落在
    # 客户端默认连接，存在清理非预期 Collection 的风险
    active = vector_db.get("active")
    if active != "milvus":
        raise ValueError(f"vector_database.active 必须为 'milvus'（实际为 {active!r}）")
    providers = _get_mapping(vector_db, "providers", "vector_database.providers")
    if "milvus" not in providers:
        raise ValueError("缺少 Milvus 配置: vector_database.providers.milvus")
    milvus = _get_mapping(providers, "milvus", "vector_database.providers.milvus")
    if not milvus:
        raise ValueError(
            "vector_database.providers.milvus 为空配置（需提供 host/port/database）"
        )

    host = _normalize_host(milvus.get("host", "localhost"))
    port = _normalize_port(milvus.get("port", 19530))
    database = (
        get_optional_str(
            milvus, "database", "vector_database.providers.milvus.database"
        )
        or ""
    ).strip() or "default"

    loaders = _get_mapping(main_config, "loaders", "loaders")
    loader_node = _get_mapping(loaders, collection_key, f"loaders.{collection_key}")
    collection = (
        get_optional_str(
            loader_node, "collection_name", f"loaders.{collection_key}.collection_name"
        )
        or ""
    ).strip()
    if not collection:
        raise ValueError(f"loaders.{collection_key}.collection_name 为空")

    return {
        "kind": "milvus",
        "display": f"{host}:{port}/{database} / collection={collection}",
        "identity": (host, port, database, collection),
        "note": "删除并重建该 Collection",
    }


def _neo4j_target(main_config: dict) -> Dict[str, Any]:
    loaders = _get_mapping(main_config, "loaders", "loaders")
    cql_loader = _get_mapping(loaders, "cql_loader", "loaders.cql_loader")
    neo = _get_mapping(cql_loader, "neo4j", "loaders.cql_loader.neo4j")
    # uri 与 CQLLoader 契约一致：必填（cql_loader._get_neo4j_config 直接取
    # neo4j_section["uri"]），resolver 不得虚构默认值——否则计划阶段显示
    # bolt://localhost:7687"合法"、任务阶段才因缺 uri 失败（exit 2 变 exit 1）。
    raw_uri = get_optional_str(neo, "uri", "loaders.cql_loader.neo4j.uri")
    if not raw_uri:
        raise ValueError(
            "loaders.cql_loader.neo4j.uri 缺失或为空（CQLLoader 将其视为必填项，"
            "请在主配置中提供 bolt:// 或 neo4j:// 地址）"
        )
    # 严格解析：协议白名单 / host 非空 / 端口范围；仅规范化结果进入
    # display 与 identity（原始 URI 可能含凭据，绝不参与输出与比较）
    uri = normalize_neo4j_uri(raw_uri)
    database = (
        get_optional_str(neo, "database", "loaders.cql_loader.neo4j.database") or ""
    ).strip() or "neo4j"
    return {
        "kind": "neo4j",
        "display": f"{uri} / database={database}",
        "identity": (uri, database),
        "note": "删除该数据库中的全部节点和关系",
    }


def resolve_clean_target(
    section: str,
    step: str,
    main_config: dict,
    *,
    domains_config_override: "Optional[Path]" = None,
    dim_config_path_override: "Optional[Path]" = None,
) -> Dict[str, Any]:
    """解析单个任务的 clean 目标（计划与运行期复核共用，doc 26 §6.4）。

    override 参数仅在 generate-domains / generate-dim-config 分支生效，
    用于让运行期复核解析出与实际 clean 执行完全一致的目标
    （独立 CLI 显式传 --domains-config/--dim-config 时；pipeline 恒 None）。

    Raises:
        ValueError: 无法解析出唯一、合法目标（调用方转为入口级错误：
            计划阶段 → PipelineConfigError/exit 2；运行期复核 → 任务 failed/exit 1）。
    """
    main_config = _require_mapping(main_config, "主配置")

    if section == "metadata":
        if step in _METADATA_STEP_DIR_KEYS:
            output_config = _get_mapping(main_config, "output", "output")
            dir_key = _METADATA_STEP_DIR_KEYS[step]
            dir_str = get_optional_str(output_config, dir_key, f"output.{dir_key}")
            if dir_str:
                return _dir_target(dir_str)
            output_dir = get_optional_str(
                output_config, "output_dir", "output.output_dir"
            )
            return _dir_target(str(resolve_project_path(output_dir or "output") / step))
        if step in ("generate-domains", "generate-dim-config"):
            # 两个共享 resolver 内部会读取 output 与 loaders 节点——提前校验类型
            _get_mapping(main_config, "output", "output")
            _get_mapping(main_config, "loaders", "loaders")
            if step == "generate-domains":
                yaml_path = resolve_domains_yaml_path(
                    main_config,
                    (
                        str(domains_config_override)
                        if domains_config_override is not None
                        else None
                    ),
                )
            else:
                yaml_path = resolve_dim_tables_yaml_path(
                    main_config,
                    (
                        str(dim_config_path_override)
                        if dim_config_path_override is not None
                        else None
                    ),
                )
            return _dir_target(str(yaml_path.parent))
        if step == "generate-sql-rag":
            sql_rag = _get_mapping(main_config, "sql_rag", "sql_rag")
            generation = _get_mapping(sql_rag, "generation", "sql_rag.generation")
            out = (
                get_optional_str(
                    generation, "output_dir", "sql_rag.generation.output_dir"
                )
                or "output/sql-rag"
            )
            return _dir_target(str(out))
        raise ValueError(f"未知 metadata 步骤: {step}")

    if section == "rag_load":
        if step == "cql":
            return _neo4j_target(main_config)
        if step in _RAG_COLLECTION_KEYS:
            return _milvus_target(main_config, _RAG_COLLECTION_KEYS[step])
        raise ValueError(f"未知 rag_load 类型: {step}")

    raise ValueError(f"未知章节: {section}")
