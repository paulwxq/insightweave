"""metadata 共享执行层（doc 26 §6.2）。

独立 metadata CLI 与 pipeline 编排共用同一业务执行入口：
- Click 包装层只负责参数解析、归一化与校验，构造 MetadataExecutionRequest；
- 本模块执行单个已确定 operation，返回结构化结果，不抛 click.Abort；
- generate-domains 的参数规范化是两个入口唯一的语义事实源。
"""

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from metaweave.utils.file_utils import clean_top_level_files
from metaweave.utils.logger import reset_current_step, set_current_step

# metadata operation 标识；db 仅供独立 CLI，暂不加入 pipeline YAML 步骤集合。
METADATA_OPERATIONS = (
    "db",
    "ddl",
    "md",
    "generate-domains",
    "json",
    "generate-dim-config",
    "rel",
    "cql",
    "generate-sql-rag",
)

# set_current_step 固定映射（doc 26 §6.2）：独立 CLI 与 pipeline 共用，
# 修复 generate-domains / generate-dim-config 原先设置为空字符串的行为
OPERATION_STEP_MAP = {
    "db": "db",
    "ddl": "ddl",
    "md": "md",
    "generate-domains": "generate-domains",
    "json": "json",
    "generate-dim-config": "dim",
    "rel": "rel",
    "cql": "cql",
    # Web 采集步骤 domain（doc 21.1 §5.1）：只进本映射，不进 METADATA_OPERATIONS
    "domain": "domain",
    # Web 采集 dim 步骤（doc 6 §2 更名）：复用 dim 日志段
    "dim": "dim",
    # Web 采集 sql 步骤：复用 SQL 生成日志段，不改变 CLI operation 名称。
    "sql": "sql-rag-generate",
    # generate-sql-rag 由执行函数按阶段切换 sql-rag-generate / sql-rag-validate
    "generate-sql-rag": "sql-rag-generate",
}

_MD_CONTEXT_MODES = ("name", "name_comment", "full")


# doc 15 §4.3：single_column / composite（匹配部分）已废弃，搬迁至
# relationships.candidate_matching + logical_key_detection.*_exclude_roles。
_DEPRECATED_RELATIONSHIP_CONFIG_NODES = {
    "single_column": (
        "节点 'single_column' 已废弃（doc 15）。"
        "匹配相关配置请迁移到 'relationships.candidate_matching'；"
        "exclude_semantic_roles 请迁移到 "
        "'logical_key_detection.single_column_exclude_roles'。"
    ),
    "composite": (
        "节点 'composite' 已废弃（doc 15）。"
        "匹配相关配置请迁移到 'relationships.candidate_matching'；"
        "exclude_semantic_roles 请迁移到 "
        "'logical_key_detection.composite_exclude_roles'。"
    ),
}


def validate_deprecated_relationship_config_nodes(config: dict) -> None:
    """检测已废弃的顶层配置节点 single_column / composite（doc 15，不静默兼容）。"""
    for key, guidance in _DEPRECATED_RELATIONSHIP_CONFIG_NODES.items():
        if key in config:
            raise ValueError(f"检测到已废弃的顶层配置节点 '{key}'。\n{guidance}")


def resolve_domain_params(
    cli_domain: "Optional[str]",
    cli_cross_domain: "Optional[bool]",
    config: dict,
) -> "tuple[Optional[str], bool]":
    """合并 domain/cross_domain 参数：CLI > yaml > 默认值 null（共享实现）。"""
    rel_cfg = config.get("relationships") or {}
    d = cli_domain if cli_domain is not None else rel_cfg.get("domain")
    cd = (
        cli_cross_domain
        if cli_cross_domain is not None
        else rel_cfg.get("cross_domain")
    )
    if not d:
        return None, False
    return d, bool(cd)


def load_main_config_snapshot(config_path) -> dict:
    """加载并校验主配置，返回单任务唯一快照（doc 26 §6.2）。

    每个任务仅调用一次：load_config + LLM 模块路径预检（declared/nonstandard）
    + 废弃关系配置节点校验。校验失败抛 ValueError（调用方收敛为任务失败或
    参数错误）；成功后的同一快照贯穿 clean 复核、下游构造与业务执行。
    """
    from services.config_loader import load_config
    from metaweave.services.llm_config_resolver import (
        _validate_declared_module_llm_paths,
        _validate_nonstandard_llm_paths,
    )

    config = load_config(config_path)
    _validate_declared_module_llm_paths(config)
    _validate_nonstandard_llm_paths(config)
    validate_deprecated_relationship_config_nodes(config)
    return config


@dataclass(frozen=True)
class NormalizedGenerateDomainsOptions:
    """generate-domains 参数规范化结果（两入口唯一语义事实源）。"""

    description: Optional[str]
    md_context_mode: str
    md_limit: Optional[int]


