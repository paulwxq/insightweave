"""沿用来源资格与完整证据图核验（doc 6.3 §6.3–6.4、§8.2；R1-02 / R2-01/02 / R3-01/03）

一个共享入口按完整身份解析证据节点并递归核验**全部** direct/input 边：
- 来源（direct_reuse_from）必须是其原 run 中最终有效、completed、已选入
  成果的任务版本；新登记（目标）记录的身份另行核验，不把目标记录当作
  来源记录（R2-01）。
- 分析/占位符输入（input_result_refs）逐项解析：跨任务消费必须来自
  上游已选定成果，同任务合法探索输入保留（R2-02）。
- R3-01：沿用记录是对**原证据的受控包装**。目标身份在本轮核验；继承的
  input_result_refs 属于原证据的 consumer scope，必须与原证据权威来源
  一致并在原来源图中递归核验，绝不按本轮目标任务重新解释、不删除血缘、
  不要求把历史 run 加入本轮 DAG。
- 快照存在即权威，缺源/缺任务不得回退 live 掩盖；未知协议、作用域不符、
  重复、环、身份冲突一律拒绝。
- 工具、completed 登记、最终提交、Runner 与清理复用同一解析/核验规则。

`evidence_edges()` 同时供清理闭包按完整 snapshot/evidence 身份追踪血缘；
R3-03：正常空输入/无 direct 来源允许，存在却畸形的血缘必须受控拒绝。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.modules.db_deepagent.persistence.archive import RUN_SNAPSHOT_SCHEMA_VERSION
from src.modules.db_deepagent.state import parse_task_ledger_key

# 账本协议版本（§8.2：封存快照另存协议版本；未知版本拒绝作为成果来源）
TASK_LEDGER_PROTOCOL_VERSION = 1


class SourceUnqualified(Exception):
    """来源资格/证据图不成立（含具体原因）"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _is_nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _ref_of(
    record: Mapping[str, Any], session_id: str, thread_id: str, run_id: str
) -> dict:
    """本轮记录自身的完整可信 scope（R2-10：记录保存自身身份）。"""
    return {
        "session_id": record.get("session_id") or session_id,
        "thread_id": record.get("thread_id") or thread_id,
        "run_id": record.get("run_id") or run_id,
        "evidence_id": record.get("evidence_id") or "",
    }


def _checked_evidence_ref(
    ref: Any, session_id: str | None, label: str
) -> dict[str, Any]:
    """严格解析一条边的完整证据引用（R3-03）。"""
    if not isinstance(ref, Mapping):
        raise SourceUnqualified(f"{label}.evidence_ref 缺失或类型非法")
    for field in ("session_id", "thread_id", "run_id", "evidence_id"):
        if not _is_nonblank(ref.get(field)):
            raise SourceUnqualified(f"{label}.evidence_ref.{field} 缺失或非法")
    if session_id is not None and ref.get("session_id") != session_id:
        raise SourceUnqualified(f"{label}.evidence_ref 会话与当前会话不符")
    return dict(ref)


def _checked_task_ref(ref: Any, label: str) -> dict[str, Any]:
    """严格解析一条边的完整任务版本引用（R3-03）。"""
    if not isinstance(ref, Mapping):
        raise SourceUnqualified(f"{label}.source_task_ref 缺失或类型非法")
    if not _is_nonblank(ref.get("sub_task_id")) or type(ref.get("revision")) is not int:
        raise SourceUnqualified(f"{label}.source_task_ref 身份不完整或类型非法")
    for field in ("session_id", "thread_id", "run_id"):
        if not _is_nonblank(ref.get(field)):
            raise SourceUnqualified(f"{label}.source_task_ref.{field} 缺失或非法")
    return dict(ref)


