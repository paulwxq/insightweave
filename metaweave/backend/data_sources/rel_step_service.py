"""受管 Rel 步骤的入口准备链；Web 采集与重跑共用（doc 23.1 §5）。

结构对齐 domain（doc 21.1 §5），差异点：
- 依赖两行（json + domain 步骤行 success）与两份产物核验；
- 连源库采样：凭据注入走 ``build_managed_ddl_config``（json 步骤同款）；
- 对象-范围核对（N2/N4）与构造预检（N1/R3/R8）在本地段、清理之前；
- 构造先于清理（R6）：``DatabaseConnector`` 急切建池等价于连接探测前移，
  构造期失败旧产物保留；构造成功后 clean 抛错由服务侧显式关闭连接池（R10）；
- 上游 marker 复查（会话 2/3 比对 json/domain 行 ``updated_at``）；
- 零关系/零推断告警互斥双分支（D3/R4/R7）。
"""

import json
import logging
import shutil
import time
from pathlib import Path

from backend.data_sources.db_step_service import (
    DbStepConfigError,
    SourceDisabledError,
)
from backend.data_sources.ddl_step_service import build_managed_ddl_config
from backend.data_sources.domain_step_service import verify_domain_artifact
from backend.data_sources.json_step_service import (
    ManagedJsonInputError,
    read_managed_json_input,
)
from backend.data_sources.md_step_service import check_objects_in_scope
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.data_sources.repository import get_db_source
from backend.jobs.repository import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    SourceRegistrationMissingError,
    StepAlreadyRunningError,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
    assert_workspace_owner,
    finish_step,
    get_step_row,
    start_step,
)
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    platform_session,
)
from backend.publishing.repository import lock_db_source_row
from backend.security.source_password import decrypt_password
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_json_output_dir,
    resolve_rel_output_dir,
)
from metaweave.core.domains import DomainResolver
from metaweave.core.metadata.db_step import validate_schema_names
from metaweave.core.metadata.execution import (
    load_main_config_snapshot,
    resolve_domain_params,
    set_operation_step,
)
from metaweave.core.relationships.name_similarity import (
    InvalidNameSimilarityConfig,
    NameSimilarityService,
)
from metaweave.core.relationships.pipeline import RelationshipDiscoveryPipeline
from metaweave.services.llm_config_resolver import resolve_module_llm_config
from metaweave.services.llm_service import LLMService
from metaweave.utils.file_utils import clean_top_level_files
from metaweave.utils.source_id import normalize_source_id

logger = logging.getLogger("backend.data_sources")

REL_STEP_NAME = "rel"
JSON_STEP_FOR_REL = "json"
DOMAIN_STEP_FOR_REL = "domain"
DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
REL_NOT_READY_TEMPLATE = (
    "同一作业的 {step} 产物未就绪；请先执行 {step} 或指定已有 job_id"
)
JSON_NOT_READY = REL_NOT_READY_TEMPLATE.format(step="JSON")
DOMAIN_NOT_READY = REL_NOT_READY_TEMPLATE.format(step="Domain")
SOURCE_GONE = "未找到指定 source_id 的数据源"

# 静态解析函数在 import 期取引用：测试对服务命名空间的
# RelationshipDiscoveryPipeline 打替身时，预检仍用真实解析（doc 23.1 §5.2 b）
_PARSE_LLM_TOP_K = RelationshipDiscoveryPipeline._parse_llm_top_k


class RelPreparationError(PlatformUnavailableError):
    """rel 准备阶段故障（本地文件/配置段与 LLM 预检的非受控异常）。

    子类化 ``PlatformUnavailableError``（doc 23.1 §5，doc 21.1 §5.2 同款）：
    ``collection_run`` 既有 ``except PlatformUnavailableError`` 分支直接覆盖，
    API 侧由更具体的 handler 按 MRO 先命中，返回 503 ``rel_preparation_error``。
    """


def _failure(started: float, message: str, job_id=None) -> dict:
    result = {
        "success": False,
        "execution_time": time.perf_counter() - started,
        "message": message,
    }
    if job_id is not None:
        result["job_id"] = str(job_id)
    return result


