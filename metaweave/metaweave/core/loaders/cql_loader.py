"""Neo4j CQL 加载器

加载 Step 4 生成的 import_all.cypher 文件到 Neo4j 图数据库。

doc 4（多数据源知识隔离）改造：
- 清理完全由节点归属驱动：删除所有无 source_id 的节点，以及
  source_id 等于本次发布的节点，均不限制 label；DETACH DELETE 连带删除
  关联关系，其他源节点及两端都保留的关系不动；
- 清理、写入、按源核验在**同一个数据事务**内完成：任一步失败整体回滚，
  图保持原状（§4.5 B 发布事务边界）；约束语句在数据事务之外逐条幂等执行
  （Neo4j 不允许数据事务内混入 schema 命令）；
- 加载拒绝旧格式脚本（缺 ``// source_id:`` 头）；构造传入的 source_id 与
  脚本头不一致时在清理之前拒绝；
- 数量核验按当前 source_id 统计 Table/Column/JOIN_ON/HAS_COLUMN，作为
  事务提交前的最后一步。
"""

from pathlib import Path
from typing import Dict, Any, List, Optional
import glob
import re
import time
import logging

from metaweave.utils.file_utils import resolve_project_path
from metaweave.utils.source_id import normalize_source_id
from metaweave.core.loaders.base import BaseLoader
from services.db.neo4j_connection import Neo4jConnectionManager
from neo4j.exceptions import Neo4jError

logger = logging.getLogger(__name__)


class CQLScriptValidationError(ValueError):
    """CQL 脚本核验拒绝（doc 23.2 §5.5-2 + doc 4 §4.4）：截断/缺必需章节/
    缺统计头/缺 source_id 头/来源不一致。

    ValueError 子类使发布侧可在通用 ``except Exception`` 之前单列分流——
    本异常恒在任何图写入之前抛出（图未动），失败不撤销 current。
    """


class _RolledBackWorkError(Exception):
    """数据事务回调内部的失败——提交状态**确认未提交**（已回滚）。

    回调内任何异常经本类包装后上抛：driver 对回调内异常不再重试、
    直接回滚事务，因此图保持原状。与之相对，回调成功返回
    后 COMMIT 阶段的失败（如 ``IncompleteCommit``/``SessionExpired``）
    提交状态不可知，不得包装为本类——发布侧须按"可能已提交"保守处理。
    """

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


class LockLostError(RuntimeError):
    """目标锁存活探测失败（doc 10.2 §5.1）。

    由发布/删除侧注入的 ``lock_probe`` 在锁专用连接断开时抛出；
    loader/cleaner 必须原样传播、不得当作业务失败回滚结果处理。
    backend.publishing.target_lock.TargetLockLostError 继承本类。
    """


