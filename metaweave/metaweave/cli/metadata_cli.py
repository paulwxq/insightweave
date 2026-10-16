"""元数据生成 CLI 命令"""

import click
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple
import yaml

from metaweave.utils.file_utils import (
    ensure_path_in_project_root,
    resolve_domains_yaml_path,
    resolve_project_path,
)
from metaweave.utils.logger import set_current_step

logger = logging.getLogger("metaweave.cli")


# 各激活模式下有效的作用域参数白名单（值为 click 参数名；--config 与模式开关本身全局有效）
# 模式：generate-domains / generate-dim-config / generate-sql-rag / step（--step 单步流程）
_MODE_SCOPED_PARAMS: Dict[str, set] = {
    "generate-domains": {
        "domains_config", "description", "clean",
        "md_context_dir", "md_context_mode", "md_limit",
    },
    "generate-dim-config": {"dim_config_path", "clean"},
    "generate-sql-rag": {"domains_config", "md_context_dir", "clean"},
    "step": {
        "schemas", "tables", "max_workers", "clean",
        "domain", "cross_domain", "domains_config",
    },
}

_PARAM_DISPLAY: Dict[str, str] = {
    "schemas": "--schemas/-s",
    "tables": "--tables/-t",
    "max_workers": "--max-workers/-w",
    "clean": "--clean",
    "domain": "--domain",
    "cross_domain": "--cross-domain",
    "domains_config": "--domains-config",
    "description": "--description",
    "md_context_dir": "--md-context-dir",
    "md_context_mode": "--md-context-mode",
    "md_limit": "--md-limit",
    "dim_config_path": "--dim-config",
}


def _warn_out_of_scope_params(active_mode: str) -> None:
    """对当前模式下不生效的显式 CLI 参数逐个告警（仅提示，不拦截执行）。

    只检查命令行显式传入的参数（click ParameterSource 为 COMMANDLINE），
    默认值不参与判定，避免把未传的参数误报为"无效"。
    """
    ctx = click.get_current_context(silent=True)
    if ctx is None or not hasattr(ctx, "get_parameter_source"):
        return
    valid = _MODE_SCOPED_PARAMS.get(active_mode, set())
    for param_name in sorted(set(_PARAM_DISPLAY) - valid):
        try:
            source = ctx.get_parameter_source(param_name)
        except Exception:
            continue
        source_name = getattr(source, "name", str(source)).lower()
        if "commandline" not in source_name:
            continue
        msg = (
            f"⚠️  参数 {_PARAM_DISPLAY[param_name]} 在当前模式（{active_mode}）下不生效，已忽略"
        )
        click.echo(msg, err=True)
        logger.warning(msg)



