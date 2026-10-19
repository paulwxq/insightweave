"""按源就绪检查（doc 2 §5.5）：配置兼容性、业务库、知识成套与版本。

readiness 说明检查时点的可用性，不是未来运行永不失败的保证。compatible 只
表示 PostgreSQL 类型、非空 selected_schemas 和知识键空间兼容性，不把网络
失败叫作“不兼容”。完整检查成功缓存 TTL 30 秒；Embedding 探针使用独立成功
缓存 TTL 300 秒（doc 2 §5.5.2）。

跨存储对象身份使用独立纯函数严格校验入口（doc 2 §5.5）：不 strip、不
lower、不去引号；两段表键仅用同一登记 db_name 补前缀；三段键首段必须精确
一致。与模型输入容错的 normalize_table_id 不是同一个函数。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

from src.modules.db_deepagent.keyspace import db_prefix_problem
from src.modules.db_deepagent.source_registry import SourceRegistration
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.readiness")

FULL_CHECK_TTL_SECONDS = 30.0
EMBEDDING_PROBE_TTL_SECONDS = 300.0

_IDENT_RE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _valid_identifier(part: str) -> bool:
    if not part or part[0] not in _IDENT_RE_CHARS or part[0].isdigit():
        return False
    return all(c in _IDENT_RE_CHARS for c in part)


def strict_table_key(raw: str, db_name: str) -> Optional[str]:
    """持久化知识身份的严格三段逻辑键校验（doc 2 §5.5）。

    - 不 strip、不修剪各段、不 lower/casefold、不去除引号；
    - 两段表键仅用同一登记 db_name 补前缀；三段键首段必须精确一致；
    - db 前缀按 §5.3 不透明字符串规则（允许内部普通空格）；
    - schema/表/列段内空白、首尾空白、非法字符、非法段数均拒绝。
    返回规范三段键 ``db.schema.table``，无法解析返回 None。
    """
    if not isinstance(raw, str) or not db_name:
        return None
    if db_prefix_problem(db_name):
        return None
    parts = raw.split(".")
    if len(parts) == 2:
        schema, table = parts
        segments = (db_name, schema, table)
    elif len(parts) == 3:
        if parts[0] != db_name:
            return None
        segments = (parts[0], parts[1], parts[2])
    else:
        return None
    if any(not seg or seg != seg.strip() for seg in segments[1:]):
        return None
    if not all(_valid_identifier(seg) for seg in segments[1:]):
        return None
    return ".".join(segments)


def strict_column_key(raw: str, db_name: str) -> Optional[tuple[str, str]]:
    """列键 (规范表键, 实际列名)：从 ``db.schema.table.column`` 拆出。"""
    if not isinstance(raw, str):
        return None
    table_part, sep, column = raw.rpartition(".")
    if not sep or not column or column != column.strip():
        return None
    if not _valid_identifier(column):
        return None
    table_key = strict_table_key(table_part, db_name)
    if table_key is None:
        return None
    return table_key, column


def source_fingerprint(registration: SourceRegistration) -> str:
    """登记连接目标/范围指纹（doc 2 §5.3）：host、port、db_name、username、
    规范化 sslmode、selected_schemas（保留顺序）；display_name/description/
    updated_at/is_active 不参与。"""
    payload = {
        "host": registration.host,
        "port": registration.port,
        "db_name": registration.db_name,
        "username": registration.username,
        "sslmode": (registration.sslmode or "prefer").strip().lower(),
        "selected_schemas": list(registration.selected_schemas),
    }
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class RegistrationLookup:
    """登记解析结果分类（供 readiness/创建/查询稳定映射错误码）。"""

    status: (
        str  # ok | not_found | inactive | unsupported | config_invalid | unavailable
    )
    registration: Optional[SourceRegistration] = None
    detail: str = ""


@dataclass
class MilvusFacts:
    """按源枚举的 Milvus 实际知识（全部分页，doc 2 §5.5）。"""

    table_keys: set[str] = field(default_factory=set)
    column_keys: set[tuple[str, str]] = field(default_factory=set)
    table_publish_ids: set[str] = field(default_factory=set)
    dim_value_publish_ids: set[str] = field(default_factory=set)
    dim_value_count: int = 0
    sql_example_publish_ids: set[str] = field(default_factory=set)
    sql_example_count: int = 0
    unparseable: int = 0
    # 列记录的 parent_id 与列键表前缀不一致的条数（doc 2 §5.5 父子归属核对）
    column_parent_mismatch: int = 0


@dataclass
class Neo4jFacts:
    table_keys: set[str] = field(default_factory=set)
    column_keys: set[tuple[str, str]] = field(default_factory=set)
    publish_ids: set[str] = field(default_factory=set)
    # HAS_COLUMN 端点有效的 (表键, 列) 对集合；端点解析失败或父子不符计入
    # has_column_invalid（doc 2 §5.5：图验收要求 Column/HAS_COLUMN 归属有效）
    has_column_pairs: set[tuple[str, str]] = field(default_factory=set)
    has_column_count: int = 0
    has_column_invalid: int = 0
    legacy_two_segment: int = 0
    unparseable: int = 0


@dataclass
class PublishRecords:
    """平台 publish_info 的本源记录（参数化只读查询）。"""

    rows: list[dict[str, Any]] = field(default_factory=list)


class ReadinessBackend(Protocol):
    """就绪检查的后端依赖（默认实现连真实服务；测试注入 fake）。"""

    def lookup_registration(self, source_id: str) -> RegistrationLookup: ...

    def load_publish_records(self, source_id: str) -> PublishRecords: ...

    def load_milvus_facts(self, source_id: str) -> MilvusFacts: ...

    def load_neo4j_facts(self, source_id: str) -> Neo4jFacts: ...

    def check_business_db(
        self, registration: SourceRegistration
    ) -> list[dict[str, Any]]: ...

    def embedding_probe(self, source_id: str) -> None: ...


class ReadinessError(Exception):
    """依赖读取无法完成（503，doc 2 §5.5 表第 3 行）。"""


# ----------------------------------------------------------------------------
# 结果结构与判定
# ----------------------------------------------------------------------------


def _check(
    name: str,
    required: bool,
    state: str,
    message: str = "",
    error_code: Optional[str] = None,
    counts: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "required": required,
        "state": state,
        "error_code": error_code,
        "message": message,
        "counts": counts,
    }


@dataclass
class ReadinessOutcome:
    """一次完整检查的结果与派生快照（doc 2 §5.5.1）。"""

    source_id: str
    compatible: Optional[bool]
    ready: bool
    checks: list[dict[str, Any]]
    fingerprint: Optional[str] = None
    knowledge_snapshot: Optional[dict[str, Any]] = None
    checked_at: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "compatible": self.compatible,
            "ready": self.ready,
            "checked_at": self.checked_at,
            "checks": self.checks,
        }

    @property
    def terminal_error_code(self) -> Optional[str]:
        """检查终止原因（404/409/422 映射用）：按 checks 顺序找首个
        required 且 state=fail 的检查码。"""
        for item in self.checks:
            if item["state"] == "fail" and item["required"]:
                return item["error_code"]
        return None


def _current_publish(rows: list[dict[str, Any]], step: str) -> Optional[dict[str, Any]]:
    """某步骤唯一 success + is_current_publish 记录；多于一条视为不一致。"""
    matched = [
        r
        for r in rows
        if r.get("step_name") == step
        and r.get("status") == "success"
        and r.get("is_current_publish")
    ]
    if len(matched) == 1:
        return matched[0]
    return None


class ReadinessService:
    """readiness 完整检查 + 30 秒缓存 + 300 秒探针缓存（doc 2 §5.5.2）。"""

    def __init__(
        self,
        backend: ReadinessBackend,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        full_ttl: float = FULL_CHECK_TTL_SECONDS,
        probe_ttl: float = EMBEDDING_PROBE_TTL_SECONDS,
    ):
        self._backend = backend
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._full_ttl = full_ttl
        self._probe_ttl = probe_ttl
        self._cache: dict[str, tuple[float, ReadinessOutcome]] = {}
        self._probe_cache: dict[str, tuple[float, None]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 公共入口

    def full_check(self, source_id: str) -> ReadinessOutcome:
        """完整检查（含缓存，doc 2 §5.5.2）：缓存键含 source_id、字段指纹
        与全部 current 发布版本——命中时先轻量复核登记与当前发布标识，
        一致才沿用；不一致（停用/重发布/复核失败）按键变化重新完整检查。
        失败不缓存成功、不沿用过期通过结果。
        """
        with self._lock:
            cached = self._cache.get(source_id)
            if cached is not None and time.monotonic() - cached[0] <= self._full_ttl:
                if self._cache_identity_matches(source_id, cached[1]):
                    return cached[1]
                self._cache.pop(source_id, None)
        outcome = self._run_checks(source_id)
        if outcome.ready:
            with self._lock:
                self._cache[source_id] = (time.monotonic(), outcome)
        return outcome

    def _cache_identity_matches(self, source_id: str, cached: ReadinessOutcome) -> bool:
        """命中缓存前的键复核：重新读取登记指纹与 current 发布快照，与
        缓存结果一致才视作同键（backend_summary 为运行期附加信息，不参与）。"""
        try:
            lookup, fingerprint, snapshot = self.light_recheck(source_id)
        except Exception:  # noqa: BLE001（复核读失败=键不可判，不沿用缓存）
            return False
        if lookup.status != "ok" or not fingerprint:
            return False
        cached_snapshot = {
            key: value
            for key, value in (cached.knowledge_snapshot or {}).items()
            if key != "backend_summary"
        }
        return cached.fingerprint == fingerprint and cached_snapshot == snapshot

    def invalidate(self, source_id: str) -> None:
        with self._lock:
            self._cache.pop(source_id, None)

    def probe_embedding(self, source_id: str, probe_key: str) -> None:
        """Embedding 探针（doc 2 §5.5.2）：独立成功缓存，键为源 + 当前
        发布版本摘要（在线重发布即失效重跑；正常"停服发布重启"流程本就
        清空进程缓存，此为异常在线变化的兜底）。"""
        with self._lock:
            cached = self._probe_cache.get(probe_key)
            if cached is not None and time.monotonic() - cached[0] <= self._probe_ttl:
                return
        self._backend.embedding_probe(source_id)
        with self._lock:
            # 同源旧版本键即时清理，探针缓存每源至多一条
            prefix = f"embedding:{source_id}:"
            for key in [k for k in self._probe_cache if k.startswith(prefix)]:
                if key != probe_key:
                    self._probe_cache.pop(key, None)
            self._probe_cache[probe_key] = (time.monotonic(), None)

    def light_recheck(
        self, source_id: str
    ) -> tuple[RegistrationLookup, str, dict[str, Any]]:
        """每轮轻量复核（doc 2 §5.5.2 第 1/3 条）：重新读取可信登记与 current
        发布标识，计算指纹与快照；不缓存、不做全量扫描。lookup.status != ok
        时 fingerprint/snapshot 为空，由调用方映射稳定错误。"""
        lookup = self._backend.lookup_registration(source_id)
        if lookup.status != "ok" or lookup.registration is None:
            return lookup, "", {}
        registration = lookup.registration
        records = self._backend.load_publish_records(source_id)
        snapshot = _snapshot_from_records(records.rows)
        return lookup, source_fingerprint(registration), snapshot

    # ------------------------------------------------------------ 检查实现

    def _run_checks(self, source_id: str) -> ReadinessOutcome:
        checks: list[dict[str, Any]] = []

        # 1) 登记（必需）：兼容性判定优先于依赖检查（doc 2 §5.5 表）
        lookup = self._backend.lookup_registration(source_id)
        if lookup.status == "not_found":
            checks.append(
                _check("registry", True, "fail", "登记源不存在", "source_not_found")
            )
            return self._outcome(source_id, None, False, checks)
        if lookup.status == "inactive":
            checks.append(
                _check("registry", True, "fail", "登记源已停用", "source_inactive")
            )
            return self._outcome(source_id, None, False, checks)
        if lookup.status == "unsupported":
            checks.append(
                _check(
                    "registry",
                    True,
                    "fail",
                    lookup.detail or "登记源类型不受支持",
                    "source_unsupported",
                )
            )
            return self._outcome(source_id, False, False, checks)
        if lookup.status == "config_invalid":
            checks.append(
                _check(
                    "registry",
                    True,
                    "fail",
                    lookup.detail or "登记配置非法",
                    "source_config_invalid",
                )
            )
            return self._outcome(source_id, False, False, checks)
        if lookup.status != "ok" or lookup.registration is None:
            checks.append(
                _check(
                    "registry",
                    True,
                    "fail",
                    "登记库读取失败",
                    "source_registry_unavailable",
                )
            )
            return self._outcome(source_id, None, False, checks)
        registration = lookup.registration

        compatible = True
        # 键空间兼容性：db_name 作为表键前缀必须合法（doc 2 §5.5）
        if db_prefix_problem(registration.db_name):
            compatible = False
            checks.append(
                _check(
                    "registry",
                    True,
                    "fail",
                    f"登记 db_name 不能作为知识键前缀: {registration.db_name!r}",
                    "source_config_invalid",
                )
            )
            return self._outcome(source_id, False, False, checks)
        if not registration.selected_schemas:
            compatible = False
            checks.append(
                _check(
                    "registry",
                    True,
                    "fail",
                    "selected_schemas 为空，源范围不可用",
                    "source_config_invalid",
                )
            )
            return self._outcome(source_id, False, False, checks)
        checks.append(_check("registry", True, "pass", "登记源有效且启用"))

        # 2) 业务库（必需）：目标一致 + 实际只读事务 + 超时生效（doc 2 §5.5）
        try:
            business_checks = self._backend.check_business_db(registration)
        except ReadinessError:
            checks.append(
                _check(
                    "business_db",
                    True,
                    "fail",
                    "业务库连接或检查未能完成",
                    "dependency_unavailable",
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        checks.extend(business_checks)
        # 业务库实际检查失败不提前返回：保留后续依赖检查项，最终统一判定

        # 3) 发布记录（必需步骤 table_schema/rel；可选 dim_value/sql）
        try:
            records = self._backend.load_publish_records(source_id)
        except ReadinessError:
            checks.append(
                _check(
                    "publish_records",
                    True,
                    "fail",
                    "发布记录读取失败",
                    "dependency_unavailable",
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        rows = records.rows
        ts_current = _current_publish(rows, "table_schema")
        rel_current = _current_publish(rows, "rel")
        missing: list[str] = []
        if ts_current is None:
            missing.append("table_schema")
        if rel_current is None:
            missing.append("rel")
        if missing:
            checks.append(
                _check(
                    "publish_records",
                    True,
                    "fail",
                    f"缺少成功且当前的发布记录: {', '.join(missing)}",
                    "knowledge_missing",
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        assert ts_current is not None and rel_current is not None
        if str(ts_current.get("job_id")) != str(rel_current.get("job_id")):
            checks.append(
                _check(
                    "publish_records",
                    True,
                    "fail",
                    "table_schema 与 rel 的当前发布不属于同一作业",
                    "knowledge_inconsistent",
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        checks.append(
            _check(
                "publish_records",
                True,
                "pass",
                "table_schema/rel 同作业且各有当前发布",
                counts={"job_id": 1},
            )
        )

        # 4) Milvus / Neo4j 实际知识（必需，成套）
        try:
            milvus = self._backend.load_milvus_facts(source_id)
            neo4j = self._backend.load_neo4j_facts(source_id)
        except ReadinessError:
            checks.append(
                _check(
                    "knowledge_backend",
                    True,
                    "fail",
                    "知识后端（Milvus/Neo4j）读取失败",
                    "dependency_unavailable",
                )
            )
            return self._outcome(source_id, compatible, False, checks)

        if not milvus.table_keys:
            checks.append(
                _check(
                    "table_schema",
                    True,
                    "fail",
                    "本源没有已发布的表对象",
                    "knowledge_missing",
                    counts={"tables": 0},
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        if milvus.unparseable or not milvus.column_keys:
            checks.append(
                _check(
                    "table_schema",
                    True,
                    "fail",
                    "table_schema 记录无法解析或没有列对象",
                    "knowledge_inconsistent",
                    counts={
                        "tables": len(milvus.table_keys),
                        "columns": len(milvus.column_keys),
                        "unparseable": milvus.unparseable,
                    },
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        checks.append(
            _check(
                "table_schema",
                True,
                "pass",
                "本源 table_schema 已发布",
                counts={
                    "tables": len(milvus.table_keys),
                    "columns": len(milvus.column_keys),
                },
            )
        )

        if not neo4j.table_keys:
            checks.append(
                _check(
                    "rel_graph",
                    True,
                    "fail",
                    "本源没有已发布的 Neo4j Table 节点",
                    "knowledge_missing",
                    counts={"tables": 0},
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        if neo4j.legacy_two_segment or neo4j.unparseable:
            checks.append(
                _check(
                    "rel_graph",
                    True,
                    "fail",
                    "图实体键为旧两段形式或无法解析，需受管重新发布 rel",
                    "knowledge_inconsistent",
                    counts={
                        "tables": len(neo4j.table_keys),
                        "legacy_two_segment": neo4j.legacy_two_segment,
                        "unparseable": neo4j.unparseable,
                    },
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        checks.append(
            _check(
                "rel_graph",
                True,
                "pass",
                "本源 rel 图已发布（JOIN_ON 可为零）",
                counts={
                    "tables": len(neo4j.table_keys),
                    "columns": len(neo4j.column_keys),
                    "has_column": neo4j.has_column_count,
                },
            )
        )

        # 5) 跨存储集合核对（doc 2 §5.5：表集合相等 + Milvus 列为图列子集）
        if milvus.table_keys != neo4j.table_keys:
            checks.append(
                _check(
                    "knowledge_consistency",
                    True,
                    "fail",
                    "Milvus 与 Neo4j 的本源表集合不一致",
                    "knowledge_inconsistent",
                    counts={
                        "milvus_tables": len(milvus.table_keys),
                        "neo4j_tables": len(neo4j.table_keys),
                    },
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        if not milvus.column_keys.issubset(neo4j.column_keys):
            checks.append(
                _check(
                    "knowledge_consistency",
                    True,
                    "fail",
                    "已发布 Milvus 列存在图中缺失的父表归属",
                    "knowledge_inconsistent",
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        # 知识范围核对（doc 2 §5.5）：表/列的 schema 必须精确属于当前登记
        # 范围；登记范围变化而知识未重发时不得就绪
        allowed_schemas = set(registration.selected_schemas)
        knowledge_schemas: set[str] = set()
        for key in milvus.table_keys | neo4j.table_keys:
            knowledge_schemas.add(key.split(".")[1])
        for table_key, _column in milvus.column_keys | neo4j.column_keys:
            knowledge_schemas.add(table_key.split(".")[1])
        out_of_scope = knowledge_schemas - allowed_schemas
        if out_of_scope:
            checks.append(
                _check(
                    "knowledge_scope",
                    True,
                    "fail",
                    "知识包含登记范围之外的 schema，请按当前登记范围重新发布",
                    # 复用闭集码（codes.READINESS_CHECK_CODES）：检查名与
                    # message 已承载"范围"语义，不扩契约
                    "knowledge_inconsistent",
                    counts={"out_of_scope_schemas": len(out_of_scope)},
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        # 父子归属端点核对（doc 2 §5.5）：Milvus 列 parent_id 必须与列键的
        # 表前缀一致；图中每个列必须有对应的有效 Table→Column 边
        if milvus.column_parent_mismatch:
            checks.append(
                _check(
                    "knowledge_consistency",
                    True,
                    "fail",
                    "Milvus 列的 parent_id 与列键表前缀不一致",
                    "knowledge_inconsistent",
                    counts={"column_parent_mismatch": milvus.column_parent_mismatch},
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        missing_edges = neo4j.column_keys - neo4j.has_column_pairs
        if missing_edges or neo4j.has_column_invalid:
            checks.append(
                _check(
                    "knowledge_consistency",
                    True,
                    "fail",
                    "图中存在缺少有效 Table→Column 边的列（或边端点归属无效）",
                    "knowledge_inconsistent",
                    counts={
                        "missing_edges": len(missing_edges),
                        "invalid_edges": neo4j.has_column_invalid,
                    },
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        # 实际 publish_id 与对应 current 记录一致（残留/混批拒绝）
        expected_ts = str(ts_current.get("publish_id"))
        expected_rel = str(rel_current.get("publish_id"))
        if milvus.table_publish_ids != {expected_ts}:
            checks.append(
                _check(
                    "knowledge_consistency",
                    True,
                    "fail",
                    "Milvus table_schema 实际版本与当前发布记录不一致",
                    "knowledge_inconsistent",
                    counts={"publish_ids": len(milvus.table_publish_ids)},
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        if neo4j.publish_ids != {expected_rel}:
            checks.append(
                _check(
                    "knowledge_consistency",
                    True,
                    "fail",
                    "Neo4j 实际版本与当前发布记录不一致",
                    "knowledge_inconsistent",
                    counts={"publish_ids": len(neo4j.publish_ids)},
                )
            )
            return self._outcome(source_id, compatible, False, checks)
        col_diff = len(neo4j.column_keys) - len(milvus.column_keys)
        consistency_message = "表集合一致，Milvus 列为图列子集"
        if col_diff > 0:
            consistency_message += (
                f"（图多 {col_diff} 列：include_columns=false 或空描述跳过属已知规则）"
            )
        checks.append(
            _check(
                "knowledge_consistency",
                True,
                "pass",
                consistency_message,
                counts={"tables": len(milvus.table_keys)},
            )
        )

        # 6) 可选知识（dim_value/sql_example）：按实际行数判空（空为 warn，
        # 正常缺省放行）；非空必须通过实际 publish_id 核对，残留/混批或
        # 缺少 publish_id 为 fail 并阻止使用（doc 2 §5.5）
        for name, facts_ids, count, step in (
            (
                "dim_value",
                milvus.dim_value_publish_ids,
                milvus.dim_value_count,
                "dim_value",
            ),
            (
                "sql_example",
                milvus.sql_example_publish_ids,
                milvus.sql_example_count,
                "sql",
            ),
        ):
            current = _current_publish(rows, step)
            if count == 0:
                checks.append(
                    _check(
                        name,
                        False,
                        "warn",
                        "可选知识为空（未发布或合法空结果）",
                        "optional_knowledge_empty",
                        counts={name: 0},
                    )
                )
                continue
            if not facts_ids:
                checks.append(
                    _check(
                        name,
                        False,
                        "fail",
                        f"存在{name}数据但记录缺少 publish_id，无法核对批次",
                        "knowledge_inconsistent",
                        counts={name: count},
                    )
                )
                continue
            if current is None:
                checks.append(
                    _check(
                        name,
                        False,
                        "fail",
                        f"存在{name}数据但没有成功且当前的发布记录",
                        "knowledge_inconsistent",
                        counts={name: count},
                    )
                )
                continue
            if facts_ids != {str(current.get("publish_id"))}:
                checks.append(
                    _check(
                        name,
                        False,
                        "fail",
                        f"{name} 实际版本与当前发布记录不一致（残留或混批）",
                        "knowledge_inconsistent",
                        counts={name: count, "publish_ids": len(facts_ids)},
                    )
                )
                continue
            checks.append(
                _check(
                    name,
                    False,
                    "pass",
                    f"{name} 与当前发布一致",
                    counts={name: count},
                )
            )

        # 7) Embedding 探针（必需，doc 2 §5.5.2 独立缓存；键含 current
        # 发布版本摘要，在线重发布后旧通过结果不沿用）
        try:
            self.probe_embedding(
                source_id,
                probe_key=_probe_cache_key(source_id, _snapshot_from_records(rows)),
            )
            checks.append(_check("embedding", True, "pass", "Embedding 一致性探针通过"))
        except ReadinessError as exc:
            checks.append(
                _check(
                    "embedding",
                    True,
                    "fail",
                    f"Embedding 一致性检查失败: {exc}",
                    "dependency_unavailable",
                )
            )
            return self._outcome(source_id, compatible, False, checks)

        snapshot = _snapshot_from_records(rows)
        snapshot["backend_summary"] = {
            "milvus_tables": len(milvus.table_keys),
            "neo4j_tables": len(neo4j.table_keys),
        }
        # 到达此处时必需检查的失败均已提前返回；剩余任何 fail（业务库实际
        # 检查、可选知识残留/混批/缺 publish_id）按 doc 2 §5.5 一律阻止
        # 就绪，仅 warn（可选知识正常缺省）放行
        ready = not any(c["state"] == "fail" for c in checks)
        outcome = self._outcome(source_id, compatible, ready, checks)
        outcome.fingerprint = source_fingerprint(registration)
        outcome.knowledge_snapshot = snapshot
        return outcome

    def _outcome(
        self,
        source_id: str,
        compatible: Optional[bool],
        ready: bool,
        checks: list[dict[str, Any]],
    ) -> ReadinessOutcome:
        return ReadinessOutcome(
            source_id=source_id,
            compatible=compatible,
            ready=ready,
            checks=checks,
            checked_at=self._clock().isoformat(),
        )


def _snapshot_from_records(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """knowledge_snapshot（doc 2 §5.5.1）：必需步骤 (job_id,publish_id)，
    可选步骤 current 或 null。"""

    def pair(step: str) -> Optional[list[str]]:
        current = _current_publish(rows, step)
        if current is None:
            return None
        return [str(current.get("job_id")), str(current.get("publish_id"))]

    return {
        "table_schema": pair("table_schema"),
        "rel": pair("rel"),
        "dim_value": pair("dim_value"),
        "sql_example": pair("sql"),
    }


def _probe_cache_key(source_id: str, snapshot: dict[str, Any]) -> str:
    """Embedding 探针缓存键 = 源 + current 发布版本摘要（doc 2 §5.5.2）：
    发布标识变化即换键，300 秒窗口内的旧通过结果不沿用。"""
    digest = hashlib.sha256(repr(sorted(snapshot.items())).encode("utf-8")).hexdigest()[
        :12
    ]
    return f"embedding:{source_id}:{digest}"
