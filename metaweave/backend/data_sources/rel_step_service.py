"""受管 Rel 步骤 worker（doc 23.1 §5、6.2 §7.1）。

上游就绪判定读取 `json/`、`domain/` 顶层唯一成功采集 marker（doc 6
§4.1）；重跑前对 rel 目录执行统一整目录备份（doc 6 §6，取代仅保留最近
一份的旧备份）；构造先于备份（R6：构造期失败旧产物保留）。
"""

import json
import logging
import time
from pathlib import Path

from backend.data_sources.ddl_step_service import build_managed_ddl_config
from backend.data_sources.domain_step_service import verify_domain_artifact
from backend.data_sources.json_step_service import (
    JSON_MANIFEST_NAME,
    ManagedJsonInputError,
    read_managed_json_input,
)
from backend.data_sources.md_step_service import check_objects_in_scope
from backend.data_sources.step_context import (
    DbStepConfigError,
    StepContext,
    StepOutcome,
)
from backend.workspace_files.backups import backup_directory_before_update
from backend.workspace_files.markers import read_collection_marker
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_json_output_dir,
    resolve_rel_output_dir,
)
from metaweave.core.domains import DomainResolver
from metaweave.core.metadata.execution import resolve_domain_params
from metaweave.core.relationships.name_similarity import (
    InvalidNameSimilarityConfig,
    NameSimilarityService,
)
from metaweave.core.relationships.pipeline import RelationshipDiscoveryPipeline
from metaweave.services.llm_config_resolver import resolve_module_llm_config
from metaweave.services.llm_service import LLMService

logger = logging.getLogger("backend.data_sources")

REL_STEP_NAME = "rel"
JSON_STEP_FOR_REL = "json"
DOMAIN_STEP_FOR_REL = "domain"
DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
REL_NOT_READY_TEMPLATE = "工作区的 {step} 产物未就绪；请先执行 {step} 采集"
JSON_NOT_READY = REL_NOT_READY_TEMPLATE.format(step="JSON")
DOMAIN_NOT_READY = REL_NOT_READY_TEMPLATE.format(step="Domain")

# 静态解析函数在 import 期取引用：测试对服务命名空间的
# RelationshipDiscoveryPipeline 打替身时，预检仍用真实解析（doc 23.1 §5.2 b）
_PARSE_LLM_TOP_K = RelationshipDiscoveryPipeline._parse_llm_top_k


class RelPreparationError(RuntimeError):
    """rel 准备阶段故障（本地文件/配置段与 LLM 预检的非受控异常）；503 载体。"""

    def __init__(self, message: str):
        super().__init__(message)


def _precheck_pipeline_config(snapshot: dict) -> None:
    """清理前覆盖管线构造期解析（doc 23.1 §5.2 第 6 步，无副作用）。"""

    def _require_object(container, key, path, *, null_as_empty):
        if key not in container:
            return {}
        value = container[key]
        if value is None:
            if null_as_empty:
                return {}
            raise ValueError(f"{path} 必须是对象（null 不豁免）")
        if not isinstance(value, dict):
            raise ValueError(f"{path} 必须是对象")
        return value

    rel = _require_object(
        snapshot, "relationships", "relationships", null_as_empty=True
    )
    _require_object(snapshot, "sampling", "sampling", null_as_empty=True)
    _require_object(
        rel, "llm_candidates", "relationships.llm_candidates", null_as_empty=True
    )
    _require_object(
        rel,
        "candidate_matching",
        "relationships.candidate_matching",
        null_as_empty=True,
    )
    _require_object(rel, "scoring", "relationships.scoring", null_as_empty=True)
    _require_object(snapshot, "decision", "decision", null_as_empty=False)
    _require_object(rel, "weights", "relationships.weights", null_as_empty=False)
    if "weights" not in rel:
        _require_object(snapshot, "weights", "weights", null_as_empty=False)
    llm_candidates_cfg = rel.get("llm_candidates") or {}
    _PARSE_LLM_TOP_K(llm_candidates_cfg.get("top_k", 50))
    try:
        NameSimilarityService(
            rel.get("name_similarity", {}), snapshot.get("embedding") or {}
        )
    except InvalidNameSimilarityConfig as exc:
        raise ValueError(str(exc)) from None
    except Exception:  # noqa: BLE001 —— 降级语义（doc 15 §5）
        pass
    if (llm_candidates_cfg.get("enabled", True)) and rel.get("llm"):
        llm_node = _require_object(rel, "llm", "relationships.llm", null_as_empty=True)
        langchain = _require_object(
            llm_node,
            "langchain_config",
            "relationships.llm.langchain_config",
            null_as_empty=True,
        )
        if langchain:
            raw = langchain.get("batch_size", 50)
            try:
                max(1, int(raw or 50))
            except (ValueError, TypeError):
                raise ValueError(
                    "relationships.llm.langchain_config.batch_size "
                    f"必须可解析为整数（检测到: {raw!r}）"
                ) from None