def normalize_generate_domains_options(
    description: Optional[str],
    md_context_mode: Optional[str],
    md_limit: Optional[int],
) -> NormalizedGenerateDomainsOptions:
    """规范化 generate-domains 的三个可选参数（doc 26 §3.1/§6.2）。

    规则（对 pipeline YAML 与独立 CLI 完全一致，且为防御性共享入口）：
    - description：None / 空串 / 纯空白 → None；非空字符串 → 去首尾空白；
      非字符串类型（int/bool/list 等）一律拒绝，不做 str() 强转
    - md_context_mode：None → "name_comment"；其余必须是规范小写
      name/name_comment/full 之一（本函数不做大小写转换，大写输入拒绝）
    - md_limit：None 表示未覆盖（沿用主配置回退链）；显式值必须为
      非布尔正整数（0、负数、浮点、字符串、布尔均拒绝）

    Raises:
        ValueError: 参数非法（调用方负责转为入口级错误：CLI → UsageError，
            pipeline → 计划配置错误；两者均发生在任何副作用之前）
    """
    if description is not None and not isinstance(description, str):
        raise ValueError(
            f"description 必须是字符串或 None"
            f"（检测到: {type(description).__name__}）"
        )
    if description is None or not description.strip():
        normalized_description = None
    else:
        normalized_description = description.strip()

    if md_context_mode is None:
        normalized_mode = "name_comment"
    elif md_context_mode in _MD_CONTEXT_MODES:
        normalized_mode = md_context_mode
    else:
        raise ValueError(
            f"md_context_mode 必须是 {'/'.join(_MD_CONTEXT_MODES)} 之一"
            f"（区分大小写，仅接受规范小写值）: {md_context_mode!r}"
        )

    if md_limit is None:
        normalized_limit: Optional[int] = None
    elif isinstance(md_limit, bool) or not isinstance(md_limit, int) or md_limit <= 0:
        raise ValueError(f"md_limit 显式值必须为正整数（非布尔）: {md_limit!r}")
    else:
        normalized_limit = md_limit

    return NormalizedGenerateDomainsOptions(
        description=normalized_description,
        md_context_mode=normalized_mode,
        md_limit=normalized_limit,
    )


@dataclass
class MetadataExecutionRequest:
    """单个 metadata operation 的执行请求（doc 26 §6.2 签名草案）。

    三个 *_override 表达用户是否**显式**覆盖路径（CLI ParameterSource 为
    COMMANDLINE 时才写入；pipeline v1 始终 None），不是解析后的最终路径。
    """

    config_path: Path
    operation: str
    clean: bool = False
    expected_clean_target: Optional[object] = (
        None  # ResolvedCleanTarget | None（阶段二 B4 引入）
    )
    domains_config_override: Optional[Path] = None
    md_context_dir_override: Optional[Path] = None
    dim_config_path_override: Optional[Path] = None
    description: Optional[str] = None  # 仅 generate-domains
    md_context_mode: str = "name_comment"  # 仅 generate-domains
    md_limit: Optional[int] = None  # 仅 generate-domains
    domain_override: Optional[str] = None  # 仅 rel（CLI 显式 --domain）
    cross_domain_override: Optional[bool] = None  # 仅 rel（CLI 显式 --cross-domain）
    schemas_override: Optional[tuple] = None  # 仅 ddl/json/md（CLI 显式 --schemas）
    tables_override: Optional[tuple] = None  # 仅 ddl/json/md（CLI 显式 --tables）
    max_workers_override: Optional[int] = (
        None  # 仅 ddl/json/md（CLI 显式 --max-workers）
    )
    cql_source_id: Optional[str] = None  # 仅 cql（doc 4 §4.2：实体键身份必填）

    def __post_init__(self) -> None:
        if self.operation not in METADATA_OPERATIONS:
            raise ValueError(f"未知 metadata operation: {self.operation!r}")
        self.config_path = Path(self.config_path)


def set_operation_step(
    operation: str, stage: Optional[str] = None
) -> contextvars.Token:
    """按固定映射设置日志 step（generate-sql-rag 允许阶段二次切换）。

    返回 set_current_step 的 token，供调用方在任务结束时恢复上下文
    （doc 10.1 §4.4）。
    """
    if operation == "generate-sql-rag" and stage in ("generate", "validate"):
        return set_current_step(f"sql-rag-{stage}")
    return set_current_step(OPERATION_STEP_MAP[operation])


# ---------------------------------------------------------------------------
# 共享执行函数（doc 26 §6.2）：不抛 click.Abort，统一返回结果 dict
# ---------------------------------------------------------------------------


def _task_result(success: bool, elapsed: float, message: str) -> dict:
    return {"success": success, "execution_time": elapsed, "message": message}


