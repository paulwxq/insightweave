"""一次性迁移：Neo4j 旧单源图 → 多源实体键身份（doc 4 §4.8.1）

默认只读预检；显式 ``--apply`` 才执行迁移。本脚本不写平台库、不写 Milvus、
不触发重新发布，也不自行停止 Neo4j 或恢复备份（离线备份由维护执行者在
Neo4j 宿主机用 ``neo4j-admin database dump`` 完成，成功后再运行本脚本）。

用法::

    # 预检（只读）
    .venv/bin/python scripts/migrate_neo4j_source_identity.py

    # 执行迁移（要求先完成离线备份并申报归档位置）
    .venv/bin/python scripts/migrate_neo4j_source_identity.py --apply \\
        --backup-archive /backup/neo4j-YYYYMMDD.dump

执行内容（apply）：
1. 再次预检（冲突即中止，保持停机）；
2. 单一数据事务内回填：Table.entity_id；Column 的 entity_id/source_id/
   publish_id（取唯一所属 Table）；HAS_COLUMN/JOIN_ON 的 source_id/
   publish_id（沿用端点 Table 的有效发布归属），并在事务内核验；
3. 约束替换（每条独立事务）：删除 table_id/table_full_name/column_id/
   column_full_name，创建 table_entity_id/column_entity_id；
4. 核验实际约束定义与按源计数（不只凭脚本未报错判断成功）。

报告：``output/maintenance/neo4j_source_identity/<执行编号>/report.json``；
失败也尽量留档并以非零码退出。报告不含密码或完整连接串。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from metaweave.core.cql_generator.keyspace import (  # noqa: E402
    build_entity_id,
    db_prefix_problem,
    identifier_problem,
)
from metaweave.core.loaders.cql_loader import normalize_neo4j_section  # noqa: E402
from metaweave.utils.source_id import normalize_source_id  # noqa: E402

DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "metadata_config.yaml"
REPORT_ROOT = PROJECT_ROOT / "output" / "maintenance" / "neo4j_source_identity"

OLD_CONSTRAINT_NAMES = (
    "table_id",
    "table_full_name",
    "column_id",
    "column_full_name",
)
NEW_CONSTRAINT_NAMES = ("table_entity_id", "column_entity_id")

# 预检只读查询
_Q_TABLES = """
MATCH (t:Table)
RETURN t.id AS id, t.full_name AS full_name, t.source_id AS source_id,
       t.publish_id AS publish_id, t.entity_id AS entity_id
"""
_Q_COLUMNS = """
MATCH (c:Column)
RETURN c.id AS id, c.full_name AS full_name, c.entity_id AS entity_id,
       c.source_id AS source_id
"""
_Q_HAS_COLUMN = """
MATCH (t:Table)-[h:HAS_COLUMN]->(c:Column)
RETURN t.full_name AS table_full_name, c.full_name AS column_full_name,
       t.source_id AS table_source_id, t.publish_id AS table_publish_id,
       h.source_id AS edge_source_id, h.publish_id AS edge_publish_id
"""
_Q_JOIN_ON = """
MATCH (a:Table)-[r:JOIN_ON]->(b:Table)
RETURN a.full_name AS src_full_name, b.full_name AS dst_full_name,
       a.source_id AS src_source_id, b.source_id AS dst_source_id,
       a.publish_id AS src_publish_id, b.publish_id as dst_publish_id,
       r.relationship_id AS relationship_id,
       r.source_id AS edge_source_id, r.publish_id AS edge_publish_id