def verify_rel_collection(result, rel_dir: Path, database_name: str):
    """核对受管 Rel 成功条件（doc 23.1 §5.3）：json/md 两份普通文件、
    目录顶层实际文件与声明一致、json 可解析且库名/relationships/statistics 合法。

    `.backup/` 子目录与点号文件不计入顶层文件比对（doc 6 §6 备份规则）。
    """
    if not getattr(result, "success", False):
        return False, "Rel 关系发现未全部成功"
    outputs = [Path(path) for path in (result.output_files or [])]
    if len(outputs) != 2 or sorted(path.suffix for path in outputs) != [".json", ".md"]:
        return False, "Rel 输出文件应为 json 与 md 各一份"
    declared = set()
    for path in outputs:
        if path.is_symlink() or not path.is_file():
            return False, "存在声称成功但未实际落盘的输出文件"
        if path.resolve().parent != rel_dir:
            return False, "存在不在本次受管 Rel 目录内的输出文件"
        declared.add(path.resolve())
    actual = set()
    for path in rel_dir.iterdir():
        if path.name.startswith("."):
            # marker、.backup/ 与临时文件不属于产物比对范围（doc 6.1 §11.4）
            continue
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            return False, f"受管 Rel 目录存在非常规文件: {path.name}"
        actual.add(path.resolve())
    if actual != declared:
        return False, "受管 Rel 目录的实际输出文件与声明清单不一致"
    json_path = next(path for path in outputs if path.suffix == ".json")
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "Rel 产物 JSON 无法解析"
    if not isinstance(data, dict) or data.get("database") != database_name:
        return False, "Rel 产物库名与登记源不一致"
    if not isinstance(data.get("relationships"), list):
        return False, "Rel 产物 relationships 不是列表"
    if not isinstance(data.get("statistics"), dict):
        return False, "Rel 产物 statistics 不是对象"
    return True, ""


def _require_success_marker(directory: Path, step_label: str) -> str:
    marker, error = read_collection_marker(directory)
    if marker is None:
        raise DbStepConfigError(error or REL_NOT_READY_TEMPLATE.format(step=step_label))
    if marker.status != "success":
        raise DbStepConfigError(REL_NOT_READY_TEMPLATE.format(step=step_label))
    return marker.run_id