def resolve_md_context_dir(config: dict, override: Optional[Path]) -> Path:
    """纯配置链解析 md 目录（无 Click context，pipeline 与共享层共用）。

    优先级：override（仅显式覆盖，pipeline 恒 None）>
    output.markdown_directory > output.output_dir/md > output/md
    """
    from metaweave.utils.file_utils import resolve_project_path

    if override is not None:
        return resolve_project_path(override)

    output_cfg = config.get("output", {}) or {}
    markdown_dir = output_cfg.get("markdown_directory")
    if markdown_dir:
        return resolve_project_path(str(markdown_dir))

    output_dir = output_cfg.get("output_dir")
    if output_dir:
        return resolve_project_path(output_dir) / "md"

    return resolve_project_path("output/md")


def execute_generate_domains(request: MetadataExecutionRequest) -> dict:
    """执行 generate-domains operation（两入口共享业务路径，doc 26 §6.2）。"""
    import time as _time

    import click

    start = _time.perf_counter()
    _step_token = set_operation_step("generate-domains")
    try:
        from metaweave.utils.file_utils import resolve_domains_yaml_path
        from metaweave.core.metadata.domain_generator import DomainGenerator

        # 单任务唯一配置快照（含 LLM 预检/废弃节点校验，doc 26 §6.2）
        config = load_main_config_snapshot(request.config_path)

        # 防御性规范化（两个输入层已各自调用；直接 Python 调用在此拦截，
        # 任何 clean/文件写入/LLM 调用之前返回失败）
        try:
            opts = normalize_generate_domains_options(
                request.description,
                request.md_context_mode,
                request.md_limit,
            )
        except ValueError as e:
            return _task_result(False, _time.perf_counter() - start, f"参数非法: {e}")

        domains_config_path = resolve_domains_yaml_path(
            config,
            (
                str(request.domains_config_override)
                if request.domains_config_override is not None
                else None
            ),
        )
        md_dir = resolve_md_context_dir(config, request.md_context_dir_override)

        _verify_err = verify_clean_target(request, "metadata", config)
        if _verify_err:
            return _task_result(False, _time.perf_counter() - start, _verify_err)
        if request.clean:
            removed = clean_top_level_files(domains_config_path.parent)
            click.echo(
                f"🧹 已清理 {domains_config_path.parent.resolve()} 下 {removed} 个文件（保留子目录）"
            )

        generator = DomainGenerator(
            config=config,
            yaml_path=str(domains_config_path),
            md_context_dir=str(md_dir),
            md_context_mode=opts.md_context_mode,
            md_context_limit=opts.md_limit,
        )
        generated_config = generator.generate_from_context(
            user_description=opts.description
        )
        final_domains = generator.write_to_yaml(generated_config)
        click.echo(
            f"✅ 已生成 {max(0, len(final_domains) - 1)} 个 domain 并写入 {domains_config_path}"
        )
        return _task_result(
            True,
            _time.perf_counter() - start,
            f"已生成 {max(0, len(final_domains) - 1)} 个 domain",
        )
    except Exception as e:  # noqa: BLE001 —— 共享层收敛一切异常为失败结果
        return _task_result(False, _time.perf_counter() - start, str(e))
    finally:
        # 任务结束恢复步骤上下文（doc 10.1 §4.4）
        reset_current_step(_step_token)