def evidence_edges(
    record: Mapping[str, Any], *, session_id: str | None = None
) -> list[dict[str, Any]]:
    """共享的完整来源边（direct_reuse_from + input_result_refs）。

    返回 [{kind, evidence_ref, source_task_ref, result_ref}]；边按完整身份寻址。
    清理闭包与证据图核验共用，避免只遍历 direct 或只遍历 result_ref 的漏检。

    R3-03：缺省/空列表是合法的「无边」；一旦存在容器或条目，就必须符合
    §6.3/§6.4 的结构（旧协议裸字符串、缺 evidence_ref、缺 task/result
    引用等），否则抛 SourceUnqualified，绝不静默当作零条边。
    """
    if not isinstance(record, Mapping):
        raise SourceUnqualified("证据记录不是映射，无法解析血缘")
    edges: list[dict[str, Any]] = []
    direct = record.get("direct_reuse_from")
    if direct is not None:
        if not isinstance(direct, Mapping):
            raise SourceUnqualified("direct_reuse_from 容器类型非法")
        ref = _checked_evidence_ref(
            direct.get("evidence_ref"), session_id, "direct_reuse_from"
        )
        task = _checked_task_ref(direct.get("source_task_ref"), "direct_reuse_from")
        edges.append(
            {
                "kind": "direct",
                "evidence_ref": ref,
                "source_task_ref": task,
                "result_ref": None,
            }
        )
    raw_inputs = record.get("input_result_refs")
    if raw_inputs is None:
        raw_inputs = []
    if not isinstance(raw_inputs, list):
        raise SourceUnqualified("input_result_refs 容器类型非法（须为列表）")
    for index, entry in enumerate(raw_inputs):
        label = f"input_result_refs[{index}]"
        if not isinstance(entry, Mapping):
            raise SourceUnqualified(f"{label} 不是映射（旧协议/损坏血缘）")
        result_ref = entry.get("result_ref")
        if not _is_nonblank(result_ref):
            raise SourceUnqualified(f"{label}.result_ref 缺失或非法")
        ref = _checked_evidence_ref(entry.get("evidence_ref"), session_id, label)
        task = _checked_task_ref(entry.get("source_task_ref"), label)
        edges.append(
            {
                "kind": "input",
                "evidence_ref": ref,
                "source_task_ref": task,
                "result_ref": result_ref,
            }
        )
    return edges


def _ref_identity(ref: Mapping[str, Any], session_id: str) -> tuple[str, str, str, str]:
    """核验并规范化完整证据引用身份。"""
    if not isinstance(ref, Mapping):
        raise SourceUnqualified("来源引用不是映射")
    if ref.get("session_id") != session_id:
        raise SourceUnqualified("来源引用不属于当前会话")
    thread_id = ref.get("thread_id")
    run_id = ref.get("run_id")
    evidence_id = ref.get("evidence_id")
    if not all(_is_nonblank(x) for x in (thread_id, run_id, evidence_id)):
        raise SourceUnqualified("来源引用身份不完整")
    return session_id, thread_id, run_id, evidence_id