def normalize_neo4j_section(neo4j_section: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化 ``loaders.cql_loader.neo4j`` 配置段（共享严格边界）。

    uri 走 ``normalize_neo4j_uri``；database 键缺失才用默认库，存在时必须
    是非空字符串。CQLLoader 与一次性迁移脚本共用同一判定。
    """
    if not neo4j_section:
        raise ValueError(
            "缺少 neo4j 配置段：请在 metadata_config.yaml 的 "
            "loaders.cql_loader.neo4j 中配置 Neo4j 连接信息。"
        )
    from metaweave.core.clean_targets import normalize_neo4j_uri

    try:
        safe_uri = normalize_neo4j_uri(neo4j_section["uri"])
    except (ValueError, KeyError) as e:
        raise ValueError(f"loaders.cql_loader.neo4j.uri 非法: {e}") from None

    if "database" in neo4j_section:
        raw_database = neo4j_section["database"]
        if not isinstance(raw_database, str) or not raw_database.strip():
            raise ValueError(
                "loaders.cql_loader.neo4j.database 必须是非空字符串或省略该键"
                f"（实际为 {type(raw_database).__name__}；省略时使用默认库 neo4j）"
            )
        database = raw_database.strip()
    else:
        database = "neo4j"

    return {
        "uri": safe_uri,
        "user": neo4j_section["user"],
        "password": neo4j_section["password"],
        "database": database,
    }


# 节点归属驱动的清理：不限制 label，删除所有无 source_id 的节点和本次
# 发布 source_id 的节点；DETACH DELETE 一并删除关联关系。两个端点都保留
# 的关系不会被这条语句触碰。
_CLEAN_NODES = (
    "MATCH (n) WHERE n.source_id IS NULL OR n.source_id = $source_id " "DETACH DELETE n"
)

# 按源统计（提交前核验；HAS_COLUMN 单查避免两次 OPTIONAL MATCH 行积）。
# 首段之后的段必须 OPTIONAL：零匹配（如该源无列）时保留行，count 记 0，
# 否则 MATCH 消灭全部行使核验读不到任何记录
_SOURCE_STATS_MAIN = """
    MATCH (t:Table) WHERE t.source_id = $source_id
    WITH count(t) AS table_count
    OPTIONAL MATCH (c:Column) WHERE c.source_id = $source_id
    WITH table_count, count(c) AS column_count
    OPTIONAL MATCH ()-[r:JOIN_ON]->() WHERE r.source_id = $source_id
    RETURN table_count, column_count, count(r) AS relationship_count
"""
_SOURCE_STATS_HAS_COLUMN = (
    "MATCH ()-[h:HAS_COLUMN]->() WHERE h.source_id = $source_id "
    "RETURN count(h) AS has_column_count"
)


class CQLLoader(BaseLoader):
    """Neo4j CQL 加载器

    加载 Step 4 生成的 import_all.cypher 文件到 Neo4j 图数据库。

    特性:
    - 按节点归属清理：删除所有无 source_id 节点和本次 source_id 节点
      （不限制 label），DETACH DELETE 连带删除关联关系
    - 单一数据事务内清理 → 写入 → 按源核验
    - 幂等性保证（MERGE + IF NOT EXISTS 约束）
    - 约束语句事务外逐条执行（幂等）
    """

    def __init__(self, config: Dict[str, Any], source_id=None, lock_probe=None):
        """初始化 CQL 加载器

        Args:
            config: 配置字典，必须包含 cql_loader 字段
            source_id: 本次发布的 source_id（后端已验证；受管发布链路注入）。
                缺省时从脚本头 ``// source_id:`` 解析（CLI 调试路径）。
            lock_probe: 目标锁存活探测回调（doc 10.2 §5.1）。每次目标
                schema/数据操作边界调用；抛 ``LockLostError`` 时原样传播，
                不得继续后续操作。受管发布链路注入；CLI 调试路径缺省。
        """
        super().__init__(config)
        self.neo4j_config = self._get_neo4j_config()
        self.neo4j_manager: Optional[Neo4jConnectionManager] = None
        self._lock_probe = lock_probe
        # 后端传入的删除范围（doc 4 §4.4-2：删除目标必须是已验证的 source_id）
        self.source_id: Optional[str] = (
            None if source_id is None else str(normalize_source_id(source_id))
        )

        # 获取输入文件路径
        cql_config = self.config.get("cql_loader", {})
        input_file_str = cql_config.get("input_file", "output/cql/import_all.*.cypher")
        # doc 26 §6.2：相对 glob pattern 统一基于项目根目录解析
        self.input_file = resolve_project_path(input_file_str)

        # 如果不是绝对路径，则相对于项目根目录
        if not self.input_file.is_absolute():
            from metaweave.utils.file_utils import get_project_root

            self.input_file = get_project_root() / input_file_str

    @staticmethod
    def _is_glob_pattern(path: Path) -> bool:
        return any(ch in str(path) for ch in ["*", "?", "["])

    def _resolve_input_file(self) -> bool:
        """将 input_file（可能包含通配符）解析为唯一的实际文件路径。"""
        if not self._is_glob_pattern(self.input_file):
            return True

        pattern = str(self.input_file)
        matches = [Path(p) for p in glob.glob(pattern)]
        matches = [p for p in matches if p.is_file()]

        if not matches:
            logger.error(f"CQL 文件不存在（通配符未匹配到文件）: {pattern}")
            return False

        if len(matches) > 1:
            logger.error(f"检测到多个匹配的 CQL 文件（请确保仅保留一个）: {pattern}")
            for m in sorted(matches):
                logger.error(f"  - {m}")
            return False

        self.input_file = matches[0]
        logger.info(f"通配符已解析到唯一 CQL 文件: {self.input_file}")
        return True

    def _get_neo4j_config(self) -> Dict[str, Any]:
        """从传入的 config dict 获取 Neo4j 配置（共享严格边界，规则见
        ``normalize_neo4j_section``）"""
        return normalize_neo4j_section(
            self.config.get("cql_loader", {}).get("neo4j", {})
        )

    def validate(self) -> bool:
        """验证配置和数据源

        验证步骤:
        1. 检查配置是否包含 cql_loader 字段
        2. 检查 CQL 文件是否存在
        3. 测试 Neo4j 连接

        Returns:
            bool: 验证是否通过
        """
        # 1. 检查配置
        if "cql_loader" not in self.config:
            logger.error("配置缺少 cql_loader 字段")
            return False

        # 2. 检查文件（支持通配符，且必须唯一匹配）
        if not self._resolve_input_file():
            return False
        if not self.input_file.exists():
            logger.error(f"CQL 文件不存在: {self.input_file}")
            return False

        # 3. 测试连接
        try:
            self.neo4j_manager = Neo4jConnectionManager(self.neo4j_config)
            self.neo4j_manager.initialize()
            if not self.neo4j_manager.test_connection():
                logger.error("Neo4j 连接测试失败")
                return False
        except Exception as e:
            from metaweave.core.clean_targets import redact_uris_in_text

            _msg = redact_uris_in_text(str(e))
            # 额外的密码抹除：异常文本可能直接内嵌密码（非 URI 形式）
            _pw = self.neo4j_config.get("password")
            if _pw:
                _msg = _msg.replace(str(_pw), "***")
            logger.error("Neo4j 连接失败: %s", _msg)
            return False

        logger.info("✅ 配置验证通过")
        logger.info(f"✅ CQL 文件存在: {self.input_file}")
        # 仅输出规范化脱敏 URI（原始值可能含 userinfo 或 ?token= 等敏感查询参数）
        try:
            from metaweave.core.clean_targets import normalize_neo4j_uri

            _safe_uri = normalize_neo4j_uri(self.neo4j_config["uri"])
        except Exception:  # noqa: BLE001
            _safe_uri = "<uri 无法规范化>"
        logger.info("✅ Neo4j 连接成功: %s", _safe_uri)

        return True

    # ------------------------------------------------------------ 加载

    def load(self) -> Dict[str, Any]:
        """执行按源替换加载（doc 4 §4.4/§4.5）。

        单一数据事务：按节点归属清理（所有无 source_id 节点及本次 source_id
        节点，不限制 label）→ 写入新内容 → 按 source_id 核验数量；任一步
        失败整体回滚，图保持原状。约束语句在数据事务之前逐条幂等执行。
        transaction_mode/validate_after_load 配置不再影响本路径
        （按源核验是事务提交前的必要步骤，不可关闭）。
        """
        start_time = time.time()

        # 1. 解析与脚本核验（任何图写入之前拒绝；图未动）
        logger.info("正在解析 CQL 文件...")
        sections = self._parse_cql_file(self.input_file)
        logger.info(f"解析完成，共 {len(sections)} 个章节")
        self._assert_required_sections(sections)

        expected_stats = self._extract_expected_stats(self.input_file)
        script_source_id = self._extract_header_source_id(self.input_file)
        source_id = self._resolve_source_identity(script_source_id)

        if self.neo4j_manager is None:
            raise RuntimeError("Neo4j 连接未初始化：请先调用 validate()")

        # 2. 约束语句（幂等 IF NOT EXISTS）——事务外逐条执行；Neo4j 不允许
        #    数据事务内混入 schema 命令。约束失败时数据事务尚未开始，本源
        #    子图未动（已执行的约束语句幂等无害）
        try:
            self._apply_constraint_statements(sections)
        except LockLostError:
            # 锁失活探测失败：不是业务回滚，原样传播（doc 10.2 §5.1）
            raise
        except Exception as e:
            logger.error("CQL 约束语句执行失败: %s", e)
            return self._failure_result(
                start_time,
                source_id,
                f"CQL 加载失败（约束语句执行错误，图数据未动）: {e}",
                [
                    {
                        "section": "constraints",
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                    }
                ],
                rolled_back=True,
            )

        # 3. 单一数据事务：删除 → 写入 → 按源核验（doc 4 §4.5）
        logger.info(f"开始按源替换加载（source_id={source_id}）...")
        data_sections = [s for s in sections if not self._is_constraint_section(s)]
        counts: Dict[str, int] = {}
        self._probe_lock()  # 数据事务开始前（doc 10.2 §5.1）
        try:
            counts = self._execute_replacement_transaction(
                source_id, data_sections, expected_stats
            )
        except LockLostError:
            raise
        except _RolledBackWorkError as exc:
            # 事务回调内部失败：driver 已回滚，图确认保留
            e = exc.original
            section = "verify" if isinstance(e, ValueError) else "data_transaction"
            return self._failure_result(
                start_time,
                source_id,
                f"Neo4j 加载失败（数据事务已回滚，旧图保留）: {e}",
                [
                    {
                        "section": section,
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "error_code": (
                            getattr(e, "code", "N/A")
                            if isinstance(e, Neo4jError)
                            else None
                        ),
                    }
                ],
                rolled_back=True,
            )
        except Exception as e:
            # 回调成功返回后的提交阶段失败（IncompleteCommit/SessionExpired/
            # 网络中断等）：图事务提交状态不可知——不得标记已回滚，发布侧
            # 须按"可能已提交"保守撤销 current
            logger.error("CQL 数据事务提交状态不可知: %s: %s", type(e).__name__, e)
            return self._failure_result(
                start_time,
                source_id,
                f"CQL 加载中断（图事务提交状态不可知，请核对图内数据后重跑发布）: {e}",
                [
                    {
                        "section": "commit",
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                    }
                ],
                rolled_back=False,
            )

        self._probe_lock()  # 数据事务提交后（doc 10.2 §5.1）

        result = {
            "success": True,
            "message": "加载成功",
            "source_id": source_id,
            "counts": counts,
            "sections": [
                {"name": section["section_name"], "success": True}
                for section in sections
            ],
            "errors": [],
        }

        result["execution_time"] = round(time.time() - start_time, 2)
        logger.info(
            "按源替换加载完成 source_id=%s counts=%s（预期 table=%s column=%s "
            "join_on=%s has_column=column）",
            source_id,
            counts,
            expected_stats.get("table_count"),
            expected_stats.get("column_count"),
            expected_stats.get("relationship_count"),
        )
        return result

    def _failure_result(
        self,
        start_time: float,
        source_id: str,
        message: str,
        errors: List[Dict[str, Any]],
        *,
        rolled_back: bool,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "message": message,
            "source_id": source_id,
            "sections": [],
            "errors": errors,
            "execution_time": round(time.time() - start_time, 2),
        }
        if rolled_back:
            # 仅在**确认未提交**时标记：图保持原状，发布侧据此
            # 不撤销 current。提交状态不可知的失败不得置位
            result["_rolled_back"] = True
        return result

    def _resolve_source_identity(self, script_source_id: Optional[str]) -> str:
        """确定本次删除范围 source_id：构造参数（后端已验证）优先。

        两者都有时必须一致——不一致在删除之前拒绝（防加载错源脚本）。
        """
        if self.source_id is not None:
            if script_source_id is not None and script_source_id != self.source_id:
                raise CQLScriptValidationError(
                    f"CQL 脚本归属（source_id={script_source_id}）与本次发布"
                    f"（source_id={self.source_id}）不一致，已拒绝加载"
                )
            return self.source_id
        if script_source_id is None:
            raise CQLScriptValidationError(
                "CQL 加载需要 source_id：脚本缺少 '// source_id:' 头，且未在"
                "构造时传入（旧格式脚本不支持按源替换，请用新链路重新生成）"
            )
        try:
            return str(normalize_source_id(script_source_id))
        except ValueError:
            raise CQLScriptValidationError(
                f"CQL 脚本头 source_id 不是合法 UUID: {script_source_id!r}"
            ) from None

    @staticmethod
    def _is_constraint_section(section: Dict[str, Any]) -> bool:
        return (
            "约束" in section["section_name"]
            or "CONSTRAINT" in section["cypher"].upper()[:100]
        )

    def _apply_constraint_statements(self, sections: List[Dict[str, Any]]) -> None:
        """逐条执行约束章节语句（幂等；每条独立事务提交）。

        失败即抛出：此时数据事务尚未开始，图数据未动。
        """
        for section in sections:
            if not self._is_constraint_section(section):
                continue
            cypher_text = self._fix_json_quotes(section["cypher"])
            for stmt in self._split_statements(cypher_text):
                # doc 10.2 §5.1：每条独立 schema 请求前探测锁连接；
                # 失锁抛 LockLostError 原样传播，不继续后续语句。
                self._probe_lock()
                logger.info("  应用约束: %s", stmt.strip()[:80])
                self.neo4j_manager.execute_write_transaction(stmt)

    def _probe_lock(self) -> None:
        """目标锁存活探测（未注入时为空操作；doc 10.2 §5.1）。"""
        if self._lock_probe is not None:
            self._lock_probe()

    def _execute_replacement_transaction(
        self,
        source_id: str,
        data_sections: List[Dict[str, Any]],
        expected_stats: Dict[str, int],
    ) -> Dict[str, int]:
        """单一数据事务执行清理/删除/写入/核验（doc 4 §4.5 B 发布事务边界）。

        回调内多次 ``tx.run()``，消费全部结果。回调内任何异常经
        ``_RolledBackWorkError`` 包装上抛（driver 对回调内异常不重试、
        直接回滚——确认未提交）；回调成功返回后 COMMIT 阶段的异常原样
        上抛（提交状态不可知）。禁用 driver 的瞬时错误自动重试是有意
        为之：回调自身幂等，单次失败即回滚的语义更保守、更易判定。
        """
        prepared_sections = [
            (section["section_name"], self._prepare_section_cypher(section))
            for section in data_sections
        ]

        def work(tx):
            try:
                return self._replacement_work(
                    tx, source_id, prepared_sections, expected_stats
                )
            except Exception as exc:  # noqa: BLE001（统一包装：确认未提交）
                raise _RolledBackWorkError(exc) from exc

        with self.neo4j_manager.get_session() as session:
            counts = session.execute_write(work)
        return {key: int(value) for key, value in counts.items()}

    def _replacement_work(
        self,
        tx,
        source_id: str,
        prepared_sections: List[tuple],
        expected_stats: Dict[str, int],
    ) -> Dict[str, int]:
        """事务回调主体：按节点归属清理 → 写入 → 按源核验。"""
        # 清理由节点归属驱动：删除所有无 source_id 节点和本源节点（不限制
        # label）；DETACH DELETE 一并删除关联关系。其他源节点及两端都保留
        # 的关系不动。清理与写入、核验同事务，失败整体回滚。
        tx.run(_CLEAN_NODES, source_id=source_id).consume()

        # 写入各数据章节
        for name, cypher in prepared_sections:
            logger.info("  写入章节: %s", name)
            tx.run(cypher).consume()

        # 按源核验（仍在事务内：不符即抛错回滚，旧图保留）
        record = tx.run(_SOURCE_STATS_MAIN, source_id=source_id).single()
        stats = dict(record) if record else {}
        hc_record = tx.run(_SOURCE_STATS_HAS_COLUMN, source_id=source_id).single()
        stats["has_column_count"] = hc_record["has_column_count"] if hc_record else 0
        mismatches = CQLLoader._compare_source_stats(expected_stats, stats)
        if mismatches:
            detail = "；".join(
                f"{m['item']} 期望 {m['expected']}/实际 {m['actual']}"
                for m in mismatches
            )
            raise ValueError(f"图内数量与脚本统计不符（已回滚，旧图保留）: {detail}")
        return stats

    @staticmethod
    def _compare_source_stats(
        expected_stats: Dict[str, int], actual: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """比较按源实际计数与统计头期望；HAS_COLUMN 期望 = 列数。"""
        expectations = {
            "table_count": expected_stats.get("table_count", 0),
            "column_count": expected_stats.get("column_count", 0),
            "relationship_count": expected_stats.get("relationship_count", 0),
            "has_column_count": expected_stats.get("column_count", 0),
        }
        mismatches = []
        for item, expected in expectations.items():
            actual_value = int(actual.get(item, 0) or 0)
            if actual_value != expected:
                mismatches.append(
                    {
                        "item": item,
                        "expected": expected,
                        "actual": actual_value,
                    }
                )
            else:
                logger.info("✅ %s 按源计数正确: %s", item, actual_value)
        return mismatches

    def _prepare_section_cypher(self, section: Dict[str, Any]) -> str:
        """数据章节文本预处理（沿用既有引号/嵌套数组修复）。"""
        cypher_text = self._fix_json_quotes(section["cypher"])
        return self._fix_nested_arrays(cypher_text)

    def _assert_required_sections(self, sections: List[Dict[str, Any]]) -> None:
        """必需章节与统计头核验（doc 23.2 §5.5-2，写入前抛出）。

        表节点与列节点章节必须存在（按 writer 实际章节标题关键字匹配；
        HAS_COLUMN/JOIN_ON 章节允许空载荷——零关系合法，D10）；统计头
        与 source_id 头必须可提取（fail closed：受管链路的 import_all 恒有）。
        """
        titles = [s["section_name"] for s in sections]
        # 四章节全查（doc 23.2 §5.5-2 实施修订）：Column 章节后截断的脚本
        # 表/列/零 JOIN_ON 计数可全匹配、却没有任何表-列边——HAS_COLUMN 与
        # JOIN_ON 章节必须同样存在（生成器恒输出全部章节；零关系时 JOIN_ON
        # 章节为 UNWIND []，D10 零关系放行不受影响）
        required_sections = (
            ("Table", "Table 节点"),
            ("Column", "Column 节点"),
            ("HAS_COLUMN", "HAS_COLUMN 关系"),
            ("JOIN_ON", "JOIN_ON 关系"),
        )
        for keyword, label in required_sections:
            if not any(keyword in title for title in titles):
                raise CQLScriptValidationError(
                    f"CQL 脚本缺少 {label} 章节（脚本可能被截断）"
                )
        if not self._extract_expected_stats(self.input_file):
            raise CQLScriptValidationError(
                "CQL 脚本缺少统计头（// 统计: …），无法做加载后数量核验"
            )

    def close(self) -> None:
        """关闭 Neo4j 驱动（doc 23.2 §5.5-4）。

        幂等：manager 为 None 时安全无操作；关闭后置回 None，可重新
        ``validate()`` 重建驱动。发布链在 finally 调用，覆盖校验失败、
        加载失败与成功全部返回路径。
        """
        if self.neo4j_manager is not None:
            self.neo4j_manager.close()
            self.neo4j_manager = None

    def _parse_cql_file(self, file_path: Path) -> List[Dict[str, Any]]:
        """解析 CQL 文件，按章节拆分

        Args:
            file_path: CQL 文件路径

        Returns:
            List[Dict]: 章节列表，每个元素包含：
                - order: 执行顺序
                - section_name: 章节名称
                - cypher: Cypher 语句
        """
        content = file_path.read_text(encoding="utf-8")

        # 章节分隔符正则：// ===... \n // 1. ... \n // ===...
        section_pattern = r"// ={50,}\n// (\d+)\. (.+?)\n// ={50,}\n"

        sections = []
        matches = list(re.finditer(section_pattern, content))

        for i, match in enumerate(matches):
            section_number = match.group(1)
            section_title = match.group(2)

            # 提取章节内容（从当前分隔符到下一个分隔符）
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            section_content = content[start:end].strip()

            # 移除注释行
            cypher = "\n".join(
                line
                for line in section_content.split("\n")
                if not line.strip().startswith("//")
            ).strip()

            if cypher:
                sections.append(
                    {
                        "order": int(section_number),
                        "section_name": section_title,
                        "cypher": cypher,
                    }
                )

        return sections

    def _split_statements(self, cypher_text: str) -> List[str]:
        """将多条 Cypher 语句按分号分隔

        处理规则：
        - 按分号分隔
        - 移除空语句
        - 保留语句完整性（不分隔字符串内的分号）

        Args:
            cypher_text: Cypher 文本

        Returns:
            List[str]: 语句列表
        """
        # 简单实现：按分号分隔
        # 注意：这里假设 CQL 文件中没有字符串内的分号
        statements = cypher_text.split(";")
        return [stmt.strip() for stmt in statements if stmt.strip()]

    def _fix_json_quotes(self, cypher_text: str) -> str:
        """修复 JSON 格式中的双引号问题

        在 Cypher 中，双引号用于转义标识符，不能用于 JSON 的字段名。
        此方法将 JSON 对象中的双引号字段名转换为不带引号的格式。

        例如：
            {"full_name": "value"} => {full_name: "value"}

        Args:
            cypher_text: 原始 Cypher 文本

        Returns:
            str: 修复后的 Cypher 文本
        """
        # 正则：匹配 JSON 对象中的双引号字段名（如 "field_name": ）
        # 替换为不带引号的字段名（如 field_name: ）
        import re

        # 匹配模式："field_name":
        # 注意：只替换冒号前的双引号字段名，保留值中的双引号
        pattern = r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:'
        replacement = r"\1:"

        return re.sub(pattern, replacement, cypher_text)

    def _fix_nested_arrays(self, cypher_text: str) -> str:
        """修复嵌套数组问题（Neo4j 技术限制）

        **问题背景：**
        Neo4j 不支持在属性中存储嵌套集合（collection of collections）。
        如果尝试存储 [["col1"]]，会报错：
            Neo.ClientError.Statement.TypeError: Collections containing collections
            can not be stored in properties.

        **解决方案：**
        将嵌套数组字段转换为 JSON 字符串，保留原始数据结构。

        **转换示例：**
            原始: logic_pk: [["company_id"], ["code", "type"]]
            转换: logic_pk: '[["company_id"], ["code", "type"]]'

        **后续使用：**
        查询时需要用 json.loads() 解析：
            >>> import json
            >>> logic_pk_str = result[0]["logic_pk"]  # '[["company_id"]]'
            >>> logic_pk = json.loads(logic_pk_str)   # [["company_id"]]

        推荐使用辅助函数（见文档 2.4.3 节）：
            >>> from metaweave.utils.neo4j_helpers import parse_nested_array_field
            >>> logic_pk = parse_nested_array_field(logic_pk_str)

        **影响字段：**
        - uk, fk (物理唯一键和外键，支持多个)
        - logic_pk, logic_fk, logic_uk (逻辑键，支持多个候选)
        - indexes (索引)

        **不转换的字段：**
        - pk (物理主键) - Step 4 生成的是一维数组，Neo4j 原生支持

        Args:
            cypher_text: Cypher 文本

        Returns:
            str: 修复后的 Cypher 文本（嵌套数组已转为 JSON 字符串）
        """
        import re
        import json

        # 匹配嵌套数组的字段（如 logic_pk, logic_fk, logic_uk）
        # 这些字段包含二维数组
        # 注意：pk 是一维数组（Neo4j 原生支持），不需要转换
        nested_array_fields = [
            "uk",
            "fk",
            "logic_pk",
            "logic_fk",
            "logic_uk",
            "indexes",
        ]

        for field in nested_array_fields:
            # 匹配模式：field: [[...]]
            # 使用非贪婪匹配，并考虑多行
            pattern = rf"{field}:\s*(\[\s*\[[\s\S]*?\]\s*\])"

            def replace_nested_array(match):
                """替换函数：将嵌套数组转换为 JSON 字符串"""
                array_str = match.group(1)
                try:
                    # 解析为 Python 对象
                    array_obj = eval(array_str)
                    # 转换为 JSON 字符串
                    json_str = json.dumps(array_obj)
                    # 使用单引号包裹 JSON 字符串（避免双引号转义问题）
                    # Cypher 支持单引号字符串
                    return f"{field}: '{json_str}'"
                except Exception:
                    # 如果解析失败，保持原样
                    logger.warning(
                        "无法解析嵌套数组字段 %s: %s...", field, array_str[:50]
                    )
                    return match.group(0)

            cypher_text = re.sub(pattern, replace_nested_array, cypher_text)

        return cypher_text

    def _extract_expected_stats(self, file_path: Path) -> Dict[str, int]:
        """从 CQL 文件头部提取期望的统计信息

        文件头部格式：
            // 统计: 6 张表, 22 个列, 6 个关系

        Args:
            file_path: CQL 文件路径

        Returns:
            Dict[str, int]: 统计信息字典，包含 table_count, column_count, relationship_count
        """
        content = file_path.read_text(encoding="utf-8")

        # 正则提取统计信息
        match = re.search(
            r"// 统计:\s*(\d+)\s*张表,\s*(\d+)\s*个列,\s*(\d+)\s*个关系", content
        )

        if match:
            return {
                "table_count": int(match.group(1)),
                "column_count": int(match.group(2)),
                "relationship_count": int(match.group(3)),
            }
        else:
            logger.warning("无法从 CQL 文件提取统计信息，跳过验证")
            return {}

    @staticmethod
    def _extract_header_source_id(file_path: Path) -> Optional[str]:
        """提取头部 ``// source_id: <短ID>``（新格式脚本必有；旧格式返回 None）。

        新格式来源为 db_src_N 短 ID（doc 6 §1）；旧 36 位 UUID 头不再
        被识别——按断代原则不兼容旧脚本。
        """
        content = file_path.read_text(encoding="utf-8")
        match = re.search(r"//\s*source_id:\s*(db_src_[1-9][0-9]*)", content)
        return match.group(1) if match else None