def execute_generate_dim_config(request: MetadataExecutionRequest) -> dict:
    """执行 generate-dim-config operation（两入口共享，doc 26 §6.2）。"""
    import time as _time

    import click

    start = _time.perf_counter()
    _step_token = set_operation_step("generate-dim-config")
    try:
        from metaweave.utils.file_utils import (
            resolve_dim_tables_yaml_path,
            resolve_project_path,
        )
        from metaweave.core.dim_value.config_generator import DimTableConfigGenerator

        config = load_main_config_snapshot(request.config_path)
        dim_tables_path = resolve_dim_tables_yaml_path(
            config,
            (
                str(request.dim_config_path_override)
                if request.dim_config_path_override is not None
                else None
            ),
        )

        _verify_err = verify_clean_target(request, "metadata", config)
        if _verify_err:
            return _task_result(False, _time.perf_counter() - start, _verify_err)
        if request.clean:
            removed = clean_top_level_files(dim_tables_path.parent)
            click.echo(
                f"🧹 已清理 {dim_tables_path.parent.resolve()} 下 {removed} 个文件（保留子目录）"
            )

        json_dir_cfg = (config.get("output") or {}).get("json_directory")
        if not json_dir_cfg:
            return _task_result(
                False,
                _time.perf_counter() - start,
                "metadata_config.yaml 缺少 output.json_directory 配置",
            )
        json_dir = resolve_project_path(json_dir_cfg)

        click.echo("")
        click.echo("🔍 开始扫描维度表...")
        click.echo(f"📂 扫描目录: {json_dir}")
        click.echo("")

        generator = DimTableConfigGenerator(
            json_dir=json_dir, output_path=dim_tables_path
        )
        result = generator.generate()

        databases_cfg = result.get("databases", {})
        total_tables = sum(
            len(db_cfg.get("tables", {}))
            for db_cfg in databases_cfg.values()
            if isinstance(db_cfg, dict)
        )

        click.echo(f"✅ 已生成 {dim_tables_path}")
        click.echo(
            f"📊 识别到 {len(databases_cfg)} 个数据库，{total_tables} 个维度表："
        )
        for db_name, db_cfg in databases_cfg.items():
            tables = db_cfg.get("tables", {}) if isinstance(db_cfg, dict) else {}
            click.echo(f"  - {db_name} ({len(tables)} 张)")
            for table_name in tables.keys():
                click.echo(f"      - {table_name}")

        click.echo("")
        click.echo("⚠️  请手工填写 embedding_col 字段（要向量化的列名）")
        click.echo(
            "⚠️  重新执行本命令会覆盖整个文件，已人工填写的 embedding_col 将被重置"
        )
        click.echo("")
        if databases_cfg:
            first_db_name = next(iter(databases_cfg.keys()))
            first_tables = (
                databases_cfg[first_db_name].get("tables", {})
                if isinstance(databases_cfg[first_db_name], dict)
                else {}
            )
            sample_table = next(iter(first_tables.keys()), "public.dim_example")
            click.echo("📝 配置示例：")
            click.echo("    databases:")
            click.echo(f"      {first_db_name}:")
            click.echo("        tables:")
            click.echo(f"          {sample_table}:")
            click.echo("            embedding_col: your_text_column       # 单列")
            click.echo("")
            click.echo("    # 多列向量化（同一张表的多个列）：")
            click.echo("    databases:")
            click.echo(f"      {first_db_name}:")
            click.echo("        tables:")
            click.echo(f"          {sample_table}:")
            click.echo(
                "            embedding_col: [col1, col2, col3]     # YAML列表（推荐）"
            )
            click.echo("            # 或")
            click.echo(
                "            embedding_col: col1, col2, col3       # 逗号分隔（自动拆分）"
            )
        return _task_result(
            True, _time.perf_counter() - start, f"已生成 {dim_tables_path}"
        )
    except Exception as e:  # noqa: BLE001
        return _task_result(False, _time.perf_counter() - start, str(e))
    finally:
        # 任务结束恢复步骤上下文（doc 10.1 §4.4）
        reset_current_step(_step_token)


def execute_generate_sql_rag(request: MetadataExecutionRequest) -> dict:
    """执行 generate-sql-rag operation（包装现有 _run_generate_sql_rag）。

    doc 26 §6.2：将 click.Abort 等失败路径收敛为结果 dict，不向 pipeline
    抛出 Abort；B2 批次将把底层改为接收 main_config 快照。
    """
    import time as _time

    start = _time.perf_counter()
    _step_token = set_operation_step("generate-sql-rag", stage="generate")
    try:
        from metaweave.cli.metadata_cli import _run_generate_sql_rag

        _main_config = load_main_config_snapshot(request.config_path)
        _verify_err = verify_clean_target(request, "metadata", _main_config)
        if _verify_err:
            return _task_result(False, _time.perf_counter() - start, _verify_err)
        _reason = _run_generate_sql_rag(
            config_path=request.config_path,
            domains_config=(
                str(request.domains_config_override)
                if request.domains_config_override is not None
                else None
            ),
            md_context_dir_override=request.md_context_dir_override,
            clean=request.clean,
            main_config=_main_config,
        )
        if _reason:
            return _task_result(False, _time.perf_counter() - start, _reason)
        return _task_result(True, _time.perf_counter() - start, "生成完成")
    except Exception as e:  # noqa: BLE001
        # 空消息兜底（如历史 Abort 路径 str(e)==""，避免汇总只有失败标记无原因）
        return _task_result(
            False, _time.perf_counter() - start, str(e) or type(e).__name__
        )
    finally:
        # 任务结束恢复步骤上下文（doc 10.1 §4.4）
        reset_current_step(_step_token)


def _clean_step_dir(step_name: str, config: dict) -> None:
    """清理 metadata 步骤输出目录（五步统一语义，共享版 doc 26 §6.2）。"""
    from metaweave.utils.file_utils import resolve_project_path

    output_config = config.get("output", {}) or {}
    output_dir = resolve_project_path(str(output_config.get("output_dir", "output")))
    key = f"{step_name}_directory"
    if step_name == "md":
        key = "markdown_directory"
    dir_str = output_config.get(key)
    if dir_str:
        target = resolve_project_path(str(dir_str))
    else:
        target = output_dir / step_name
    clean_top_level_files(target)