"""
_Q_CONSTRAINTS = "SHOW CONSTRAINTS"
_Q_SOURCE_COUNTS = """
MATCH (n)
WHERE (n:Table OR n:Column) AND n.source_id IS NOT NULL
RETURN n.source_id AS source_id, labels(n)[0] AS label, count(n) AS count
ORDER BY source_id, label
"""
_Q_UNATTRIBUTED = (
    "MATCH (n) WHERE (n:Table OR n:Column) AND (n.source_id IS NULL "
    "OR n.entity_id IS NULL) RETURN labels(n)[0] AS label, count(n) AS count"
)
_Q_EDGE_UNATTRIBUTED = """
MATCH ()-[r:HAS_COLUMN|JOIN_ON]->()
WHERE r.source_id IS NULL
RETURN type(r) AS type, count(r) AS count
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_manager(config_path: Path):
    """从 metadata_config.yaml 读取 Neo4j 连接并建立连接管理器。"""
    from services.config_loader import ConfigLoader
    from services.db.neo4j_connection import Neo4jConnectionManager

    config = ConfigLoader(str(config_path)).load()
    section = (config.get("loaders") or {}).get("cql_loader") or {}
    neo4j_cfg = normalize_neo4j_section((section or {}).get("neo4j") or {})
    manager = Neo4jConnectionManager(neo4j_cfg)
    manager.initialize()
    return manager, neo4j_cfg


def _read_rows(session, query: str) -> list[dict]:
    return [dict(record) for record in session.run(query)]


def _parse_logical_id(node_id: object, full_name: object, kind: str) -> tuple:
    """校验节点逻辑 id 与 full_name 归一一致（doc 4 §4.2 键空间）。

    返回 (问题列表, (db, schema, name))；id 必须是 ``db.schema[.table[.col]]``
    且与 full_name 拼接一致。
    """
    problems: list[str] = []
    if not isinstance(node_id, str) or not isinstance(full_name, str):
        return [f"{kind} id/full_name 不是字符串"], (None, None, None)
    parts = node_id.split(".")
    if len(parts) < 3:
        return (
            [f"{kind} {node_id!r} 逻辑 id 不是 db.schema… 三段以上键"],
            (None, None, None),
        )
    db = parts[0]
    db_problem = db_prefix_problem(db)
    if db_problem:
        problems.append(f"{kind} {node_id!r} db 前缀非法: {db_problem}")
        return problems, (None, None, None)
    rest = parts[1:]
    for part in rest:
        problem = identifier_problem(part, kind="标识符段")
        if problem:
            problems.append(problem)
    if problems:
        return problems, (db, None, None)
    if ".".join(rest) != full_name:
        problems.append(f"{kind} {node_id!r} 与 full_name {full_name!r} 拼接不一致")
    return problems, (db, rest[0], rest[-1])


