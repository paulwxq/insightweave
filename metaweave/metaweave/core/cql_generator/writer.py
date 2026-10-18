"""Step 4 Cypher 文件写入器

生成符合 v3.2 规范的 Neo4j Cypher 脚本文件。

doc 4（多数据源知识隔离）改造：
- Table/Column 按 ``entity_id = source_id:逻辑id`` MERGE，保留原业务 id 与
  full_name 属性；source_id/publish_id 由 provenance 服务端上下文提供并写入
  全部节点与边；
- HAS_COLUMN/JOIN_ON 端点按实体键定位；JOIN_ON 保留按 relationship_id
  MERGE 的端点间业务标识；
- 约束脚本只生成 Table.entity_id / Column.entity_id 唯一约束（完整与分步
  脚本同批更新，不再生成旧 id/full_name 全局唯一约束）；
- 头部写 ``// source_id:`` 行，加载器据此与后端传入的 source_id 核对，
  防止加载错源脚本。
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

from metaweave.core.cql_generator.keyspace import build_entity_id
from metaweave.core.cql_generator.models import (
    TableNode,
    ColumnNode,
    HASColumnRelation,
    JOINOnRelation,
    RelationshipFilterStats,
)
from metaweave.utils.source_id import normalize_source_id
from metaweave.utils.time_utils import utc_isoformat

logger = logging.getLogger("metaweave.cql_generator.writer")


class CypherWriter:
    """Cypher 文件写入器

    负责生成 Neo4j Cypher 脚本文件（.cypher），确保幂等性。
    """

    def __init__(self, output_dir: Path):
        """初始化写入器

        Args:
            output_dir: 输出目录（./output/cql/）
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"初始化 Cypher 写入器，输出目录: {self.output_dir}")

    @staticmethod
    def _resolve_database_name(
        tables: List[TableNode], columns: List[ColumnNode]
    ) -> str:
        candidates = {
            value
            for value in [*(t.database for t in tables), *(c.database for c in columns)]
            if value
        }
        if not candidates:
            return "unknown"
        if len(candidates) > 1:
            raise ValueError(f"检测到多个 database: {sorted(candidates)}")
        return next(iter(candidates))

    @staticmethod
    def _normalize_provenance(
        provenance: Optional[Tuple],
    ) -> Tuple[str, str]:
        """校验并规范化 (source_id, publish_id) 溯源对。

        doc 4 §4.2：实体身份按源构造，provenance 为必填——缺失即配置错误。
        """
        if provenance is None:
            raise ValueError(
                "CQL 生成需要 provenance=(source_id, publish_id)（受管发布链路"
                "注入）；实体键身份依赖 source_id，无法省略"
            )
        if not isinstance(provenance, tuple) or len(provenance) != 2:
            raise ValueError("provenance 必须是 (source_id, publish_id) 二元组")
        source_id, publish_id = provenance
        try:
            source_str = str(normalize_source_id(source_id))
            publish_str = str(normalize_source_id(publish_id))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"provenance 校验失败: {exc}") from exc
        return source_str, publish_str

    @staticmethod
    def _build_entity_maps(
        source_id: str,
        tables: List[TableNode],
        columns: List[ColumnNode],
        has_column_rels: List[HASColumnRelation],
        join_on_rels: List[JOINOnRelation],
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """构造 full_name → entity_id 映射，并核验关系端点可解析。

        端点缺失说明画像与关系产物不一致，直接拒绝生成（不静默丢边）。
        """
        table_entity: Dict[str, str] = {}
        for table in tables:
            if table.full_name in table_entity:
                raise ValueError(f"重复的表 full_name: {table.full_name}")
            table_entity[table.full_name] = build_entity_id(source_id, table.id)
        column_entity: Dict[str, str] = {}
        for column in columns:
            if column.full_name in column_entity:
                raise ValueError(f"重复的列 full_name: {column.full_name}")
            column_entity[column.full_name] = build_entity_id(source_id, column.id)

        missing: List[str] = []
        for rel in has_column_rels:
            if rel.table_full_name not in table_entity:
                missing.append(f"HAS_COLUMN 表端点 {rel.table_full_name}")
            if rel.column_full_name not in column_entity:
                missing.append(f"HAS_COLUMN 列端点 {rel.column_full_name}")
        for rel in join_on_rels:
            for endpoint in (rel.src_full_name, rel.dst_full_name):
                if endpoint not in table_entity:
                    missing.append(f"JOIN_ON 表端点 {endpoint}")
        if missing:
            raise ValueError(
                "关系端点无法在画像对象中定位，拒绝生成: " + "; ".join(missing)
            )
        return table_entity, column_entity

    def write_all(
        self,
        tables: List[TableNode],
        columns: List[ColumnNode],
        has_column_rels: List[HASColumnRelation],
        join_on_rels: List[JOINOnRelation],
        provenance: Optional[tuple] = None,
    ) -> List[str]:
        """写入 Cypher 文件（默认 global 模式，生成单个完整文件）

        Args:
            tables: Table 节点列表
            columns: Column 节点列表
            has_column_rels: HAS_COLUMN 关系列表
            join_on_rels: JOIN_ON 关系列表
            provenance: (source_id, publish_id) 溯源对——**必填**（doc 4 §4.2：
                实体键身份依赖 source_id）；Table/Column/边统一注入归属属性

        Returns:
            生成的文件路径列表
        """
        source_id, publish_id = self._normalize_provenance(provenance)

        output_files = []

        logger.info("开始生成 Cypher 文件 (global 模式)...")

        database_name = self._resolve_database_name(tables, columns)

        # 生成单个完整的 import_all.{db}.cypher 文件
        import_all_file = self._write_import_all(
            tables,
            columns,
            has_column_rels,
            join_on_rels,
            database_name=database_name,
            source_id=source_id,
            publish_id=publish_id,
        )
        output_files.append(str(import_all_file))

        logger.info(f"Cypher 文件生成完成: {import_all_file.name}")
        return output_files

    def _write_constraints(self) -> Path:
        """生成 01_constraints.cypher（分步约束脚本，doc 4 §4.3）

        仅生成实体键唯一约束；旧 id/full_name 全局唯一约束已由一次性迁移
        显式删除，此处不得重新创建。
        """
        output_file = self.output_dir / "01_constraints.cypher"

        content = """// 01_constraints.cypher
// 创建唯一约束（幂等；doc 4：仅实体键约束，旧 id/full_name 约束由迁移移除）

	CREATE CONSTRAINT table_entity_id IF NOT EXISTS FOR (t:Table) REQUIRE t.entity_id IS UNIQUE;
	CREATE CONSTRAINT column_entity_id IF NOT EXISTS FOR (c:Column) REQUIRE c.entity_id IS UNIQUE;
"""

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"生成约束文件: {output_file}")
        return output_file

    def _write_import_all(
        self,
        tables: List[TableNode],
        columns: List[ColumnNode],
        has_column_rels: List[HASColumnRelation],
        join_on_rels: List[JOINOnRelation],
        database_name: str,
        source_id: str,
        publish_id: str,
    ) -> Path:
        """生成 import_all.{db}.cypher（完整的 global 模式 CQL 脚本）"""
        output_file = self.output_dir / f"import_all.{database_name}.cypher"

        timestamp = utc_isoformat(datetime.now(timezone.utc))

        table_entity, column_entity = self._build_entity_maps(
            source_id, tables, columns, has_column_rels, join_on_rels
        )

        # 转换为 Cypher 参数格式（实体键与归属由本函数从 provenance 注入）
        tables_data = [t.to_cypher_dict() for t in tables]
        for row, table in zip(tables_data, tables):
            row["entity_id"] = table_entity[table.full_name]
            row["source_id"] = source_id
            row["publish_id"] = publish_id
        columns_data = [c.to_cypher_dict() for c in columns]
        for row, column in zip(columns_data, columns):
            row["entity_id"] = column_entity[column.full_name]
            row["source_id"] = source_id
            row["publish_id"] = publish_id
        has_column_data = [
            {
                "table_entity_id": table_entity[rel.table_full_name],
                "column_entity_id": column_entity[rel.column_full_name],
                "source_id": source_id,
                "publish_id": publish_id,
            }
            for rel in has_column_rels
        ]
        join_on_data = [r.to_cypher_dict() for r in join_on_rels]
        for i, rel in enumerate(join_on_rels):
            join_on_data[i]["src_entity_id"] = table_entity[rel.src_full_name]
            join_on_data[i]["dst_entity_id"] = table_entity[rel.dst_full_name]
            join_on_data[i]["source_id"] = source_id
            join_on_data[i]["publish_id"] = publish_id

        tables_json = json.dumps(tables_data, ensure_ascii=False, indent=2)
        columns_json = json.dumps(columns_data, ensure_ascii=False, indent=2)
        has_column_json = json.dumps(has_column_data, ensure_ascii=False, indent=2)
        join_on_json = json.dumps(join_on_data, ensure_ascii=False, indent=2)

        content = f"""// import_all.{database_name}.cypher
// Neo4j 元数据导入脚本（global 模式，包含所有表和关系）
// 生成时间: {timestamp}
// source_id: {source_id}
// 统计: {len(tables)} 张表, {len(columns)} 个列, {len(join_on_rels)} 个关系

// =====================================================================
// 1. 创建唯一约束
// =====================================================================

	CREATE CONSTRAINT table_entity_id IF NOT EXISTS FOR (t:Table) REQUIRE t.entity_id IS UNIQUE;
	CREATE CONSTRAINT column_entity_id IF NOT EXISTS FOR (c:Column) REQUIRE c.entity_id IS UNIQUE;

// =====================================================================
// 2. 创建 Table 节点
// =====================================================================

	UNWIND {tables_json} AS t
	MERGE (n:Table {{entity_id: t.entity_id}})
	SET n.entity_id = t.entity_id,
	    n.source_id = t.source_id,
	    n.publish_id = t.publish_id,
	    n.id        = t.id,
	    n.full_name = t.full_name,
	    n.database  = t.database,
	    n.schema    = t.schema,
	    n.name      = t.name,
	    n.comment   = t.comment,
	    n.object_type = t.object_type,
	    n.pk       = t.pk,
	    n.uk       = t.uk,
    n.fk       = t.fk,
    n.logic_pk = t.logic_pk,
    n.logic_fk = t.logic_fk,
    n.logic_uk = t.logic_uk,
    n.indexes  = t.indexes,
    n.table_domains = t.table_domains,
    n.table_category = CASE
        WHEN t.table_category IS NOT NULL
            THEN t.table_category
        ELSE n.table_category
    END;

// =====================================================================
// 3. 创建 Column 节点
// =====================================================================

	UNWIND {columns_json} AS c
	MERGE (n:Column {{entity_id: c.entity_id}})
	SET n.entity_id = c.entity_id,
	    n.source_id = c.source_id,
	    n.publish_id = c.publish_id,
	    n.id           = c.id,
	    n.full_name    = c.full_name,
	    n.database     = c.database,
	    n.schema       = c.schema,
	    n.table        = c.table,
	    n.name         = c.name,
	    n.comment      = c.comment,
	    n.data_type    = c.data_type,
	    n.semantic_role= c.semantic_role,
    n.is_pk        = c.is_pk,
    n.is_uk        = c.is_uk,
    n.is_fk        = c.is_fk,
    n.is_time      = c.is_time,
    n.is_measure   = c.is_measure,
    n.pk_position  = c.pk_position,
    n.uniqueness   = c.uniqueness,
    n.null_rate    = c.null_rate;

// =====================================================================
// 4. 建立 HAS_COLUMN 关系
// =====================================================================

UNWIND {has_column_json} AS hc
MATCH (t:Table {{entity_id: hc.table_entity_id}})
MATCH (c:Column {{entity_id: hc.column_entity_id}})
MERGE (t)-[r:HAS_COLUMN]->(c)
SET r.source_id = hc.source_id,
    r.publish_id = hc.publish_id;

// =====================================================================
// 5. 建立 JOIN_ON 关系
// =====================================================================

UNWIND {join_on_json} AS j
MATCH (src:Table {{entity_id: j.src_entity_id}})
MATCH (dst:Table {{entity_id: j.dst_entity_id}})
MERGE (src)-[r:JOIN_ON {{relationship_id: j.relationship_id}}]->(dst)
SET r.source_id = j.source_id,
    r.publish_id = j.publish_id,
    r.cardinality     = j.cardinality,
    r.constraint_name = j.constraint_name,
    r.join_type       = coalesce(j.join_type, 'INNER JOIN'),
    r.on              = j.on,
    r.source_columns  = j.source_columns,
    r.target_columns  = j.target_columns,
    r.source_table    = j.source_table,
    r.target_table    = j.target_table;
"""

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"生成 global 模式 CQL 脚本: {output_file}")
        return output_file

    def write_metadata(
        self,
        tables: List[TableNode],
        columns: List[ColumnNode],
        has_column_rels: List[HASColumnRelation],
        join_on_rels: List[JOINOnRelation],
        step_name: str,
        json_dir: Path,
        rel_dir: Path,
        filter_stats: RelationshipFilterStats,
        composite_score_threshold: Optional[float] = None,
    ) -> Path:
        """生成 import_all.md 元数据文档（最小必需字段）

        Args:
            tables: 表节点列表
            columns: 列节点列表
            has_column_rels: HAS_COLUMN 关系列表
            join_on_rels: JOIN_ON 关系列表
            step_name: 执行的步骤名称
            json_dir: JSON 输入目录
            rel_dir: 关系输入目录
            filter_stats: 关系过滤统计（doc 18 §5，必传——否则 md 中
                JOIN_ON 数与"最终 JOIN_ON 数"可能自相矛盾）
            composite_score_threshold: 当前生效的置信度阈值（仅展示性信息，
                可选;None 时 md 显示"未设置"）

        Returns:
            元数据文档路径

        Note:
            CQL 输出目录使用 self.output_dir，无需额外传递
        """
        database_name = self._resolve_database_name(tables, columns)
        output_file = self.output_dir / f"import_all.{database_name}.md"

        # 生成时间
        timestamp = utc_isoformat(datetime.now(timezone.utc))

        # 阈值展示(仅展示性信息,可选参数)
        threshold_display = (
            f"{composite_score_threshold:.4g}"
            if composite_score_threshold is not None
            else "未设置"
        )

        # ✅ 统计数据（使用准确的关系列表）
        has_column_count = len(has_column_rels)
        join_on_count = len(join_on_rels)
        edges_total = has_column_count + join_on_count

        # 最终 JOIN_ON 数必须等于 filter_stats.final_count。
        # 用显式 ValueError(而非 assert——-O 运行会删除 assert,不能承担
        # 产物正确性校验)
        if join_on_count != filter_stats.final_count:
            raise ValueError(
                f"JOIN_ON 数与过滤统计不一致: {join_on_count} vs "
                f"{filter_stats.final_count}"
            )
        stats = filter_stats

        # ✅ 获取 import_all.{db}.cypher 的行数（完善的容错处理）
        # 注意：此方法必须在 write_all() 之后调用，确保 Cypher 文件已生成
        cypher_file = self.output_dir / f"import_all.{database_name}.cypher"
        cypher_lines = 0
        if cypher_file.exists():
            try:
                with open(cypher_file, "r", encoding="utf-8") as f:
                    cypher_lines = sum(1 for _ in f)
                logger.debug(f"成功读取 Cypher 文件行数: {cypher_lines}")
            except Exception as e:
                logger.warning(f"无法读取 Cypher 文件行数: {e}")
                cypher_lines = 0  # 降级处理：设为 0
        else:
            logger.warning(
                f"import_all.{database_name}.cypher 不存在，无法统计行数。"
                f"路径: {cypher_file}"
            )
            cypher_lines = 0  # 降级处理：设为 0

        # ✅ 生成最小必需内容
        content = f"""# Neo4j CQL 导入脚本元数据

> **自动生成文档**
> 本文件记录 CQL 脚本的生成信息和统计数据。

---

## 生成信息

- **生成时间**: {timestamp}
- **生成命令**: `metaweave metadata --step {step_name}`
- **CQL 置信度阈值**: {threshold_display}(composite_score >= 阈值才导入;物理外键直通不受限)
- **导入 JOIN_ON 关系数**: {stats.final_count} 条(rel 候选 {stats.candidate_count} 条 → 通过阈值 {stats.threshold_passed_count} 条 → 去重后 {stats.final_count} 条)

---

## 统计数据

### 节点

| 节点类型 | 数量 |
|---------|------|
| Table | {len(tables)} |
| Column | {len(columns)} |
| **节点总数** | **{len(tables) + len(columns)}** |

### 边

| 边类型 | 数量 |
|-------|------|
| HAS_COLUMN | {has_column_count} |
| JOIN_ON | {join_on_count} |
| **边总数** | **{edges_total}** |

### JOIN_ON 过滤统计

| 统计项 | 数量 |
|-------|------|
| 候选关系数 | {stats.candidate_count} |
| 通过阈值数 | {stats.threshold_passed_count} |
| 被阈值过滤数 | {stats.threshold_filtered_count} |
| 重复 relationship_id 组数 | {stats.duplicate_group_count} |
| 去重丢弃数 | {stats.duplicate_discarded_count} |
| 最终 JOIN_ON 数 | {stats.final_count} |

---

## 输入输出目录

| 类型 | 路径 |
|-----|------|
| JSON 元数据 | `{json_dir}` |
| 关系数据 | `{rel_dir}` |
| CQL 输出 | `{self.output_dir}` |

---

## 输出文件

| 文件名 | 行数 |
|-------|------|
| import_all.{database_name}.cypher | {cypher_lines} |
| import_all.{database_name}.md | - |

---

**文档生成工具**: metaweave
**文档版本**: 1.0
"""

        # 写入文件
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"元数据文档已生成: {output_file}")
        return output_file