def verify_clean_target(
    request: MetadataExecutionRequest, section: str, main_config: dict
) -> "Optional[str]":
    """clean 期望目标的运行期复核（doc 26 §6.2）。

    任务开始后从唯一配置快照解析当前目标，与 pipeline 计划阶段传入的
    expected_clean_target 比较（仅比较规范化身份字段）。
    返回错误消息（str）表示复核失败——调用方必须在该任务的任何 clean
    副作用之前返回 success=False；返回 None 表示通过或无期望值（独立
    CLI 传 None，不执行比较）。
    """
    if not request.clean:
        return None
    from metaweave.core.clean_targets import resolve_clean_target

    # clean=true 时**始终**从唯一快照解析一次当前目标（doc 26 §6.2：独立 CLI
    # 无预警阶段但仍解析并用于安全校验，只是 expected 为 None 时不比较）——
    # 用户审核第三轮第 3 条：独立命令也须共用同一套 clean 安全边界
    # （节点类型/host-port/Neo4j URI 严格解析）。
    try:
        current = resolve_clean_target(
            section,
            request.operation,
            main_config,
            domains_config_override=request.domains_config_override,
            dim_config_path_override=request.dim_config_path_override,
        )
    except ValueError as e:
        return f"clean 当前目标无法解析，未执行任何清理: {e}"

    expected = request.expected_clean_target
    if expected is None:
        return None  # 独立 CLI：解析成功即通过，不执行期望值比较
    if current["identity"] != expected.get("identity"):
        return (
            "clean 目标与预警不一致，未执行任何清理: "
            f"预警={expected.get('display')} 当前={current['display']}"
        )
    return None


def execute_cql(request: MetadataExecutionRequest) -> dict:
    """执行 cql operation（两入口共享，doc 26 §6.2）。"""
    import time as _time

    import click

    from metaweave.utils.file_utils import resolve_domains_yaml_path

    start = _time.perf_counter()
    _step_token = set_operation_step("cql")
    try:
        from metaweave.core.cql_generator.generator import CQLGenerator

        config = load_main_config_snapshot(request.config_path)
        click.echo("🔧 开始生成 Neo4j CQL...")
        click.echo("")

        from metaweave.core.domains import DomainResolver as _DR

        _domains_val = (
            str(request.domains_config_override)
            if request.domains_config_override is not None
            else None
        )
        default_path = resolve_domains_yaml_path(config, _domains_val)
        resolver = _DR(default_path) if default_path.exists() else None

        # doc 4 §4.2：实体键身份依赖 source_id，CLI 生成入口必须显式携带
        # （调试 publish_run_id 用随机 UUID；不写平台库发布记录）
        from uuid import uuid4

        from metaweave.utils.source_id import normalize_source_id

        try:
            source_uuid = str(normalize_source_id(request.cql_source_id))
        except ValueError:
            return _task_result(
                False,
                _time.perf_counter() - start,
                "--step cql 需要 --source-id（平台登记短 ID，形如 db_src_1）：实体键身份"
                "（entity_id = source_id:逻辑id）无法省略",
            )
        generator = CQLGenerator(
            request.config_path,
            domain_resolver=resolver,
            config=config,
            provenance=(source_uuid, str(uuid4())),
        )

        _verify_err = verify_clean_target(request, "metadata", config)
        if _verify_err:
            return _task_result(False, _time.perf_counter() - start, _verify_err)
        if request.clean:
            _clean_step_dir("cql", config)

        if generator.config.get("output", {}).get("json_llm_directory"):
            import logging

            logging.getLogger("metaweave.cli").warning(
                "⚠️ 配置项 'json_llm_directory' 已废弃，统一使用 'json_directory'"
            )

        result = generator.generate(step_name="cql")

        click.echo("")
        click.echo("=" * 60)
        click.echo("📊 CQL 生成结果统计")
        click.echo("=" * 60)
        click.echo(f"✅ 表节点: {result.tables_count} 个")
        click.echo(f"✅ 列节点: {result.columns_count} 个")
        click.echo(f"✅ HAS_COLUMN 关系: {result.has_column_count} 个")
        click.echo(f"✅ JOIN_ON 关系: {result.relationships_count} 个")
        click.echo(
            f"✅ 边总数: {result.has_column_count + result.relationships_count} 个"
        )
        _fstats = result.filter_stats
        click.echo(
            f"🔍 JOIN_ON 过滤统计: 候选 {_fstats.candidate_count} | "
            f"通过阈值 {_fstats.threshold_passed_count} | "
            f"被过滤 {_fstats.threshold_filtered_count} | "
            f"重复组 {_fstats.duplicate_group_count} | "
            f"去重丢弃 {_fstats.duplicate_discarded_count} | "
            f"最终 {_fstats.final_count}"
        )
        click.echo(f"📁 输出文件: {len(result.output_files)} 个")

        for file_path in result.output_files:
            click.echo(f"  - {Path(file_path).name}")

        if result.errors:
            click.echo("\n⚠️  错误列表:")
            for error in result.errors[:5]:
                click.echo(f"  - {error}", err=True)
            if len(result.errors) > 5:
                click.echo(f"  ... 还有 {len(result.errors) - 5} 个错误", err=True)

        click.echo("=" * 60)

        if not result.success:
            click.echo("⚠️  CQL 生成完成，但存在错误", err=True)
            return _task_result(
                False, _time.perf_counter() - start, "CQL 生成完成，但存在错误"
            )
        click.echo("✨ CQL 生成完成！")
        return _task_result(True, _time.perf_counter() - start, "CQL 生成完成")
    except Exception as e:  # noqa: BLE001
        return _task_result(False, _time.perf_counter() - start, str(e))
    finally:
        # 任务结束恢复步骤上下文（doc 10.1 §4.4）
        reset_current_step(_step_token)