class EvidenceGraphVerifier:
    """按完整身份解析证据/任务记录，递归核验完整证据图。

    快照（QaArchive.read_run_snapshot）存在即为该 (thread, run) 的权威
    来源；缺源/缺任务不再回退 live。无快照时才用线程账本（含轮换携带）。
    """

    def __init__(
        self,
        *,
        session_id: str,
        state: Mapping[str, Any],
        archive=None,
        carried: Iterable[Mapping[str, Any]] = (),
        extra_records: Iterable[Mapping[str, Any]] = (),
    ):
        self.session_id = session_id
        self.state = state
        self.archive = archive
        self.carried = list(carried or ())
        self.extra_records = list(extra_records or ())
        self._snapshot_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        # R4 M1：本次一致视图内「已完成节点」缓存，键＝(完整身份, 核验角色)。
        # 角色入键保证 input/outcome 验证过不会跳过更严格的 source 资格检查；
        # 缓存是 verifier 实例级，不跨 run/视图全局共享。
        self._verified_nodes: set[tuple[tuple[str, str, str, str], str]] = set()

    # -------------------------------------------------------------- 解析
    def snapshot(self, thread_id: str, run_id: str) -> dict[str, Any] | None:
        key = (thread_id, run_id)
        if key in self._snapshot_cache:
            return self._snapshot_cache[key]
        snap = None
        if self.archive is not None:
            try:
                snap = self.archive.read_run_snapshot(
                    self.session_id, thread_id, run_id
                )
            except ValueError as exc:
                # 确定性协议损坏/作用域不符 → 受控业务门控拒绝（R4-01 要求 5）。
                raise SourceUnqualified(f"运行快照不可核验: {exc}") from exc
            # R4-01：psycopg/PoolTimeout/PlatformSchemaError 等持久化 I/O 故障
            # 与未知运行错误一律不在此吞掉、不转成 SourceUnqualified；原样
            # 上抛给 Runner 收尾 / 提交门控的受控异常边界，按
            # is_persistence_error 接入既有异常分类（要求 1/6/7）。
            if snap is not None:
                version = snap.get("run_snapshot_schema_version")
                if type(version) is not int or version != RUN_SNAPSHOT_SCHEMA_VERSION:
                    raise SourceUnqualified(f"未知运行快照协议版本: {version!r}")
                protocol = snap.get("ledger_protocol_version")
                if (
                    type(protocol) is not int
                    or protocol != TASK_LEDGER_PROTOCOL_VERSION
                ):
                    raise SourceUnqualified(f"未知账本协议版本: {protocol!r}")
                if (
                    snap.get("session_id") != self.session_id
                    or snap.get("run_id") != run_id
                    or (snap.get("thread_id") or "") != (thread_id or "")
                ):
                    raise SourceUnqualified("快照内部作用域与寻址不符")
        self._snapshot_cache[key] = snap
        return snap

    @staticmethod
    def _strict_match(matches: list[dict[str, Any]], label: str) -> dict[str, Any]:
        """同完整身份同事实去重；异事实即冲突，拒绝且不取第一条掩盖。"""
        if not matches:
            raise SourceUnqualified(f"{label} 不可核验（缺源）")
        facts = {_canonical(m) for m in matches}
        if len(facts) > 1:
            raise SourceUnqualified(f"{label} 存在不同事实的多条记录（来源冲突）")
        return dict(matches[0])

    def resolve_evidence(self, ref: Mapping[str, Any]) -> dict[str, Any]:
        session_id, thread_id, run_id, evidence_id = _ref_identity(ref, self.session_id)
        snap = self.snapshot(thread_id, run_id)
        records: Iterable[Mapping[str, Any]]
        if snap is not None:
            records = [
                *(snap.get("executions") or []),
                *(snap.get("analyses") or []),
            ]
            authoritative = True
        else:
            records = [
                *(self.state.get("db_executions") or []),
                *(self.state.get("db_analyses") or []),
                *self.extra_records,
            ]
            authoritative = False
        matches = [
            dict(r)
            for r in records
            if r.get("evidence_id") == evidence_id and r.get("run_id") == run_id
        ]
        # 权威快照存在即为唯一来源：缺源不得用 live/轮换携带掩盖（R2-02），
        # 也不把携带副本并入候选造成「事实冲突」误判。
        if authoritative:
            if not matches:
                raise SourceUnqualified(
                    f"来源证据 {evidence_id} 在已封存快照中缺失（缺源，不回退 live）"
                )
            rec = self._strict_match(matches, f"证据 {run_id}:{evidence_id}")
        else:
            # 无快照时线程账本与轮换携带（origin 携带完整身份）同为可见来源。
            for item in self.carried:
                if not isinstance(item, Mapping) or item.get("kind") not in (
                    "execution",
                    "analysis",
                ):
                    continue
                origin = item.get("origin") or {}
                record = item.get("record") or {}
                if (
                    origin.get("thread_id") == thread_id
                    and origin.get("run_id") == run_id
                    and origin.get("evidence_id") == evidence_id
                ):
                    matches.append(dict(record))
            rec = self._strict_match(matches, f"证据 {run_id}:{evidence_id}")
        # 内部作用域严格一致（R2-02 反例 5：thread=WRONG 不得读出）
        if rec.get("thread_id") and rec["thread_id"] != thread_id:
            raise SourceUnqualified("证据记录内部 thread 与引用不符")
        if rec.get("session_id") and rec["session_id"] != session_id:
            raise SourceUnqualified("证据记录内部会话与引用不符")
        return rec

    def task_versions(
        self, thread_id: str, run_id: str, sub_task_id: str
    ) -> list[dict[str, Any]]:
        snap = self.snapshot(thread_id, run_id)
        if snap is not None:
            return [
                dict(t)
                for t in snap.get("tasks") or []
                if isinstance(t, Mapping)
                and t.get("run_id") == run_id
                and t.get("sub_task_id") == sub_task_id
            ]
        out: list[dict[str, Any]] = []
        for key, record in (self.state.get("db_sub_tasks") or {}).items():
            parsed = parse_task_ledger_key(key)
            if parsed is None or not isinstance(record, Mapping):
                raise SourceUnqualified("任务账本键不可解码（来源核验失败）")
            if parsed[0] == run_id and parsed[1] == sub_task_id:
                if record.get("run_id") != run_id:
                    raise SourceUnqualified("任务记录身份与账本键不符")
                out.append(dict(record))
        return out

    def _task_record(
        self, thread_id: str, run_id: str, sub_task_id: str, revision: Any
    ) -> dict[str, Any]:
        versions = self.task_versions(thread_id, run_id, sub_task_id)
        if not versions:
            raise SourceUnqualified(
                f"来源任务 {sub_task_id}@{revision} 的原始记录不可核验（缺源）"
            )
        matched = [v for v in versions if v.get("revision") == revision]
        if len(matched) > 1:
            raise SourceUnqualified(f"来源任务 {sub_task_id}@{revision} 存在多条记录")
        if not matched:
            raise SourceUnqualified(
                f"来源任务 {sub_task_id}@{revision} 的版本记录缺失（缺源）"
            )
        return matched[0]

    # -------------------------------------------------------------- 核验
    def verify_input_edges(
        self,
        edges: list[dict[str, Any]],
        *,
        consumer_task: Mapping[str, Any],
        consumer_run: str,
        consumer_thread: str,
        consumer_task_id: str,
        consumer_task_rev: Any,
        visiting: frozenset[tuple[str, str, str, str]] = frozenset(),
    ) -> None:
        """共享的消费输入资格规则（R3-01/02）。

        - 跨任务输入：必须来自消费者声明依赖、上游 completed、且为上游
          **已选定成果**；
        - 同任务探索输入：按方案保留；
        - 每条边按完整身份递归核验全部祖先（收敛裸 evidence_id 递归）。
        """
        declared = {
            (d.get("sub_task_id"), d.get("revision"))
            for d in consumer_task.get("dependencies") or []
        }
        for edge in edges:
            if edge["kind"] != "input":
                continue
            inner_ref = edge["evidence_ref"]
            inner_task = edge["source_task_ref"]
            _sid, i_thread, i_run, i_eid = _ref_identity(inner_ref, self.session_id)
            same_task = (
                inner_task.get("sub_task_id") == consumer_task_id
                and inner_task.get("revision") == consumer_task_rev
                and inner_task.get("run_id") == consumer_run
                and inner_task.get("thread_id") in (None, consumer_thread)
            )
            if not same_task:
                if i_run != consumer_run:
                    raise SourceUnqualified(
                        "跨任务输入不得来自历史 run（本轮 DAG 之外）"
                    )
                key = (inner_task.get("sub_task_id"), inner_task.get("revision"))
                if key not in declared:
                    raise SourceUnqualified("跨任务输入未在其声明依赖中")
                upstream = self.task_versions(i_thread, i_run, key[0])
                up = [u for u in upstream if u.get("revision") == key[1]]
                if len(up) != 1 or up[0].get("status") != "completed":
                    raise SourceUnqualified("跨任务上游未完成或版本缺失")
                if i_eid not in (up[0].get("evidence_ids") or []):
                    raise SourceUnqualified("跨任务输入不是上游选定成果")
            self.verify_node(
                inner_ref,
                role="input",
                expected_task_ref=inner_task,
                expected_result_ref=edge["result_ref"],
                visiting=visiting,
            )

    def verify_node(
        self,
        ref: Mapping[str, Any],
        *,
        role: str,
        expected_task_ref: Mapping[str, Any] | None = None,
        expected_result_ref: str | None = None,
        visiting: frozenset[tuple[str, str, str, str]] = frozenset(),
    ) -> None:
        """递归核验一个证据节点及其全部 direct/input 边。

        role="source"：直接沿用来源——须为最终有效 completed 版本的选定成果。
        role="outcome"/"input"：本轮或中间记录——解析身份并递归核验其边。

        R3-01：带 direct_reuse_from 的沿用记录，其 input_result_refs 是**从
        原证据继承**的输入事实。目标身份（本轮 task/run）在此独立核验；继承
        输入不按本轮 DAG 重验，而是与权威来源逐项比对后，在原来源图中递归。

        R4 M1：按 (完整身份, 核验角色) 缓存已完成节点，避免共享祖先的指数级
        重复遍历。缓存命中时仍先逐边核对 expected task/result 约束，仅跳过
        递归下降；visiting 环检测保留。角色入键保证 input/outcome 验证过不会
        跳过更严格的 source 资格检查；缓存仅为本次一致视图（实例级）。
        """
        _, thread_id, run_id, evidence_id = _ref_identity(ref, self.session_id)
        visit_key = (self.session_id, thread_id, run_id, evidence_id)
        if visit_key in visiting:
            raise SourceUnqualified("证据图成环（direct/input 来源链）")

        rec = self.resolve_evidence(ref)
        task_id = rec.get("sub_task_id")
        task_rev = rec.get("sub_task_revision")
        if not task_id or task_rev is None:
            raise SourceUnqualified("来源记录缺少任务归属")
        # 每条边的 expected task/result 约束：每次调用都核验（缓存命中也逐边
        # 核对），不被缓存跳过（R4 M1）。
        if expected_result_ref is not None and (
            rec.get("result_ref") != expected_result_ref
            and rec.get("output_result_ref") != expected_result_ref
        ):
            raise SourceUnqualified(
                f"来源 {evidence_id} 的 result_ref 与结构化血缘不符"
            )
        if isinstance(expected_task_ref, Mapping):
            if (
                expected_task_ref.get("sub_task_id") != task_id
                or expected_task_ref.get("revision") != task_rev
                or expected_task_ref.get("run_id") != run_id
            ):
                raise SourceUnqualified(
                    f"来源 {evidence_id} 的任务归属与结构化血缘不符"
                )
            if expected_task_ref.get("session_id") not in (None, self.session_id):
                raise SourceUnqualified("来源血缘的会话身份不符")
            if expected_task_ref.get("thread_id") not in (None, thread_id):
                raise SourceUnqualified("来源血缘的线程身份不符")

        cache_key = (visit_key, role)
        if cache_key in self._verified_nodes:
            return  # 该身份+角色已完整核验（含递归），本条边约束已核对
        visiting = visiting | {visit_key}

        task_rec = self._task_record(thread_id, run_id, task_id, task_rev)
        if role == "source":
            versions = self.task_versions(thread_id, run_id, task_id)
            latest = max(versions, key=lambda r: r.get("revision", 0))
            if latest.get("revision") != task_rev:
                raise SourceUnqualified(
                    f"来源任务 {task_id} 当前最新版本为 {latest.get('revision')}，"
                    f"来源 revision={task_rev} 已被取代"
                )
            if task_rec.get("status") == "voided":
                raise SourceUnqualified(f"来源任务 {task_id}@{task_rev} 已作废")
            if task_rec.get("status") != "completed":
                raise SourceUnqualified(
                    f"来源任务 {task_id}@{task_rev} 未完成"
                    f"（{task_rec.get('status')}），其成果不能沿用"
                )
            if evidence_id not in (task_rec.get("evidence_ids") or []):
                raise SourceUnqualified(
                    f"来源证据 {evidence_id} 未选入来源任务 "
                    f"{task_id}@{task_rev} 的成果列表"
                )
        elif task_rec.get("status") == "voided":
            raise SourceUnqualified(f"来源任务 {task_id}@{task_rev} 已作废")

        edges = evidence_edges(rec, session_id=self.session_id)
        direct = [e for e in edges if e["kind"] == "direct"]
        inputs = [e for e in edges if e["kind"] == "input"]
        if direct:
            if len(direct) != 1:
                raise SourceUnqualified("沿用记录存在多条 direct_reuse_from 边")
            d = direct[0]
            # 继承输入事实必须与权威来源一致（不删血缘、不伪造），随后在
            # 原来源的 consumer scope 中递归核验，不加入本轮 DAG。
            src = self.resolve_evidence(d["evidence_ref"])
            if _canonical(rec.get("input_result_refs") or []) != _canonical(
                src.get("input_result_refs") or []
            ):
                raise SourceUnqualified(
                    "沿用记录的继承输入与权威来源不一致（伪造/缺失血缘）"
                )
            self.verify_node(
                d["evidence_ref"],
                role="source",
                expected_task_ref=d["source_task_ref"],
                visiting=visiting,
            )
            self._verified_nodes.add(cache_key)
            return
        self.verify_input_edges(
            inputs,
            consumer_task=task_rec,
            consumer_run=run_id,
            consumer_thread=thread_id,
            consumer_task_id=task_id,
            consumer_task_rev=task_rev,
            visiting=visiting,
        )
        self._verified_nodes.add(cache_key)