def _run_generate_sql_rag(
    config_path: Path,
    domains_config: Optional[str],
    md_context_dir_override: "Optional[Path]",
    clean: bool,
    main_config: Optional[dict] = None,
) -> "Optional[str]":
    """--generate-sql-rag 模式：生成 Question-SQL 样例 + 可选 SQL EXPLAIN 校验。

    Returns:
        None 表示成功；字符串为失败原因（doc 26 §6.2：共享执行层不抛
        click.Abort，失败路径以结构化原因返回，供 pipeline 汇总与 CLI 报错）。
    

    由原 `sql-rag generate / validate / run-all` 三命令合并而来：
    - 生成段 == 原 generate（LLM 预检、domains 路径解析、--clean 语义原样继承）；
    - 校验段 == run-all 阶段 2（复用生成段 llm_service 与 gen_result.output_file），
      由 sql_rag.validation.enabled（默认 true）控制，行为参数全部来自配置；
    - 质量阻断对齐 pipeline 步骤 9：enable_sql_repair=false 且存在无效 SQL 时
      非 0 退出（SQLExampleLoader 不校验有效性，避免无效 SQL 污染向量库）。
    """
    set_current_step("sql-rag-generate")

    # doc 26 §6.2：main_config 快照注入优先；缺失时由共享快照函数读盘一次
    # （含 LLM 模块路径预检与废弃节点校验），同一任务不二次读盘
    if main_config is None:
        from metaweave.core.metadata.execution import load_main_config_snapshot

        main_config = load_main_config_snapshot(config_path)

    sql_rag_cfg = main_config.get("sql_rag", {}) or {}
    generation_config = sql_rag_cfg.get("generation", {}) or {}
    validation_config = sql_rag_cfg.get("validation", {}) or {}

    # db_domains.yaml 有效路径：CLI 显式传入 > output.domains_directory（默认 output/domains）
    domains_path = resolve_domains_yaml_path(main_config, domains_config)
    if not domains_path.exists():
        click.echo(
            f"❌ 未找到业务域配置 {domains_path}，"
            f"请先执行 metaweave metadata --generate-domains",
            err=True,
        )
        return f"未找到业务域配置 {domains_path}，请先执行 --generate-domains"

    # md 目录：共享纯函数解析（override 仅由 CLI 包装层按"显式传参"注入；
    # pipeline/共享层始终传 None）——生成与校验共用同一结果
    from metaweave.core.metadata.execution import resolve_md_context_dir

    md_dir = resolve_md_context_dir(main_config, md_context_dir_override)
    if not md_dir.exists():
        click.echo(
            f"❌ 未找到 Markdown 表结构目录 {md_dir}，"
            f"请先执行 metaweave metadata --step md",
            err=True,
        )
        return f"未找到 Markdown 表结构目录 {md_dir}，请先执行 --step md"

    # rel 目录（校验修复上下文）：output.rel_directory（默认 output/rel）
    # doc 26 §6.2：共享 resolver
    rel_dir = resolve_project_path(
        (main_config.get("output") or {}).get("rel_directory", "output/rel")
    )

    # LLM 与生成器（延迟导入，与现有分支一致）
    from metaweave.services.llm_service import LLMService
    from metaweave.services.llm_config_resolver import resolve_module_llm_config
    from metaweave.core.sql_rag.generator import QuestionSQLGenerator

    llm_service = LLMService(resolve_module_llm_config(main_config, "sql_rag.llm"))
    generator = QuestionSQLGenerator(llm_service, generation_config)

    # --clean：清理输出目录顶层所有文件（保留子目录，与其他步骤统一语义）
    if clean:
        sql_output_dir = resolve_project_path(
            (main_config.get("sql_rag", {}) or {})
            .get("generation", {})
            .get("output_dir", "output/sql-rag")
        )
        try:
            resolved_dir = ensure_path_in_project_root(sql_output_dir)
        except ValueError as e:
            raise click.UsageError(
                f"❌ --clean 输出目录不在项目根目录内: {e}"
            )
        resolved_dir.mkdir(parents=True, exist_ok=True)
        removed = 0
        for child in resolved_dir.iterdir():
            if child.is_file():
                child.unlink()
                removed += 1
        click.echo(f"🧹 已清理 {resolved_dir} 下 {removed} 个文件（保留子目录）")

    # === 生成段 ===
    click.echo("开始生成 Question-SQL...")
    gen_result = generator.generate(
        domains_config_path=str(domains_path),
        md_dir=str(md_dir),
        rel_dir=str(rel_dir),
    )
    if not gen_result.success:
        click.echo("❌ 生成失败，请查看日志", err=True)
        return "Question-SQL 生成失败，请查看日志"
    click.echo(f"生成完成: 共 {gen_result.total_generated} 条 Question-SQL")
    click.echo(f"输出文件: {gen_result.output_file}")
    for domain_name, count in gen_result.domain_stats.items():
        click.echo(f"  {domain_name}: {count} 条")

    # === 校验段（受 sql_rag.validation.enabled 控制，默认开启）===
    if not validation_config.get("enabled", True):
        click.echo("ℹ️  已按配置跳过 SQL 校验（sql_rag.validation.enabled=false）")
        return

    set_current_step("sql-rag-validate")
    from metaweave.core.metadata.connector import DatabaseConnector
    from metaweave.core.sql_rag.validator import SQLValidator

    enable_repair = validation_config.get("enable_sql_repair", False)  # 仅从配置读
    connector = DatabaseConnector(main_config.get("database", {}) or {})
    try:
        validator = SQLValidator(
            connector=connector,
            config=validation_config,
            llm_service=llm_service if enable_repair else None,
            md_dir=str(md_dir),
            rel_dir=str(rel_dir),
        )
        click.echo("开始校验 SQL...")
        stats = validator.validate_file(
            input_file=gen_result.output_file,
            enable_repair=enable_repair,
        )

        # 统计输出（沿用原 validate/run-all 格式）
        click.echo("校验完成:")
        click.echo(f"  总数: {stats['total']}")
        click.echo(f"  原始有效: {stats['valid']}, 无效: {stats['invalid']}")
        click.echo(f"  原始有效率: {stats['success_rate']:.1f}%")
        rs = stats["repair_stats"]
        final_valid = stats["valid"]
        if rs["attempted"] > 0:
            click.echo(
                f"  修复尝试: {rs['attempted']}, 成功: {rs['successful']}, 失败: {rs['failed']}"
            )
            final_valid = stats["valid"] + rs["successful"]
            final_rate = final_valid / stats["total"] * 100 if stats["total"] else 0
            click.echo(f"  修复后有效: {final_valid}, 有效率: {final_rate:.1f}%")
        ras = stats["repair_apply_stats"]
        if ras["modified"] > 0 or ras["deleted"] > 0:
            click.echo(f"  修复回写: 替换 {ras['modified']} 条, 删除 {ras['deleted']} 条")
        click.echo(f"  耗时: {stats['total_time']:.2f}s")

        # 质量阻断（对齐 pipeline 步骤 9）
        final_invalid = stats["total"] - final_valid
        if final_invalid > 0:
            if enable_repair:
                # 修复失败的条目已被删除出文件，落盘内容全部有效，仅告警
                logger.warning(
                    "SQL 校验完成（已启用修复）：原始 %d 条，有效 %d 条，"
                    "修复失败 %d 条（已从文件删除）",
                    stats["total"], final_valid, final_invalid,
                )
            else:
                # SQLExampleLoader 不校验有效性，无效 SQL 会原样装载进向量库，必须阻断
                click.echo(
                    f"❌ 校验完成但存在 {final_invalid} 条无效 SQL（未启用修复），"
                    f"为避免污染向量库已终止。请启用 metadata_config.yaml -> "
                    f"sql_rag.validation.enable_sql_repair 或修正后重新执行",
                    err=True,
                )
                return (
                    f"SQL 校验发现 {final_invalid} 条无效 SQL 且未启用修复，"
                    f"为避免污染向量库已终止"
                )
    finally:
        connector.close()