def collect_precheck(manager) -> dict:
    """只读预检：结构、归属、实体键冲突、约束与按源分布。"""
    with manager.get_session() as session:
        tables = _read_rows(session, _Q_TABLES)
        columns = _read_rows(session, _Q_COLUMNS)
        has_column = _read_rows(session, _Q_HAS_COLUMN)
        join_on = _read_rows(session, _Q_JOIN_ON)
        constraints = _read_rows(session, _Q_CONSTRAINTS)
        source_counts = _read_rows(session, _Q_SOURCE_COUNTS)
        unattributed = _read_rows(session, _Q_UNATTRIBUTED)
        edge_unattributed = _read_rows(session, _Q_EDGE_UNATTRIBUTED)

    errors: list[str] = []
    warnings: list[str] = []

    # --- Table：source_id/publish_id 必须已有（doc 23.2 D3 写入）；UUID 合法；
    #     逻辑键归一；已有 entity_id 不冲突 ---
    table_meta: dict[str, dict] = {}
    for row in tables:
        full_name = row.get("full_name")
        source_id = row.get("source_id")
        publish_id = row.get("publish_id")
        if not source_id:
            errors.append(f"Table {full_name!r} 缺少 source_id（无法回填归属）")
            continue
        try:
            normalize_source_id(source_id)
        except ValueError:
            errors.append(f"Table {full_name!r} source_id 不是合法 UUID")
            continue
        if not publish_id:
            errors.append(f"Table {full_name!r} 缺少 publish_id（无法沿用发布归属）")
        problems, _ = _parse_logical_id(row.get("id"), full_name, f"Table {source_id}")
        errors.extend(problems)
        if isinstance(row.get("id"), str):
            expected_entity = build_entity_id(str(source_id), row["id"])
            if row.get("entity_id") not in (None, expected_entity):
                errors.append(
                    f"Table {full_name!r} 已有 entity_id 与期望冲突: "
                    f"{row.get('entity_id')!r} != {expected_entity!r}"
                )
        table_meta[full_name] = {
            "source_id": str(source_id),
            "publish_id": str(publish_id) if publish_id else None,
            "entity_id": expected_entity,
        }

    # --- Column：唯一所属 Table；逻辑键归一；entity_id 冲突 ---
    column_owner: dict[str, dict] = {}
    owner_counts: dict[str, list[str]] = {}
    for row in has_column:
        owner_counts.setdefault(row.get("column_full_name"), []).append(
            row.get("table_full_name")
        )
    for column_full_name, owners in owner_counts.items():
        if len(owners) > 1:
            errors.append(
                f"Column {column_full_name!r} 有多个所属 Table: {sorted(set(owners))}"
            )
    for row in columns:
        full_name = row.get("full_name")
        owners = owner_counts.get(full_name) or []
        if not owners:
            errors.append(
                f"Column {full_name!r} 没有所属 Table（孤儿列，无法回填归属）"
            )
            continue
        owner = table_meta.get(owners[0])
        if owner is None:
            errors.append(f"Column {full_name!r} 所属 Table {owners[0]!r} 缺少有效归属")
            continue
        problems, _ = _parse_logical_id(
            row.get("id"), full_name, f"Column {owner['source_id']}"
        )
        errors.extend(problems)
        if isinstance(row.get("id"), str):
            expected_entity = build_entity_id(owner["source_id"], row["id"])
            if row.get("entity_id") not in (None, expected_entity):
                errors.append(
                    f"Column {full_name!r} 已有 entity_id 与期望冲突: "
                    f"{row.get('entity_id')!r} != {expected_entity!r}"
                )
        if row.get("source_id") not in (None, owner["source_id"]):
            errors.append(
                f"Column {full_name!r} 已有 source_id 与所属 Table 冲突: "
                f"{row.get('source_id')!r} != {owner['source_id']!r}"
            )
        column_owner[full_name] = owner

    # --- HAS_COLUMN 边：端点可解析；已有归属不冲突 ---
    for row in has_column:
        table_full_name = row.get("table_full_name")
        column_full_name = row.get("column_full_name")
        if table_full_name not in table_meta:
            errors.append(f"HAS_COLUMN 端点 Table 缺失/无归属: {table_full_name!r}")
            continue
        if column_full_name not in column_owner:
            continue  # 已由列检查报错
        edge_source = row.get("edge_source_id")
        if (
            edge_source is not None
            and edge_source != table_meta[table_full_name]["source_id"]
        ):
            errors.append(
                f"HAS_COLUMN {table_full_name}->{column_full_name} 已有 source_id "
                f"与端点冲突: {edge_source!r}"
            )

    # --- JOIN_ON 边：端点同源；publish_id 一致（沿用归属需无歧义）；已有归属不冲突 ---
    for row in join_on:
        src, dst = row.get("src_full_name"), row.get("dst_full_name")
        src_meta = table_meta.get(src)
        dst_meta = table_meta.get(dst)
        if src_meta is None or dst_meta is None:
            errors.append(f"JOIN_ON 端点 Table 缺失/无归属: {src!r} -> {dst!r}")
            continue
        if src_meta["source_id"] != dst_meta["source_id"]:
            errors.append(
                f"JOIN_ON {src!r} -> {dst!r} 端点跨源: "
                f"{src_meta['source_id']} vs {dst_meta['source_id']}"
            )
            continue
        if src_meta["publish_id"] != dst_meta["publish_id"]:
            errors.append(
                f"JOIN_ON {src!r} -> {dst!r} 端点 publish_id 不一致，无法确定沿用归属: "
                f"{src_meta['publish_id']} vs {dst_meta['publish_id']}"
            )
        edge_source = row.get("edge_source_id")
        if edge_source is not None and edge_source != src_meta["source_id"]:
            errors.append(
                f"JOIN_ON {src!r} -> {dst!r} 已有 source_id 与端点冲突: "
                f"{edge_source!r}"
            )

    # --- 约束现状（回填不依赖；apply 后核对） ---
    constraint_names = sorted(c.get("name") or "" for c in constraints if c.get("name"))
    for name in NEW_CONSTRAINT_NAMES:
        if name in constraint_names:
            warnings.append(f"新约束已存在（IF NOT EXISTS 将跳过）: {name}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "tables": len(tables),
            "columns": len(columns),
            "has_column": len(has_column),
            "join_on": len(join_on),
        },
        "source_distribution": [
            {
                "source_id": row["source_id"],
                "label": row["label"],
                "count": row["count"],
            }
            for row in source_counts
        ],
        "unattributed_nodes": [
            {"label": row["label"], "count": row["count"]} for row in unattributed
        ],
        "unattributed_edges": [
            {"type": row["type"], "count": row["count"]} for row in edge_unattributed
        ],
        "constraints_before": [
            {
                "name": c.get("name"),
                "type": c.get("type"),
                "entity_type": c.get("entityType"),
                "properties": c.get("properties"),
            }
            for c in constraints
        ],
        "_table_meta": table_meta,
        "_column_owner": column_owner,
        "_join_on": join_on,
        "_has_column": has_column,
    }