def _visible_record_matches(
    visible_record: Mapping[str, Any] | None, resolved: Mapping[str, Any]
) -> bool:
    """模型可见候选仅作交叉核对，不顶替权威解析（R2-02）。"""
    if visible_record is None:
        return True
    for field in (
        "run_id",
        "evidence_id",
        "sub_task_id",
        "sub_task_revision",
        "result_ref",
        "output_result_ref",
    ):
        if visible_record.get(field) != resolved.get(field):
            return False
    return True


def verify_reuse_source(
    evidence_ref: Mapping[str, Any],
    visible_record: Mapping[str, Any] | None = None,
    *,
    session_id: str,
    state: Mapping[str, Any],
    archive=None,
    carried: Iterable[Mapping[str, Any]] = (),
    extra_records: Iterable[Mapping[str, Any]] = (),
) -> None:
    """核验一条历史来源的完整资格（R2-01/02）。

    - 按 direct 引用解析**真正的来源原记录**（快照优先），再核验来源资格；
      不再把本轮新登记的目标记录当作来源记录。
    - visible_record（工具可见候选）只做身份交叉核对，不作为权威事实。
    """
    verifier = EvidenceGraphVerifier(
        session_id=session_id,
        state=state,
        archive=archive,
        carried=carried,
        extra_records=([visible_record] if visible_record is not None else []),
    )
    resolved = verifier.resolve_evidence(evidence_ref)
    if not _visible_record_matches(visible_record, resolved):
        raise SourceUnqualified("可见来源记录与权威运行记录身份不符")
    verifier.verify_node(evidence_ref, role="source")