def _precheck_pipeline_config(snapshot: dict) -> None:
    """清理前覆盖管线构造期解析（doc 23.1 §5.2 第 6 步，无文件/数据库副作用）。

    节点检查按运行时 null 语义分两族（R3/R8）；异常统一抛带配置路径的
    ``ValueError``，由调用方转 ``DbStepConfigError``。
    """

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
    # null 豁免族（运行时有 or {} 链，null 安全；键存在且非对象非 null → 拒）
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
    # null 也拒绝族（.get(k, default) 无 or 链，null → None.get/sum(None.values()) 即炸）
    _require_object(snapshot, "decision", "decision", null_as_empty=False)
    _require_object(rel, "weights", "relationships.weights", null_as_empty=False)
    # R8：顶层 weights 仅当 relationships 无该键时才是被消费的 fallback 路径
    if "weights" not in rel:
        _require_object(snapshot, "weights", "weights", null_as_empty=False)
    # b) llm_candidates.top_k（消息自带配置路径）
    llm_candidates_cfg = rel.get("llm_candidates") or {}
    _PARSE_LLM_TOP_K(llm_candidates_cfg.get("top_k", 50))
    # c) name_similarity 显式非法：镜像管线窄/宽捕获（pipeline.py:121-128）——
    # InvalidNameSimilarityConfig 转 ValueError，其他异常放行（运行期降级）
    try:
        NameSimilarityService(
            rel.get("name_similarity", {}), snapshot.get("embedding") or {}
        )
    except InvalidNameSimilarityConfig as exc:
        raise ValueError(str(exc)) from None
    except Exception:  # noqa: BLE001 —— 降级语义（doc 15 §5）
        pass
    # d) LLM 开启时 langchain_config（原始 relationships.llm 节点）：
    # 非对象在 llm_relationship_discovery.py:137 的 .get 即炸；batch_size
    # 逐字镜像 :138 的 max(1, int(x or 50))——真抛时统一转配置错误，
    # TypeError 不得落入 RelPreparationError/503
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
    """核对受管 Rel 成功条件（doc 23.1 §5.3）：声明恰为 json/md 两份普通文件、
    目录顶层实际文件与声明一致、json 可解析且库名/relationships/statistics 合法。"""
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


def _upstream_unchanged(session, job_uuid, step_name, marker) -> bool:
    step = get_step_row(session, job_uuid, step_name)
    return (
        step is not None
        and step.last_status == STATUS_SUCCESS
        and step.updated_at == marker
    )


def _backup_previous_rel_artifacts(rel_dir: Path, database_name: str) -> None:
    """受管重跑前备份上一份 rel 产物（doc 23.2 §5.3，D5）。

    移入 ``rel/.backup/`` 子目录（写入前先清空，只保留最近一份）；恢复为
    手工操作，不做 UI。发生在 ``clean_top_level_files`` 之前——旧产物先移走
    再清顶层；``.backup`` 是子目录，clean（只删顶层文件）、采集核验（跳过
    子目录）与发布产物就绪判定均不受影响。仅受管侧调用，CLI
    ``--step rel --clean`` 不加备份。
    """
    backup_dir = rel_dir / ".backup"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True)
    for suffix in (".json", ".md"):
        src = rel_dir / f"{database_name}.relationships_global{suffix}"
        if src.is_file() and not src.is_symlink():
            src.replace(backup_dir / src.name)


