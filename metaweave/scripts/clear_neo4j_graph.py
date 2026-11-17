#!/usr/bin/env python
"""清空 Neo4j 图数据（不做向下兼容的一次性重置，2026-10-26 定案）。

背景：doc 6/6.1 改造前旧发布写入的子图 source_id 为 UUID 格式，与当前
短 ID（db_src_N）体系不兼容，且旧 full_name/id 唯一约束残留导致多源
同名表发布失败（ConstraintValidationFailed）。定案不做向下兼容：
直接清空全部节点与关系，由新发布重建。

- 默认只读预检（统计节点/边/约束），``--apply`` 才执行删除
- 批量 DETACH DELETE（elementId 批删，默认每批 5000），大图同样适用
- 约束不在此处理：entity_id 唯一约束为当前版本发布所需、保留；
  旧 full_name/id 约束已于 2026-10-26 移除、不再恢复
- 不改变发布语义：每次 rel 发布仍在数据事务内先清理本源（source_id）
  与无归属节点再写入（doc 4 按源替换、doc 23.2 D 组）

用法：
    .venv/bin/python scripts/clear_neo4j_graph.py            # 预检
    .venv/bin/python scripts/clear_neo4j_graph.py --apply    # 清空
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from metaweave.core.loaders.cql_loader import normalize_neo4j_section  # noqa: E402

DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "metadata_config.yaml"
REPORT_ROOT = PROJECT_ROOT / "output" / "maintenance" / "neo4j_clear"
BATCH_SIZE = 5000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_manager(config_path: Path):
    """与 migrate_neo4j_source_identity 相同的连接方式（config → manager）。"""
    from services.config_loader import ConfigLoader
    from services.db.neo4j_connection import Neo4jConnectionManager

    config = ConfigLoader(str(config_path)).load()
    section = (config.get("loaders") or {}).get("cql_loader") or {}
    neo4j_cfg = normalize_neo4j_section((section or {}).get("neo4j") or {})
    manager = Neo4jConnectionManager(neo4j_cfg)
    manager.initialize()
    return manager


def _counts(session) -> dict:
    nodes_by_label = [
        dict(r) for r in session.run(
            "MATCH (n) UNWIND labels(n) AS label "
            "RETURN label, count(*) AS count ORDER BY count DESC"
        )
    ]
    rels_by_type = [
        dict(r) for r in session.run(
            "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY count DESC"
        )
    ]
    total_nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    total_rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    return {
        "total_nodes": total_nodes,
        "total_relationships": total_rels,
        "nodes_by_label": nodes_by_label,
        "relationships_by_type": rels_by_type,
    }


def _constraints(session) -> list[dict]:
    return [
        {"name": r["name"], "type": r["type"], "properties": list(r["properties"] or [])}
        for r in session.run("SHOW CONSTRAINTS")
    ]


def write_report(exec_id: str, report: dict) -> Path:
    path = REPORT_ROOT / exec_id / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清空 Neo4j 图数据（保留 entity_id 约束）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="metadata_config.yaml 路径")
    parser.add_argument("--apply", action="store_true", help="执行清空（缺省只读预检）")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="批量删除的批大小")
    args = parser.parse_args(argv)

    exec_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    report: dict = {
        "mode": "apply" if args.apply else "precheck",
        "exec_id": exec_id,
        "started_at": _utc_now(),
        "config": str(Path(args.config).resolve()),
    }

    manager = _load_manager(Path(args.config))
    try:
        with manager.get_session() as session:
            before = _counts(session)
            report["before"] = before
            report["constraints_kept"] = _constraints(session)
            print(f"== {'执行清空' if args.apply else '预检（只读）'} ==")
            print(f"节点 {before['total_nodes']} / 关系 {before['total_relationships']}")
            for row in before["nodes_by_label"]:
                print(f"  :{row['label']} x {row['count']}")

            if not args.apply:
                print("预检完成；加 --apply 执行清空。")
                return 0

            deleted_nodes = 0
            while True:
                ids = [
                    r["eid"]
                    for r in session.run(
                        "MATCH (n) RETURN elementId(n) AS eid LIMIT $batch",
                        batch=args.batch_size,
                    )
                ]
                if not ids:
                    break
                session.run(
                    "UNWIND $ids AS eid MATCH (n) WHERE elementId(n) = eid "
                    "DETACH DELETE n",
                    ids=ids,
                )
                deleted_nodes += len(ids)

            after = _counts(session)
            report["after"] = after
            report["deleted_nodes"] = deleted_nodes
            if after["total_nodes"] != 0 or after["total_relationships"] != 0:
                print(f"清空未完成：剩余节点 {after['total_nodes']} / 关系 {after['total_relationships']}")
                report["ok"] = False
                return 1
            print(f"已删除 {deleted_nodes} 个节点；图已清空。")
            print("保留约束（发布所需）:",
                  [c["name"] for c in report["constraints_kept"]])
            report["ok"] = True
            return 0
    finally:
        report["finished_at"] = _utc_now()
        path = write_report(exec_id, report)
        print(f"报告: {path}")
        try:
            manager.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不影响结果汇报
            pass


if __name__ == "__main__":
    sys.exit(main())