def verify_consumed_edges(
    edges: list[dict[str, Any]],
    *,
    session_id: str,
    state: Mapping[str, Any],
    archive=None,
    carried: Iterable[Mapping[str, Any]] = (),
    consumer_task: Mapping[str, Any],
    consumer_run: str,
    consumer_thread: str,
    consumer_task_id: str,
    consumer_task_rev: Any,
) -> tuple[bool, str | None]:
    """工具/服务侧消费前置核验（R3-01/02）：完整身份解析 + 全部祖先。

    返回 (ok, reason)。由 SQL 占位符解析与 analyze_result 输入解析共用，
    取代各自按裸 evidence_id 的另一套递归。
    """
    verifier = EvidenceGraphVerifier(
        session_id=session_id,
        state=state,
        archive=archive,
        carried=carried,
    )
    try:
        verifier.verify_input_edges(
            edges,
            consumer_task=consumer_task,
            consumer_run=consumer_run,
            consumer_thread=consumer_thread,
            consumer_task_id=consumer_task_id,
            consumer_task_rev=consumer_task_rev,
        )
    except SourceUnqualified as exc:
        return False, exc.reason
    return True, None


def unqualified_reuse_evidence(
    evidence_records: Iterable[Mapping[str, Any]],
    *,
    session_id: str,
    thread_id: str | None = None,
    run_id: str | None = None,
    state: Mapping[str, Any],
    archive=None,
    carried: Iterable[Mapping[str, Any]] = (),
) -> dict[str, str]:
    """批量核验本轮证据的完整证据图；返回 {evidence_id: 原因}。

    由提交门控、Runner 与 completed 登记共享；无最终提交的收尾同样复用
    本任务级核验（R2-02/R2-08）。thread/run 未显式传入时从记录与任务账本
    推断（兼容只读探针/旧状态）；生产入口应显式传入可信 ctx 身份。
    """
    records = list(evidence_records)
    if thread_id is None:
        thread_id = state.get("thread_id") or next(
            (
                t.get("thread_id")
                for t in (state.get("db_sub_tasks") or {}).values()
                if isinstance(t, Mapping) and t.get("thread_id")
            ),
            "",
        )
    if run_id is None:
        run_id = state.get("run_id") or (records[0].get("run_id") if records else "")
    verifier = EvidenceGraphVerifier(
        session_id=session_id,
        state=state,
        archive=archive,
        carried=carried,
    )
    failures: dict[str, str] = {}
    for record in records:
        ref = _ref_of(record, session_id, thread_id or "", run_id or "")
        eid = ref["evidence_id"]
        if not eid:
            continue
        try:
            verifier.verify_node(ref, role="outcome")
        except SourceUnqualified as exc:
            failures[eid] = exc.reason
    return failures