def execute_rel(request: MetadataExecutionRequest) -> dict:
    """执行 rel operation（两入口共享，doc 26 §6.2）。

    domain/cross_domain 走 CLI > yaml > null 合并语义：request 的
    domain_override/cross_domain_override 为 None 表示未覆盖（pipeline 恒 None）。
    """
    import time as _time

    import click

    start = _time.perf_counter()
    _step_token = set_operation_step("rel")
    try:
        from metaweave.utils.file_utils import resolve_domains_yaml_path
        from metaweave.core.domains import DomainResolver
        from metaweave.core.relationships.pipeline import RelationshipDiscoveryPipeline

        config = load_main_config_snapshot(request.config_path)
        effective_domain, effective_cross_domain = resolve_domain_params(
            request.domain_override, request.cross_domain_override, config
        )

        click.echo("🔗 开始关系发现...")
        click.echo("")

        # 依赖校验必须先于任何 clean 副作用（用户审核第 1 条）：domains 校验
        # 失败时若已清空 rel 输出目录，旧产物将无谓丢失
        domain_resolver = None
        if effective_domain:
            _dv = (
                str(request.domains_config_override)
                if request.domains_config_override is not None
                else None
            )
            domains_config_path = resolve_domains_yaml_path(config, _dv)
            source_hint = (
                f"CLI 参数 --domain={request.domain_override}"
                if request.domain_override is not None
                else f"yaml 配置 relationships.domain={effective_domain}"
            )
            if not domains_config_path.exists():
                return _task_result(
                    False,
                    _time.perf_counter() - start,
                    f"当前生效的 domain 配置（来源: {source_hint}）需要 domains 配置文件，"
                    f"但 {domains_config_path} 不存在。"
                    f"请先执行 --generate-domains 生成该文件，或取消 domain 配置",
                )
            # DomainResolver 对空文件/损坏 YAML 会静默退化为空配置（resolver.py
            # _load 直接 return），因此必须显式校验业务域列表非空，
            # 否则会出现"成功执行但未生成任何关系"
            domain_resolver = DomainResolver(domains_config_path)
            if not domain_resolver.get_all_domains():
                return _task_result(
                    False,
                    _time.perf_counter() - start,
                    f"当前生效的 domain 配置（来源: {source_hint}）需要有效的 domains 列表，"
                    f"但 {domains_config_path} 为空、内容无法解析或未定义任何业务域。"
                    f"请先执行 --generate-domains 生成该文件，或取消 domain 配置",
                )

        _verify_err = verify_clean_target(request, "metadata", config)
        if _verify_err:
            return _task_result(False, _time.perf_counter() - start, _verify_err)
        if request.clean:
            _clean_step_dir("rel", config)

        pipeline = RelationshipDiscoveryPipeline(
            request.config_path,
            domain_filter=effective_domain,
            cross_domain=bool(effective_cross_domain),
            domain_resolver=domain_resolver,
            config=config,
        )
        result = pipeline.discover()

        click.echo("")
        click.echo("=" * 60)
        click.echo("📊 关系发现结果统计")
        click.echo("=" * 60)
        click.echo(f"✅ 发现关系: {result.total_relations} 个")
        click.echo(f"  - 外键直通: {result.foreign_key_relations}")
        click.echo(f"  - 推断关系: {result.inferred_relations}")
        click.echo(f"  - 高置信度: {result.high_confidence_count}")
        click.echo(f"  - 中置信度: {result.medium_confidence_count}")
        click.echo(f"  - 未达阈值: {result.below_threshold_count}")
        click.echo(f"  - 复合键抑制: {result.suppressed_count}")
        if result.llm_candidates_enabled:
            click.echo(
                f"  - LLM 候选产出: 成功 {result.llm_success_pairs}/"
                f"{result.llm_total_pairs} 个表对，失败 {result.llm_failed_pairs}"
            )
        click.echo(f"📁 输出文件: {len(result.output_files)} 个")

        if result.errors:
            click.echo("\n⚠️  错误列表:")
            for error in result.errors[:5]:
                click.echo(f"  - {error}", err=True)
            if len(result.errors) > 5:
                click.echo(f"  ... 还有 {len(result.errors) - 5} 个错误", err=True)

        click.echo("=" * 60)

        if not result.success:
            click.echo("⚠️  关系发现完成，但存在错误", err=True)
            return _task_result(
                False, _time.perf_counter() - start, "关系发现完成，但存在错误"
            )
        click.echo("✨ 关系发现完成！")
        return _task_result(True, _time.perf_counter() - start, "关系发现完成")
    except Exception as e:  # noqa: BLE001
        return _task_result(False, _time.perf_counter() - start, str(e))
    finally:
        # 任务结束恢复步骤上下文（doc 10.1 §4.4）
        reset_current_step(_step_token)