def apply_backfill_transaction(manager, precheck: dict) -> dict:
    """单一数据事务回填归属与实体键，事务内核验（doc 4 §4.8.1）。"""
    table_meta = precheck["_table_meta"]
    column_owner = precheck["_column_owner"]

    table_rows = [
        {"full_name": full_name, "entity_id": meta["entity_id"]}
        for full_name, meta in table_meta.items()
    ]
    # 列回填行需要列逻辑 id（entity_id = source_id:id），从图上重新读取
    with manager.get_session() as session:
        columns = _read_rows(session, _Q_COLUMNS)
    column_rows = []
    for row in columns:
        owner = column_owner.get(row.get("full_name"))
        if owner is None:
            continue
        column_rows.append(
            {
                "full_name": row["full_name"],
                "entity_id": build_entity_id(owner["source_id"], row["id"]),
                "source_id": owner["source_id"],
                "publish_id": owner["publish_id"],
            }
        )
    has_column_rows = []
    for row in precheck["_has_column"]:
        meta = table_meta.get(row.get("table_full_name"))
        if meta is None or row.get("column_full_name") not in column_owner:
            continue
        has_column_rows.append(
            {
                "table_full_name": row["table_full_name"],
                "column_full_name": row["column_full_name"],
                "source_id": meta["source_id"],
                "publish_id": meta["publish_id"],
            }
        )
    join_on_rows = []
    for row in precheck["_join_on"]:
        meta = table_meta.get(row.get("src_full_name"))
        if meta is None or row.get("dst_full_name") not in table_meta:
            continue
        join_on_rows.append(
            {
                "src_full_name": row["src_full_name"],
                "dst_full_name": row["dst_full_name"],
                "relationship_id": row["relationship_id"],
                "source_id": meta["source_id"],
                "publish_id": meta["publish_id"],
            }
        )

    def work(tx):
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (t:Table {full_name: row.full_name})
            SET t.entity_id = row.entity_id
            """,
            rows=table_rows,
        ).consume()
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (c:Column {full_name: row.full_name})
            SET c.entity_id = row.entity_id,
                c.source_id = row.source_id,
                c.publish_id = row.publish_id
            """,
            rows=column_rows,
        ).consume()
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (t:Table {full_name: row.table_full_name})-[h:HAS_COLUMN]->(c:Column)
            SET h.source_id = row.source_id,
                h.publish_id = row.publish_id
            """,
            rows=has_column_rows,
        ).consume()
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (a:Table {full_name: row.src_full_name})
                  -[r:JOIN_ON {relationship_id: row.relationship_id}]->
                  (b:Table {full_name: row.dst_full_name})
            SET r.source_id = row.source_id,
                r.publish_id = row.publish_id
            """,
            rows=join_on_rows,
        ).consume()
        # 事务内核验：回填后不允许存在无 entity_id/source_id 的节点与边。
        # 第二段必须 OPTIONAL MATCH——全部回填成功时普通 MATCH 匹配零行，
        # 会把整条查询的返回行吞掉（single() 为 None），核验被误判为失败
        # 并回滚已成功的回填；OPTIONAL 在零匹配时保留行、count 记 0
        record = tx.run(
            """
            MATCH (n) WHERE (n:Table OR n:Column) AND (n.entity_id IS NULL OR n.source_id IS NULL)
            WITH count(n) AS nodes_missing
            OPTIONAL MATCH ()-[r:HAS_COLUMN|JOIN_ON]->() WHERE r.source_id IS NULL
            RETURN nodes_missing, count(r) AS edges_missing
            """
        ).single()
        nodes_missing = record["nodes_missing"] if record else -1
        edges_missing = record["edges_missing"] if record else -1
        if nodes_missing or edges_missing:
            raise RuntimeError(
                f"回填核验失败（事务将回滚）: 无 entity_id/source_id 节点 "
                f"{nodes_missing} 个 / 边 {edges_missing} 条"
            )
        return {
            "tables": len(table_rows),
            "columns": len(column_rows),
            "has_column": len(has_column_rows),
            "join_on": len(join_on_rows),
        }

    with manager.get_session() as session:
        return session.execute_write(work)


