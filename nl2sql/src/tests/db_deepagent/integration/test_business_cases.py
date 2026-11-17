"""业务任务验收用例 F01–F12（docs/v4/4、doc 5 §18.3；requires_db + requires_llm）

参数化读取 acceptance_cases.yaml；断言＝状态＋答案关键值＋证据 SQL 边界。
参考值为直连 orders 库独立冻结（非 Agent 答案）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

# 业务用例运行于真实单源环境；占位来源仅满足构造绑定（doc 4 §3/§4.7：
# 正式多源验收以登记 source_id 为准，见交付说明）
_TEST_SOURCE_ID = "db_src_1"  # doc 5.1 §1 短 ID（doc 12.2 M-1）

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_llm,
    pytest.mark.requires_db,
]

_MODULE_DIR = Path(__file__).resolve().parents[3] / "modules" / "db_deepagent"
_CASES = yaml.safe_load(
    (_MODULE_DIR / "config" / "acceptance_cases.yaml").read_text(encoding="utf-8")
)


@pytest.fixture(scope="module")
def runtime(db_agent_config):
    """模块级真实运行时：services + postgres 持久化 + agent + runner。"""
    from src.modules.db_deepagent.agent import build_db_agent
    from src.modules.db_deepagent.persistence.archive import QaArchive
    from src.modules.db_deepagent.persistence.checkpoint import build_persistence
    from src.modules.db_deepagent.persistence.platform_pool import (
        build_platform_pool,
        check_platform_tables,
    )
    from src.modules.db_deepagent.runner import DbAgentRunner
    from src.modules.db_deepagent.services import build_services

    cfg = db_agent_config
    services = build_services(cfg, source_id=_TEST_SOURCE_ID)
    pool = build_platform_pool(cfg.platform_db)
    pool.open(wait=False)
    pool.wait()
    missing = check_platform_tables(pool, cfg.platform_db.schema_name)
    assert not missing, f"平台库缺表: {missing}"
    persistence = build_persistence(cfg, platform_pool=pool)
    handle = build_db_agent(
        services,
        session_id="acceptance",
        checkpointer=persistence.checkpointer,
        store=persistence.store,
    )
    runner = DbAgentRunner(
        services=services,
        handle=handle,
        archive=QaArchive(persistence.store),
        persistence=persistence,
    )
    yield {"runner": runner, "workspace": Path(cfg.workspace.root)}
    services.close()


def _evidence_sqls(runtime, session_id: str, request_id: str) -> list[str]:
    """从 P4 本地记录提取本运行全部证据 SQL。"""
    record_path = (
        runtime["workspace"] / "sessions" / session_id / "runs" / f"{request_id}.json"
    )
    if not record_path.exists():
        return []
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    return [e.get("sql") or "" for e in payload.get("evidence") or []]


def _fresh_session(case_id: str) -> str:
    """每轮独立会话：避免多轮验收的消息历史污染后续轮次（F06 根因）。"""
    import uuid

    return f"acc-{case_id.lower()}-{uuid.uuid4().hex[:6]}"


def _run_case(runtime, case_id: str, question: str):
    session_id = _fresh_session(case_id)
    result = runtime["runner"].run(question, session_id)
    sqls = _evidence_sqls(runtime, session_id, result.request_id)
    return result, "\n".join(sqls)


def _assert_f08_counts(runtime, result) -> None:
    """F08 逐客户核对：八月订单数须有结果行支撑（60→0、15→1、81→2）。

    读全部证据引用的结果文件，按证据 SQL 是否含 8 月边界区分“八月计数
    文件”与 top3 文件，并用列元数据定位客户列与计数列后按列取值比对——
    “同一行任意位置含目标数字”会被排名等旁列误满足。计数=0 在 GROUP BY
    结果中天然没有行，按“缺席”核对：top3 行含该客户、计数行缺席、且
    计数 SQL 的客户范围覆盖该客户（IN 列表含之或子查询/JOIN 推导）。
    """
    from src.modules.db_deepagent.services.result_store import ResultStore

    def _col(columns, keywords, exact=()):
        names = [str(c["name"]).lower() for c in columns]
        for i, name in enumerate(names):
            if any(k in name for k in keywords) or name in exact:
                return i
        return None

    def _customer_col(columns):
        # 精确匹配客户 ID 列名：包含式匹配会把 customer_name 误当 ID 列
        names = [str(c["name"]).lower() for c in columns]
        for i, name in enumerate(names):
            if name in ("user_id", "customer_id", "userid"):
                return i
        return None

    def _count_col(columns):
        return _col(columns, ("count", "cnt", "订单数"), exact=("n", "num"))

    results = ResultStore(Path(runtime["workspace"]))
    aug_files: list[tuple[list, list]] = []  # (rows, columns)
    top3_files: list[tuple[list, list]] = []
    aug_sql = ""
    for evidence in result.evidence:
        ref = evidence.result_ref
        if not ref:
            continue
        try:
            page = results.read_rows(ref, session_id=result.session_id, limit=1000)
        except Exception:
            continue
        sql = (evidence.sql or "").replace("\n", " ")
        if "2026-08-01" in sql:  # 八月计数查询的结果
            aug_files.append((page.rows, page.columns))
            aug_sql = aug_sql or sql
        else:
            top3_files.append((page.rows, page.columns))

    def _aug_pair_matches(customer_id, value) -> bool:
        for rows, columns in aug_files:
            ci, ki = _customer_col(columns), _count_col(columns)
            assert ci is not None and ki is not None, (
                f"F08: 八月计数文件列无法定位客户/计数列: "
                f"{[c['name'] for c in columns]}"
            )
            if any(row[ci] == customer_id and row[ki] == value for row in rows):
                return True
        return False

    for customer_id, aug_count in ((60, 0), (15, 1), (81, 2)):
        if _aug_pair_matches(customer_id, aug_count):
            continue  # 按列配对命中（含 COALESCE 风格的显式 0 行）
        if aug_count > 0:
            raise AssertionError(
                f"F08: 八月计数结果中没有 客户{customer_id} 的订单数列为 "
                f"{aug_count} 的行（无证据支撑的结论数值不应通过）"
            )
        # 计数为 0：GROUP BY 不产生行，按“缺席”核对
        in_top3 = any(
            ci is not None and any(row[ci] == customer_id for row in rows)
            for rows, columns in top3_files
            for ci in [_customer_col(columns)]
        )
        absent = all(
            ci is None or all(row[ci] != customer_id for row in rows)
            for rows, columns in aug_files
            for ci in [_customer_col(columns)]
        )
        in_list = re.search(r"IN\s*\(([^()]*)\)", aug_sql or "")
        covered = bool(in_list and str(customer_id) in in_list.group(1)) or bool(
            aug_sql and ("IN (SELECT" in aug_sql.upper() or "JOIN" in aug_sql.upper())
        )
        assert in_top3 and absent and covered, (
            f"F08: 客户{customer_id} 的八月订单数 0 缺少结果支撑"
            f"（需 top3 行含该客户、计数行缺席且计数 SQL 覆盖该客户）"
        )


def _assert_case(case_id: str, spec: dict, result, sql_text: str, runtime=None) -> None:
    assert result.status in spec.get(
        "expect_status", ["completed"]
    ), f"{case_id}: status={result.status}, answer={result.answer[:200]}"
    answer_plain = result.answer.replace(",", "")  # 去千分位后比对
    for needle in spec.get("answer_contains", []):
        assert (
            needle in answer_plain or needle in result.answer
        ), f"{case_id}: 答案缺少 {needle!r}: {result.answer[:200]}"
    any_of = spec.get("answer_contains_any")
    if any_of:
        assert any(
            needle in answer_plain or needle in result.answer for needle in any_of
        ), f"{case_id}: 答案未命中任一 {any_of!r}: {result.answer[:200]}"
    sql_any = spec.get("sql_regex_any")
    if sql_any:
        assert any(
            re.search(p, sql_text, re.IGNORECASE) for p in sql_any
        ), f"{case_id}: 证据 SQL 未命中任一 {sql_any!r}: {sql_text[:300]}"
    for part in spec.get("sql_regex_parts", []):
        assert re.search(
            part, sql_text, re.IGNORECASE
        ), f"{case_id}: 证据 SQL 未匹配 {part}: {sql_text[:300]}"
    pattern = spec.get("sql_regex")
    if pattern:
        assert re.search(
            pattern, sql_text, re.IGNORECASE
        ), f"{case_id}: 证据 SQL 未匹配 {pattern}: {sql_text[:300]}"


@pytest.mark.parametrize("case_id", sorted(_CASES["business_cases"]))
def test_business_case(case_id, runtime, request):
    spec = _CASES["business_cases"][case_id]
    if "question_round1" in spec:  # F12：同会话两轮（两轮内共享，轮间独立）
        session_id = _fresh_session(case_id)
        first = runtime["runner"].run(spec["question_round1"], session_id)
        assert first.status in ("completed", "partial"), first.status
        second = runtime["runner"].run(spec["question_round2"], session_id)
        sqls = _evidence_sqls(runtime, session_id, second.request_id)
        _assert_case(case_id, spec, second, "\n".join(sqls), runtime)
        return
    result, sql_text = _run_case(runtime, case_id, spec["question"])
    _assert_case(case_id, spec, result, sql_text, runtime)
    if "forbid_sql_regex" in spec:  # F10：不得替换/扩大条件
        assert not re.search(
            spec["forbid_sql_regex"], sql_text
        ), f"{case_id}: 疑似替换了不存在的对象: {sql_text[:300]}"
    if "days_expected" in spec:  # F03：逐日行数或天数
        assert (
            str(spec["days_expected"]) in result.answer or "20" in result.answer
        ), f"{case_id}: 天数断言失败: {result.answer[:200]}"
    if case_id == "F08":
        # 逐客户八月订单数以结果文件行为准（见 _assert_f08_counts 说明）
        _assert_f08_counts(runtime, result)