def execute_ddl_json_md(request: MetadataExecutionRequest) -> dict:
    """执行 ddl / json / md operation（两入口共享，doc 26 §6.2）。

    共享层语义默认值（与用户裸执行 metadata 且未传可选参数一致）：
    schemas/tables 未覆盖、max_workers=4。
    """
    import time as _time

    import click

    start = _time.perf_counter()
    _step_token = set_operation_step(request.operation)
    step = request.operation
    try:
        from metaweave.utils.file_utils import resolve_project_path
        from metaweave.core.metadata.generator import MetadataGenerator

        # 单任务唯一配置快照：一次加载贯穿 md 前置检查、clean 与执行（doc 26 §6.2）
        config = load_main_config_snapshot(request.config_path)

        # 参数防御必须先于一切依赖检查与 clean 副作用（用户审核第三轮第 2 条）：
        # 校验失败时不得删除旧产物。0/负数/布尔一律拒绝（不做 `or 4` 假值重置）。
        if request.max_workers_override is not None:
            if (
                isinstance(request.max_workers_override, bool)
                or not isinstance(request.max_workers_override, int)
                or request.max_workers_override < 1
            ):
                return _task_result(
                    False,
                    _time.perf_counter() - start,
                    f"max_workers 必须为正整数（检测到: {request.max_workers_override!r}）",
                )
            max_workers = request.max_workers_override
        else:
            max_workers = 4

        # md 前置：JSON 目录存在且当前库有严格三段式 JSON 文件（doc 17 §3.4）
        if step == "md":
            output_config = config.get("output", {})
            output_dir = resolve_project_path(output_config.get("output_dir", "output"))
            json_dir_str = output_config.get("json_directory")
            json_dir = (
                resolve_project_path(json_dir_str)
                if json_dir_str
                else output_dir / "json"
            )
            if not json_dir.exists():
                return _task_result(
                    False,
                    _time.perf_counter() - start,
                    f"--step md 依赖 JSON 产物，但 JSON 目录不存在: {json_dir}；"
                    f"请先执行 --step json",
                )
            database_name = config.get("database", {}).get("database")
            if not database_name:
                return _task_result(
                    False,
                    _time.perf_counter() - start,
                    "--step md 无法确定当前数据库名，请配置 database.database",
                )
            db_json_files = sorted(
                f
                for f in json_dir.glob("*.json")
                if f.name.startswith(f"{database_name}.")
            )
            # 先检查当前库的全部候选文件，再判断是否有有效输入。
            # 这样两段式文件名和“合法文件旁混有坏文件”都不会被忽略。
            misnamed = []
            for json_file in db_json_files:
                parts = json_file.stem.split(".")
                if len(parts) != 3 or not all(parts):
                    misnamed.append(json_file.name)
            if misnamed:
                shown = ", ".join(misnamed[:5])
                suffix = f" 等 {len(misnamed)} 个文件" if len(misnamed) > 5 else ""
                return _task_result(
                    False,
                    _time.perf_counter() - start,
                    f"--step md 依赖当前数据库 {database_name} 的 JSON 产物，"
                    f"但文件名不合规（期望 {database_name}.<schema>.<object>.json）: "
                    f"{shown}{suffix}",
                )
            if not db_json_files:
                return _task_result(
                    False,
                    _time.perf_counter() - start,
                    f"--step md 依赖当前数据库 {database_name} 的 JSON 产物，"
                    f"但未找到有效输入: {json_dir}；请先执行 --step json",
                )
            click.echo(
                f"✅ 检测到当前数据库 {database_name} 的 "
                f"{len(db_json_files)} 个 JSON 文件，继续执行..."
            )

        # ddl/json 注释模式预检（与独立 CLI 原行为一致，非法配置前置失败）
        if step in ("ddl", "json"):
            from metaweave.core.metadata.generation_config import (
                MetadataGenerationConfig,
            )

            MetadataGenerationConfig.from_config(config)

        _verify_err = verify_clean_target(request, "metadata", config)
        if _verify_err:
            return _task_result(False, _time.perf_counter() - start, _verify_err)
        if request.clean:
            _clean_step_dir(step, config)

        schema_list = (
            list(request.schemas_override) if request.schemas_override else None
        )
        if schema_list:
            click.echo(f"🎯 指定 Schema: {schema_list}")
        table_list = list(request.tables_override) if request.tables_override else None
        if table_list:
            click.echo(f"🎯 指定表: {table_list}")
        generator = MetadataGenerator(request.config_path, config=config)
        click.echo(f"⚙️  并发数: {max_workers}")
        click.echo(f"🧱 执行步骤: {step}")
        click.echo("")
        click.echo("🚀 开始生成元数据...")
        result = generator.generate(
            schemas=schema_list,
            tables=table_list,
            max_workers=max_workers,
            step=step,
        )

        click.echo("")
        click.echo("=" * 60)
        click.echo("📊 生成结果统计")
        click.echo("=" * 60)
        click.echo(f"✅ 成功处理: {result.processed_tables} 个对象")
        click.echo(f"  - Table: {result.processed_object_counts.get('table', 0)} 个")
        click.echo(f"  - View: {result.processed_object_counts.get('view', 0)} 个")
        click.echo(
            "  - Materialized View: "
            f"{result.processed_object_counts.get('materialized_view', 0)} 个"
        )
        if result.failed_tables > 0:
            click.echo(f"❌ 处理失败: {result.failed_tables} 个对象", err=True)
        click.echo(f"💬 生成注释: {result.generated_comments} 个")
        if step == "ddl":
            click.echo(f"🤖 LLM 请求: {result.llm_request_count} 次")
            click.echo(
                f"   - 对象注释: 成功 {result.llm_object_comment_success_count}，"
                f"失败 {result.llm_object_comment_failure_count}"
            )
            click.echo(
                f"   - 字段注释: 成功 {result.llm_column_comment_success_count}，"
                f"失败 {result.llm_column_comment_failure_count}"
            )
            click.echo(
                f"🔐 物理主键约束: {result.physical_primary_key_constraints_found} 个"
            )
            click.echo(
                f"🔗 物理外键约束: {result.physical_foreign_key_constraints_found} 个"
            )
            click.echo(f"🔒 唯一约束: {result.unique_constraints_found} 个")
            click.echo(f"📇 索引总数: {result.indexes_found} 个")
            click.echo(f"  - 普通索引: {result.regular_indexes_found} 个")
            click.echo(f"  - 唯一索引: {result.unique_indexes_found} 个")
            click.echo("🔑 逻辑主键识别: 未执行")
        elif step == "json":
            click.echo(f"🔑 识别逻辑主键: {result.logical_keys_found} 个")
            click.echo(f"🤖 LLM 请求: {result.llm_request_count} 次")
            click.echo(
                f"   - 对象增强: 成功 {result.llm_success_count}，失败 {result.llm_failure_count}"
            )
            click.echo(
                f"   - 注释任务: 成功 {result.llm_comment_success_count}，"
                f"失败 {result.llm_comment_failure_count}"
            )
            click.echo(
                f"     - 对象注释项: 成功 {result.llm_object_comment_success_count}，"
                f"失败 {result.llm_object_comment_failure_count}"
            )
            click.echo(
                f"     - 字段注释项: 成功 {result.llm_column_comment_success_count}，"
                f"失败 {result.llm_column_comment_failure_count}"
            )
            click.echo(
                f"   - 分类任务: 成功 {result.llm_classification_success_count}，"
                f"失败 {result.llm_classification_failure_count}"
            )
            click.echo("📂 表分类统计（table_category）")
            click.echo(f"   - fact: {result.table_category_counts.get('fact', 0)}")
            click.echo(f"   - dim: {result.table_category_counts.get('dim', 0)}")
            click.echo(f"   - bridge: {result.table_category_counts.get('bridge', 0)}")
            click.echo(
                f"   - unknown: {result.table_category_counts.get('unknown', 0)}"
            )
        click.echo(f"📁 输出文件: {len(result.output_files)} 个")

        if result.errors:
            click.echo("\n⚠️  错误列表:")
            for error in result.errors[:5]:
                click.echo(f"  - {error}", err=True)
            if len(result.errors) > 5:
                click.echo(f"  ... 还有 {len(result.errors) - 5} 个错误", err=True)

        click.echo("=" * 60)
        if not result.success:
            click.echo("⚠️  元数据生成完成，但存在错误", err=True)
            return _task_result(
                False, _time.perf_counter() - start, "元数据生成完成，但存在错误"
            )
        click.echo("✨ 元数据生成完成！")
        return _task_result(True, _time.perf_counter() - start, "元数据生成完成")
    except Exception as e:  # noqa: BLE001
        return _task_result(False, _time.perf_counter() - start, str(e))
    finally:
        # 任务结束恢复步骤上下文（doc 10.1 §4.4）
        reset_current_step(_step_token)