def run_rel_step(config_path, source_id, job_id=None) -> dict:
    """准备链 → 会话 2 锁下登记 → 构造 → 清理 → 生成 → 核验 → 会话 3 锁下
    marker 复查与状态写回（doc 23.1 §5）。"""
    started = time.perf_counter()
    set_operation_step(REL_STEP_NAME)
    if job_id is None:
        # 新作业 rel 单选没有任何工作区可用（与 md/domain/dim_config 相同）
        raise DbStepConfigError(JSON_NOT_READY)
    try:
        source_uuid = normalize_source_id(source_id)
        job_uuid = normalize_source_id(job_id)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    try:
        main_config = load_main_config_snapshot(config_path)
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        raise DbStepConfigError(
            f"主配置加载或校验失败（{type(exc).__name__}）"
        ) from None
    try:
        settings = load_platform_settings_from_env()
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None

    # ---- 平台库读取段（会话 1，无凭据，只读） ----
    try:
        with platform_session(settings) as session:
            source = get_db_source(session, source_uuid)
            if source is None:
                raise DbStepConfigError(SOURCE_GONE)
            if source.db_type != "postgresql":
                raise DbStepConfigError("当前仅支持 PostgreSQL 数据源")
            if not source.username or not source.host or not source.db_name:
                raise DbStepConfigError("数据源登记缺少必要的连接字段")
            if not source.is_active:
                raise SourceDisabledError("该数据源已禁用，不能执行作业")
            try:
                assert_workspace_owner(session, job_uuid, source_uuid)
            except (WorkspaceMissingError, WorkspaceSourceMismatchError) as exc:
                raise DbStepConfigError(str(exc)) from None
            json_step = get_step_row(session, job_uuid, JSON_STEP_FOR_REL)
            if json_step is None or json_step.last_status != STATUS_SUCCESS:
                raise DbStepConfigError(JSON_NOT_READY)
            domain_step = get_step_row(session, job_uuid, DOMAIN_STEP_FOR_REL)
            if domain_step is None or domain_step.last_status != STATUS_SUCCESS:
                raise DbStepConfigError(DOMAIN_NOT_READY)
            json_marker = json_step.updated_at
            domain_marker = domain_step.updated_at
            try:
                # 与 json（json_step_service.py:297-301）/md（:289）同款的成对
                # 调用（L5）：非法 schema 名在会话 1 即拦
                validate_schema_names(source.selected_schemas)
                password = decrypt_password(source.password_ciphertext)
            except ValueError as exc:
                raise DbStepConfigError(str(exc)) from None
    except DbStepConfigError:
        raise
    except ValueError as exc:
        raise DbStepConfigError(str(exc)) from None
    except Exception as exc:
        return _failure(started, f"平台库连接或读取失败（{type(exc).__name__}）")

    # ---- 本地文件与配置段（不依赖数据库连接） ----
    stage = "json 产物校验"
    try:
        try:
            objects = read_managed_json_input(source_uuid, job_uuid, source.db_name)
        except ManagedJsonInputError as exc:
            raise DbStepConfigError(f"同一作业的 JSON 产物未就绪：{exc}") from None
        stage = "domain 产物校验"
        domain_yaml_path = (
            resolve_domain_output_dir(source_uuid, job_uuid) / DOMAIN_ARTIFACT_NAME
        )
        artifact_ok, artifact_reason = verify_domain_artifact(domain_yaml_path)
        if not artifact_ok:
            raise DbStepConfigError(
                f"{artifact_reason}；请先执行 Domain 或指定已有 job_id"
            )
        stage = "快照构造"
        snapshot = build_managed_ddl_config(main_config, source, password, job_uuid)
        if Path(snapshot["output"]["json_directory"]) != resolve_json_output_dir(
            source_uuid, job_uuid
        ):
            raise DbStepConfigError("受管 Rel 输入目录与作业工作区不一致")
        if Path(snapshot["output"]["rel_directory"]) != resolve_rel_output_dir(
            source_uuid, job_uuid
        ):
            raise DbStepConfigError("受管 Rel 输出目录与作业工作区不一致")
        rel_dir = Path(snapshot["output"]["rel_directory"])
        # 对象-范围核对（N2/N3：快照构造之后，与 md 同序；N4：文案按步格式化）
        check_objects_in_scope(objects, snapshot, source, step_label="Rel")
        # 构造预检必须先于 domain 参数解析与 LLM 预检（R11）：后两者对
        # relationships/llm_candidates 节点的 .get 解引用在节点非对象时抛
        # AttributeError，会落入 RelPreparationError/503——先经本预检把
        # 节点类型收成 ValueError（400），后续读取只在节点合法时执行
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
    except Exception as exc:  # noqa: BLE001 —— 本地段非受控故障
        logger.error("Rel 准备阶段故障 stage=%s type=%s", stage, type(exc).__name__)
        raise RelPreparationError(
            f"Rel 准备阶段故障（{stage}：{type(exc).__name__}）"
        ) from None

    # ---- 会话 2：锁下复查上游并登记（start_step 唯一一次 commit） ----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise DbStepConfigError(SOURCE_GONE)
            if not _upstream_unchanged(
                session, job_uuid, JSON_STEP_FOR_REL, json_marker
            ):
                raise DbStepConfigError(JSON_NOT_READY)
            if not _upstream_unchanged(
                session, job_uuid, DOMAIN_STEP_FOR_REL, domain_marker
            ):
                raise DbStepConfigError(DOMAIN_NOT_READY)
            start_step(session, job_uuid, source_uuid, REL_STEP_NAME)
    except DbStepConfigError:
        raise
    except SourceRegistrationMissingError:
        raise DbStepConfigError(SOURCE_GONE) from None
    except StepAlreadyRunningError as exc:
        return _failure(started, str(exc), job_uuid)

    # ---- 构造 → 备份 → 清理 → 生成（R6 顺序；doc 23.2 §5.3 备份插在构造成功后、清理前） ----
    pipeline = None
    discover_started = False
    try:
        pipeline = RelationshipDiscoveryPipeline(
            config_path,
            domain_filter=effective_domain,
            cross_domain=bool(effective_cross_domain),
            domain_resolver=domain_resolver,
            config=snapshot,
        )
        _backup_previous_rel_artifacts(rel_dir, source.db_name)
        clean_top_level_files(rel_dir)
        discover_started = True
        result = pipeline.discover()
    except Exception as exc:  # noqa: BLE001 —— 生成段收敛为失败结果
        if pipeline is not None and not discover_started:
            # R10：构造成功后、discover() 开始前的失败（如 clean 抛 OSError），
            # discover 的 finally 不会执行——服务侧持有实例，显式关闭连接池
            try:
                pipeline.connector.close()
            except Exception as close_exc:  # noqa: BLE001 —— 关闭失败不掩盖原失败
                logger.warning("Rel 连接池关闭失败（%s）", type(close_exc).__name__)
        message = f"rel 采集失败（{type(exc).__name__}）"
        try:
            with platform_session(settings) as session:
                finish_step(session, job_uuid, REL_STEP_NAME, STATUS_FAILED)
        except Exception as state_exc:
            message += (
                f"；作业状态更新失败（{type(state_exc).__name__}），"
                "请查看日志确认作业状态"
            )
        return _failure(started, message, job_uuid)

    # ---- 核验 ----
    try:
        ok, reason = verify_rel_collection(result, rel_dir, source.db_name)
    except Exception as exc:  # noqa: BLE001 —— 核验异常同样记 failed
        ok, reason = False, f"Rel 采集核验失败（{type(exc).__name__}）"

    # ---- 会话 3：锁下 marker 复查与状态写回（同一事务，finish 提交释放锁） ----
    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_uuid) is None:
                raise SourceRegistrationMissingError(
                    f"数据源登记已不存在: {source_uuid}"
                )
            changed = []
            if not _upstream_unchanged(
                session, job_uuid, JSON_STEP_FOR_REL, json_marker
            ):
                changed.append("JSON")
            if not _upstream_unchanged(
                session, job_uuid, DOMAIN_STEP_FOR_REL, domain_marker
            ):
                changed.append("Domain")
            if ok and changed:
                ok = False
                reason = (
                    f"上游 {' 与 '.join(changed)} 已重跑，本次 Rel 产物已失效；"
                    "请重跑该步骤后再执行 Rel"
                )
            finish_step(
                session,
                job_uuid,
                REL_STEP_NAME,
                STATUS_SUCCESS if ok else STATUS_FAILED,
            )
    except SourceRegistrationMissingError:
        return _failure(
            started,
            "Rel 产物可能已生成，但数据源登记已不存在；"
            f"job_id={job_uuid}，请查看日志确认作业状态",
            job_uuid,
        )
    except Exception as exc:
        logger.error(
            "作业状态更新失败（%s） source_id=%s job_id=%s 产物目录=%s",
            type(exc).__name__,
            source_uuid,
            job_uuid,
            rel_dir,
        )
        return _failure(
            started,
            "Rel 产物可能已生成，但作业状态更新失败"
            f"（{type(exc).__name__}）；job_id={job_uuid}，"
            "请查看日志确认作业状态与产物",
            job_uuid,
        )

    if not ok:
        return _failure(started, reason, job_uuid)

    # ---- 成功消息与告警（D3/R4/R7 互斥分支，每次至多一条） ----
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
    return {
        "success": True,
        "execution_time": time.perf_counter() - started,
        "message": message,
        "job_id": str(job_uuid),
    }