def apply_constraint_migration(manager) -> list[dict]:
    """约束替换：旧四删、新两建（每条独立事务，幂等）。"""
    statements = [
        *(f"DROP CONSTRAINT {name} IF EXISTS" for name in OLD_CONSTRAINT_NAMES),
        (
            "CREATE CONSTRAINT table_entity_id IF NOT EXISTS "
            "FOR (t:Table) REQUIRE t.entity_id IS UNIQUE"
        ),
        (
            "CREATE CONSTRAINT column_entity_id IF NOT EXISTS "
            "FOR (c:Column) REQUIRE c.entity_id IS UNIQUE"
        ),
    ]
    applied = []
    for stmt in statements:
        manager.execute_write_transaction(stmt)
        applied.append(stmt)
    return applied


def verify_after_apply(manager) -> dict:
    """迁移后核验：实际约束定义（含节点标签）、按源计数、无未归属节点/边。"""
    with manager.get_session() as session:
        constraints = _read_rows(session, _Q_CONSTRAINTS)
        source_counts = _read_rows(session, _Q_SOURCE_COUNTS)
        unattributed = _read_rows(session, _Q_UNATTRIBUTED)
        edge_unattributed = _read_rows(session, _Q_EDGE_UNATTRIBUTED)
    names = {c.get("name"): c for c in constraints}
    expected_labels = {
        "table_entity_id": ["Table"],
        "column_entity_id": ["Column"],
    }
    problems = []
    for name in OLD_CONSTRAINT_NAMES:
        if name in names:
            problems.append(f"旧约束仍存在: {name}")
    for name, labels in expected_labels.items():
        constraint = names.get(name)
        if constraint is None:
            problems.append(f"新约束缺失: {name}")
            continue
        # 完整定义核对：节点唯一约束 + 正确标签 + entity_id——同名但
        # 约束了错误标签/类型时明确报错，不凭名称与属性判定通过
        if (
            constraint.get("type") != "UNIQUENESS"
            or constraint.get("entityType") != "NODE"
            or constraint.get("labelsOrTypes") != labels
            or constraint.get("properties") != ["entity_id"]
        ):
            problems.append(
                f"新约束定义异常: {name} -> {constraint.get('type')} "
                f"{constraint.get('entityType')} {constraint.get('labelsOrTypes')} "
                f"{constraint.get('properties')}"
            )
    for row in unattributed:
        problems.append(f"仍有无 entity_id/source_id 节点: {row}")
    for row in edge_unattributed:
        problems.append(f"仍有无 source_id 边: {row}")
    return {
        "ok": not problems,
        "problems": problems,
        "constraints_after": [
            {
                "name": c.get("name"),
                "type": c.get("type"),
                "entity_type": c.get("entityType"),
                "properties": c.get("properties"),
            }
            for c in constraints
        ],
        "source_distribution_after": [
            {
                "source_id": row["source_id"],
                "label": row["label"],
                "count": row["count"],
            }
            for row in source_counts
        ],
    }