@click.command(name="metadata")
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    default="configs/metadata_config.yaml",
    show_default=True,
    help="配置文件路径"
)
@click.option(
    "--schemas",
    "-s",
    type=str,
    help="要处理的 schema 列表（逗号分隔）"
)
@click.option(
    "--tables",
    "-t",
    type=str,
    help="要处理的数据库对象名称列表（逗号分隔；兼容参数名仍为 tables）"
)
@click.option(
    "--max-workers",
    "-w",
    type=click.IntRange(min=1),
    default=4,
    help="最大并发数（默认: 4）"
)
@click.option(
    "--clean",
    is_flag=True,
    default=False,
    help="在写入前清理输出：删除目标目录顶层所有文件（不限扩展名、不递归、保留子目录）；"
    "适用于全部 --step 步骤与 --generate-domains / --generate-dim-config / --generate-sql-rag"
)
@click.option(
    "--step",
    type=click.Choice(["db", "ddl", "json", "cql", "md", "rel"], case_sensitive=False),
    multiple=True,
    help="执行步骤（仅允许传入一次）：db/ddl/json/cql/md/rel"
    "（rel_llm 已并入 rel，由 relationships.llm_candidates.enabled 配置开关控制，见 doc 15）"
    "（md 依赖 JSON 产物，执行顺序 ddl → json → md，见 doc 17）"
)
@click.option(
    "--source-id",
    multiple=True,
    help="仅 --step db 必填、--step ddl 可选（受管模式）：平台库中已登记的数据源 UUID（仅允许传入一次）",
)
@click.option(
    "--job-id",
    multiple=True,
    help="仅 --step db / 受管 --step ddl 可选：复用已登记的工作区 UUID；省略则新建工作区（仅允许传入一次）",
)
@click.option(
    "--domain",
    type=str,
    default=None,
    flag_value="all",
    help=(
        "控制 domain 表对分组。"
        "'all' 使用所有 domain；'A,B' 指定 domain。"
        "不传时从 yaml relationships.domain 读取；yaml 也未配置则全表两两组合"
    ),
)
@click.option(
    "--domains-config",
    type=click.Path(exists=False),
    default=None,
    help="业务主题配置文件路径（默认 output/domains/db_domains.yaml，由 output.domains_directory 决定）"
)
@click.option(
    "--generate-domains",
    is_flag=True,
    default=False,
    help="基于 md 摘要（和可选的 --description）自动生成 domains 配置并写入 YAML"
)
@click.option(
    "--generate-dim-config",
    is_flag=True,
    default=False,
    help="扫描 json 目录生成 dim_tables.yaml 维度表配置（识别 table_category='dim'，embedding_col 需人工填写）"
)
@click.option(
    "--dim-config",
    "dim_config_path",
    type=click.Path(exists=False),
    default=None,
    help="dim_tables.yaml 路径（默认 output/dim_config/dim_tables.yaml，由 loaders.dim_loader.config_file 决定）"
)
@click.option(
    "--generate-sql-rag",
    is_flag=True,
    default=False,
    help="基于 md 文档与业务域配置生成 Question-SQL 样例；"
    "生成后按 sql_rag.validation.enabled（默认 true）执行 SQL EXPLAIN 校验与可选 LLM 修复"
)
@click.option(
    "--description",
    type=str,
    default=None,
    help="可选：生成 domains 时提供数据库补充说明（用于 LLM 提示词，不直接写入 YAML）"
)
@click.option(
    "--cross-domain/--no-cross-domain",
    default=None,
    help="是否包含跨域关系。不传时从 yaml relationships.cross_domain 读取；yaml 也未配置则不跨域",
)
@click.option(
    "--md-context-dir",
    type=click.Path(exists=False),
    default="output/md",
    help="md 摘要目录（默认：output/md）"
)
@click.option(
    "--md-context-mode",
    type=click.Choice(["name", "name_comment", "full"], case_sensitive=False),
    default="name_comment",
    show_default=True,
    help="md 摘要模式：name 仅表名；name_comment 表名+首行；full 全文"
)
@click.option(
    "--md-limit",
    type=int,
    default=None,
    help="md 文件数量上限（默认：配置文件 domain_generation.md_context_limit，否则 100）"
)
def metadata_command(
    config: str,
    schemas: str,
    tables: str,
    max_workers: int,
    clean: bool,
    step: Tuple[str, ...],
    source_id: Tuple[str, ...],
    job_id: Tuple[str, ...],
    domain: Optional[str],
    domains_config: str,
    generate_domains: bool,
    generate_dim_config: bool,
    dim_config_path: Optional[str],
    generate_sql_rag: bool,
    description: Optional[str],
    cross_domain: bool,
    md_context_dir: str,
    md_context_mode: str,
    md_limit: int,
):
    """生成数据库元数据

    从 PostgreSQL 数据库中提取元数据、生成注释、识别逻辑主键，
    并输出为 DDL、Markdown、JSON 格式。

    md 步骤只读 output/json/*.json（doc 17），执行顺序 ddl → json → md。
    注意：旧 JSON（ddl 阶段 LLM 补齐过注释）的 comment_source 不构成
    "原始注释"口径，需重跑 ddl+json 后才有治理含义。
    
    示例:
    
        metaweave metadata --config configs/metadata_config.yaml
        
        metaweave metadata -c config.yaml --schemas public,myschema
        
        metaweave metadata -c config.yaml --tables users,orders --max-workers 8
    """
    # 重复传入防护 + 归一化（doc 27 §3.1）：multiple 元组 → 单值；
    # 必须置于命令体首段——早于裸调用检查、set_current_step 与一切
    # 把 step 当字符串使用的分支（防 tuple 泄漏）
    if len(step) > 1:
        raise click.UsageError(
            f"--step 仅允许传入一次，检测到 {len(step)} 次: {', '.join(step)}"
        )
    step = step[0] if step else None
    if len(source_id) > 1:
        raise click.UsageError("--source-id 仅允许传入一次")
    if step not in ("db", "ddl") and source_id:
        raise click.UsageError("--source-id 仅适用于 --step db 或受管 --step ddl")
    if len(job_id) > 1:
        raise click.UsageError("--job-id 仅允许传入一次")
    if step not in ("db", "ddl") and job_id:
        raise click.UsageError("--job-id 仅适用于 --step db 或受管 --step ddl")
    if step == "db":
        if not source_id:
            raise click.UsageError("--step db 必须指定 --source-id")
        if generate_domains or generate_dim_config or generate_sql_rag:
            raise click.UsageError("--step db 不能与 --generate-* 模式同时使用")
        from metaweave.utils.source_id import normalize_source_id

        try:
            source_uuid = normalize_source_id(source_id[0])
        except ValueError as exc:
            raise click.UsageError(str(exc)) from None
        job_uuid_arg = None
        if job_id:
            try:
                job_uuid_arg = normalize_source_id(job_id[0])
            except ValueError:
                raise click.UsageError("--job-id 不是合法 UUID") from None
        ctx = click.get_current_context()
        excluded = {
            "schemas", "tables", "max_workers", "domain", "cross_domain",
            "domains_config", "dim_config_path", "description",
            "md_context_dir", "md_context_mode", "md_limit",
        }
        for name in excluded:
            origin = ctx.get_parameter_source(name)
            if origin is not None and origin.name == "COMMANDLINE":
                raise click.UsageError(f"--step db 不接受 {_PARAM_DISPLAY[name]}")
        try:
            from backend.data_sources.db_step_service import (
                DbStepConfigError,
                run_db_step,
            )
        except ModuleNotFoundError as exc:
            if exc.name in {"sqlalchemy", "cryptography"}:
                raise click.UsageError(
                    "--step db 需要平台依赖，请执行 uv sync --extra platform"
                ) from None
            raise
        config_path = resolve_project_path(config)
        try:
            result = run_db_step(
                config_path, source_uuid, clean, job_id=job_uuid_arg
            )
        except DbStepConfigError as exc:
            raise click.UsageError(str(exc)) from None
        result_job = result.get("job_id")
        job_suffix = f"（job_id: {result_job}）" if result_job else ""
        if not result["success"]:
            click.echo(f"❌ 错误: {result['message']}{job_suffix}", err=True)
            raise click.Abort()
        click.echo(f"✅ {result['message']}{job_suffix}")
        return

    if step == "ddl":
        # 受管 DDL（doc 16 §4）：带 --source-id 进入受管模式，产物落在
        # output/<source_id>/<job_id>/ddl/；不带则保持未受管行为不变。
        if job_id and not source_id:
            raise click.UsageError("--job-id 仅在受管模式（--source-id）下可用")
        if source_id:
            if generate_domains or generate_dim_config or generate_sql_rag:
                raise click.UsageError("--step ddl 不能与 --generate-* 模式同时使用")
            from metaweave.utils.source_id import normalize_source_id

            try:
                source_uuid = normalize_source_id(source_id[0])
            except ValueError as exc:
                raise click.UsageError(str(exc)) from None
            job_uuid_arg = None
            if job_id:
                try:
                    job_uuid_arg = normalize_source_id(job_id[0])
                except ValueError:
                    raise click.UsageError("--job-id 不是合法 UUID") from None
            ctx = click.get_current_context()
            # 受管组合硬校验（doc 16 §4）：--schemas/--tables 缩小采集范围、
            # --clean 与受管模式每次自动清理重复；显式传入即拒绝，且必须
            # 早于创建工作区或写 job_info
            for name in ("schemas", "tables", "clean"):
                origin = ctx.get_parameter_source(name)
                if origin is not None and origin.name == "COMMANDLINE":
                    raise click.UsageError(
                        f"受管 --step ddl（--source-id）不接受 {_PARAM_DISPLAY[name]}："
                        "采集范围固定为数据源登记的全部选中 schema，"
                        "受管模式每次自动清理本次 DDL 目录"
                    )
            workers_origin = ctx.get_parameter_source("max_workers")
            workers_arg = (
                max_workers
                if workers_origin is not None
                and workers_origin.name == "COMMANDLINE"
                else None
            )
            try:
                from backend.data_sources.ddl_step_service import (
                    DbStepConfigError,
                    run_ddl_step,
                )
            except ModuleNotFoundError as exc:
                if exc.name in {"sqlalchemy", "cryptography"}:
                    raise click.UsageError(
                        "受管 --step ddl 需要平台依赖，请执行 uv sync --extra platform"
                    ) from None
                raise
            config_path = resolve_project_path(config)
            try:
                result = run_ddl_step(
                    config_path,
                    source_uuid,
                    job_id=job_uuid_arg,
                    max_workers=workers_arg,
                )
            except DbStepConfigError as exc:
                raise click.UsageError(str(exc)) from None
            result_job = result.get("job_id")
            job_suffix = f"（job_id: {result_job}）" if result_job else ""
            if not result["success"]:
                click.echo(f"❌ 错误: {result['message']}{job_suffix}", err=True)
                raise click.Abort()
            click.echo(f"✅ {result['message']}{job_suffix}")
            return

    # 无编排契约（doc 25 §2.2）：未指定 step 且未启用任何 --generate-* 独立模式 → 拒绝
    # （置于 try 之前：参数级校验按 click 标准以退出码 2 报出，不被末尾
    #   except Exception 吞成 Abort/退出码 1）
    if not step and not generate_domains and not generate_dim_config and not generate_sql_rag:
        raise click.UsageError(
            "必须指定 step 或 generate 模式：--step db|ddl|json|md|rel|cql，"
            "或 --generate-domains / --generate-dim-config / --generate-sql-rag"
        )

    try:
        set_current_step((step or "").lower())

        def _load_yaml(path: Path) -> Dict:
            if not path.exists():
                return {}
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}

        # 解析配置文件路径（doc 26 §6.2：共享 resolver）
        config_path = resolve_project_path(config)

        click.echo(f"📋 加载配置: {config_path}")

        step_lower = (step or "").lower()

        def _ensure_in_project_root(path: Path) -> Path:
            # doc 26 §6.2：共享防护 + CLI 层 UsageError 转换
            try:
                return ensure_path_in_project_root(path)
            except ValueError as e:
                raise click.UsageError(f"❌ --clean 输出目录不在项目根目录内: {e}")

        def _clean_step_output_dir(step_name: str, loaded_config: Dict) -> None:
            """清理指定步骤的输出目录。

            所有步骤（ddl/json/md/rel/cql）统一语义：删除目录顶层**所有文件**
            （不限扩展名），不递归，保留子目录（doc 25 定案 A）。

            Args:
                step_name: 步骤名称（ddl/json/md/rel/cql）
                loaded_config: 已加载的配置字典

            Raises:
                click.UsageError: 不支持的步骤或输出目录不在项目根目录内
            """
            step_name = (step_name or "").lower()
            output_config = loaded_config.get("output", {}) or {}

            # doc 26 §6.2：相对路径统一基于项目根目录（共享 resolver）
            def _resolve_dir(path_str: str) -> Path:
                return _ensure_in_project_root(resolve_project_path(path_str))

            output_dir = _resolve_dir(str(output_config.get("output_dir", "output")))

            if step_name == "ddl":
                target_dir = _resolve_dir(str(output_config.get("ddl_directory", output_dir / "ddl")))
            elif step_name == "json":
                target_dir = _resolve_dir(str(output_config.get("json_directory", output_dir / "json")))
            elif step_name == "md":
                target_dir = _resolve_dir(str(output_config.get("markdown_directory", output_dir / "md")))
            elif step_name == "rel":
                target_dir = _resolve_dir(str(output_config.get("rel_directory", output_dir / "rel")))
            elif step_name == "cql":
                target_dir = _resolve_dir(str(output_config.get("cql_directory", output_dir / "cql")))
            else:
                raise click.UsageError(f"❌ --clean 不支持的 step: {step_name}")

            target_dir.mkdir(parents=True, exist_ok=True)
            for child in target_dir.iterdir():
                if child.is_file():
                    child.unlink()
            logger.debug("🧹 已清理输出目录顶层文件: %s", target_dir)

        # 阶段 1: CLI 级互斥校验（只看 CLI 参数，不涉及 yaml）
        if generate_domains and (domain is not None or cross_domain is not None):
            raise click.UsageError("--generate-domains 与 --domain/--cross-domain 互斥，请分两步执行")
        if generate_dim_config and (domain is not None or cross_domain is not None):
            raise click.UsageError("--generate-dim-config 与 --domain/--cross-domain 互斥，请分两步执行")
        if generate_domains and generate_dim_config:
            raise click.UsageError("--generate-domains 与 --generate-dim-config 互斥，请分两步执行")
        if generate_sql_rag and (domain is not None or cross_domain is not None):
            raise click.UsageError("--generate-sql-rag 与 --domain/--cross-domain 互斥，请分两步执行")
        if generate_sql_rag and generate_domains:
            raise click.UsageError("--generate-sql-rag 与 --generate-domains 互斥，请分两步执行")
        if generate_sql_rag and generate_dim_config:
            raise click.UsageError("--generate-sql-rag 与 --generate-dim-config 互斥，请分两步执行")

        # 阶段 1.2: 参数作用域告警——当前模式下不生效的显式参数仅提示，不拦截执行
        _active_mode = (
            "generate-domains"
            if generate_domains
            else "generate-dim-config"
            if generate_dim_config
            else "generate-sql-rag"
            if generate_sql_rag
            else "step"
        )
        _warn_out_of_scope_params(_active_mode)

        # generate-domains 可独立执行（不依赖 step），并在完成后提前返回
        if generate_domains:
            from metaweave.core.metadata.execution import (
                MetadataExecutionRequest,
                execute_generate_domains,
                normalize_generate_domains_options,
            )

            # CLI 包装层：ParameterSource 判定显式覆盖（doc 26 §6.2——共享层不读 Click context）
            _ctx_gd = click.get_current_context(silent=True)

            def _explicit(param_name: str) -> bool:
                if _ctx_gd is None or not hasattr(_ctx_gd, "get_parameter_source"):
                    return False
                try:
                    _src = _ctx_gd.get_parameter_source(param_name)
                except Exception:
                    return False
                return "commandline" in getattr(_src, "name", str(_src)).lower()

            _domains_override = (
                Path(domains_config) if _explicit("domains_config") else None
            )
            _md_dir_override = (
                Path(md_context_dir) if _explicit("md_context_dir") else None
            )

            # 参数校验先于任何副作用（doc 26 §3.1：显式非法值 exit 2）
            try:
                _norm = normalize_generate_domains_options(
                    description,
                    md_context_mode.lower() if md_context_mode else None,
                    md_limit,
                )
            except ValueError as e:
                raise click.UsageError(f"❌ --generate-domains 参数非法: {e}")

            _gd_result = execute_generate_domains(
                MetadataExecutionRequest(
                    config_path=config_path,
                    operation="generate-domains",
                    clean=clean,
                    domains_config_override=_domains_override,
                    md_context_dir_override=_md_dir_override,
                    description=_norm.description,
                    md_context_mode=_norm.md_context_mode,
                    md_limit=_norm.md_limit,
                )
            )
            if not _gd_result["success"]:
                click.echo(f"❌ 错误: {_gd_result['message']}", err=True)
                raise click.Abort()
            return

        # generate-dim-config 可独立执行（不依赖 step），并在完成后提前返回
        if generate_dim_config:
            from metaweave.core.metadata.execution import (
                MetadataExecutionRequest,
                execute_generate_dim_config,
            )

            _ctx_dc = click.get_current_context(silent=True)

            def _explicit_dc(param_name: str) -> bool:
                if _ctx_dc is None or not hasattr(_ctx_dc, "get_parameter_source"):
                    return False
                try:
                    _src = _ctx_dc.get_parameter_source(param_name)
                except Exception:
                    return False
                return "commandline" in getattr(_src, "name", str(_src)).lower()

            _dim_override = (
                Path(dim_config_path) if _explicit_dc("dim_config_path") else None
            )

            _dc_result = execute_generate_dim_config(
                MetadataExecutionRequest(
                    config_path=config_path,
                    operation="generate-dim-config",
                    clean=clean,
                    dim_config_path_override=_dim_override,
                )
            )
            if not _dc_result["success"]:
                click.echo(f"❌ 错误: {_dc_result['message']}", err=True)
                raise click.Abort()
            return

        # generate-sql-rag 可独立执行（不依赖 step），并在完成后提前返回；
        # 经共享执行入口（doc 26 §6.2：CLI 只做参数级操作并构造请求）
        if generate_sql_rag:
            from metaweave.core.metadata.execution import (
                MetadataExecutionRequest,
                execute_generate_sql_rag,
            )

            _ctx_sr = click.get_current_context(silent=True)

            def _explicit_sr(param_name: str) -> bool:
                if _ctx_sr is None or not hasattr(_ctx_sr, "get_parameter_source"):
                    return False
                try:
                    _src = _ctx_sr.get_parameter_source(param_name)
                except Exception:
                    return False
                return "commandline" in getattr(_src, "name", str(_src)).lower()

            _sr_result = execute_generate_sql_rag(
                MetadataExecutionRequest(
                    config_path=config_path,
                    operation="generate-sql-rag",
                    clean=clean,
                    domains_config_override=(
                        Path(domains_config) if _explicit_sr("domains_config") else None
                    ),
                    md_context_dir_override=(
                        Path(md_context_dir) if _explicit_sr("md_context_dir") else None
                    ),
                )
            )
            if not _sr_result["success"]:
                click.echo(f"❌ 错误: {_sr_result['message']}", err=True)
                raise click.Abort()
            return

        # Step 4: CQL 生成
        if step == "cql":
            from metaweave.core.metadata.execution import (
                MetadataExecutionRequest,
                execute_cql,
            )

            _ctx_cql = click.get_current_context(silent=True)

            def _explicit_cql(param_name: str) -> bool:
                if _ctx_cql is None or not hasattr(_ctx_cql, "get_parameter_source"):
                    return False
                try:
                    _src = _ctx_cql.get_parameter_source(param_name)
                except Exception:
                    return False
                return "commandline" in getattr(_src, "name", str(_src)).lower()

            _cql_result = execute_cql(
                MetadataExecutionRequest(
                    config_path=config_path,
                    operation="cql",
                    clean=clean,
                    domains_config_override=(
                        Path(domains_config) if _explicit_cql("domains_config") else None
                    ),
                )
            )
            if not _cql_result["success"]:
                click.echo(f"❌ 错误: {_cql_result['message']}", err=True)
                raise click.Abort()
            return

        # Step 3: 关系发现
        if step == "rel":
            from metaweave.core.metadata.execution import (
                MetadataExecutionRequest,
                execute_rel,
            )

            _ctx_rel = click.get_current_context(silent=True)

            def _explicit_rel(param_name: str) -> bool:
                if _ctx_rel is None or not hasattr(_ctx_rel, "get_parameter_source"):
                    return False
                try:
                    _src = _ctx_rel.get_parameter_source(param_name)
                except Exception:
                    return False
                return "commandline" in getattr(_src, "name", str(_src)).lower()

            _rel_result = execute_rel(
                MetadataExecutionRequest(
                    config_path=config_path,
                    operation="rel",
                    clean=clean,
                    domains_config_override=(
                        Path(domains_config) if _explicit_rel("domains_config") else None
                    ),
                    domain_override=domain if _explicit_rel("domain") else None,
                    cross_domain_override=(
                        bool(cross_domain) if _explicit_rel("cross_domain") else None
                    ),
                )
            )
            if not _rel_result["success"]:
                click.echo(f"❌ 错误: {_rel_result['message']}", err=True)
                raise click.Abort()
            return

        # ddl / json / md：共享执行入口（doc 26 §6.2）
        from metaweave.core.metadata.execution import (
            MetadataExecutionRequest,
            execute_ddl_json_md,
        )

        _ctx_djm = click.get_current_context(silent=True)

        def _explicit_djm(param_name: str) -> bool:
            if _ctx_djm is None or not hasattr(_ctx_djm, "get_parameter_source"):
                return False
            try:
                _src = _ctx_djm.get_parameter_source(param_name)
            except Exception:
                return False
            return "commandline" in getattr(_src, "name", str(_src)).lower()

        _schemas_ov = (
            tuple(x.strip() for x in schemas.split(",") if x.strip())
            if _explicit_djm("schemas")
            else None
        )
        _tables_ov = (
            tuple(x.strip() for x in tables.split(",") if x.strip())
            if _explicit_djm("tables")
            else None
        )
        _workers_ov = max_workers if _explicit_djm("max_workers") else None

        _djm_result = execute_ddl_json_md(
            MetadataExecutionRequest(
                config_path=config_path,
                operation=step_lower,
                clean=clean,
                schemas_override=_schemas_ov,
                tables_override=_tables_ov,
                max_workers_override=_workers_ov,
            )
        )
        if not _djm_result["success"]:
            click.echo(f"❌ 错误: {_djm_result['message']}", err=True)
            raise click.Abort()

    except click.UsageError:
        # 参数级 UsageError 穿透（exit 2，doc 26 §3.1：非法显式参数在任何副作用前拒绝）
        raise

    except click.Abort:
        # 各分支的失败出口已打印业务消息，这里再 raise 一次 Abort 只为终止命令。
        # Abort 是 RuntimeError 子类，若不先放行会被下面的 except Exception 接住，
        # 以空消息多打一条日志与错误（str(Abort()) 为空串）；穿透后由 click 顶层
        # 打印 "Aborted!" 并以退出码 1 结束。
        raise

    except Exception as e:
        logger.error(f"元数据生成失败: {e}")
        click.echo(f"❌ 错误: {e}", err=True)
        raise click.Abort()