def run_rel_step(ctx: StepContext) -> StepOutcome:
    """执行受管 Rel 步骤：上游 marker 前置 → 构造 → 备份 → 采样生成 → 核验。"""
    started = time.perf_counter()
    stage = "json 产物校验"
    try:
        json_dir = resolve_json_output_dir(ctx.source_id, ctx.workspace_id)
        json_run_id = _require_success_marker(json_dir, "JSON")
        try:
            objects = read_managed_json_input(
                ctx.source_id, ctx.workspace_id, ctx.source.db_name
            )
        except ManagedJsonInputError as exc:
            raise DbStepConfigError(f"工作区的 JSON 产物未就绪：{exc}") from None
        stage = "domain 产物校验"
        domain_yaml_path = (
            resolve_domain_output_dir(ctx.source_id, ctx.workspace_id)
            / DOMAIN_ARTIFACT_NAME
        )
        domain_run_id = _require_success_marker(domain_yaml_path.parent, "Domain")
        artifact_ok, artifact_reason = verify_domain_artifact(domain_yaml_path)
        if not artifact_ok:
            raise DbStepConfigError(f"{artifact_reason}；请先执行 Domain 采集")
        stage = "快照构造"
        snapshot = build_managed_ddl_config(
            ctx.main_config, ctx.source, ctx.decrypt_password(), ctx.workspace_id
        )
        if Path(snapshot["output"]["json_directory"]) != json_dir:
            raise DbStepConfigError("受管 Rel 输入目录与工作区不一致")
        rel_dir = resolve_rel_output_dir(ctx.source_id, ctx.workspace_id)
        if Path(snapshot["output"]["rel_directory"]) != rel_dir:
            raise DbStepConfigError("受管 Rel 输出目录与工作区不一致")
        check_objects_in_scope(objects, snapshot, ctx.source, step_label="Rel")
        stage = "构造预检"
        _precheck_pipeline_config(snapshot)
        stage = "domain 参数解析"
        effective_domain, effective_cross_domain = resolve_domain_params(
            None, None, snapshot
        )
        domain_resolver = None
        if effective_domain:
            domain_resolver = DomainResolver(domain_yaml_path)
            if not domain_resolver.get_all_domains():
                raise DbStepConfigError(
                    "当前生效的 domain 配置（来源: yaml 配置 relationships.domain）"
                    f"需要有效的 domains 列表，但 {domain_yaml_path} 未定义任何业务域"
                )
        stage = "LLM 预检"
        llm_candidates_cfg = (snapshot.get("relationships") or {}).get(
            "llm_candidates"
        ) or {}
        if llm_candidates_cfg.get("enabled", True):
            llm_config = resolve_module_llm_config(snapshot, "relationships.llm")
            LLMService(llm_config)
    except DbStepConfigError:
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        logger.error("Rel 准备阶段故障 stage=%s type=%s", stage, type(exc).__name__)
        raise RelPreparationError(
            f"Rel 准备阶段故障（{stage}：{type(exc).__name__}）"
        ) from None

    # ---- 构造先于备份（R6）：构造期失败旧产物保留 ----
    pipeline = None
    discover_started = False
    try:
        pipeline = RelationshipDiscoveryPipeline(
            ctx.config_path,
            domain_filter=effective_domain,
            cross_domain=bool(effective_cross_domain),
            domain_resolver=domain_resolver,
            config=snapshot,
        )
        backup_directory_before_update(rel_dir)
        discover_started = True
        result = pipeline.discover()
    except Exception as exc:  # noqa: BLE001
        if pipeline is not None and not discover_started:
            try:
                pipeline.connector.close()
            except Exception as close_exc:  # noqa: BLE001
                logger.warning("Rel 连接池关闭失败（%s）", type(close_exc).__name__)
        logger.error(
            "rel 采集执行失败 type=%s source_id=%s workspace_id=%s",
            type(exc).__name__,
            ctx.source_id,
            ctx.workspace_id,
        )
        return StepOutcome(
            success=False, message=f"Rel 采集失败（{type(exc).__name__}）"
        )

    try:
        ok, reason = verify_rel_collection(result, rel_dir, ctx.source.db_name)
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, f"Rel 采集核验失败（{type(exc).__name__}）"
    if not ok:
        return StepOutcome(success=False, message=reason)

    total = result.total_relations
    fk = result.foreign_key_relations
    inferred = result.inferred_relations
    if effective_domain is not None and inferred == 0:
        message = (
            f"受管 Rel 关系发现完成（{total} 条关系：外键 {fk} / 推断 0；"
            "domain 分组下推断为空，请检查 db_domains.yaml 表名与评分阈值）"
        )
        logger.warning(
            "受管 Rel 零推断告警：total=%s fk=%s domain=%r tables=%s",
            total,
            fk,
            effective_domain,
            len(objects),
        )
    elif effective_domain is None and total == 0:
        message = "受管 Rel 关系发现完成（0 条关系；外键与推断均为空，请检查评分阈值）"
        logger.warning(
            "受管 Rel 零关系告警：total=0 domain=null tables=%s", len(objects)
        )
    else:
        message = (
            f"受管 Rel 关系发现完成（{total} 条关系：外键 {fk} / 推断 {inferred}）"
        )
    input_files = [
        (f"json/{path.name}", json_run_id)
        for path in sorted(json_dir.glob("*.json"), key=lambda item: item.name)
        if path.name != JSON_MANIFEST_NAME
    ]
    input_files.append((f"domain/{DOMAIN_ARTIFACT_NAME}", domain_run_id))
    output_files = [
        f"rel/{path.name}"
        for path in sorted(rel_dir.iterdir(), key=lambda item: item.name)
        if not path.name.startswith(".") and path.is_file()
    ]
    return StepOutcome(
        success=True,
        message=f"{message}（{time.perf_counter() - started:.1f}s）",
        input_files=input_files,
        output_files=output_files,
    )