def write_report(exec_id: str, report: dict) -> Path:
    report_dir = REPORT_ROOT / exec_id
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "report.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Neo4j source identity 一次性迁移（doc 4 §4.8.1）"
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="metadata_config.yaml 路径（默认 configs/metadata_config.yaml）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行迁移（缺省只读预检）",
    )
    parser.add_argument(
        "--backup-archive",
        default=None,
        help="已完成的 neo4j-admin 离线备份归档位置（--apply 必填，仅记录到报告）",
    )
    args = parser.parse_args(argv)

    exec_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    report: dict = {
        "mode": "apply" if args.apply else "precheck",
        "exec_id": exec_id,
        "started_at": _utc_now(),
        "config": str(Path(args.config).resolve()),
        "stages": [],
    }
    exit_code = 0

    if args.apply and not args.backup_archive:
        print(
            "--apply 需要 --backup-archive（先完成离线备份并申报归档位置）",
            file=sys.stderr,
        )
        report["stages"].append(
            {"stage": "preflight", "ok": False, "error": "缺少 --backup-archive"}
        )
        report["finished_at"] = _utc_now()
        write_report(exec_id, report)
        return 2

    try:
        manager, neo4j_cfg = _load_manager(Path(args.config))
        # 不记录密码；URI 已经由共享边界规范化
        report["target"] = {
            "uri": neo4j_cfg["uri"],
            "database": neo4j_cfg["database"],
        }
        report["backup_archive"] = args.backup_archive

        # 阶段 1：预检（apply 时再次执行）
        print("== 阶段 1/3 预检 ==")
        precheck = collect_precheck(manager)
        report["stages"].append(
            {
                "stage": "precheck",
                "ok": precheck["ok"],
                "errors": precheck["errors"],
                "warnings": precheck["warnings"],
                "counts": precheck["counts"],
                "source_distribution": precheck["source_distribution"],
                "unattributed_nodes": precheck["unattributed_nodes"],
                "constraints_before": precheck["constraints_before"],
            }
        )
        print(
            f"节点/边: {precheck['counts']}；错误 {len(precheck['errors'])} 项，"
            f"告警 {len(precheck['warnings'])} 项"
        )
        for error in precheck["errors"][:20]:
            print(f"  [错误] {error}")
        if not precheck["ok"]:
            exit_code = 1
            report["finished_at"] = _utc_now()
            path = write_report(exec_id, report)
            print(f"预检未通过，未执行任何写操作；报告: {path}")
            return exit_code
        if not args.apply:
            report["finished_at"] = _utc_now()
            path = write_report(exec_id, report)
            print(f"预检通过（只读模式，未执行迁移）；报告: {path}")
            return 0

        # 阶段 2：回填（单一数据事务）
        print("== 阶段 2/3 归属与实体键回填 ==")
        backfilled = apply_backfill_transaction(manager, precheck)
        report["stages"].append({"stage": "backfill", "ok": True, **backfilled})
        print(f"回填完成: {backfilled}")

        # 阶段 3：约束替换（每条独立事务）
        print("== 阶段 3/3 约束替换 ==")
        statements = apply_constraint_migration(manager)
        report["stages"].append(
            {"stage": "constraints", "ok": True, "statements": statements}
        )

        # 阶段 4：迁移后核验
        print("== 迁移后核验 ==")
        verification = verify_after_apply(manager)
        report["stages"].append(
            {
                "stage": "verify",
                "ok": verification["ok"],
                "problems": verification["problems"],
                "constraints_after": verification["constraints_after"],
                "source_distribution_after": verification["source_distribution_after"],
            }
        )
        if not verification["ok"]:
            exit_code = 1
            print("迁移后核验失败（保持停机，修复或恢复备份后重试）：")
            for problem in verification["problems"]:
                print(f"  [问题] {problem}")
        else:
            print("迁移完成并通过核验。")
        manager.close()
    except Exception as exc:  # noqa: BLE001（失败留档 + 非零退出）
        exit_code = 1
        report["stages"].append(
            {"stage": "aborted", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
        print(f"迁移失败: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        report["finished_at"] = _utc_now()
        try:
            path = write_report(exec_id, report)
            print(f"报告: {path}")
        except OSError as exc:
            print(f"报告写入失败: {exc}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
