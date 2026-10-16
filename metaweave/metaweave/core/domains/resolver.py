"""DomainResolver — db_domains.yaml 的统一读取组件

将 configs/db_domains.yaml 作为 domain 信息的单一事实来源，
提供 table→domains、domain→tables 映射及表对生成能力。
"""

import logging
from itertools import combinations, product
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


class DomainResolver:
    """从 db_domains.yaml 解析 domain 映射并提供查询接口"""

    def __init__(self, domains_config_path: "Path | str"):
        self._path = Path(domains_config_path)
        self._config: Dict[str, Any] = {}
        self._table_to_domains: Dict[str, List[str]] = {}
        self._domain_to_tables: Dict[str, List[str]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning("db_domains.yaml 不存在: %s", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("加载 db_domains.yaml 失败: %s", exc)
            return

        if not isinstance(data, dict):
            return

        self._config = data

        for domain in data.get("domains", []):
            if not isinstance(domain, dict):
                continue
            domain_name = domain.get("name", "")
            if not domain_name:
                continue
            tables = domain.get("tables", []) or []
            table_list: List[str] = []
            for table in tables:
                key = str(table).strip().casefold()
                if key:
                    self._table_to_domains.setdefault(key, []).append(domain_name)
                    table_list.append(str(table).strip())
            if table_list:
                self._domain_to_tables[domain_name] = table_list

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    def get_domains_for_full_name(self, full_table_name: str) -> List[str]:
        """根据完整表名（如 dvdrental.public.customer）查询所属 domain 列表"""
        key = full_table_name.strip().casefold()
        return list(self._table_to_domains.get(key, []))

    def get_tables_for_domain(self, domain_name: str) -> List[str]:
        """根据 domain 名称获取其下所有表"""
        return list(self._domain_to_tables.get(domain_name, []))

    def get_all_domains(self) -> List[str]:
        """返回所有 domain 名称（包括 _未分类_，即使其 tables 为空）"""
        domains = []
        for domain in self._config.get("domains", []):
            if isinstance(domain, dict) and domain.get("name"):
                domains.append(domain["name"])
        return domains

    def build_domain_table_map(self) -> Dict[str, List[str]]:
        """返回 {domain_name: [table_name, ...]} 的完整映射"""
        return {k: list(v) for k, v in self._domain_to_tables.items()}

    # ------------------------------------------------------------------
    # 便捷接口
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_table_name(name: str, db_name: Optional[str] = None) -> str:
        """将 schema.table 补全为 db.schema.table 格式"""
        parts = name.strip().split(".")
        if len(parts) == 2 and db_name:
            return f"{db_name}.{name.strip()}"
        return name.strip()

    def get_domains_for_schema_table(
        self, schema_table: str, db_name: str
    ) -> List[str]:
        """用 schema.table（如 public.customer）+ db_name 查询 domain"""
        full_name = self.normalize_table_name(schema_table, db_name)
        return self.get_domains_for_full_name(full_name)

    # ------------------------------------------------------------------
    # 业务辅助：表对生成（移植自 llm_relationship_discovery.py）
    # ------------------------------------------------------------------

    def resolve_table_pairs(
        self,
        available_tables: List[str],
        domain_filter: Optional[str] = None,
        cross_domain: bool = False,
        *,
        db_name: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """根据 domain 过滤条件生成表对列表

        Args:
            available_tables: 当前可用的表名列表（完整名称）
            domain_filter: None=全量组合, "all"=所有domain内, 逗号分隔=指定domain
            cross_domain: 是否生成跨 domain 表对
            db_name: 数据库名（doc 23.1 §5.5）。提供时为每个 available 名登记
                两段式原名与三段式 ``f"{db_name}.{原名}"`` 两个 casefold 匹配键，
                使 db_domains.yaml 的三段式条目（db.schema.table）命中两段式
                available 表（schema.table）并映射回原始名；同表双写（两段式与
                三段式并存）归并去重，防止域内 combinations 产出 (t, t) 自配对。
                缺省时行为与历史版本逐字节一致。

        Returns:
            去重后的表对列表
        """
        if not domain_filter:
            return list(combinations(available_tables, 2))

        # 解析 domain_filter
        target_domains = self._parse_domain_filter(domain_filter)

        # 按 domain 分组（仅保留 available_tables 中的表）
        # 匹配键 → 原始 available 名：db_name 提供时双键登记（原名 + 三段式）
        match_to_original: Dict[str, str] = {}
        for table in available_tables:
            original = match_to_original.setdefault(table.casefold(), table)
            if db_name:
                match_to_original.setdefault(
                    f"{db_name.casefold()}.{table.casefold()}", original
                )

        domain_tables: Dict[str, List[str]] = {}
        for domain_name, tables in self._domain_to_tables.items():
            if target_domains and domain_name not in target_domains:
                continue
            filtered = []
            seen: set = set()
            for t in tables:
                original = match_to_original.get(t.casefold())
                if original is None:
                    continue
                # 同表双写（两段式 + 三段式条目）映射到同一原始名，归并去重
                if original not in seen:
                    seen.add(original)
                    filtered.append(original)
            if filtered:
                domain_tables[domain_name] = filtered

        sample = available_tables[:3]
        logger.info(
            "resolve_table_pairs: available_tables=%d 张 (示例: %s), "
            "domain_filter=%r, 命中 %d 个 domain %s",
            len(available_tables),
            sample,
            domain_filter,
            len(domain_tables),
            {d: len(ts) for d, ts in domain_tables.items()},
        )
        if available_tables and not domain_tables:
            logger.warning(
                "available_tables 共 %d 张表，但没有任何表命中 domain 配置。"
                "请检查 db_domains.yaml 中的表名格式是否与 available_tables 一致 "
                "(示例 available: %s, domain 配置中的表: %s)",
                len(available_tables),
                sample,
                [t for ts in self._domain_to_tables.values() for t in ts[:2]][:4],
            )

        # 生成域内表对
        intra_pairs: List[Tuple[str, str]] = []
        for table_list in domain_tables.values():
            intra_pairs.extend(combinations(table_list, 2))

        if not cross_domain:
            return intra_pairs

        # 生成跨域表对
        processed = set(intra_pairs)
        cross_pairs: List[Tuple[str, str]] = []
        domain_list = list(domain_tables.keys())

        for i, d1 in enumerate(domain_list):
            for d2 in domain_list[i + 1 :]:
                for t1, t2 in product(domain_tables[d1], domain_tables[d2]):
                    if t1 == t2:
                        continue
                    pair = tuple(sorted([t1, t2]))
                    if pair not in processed:
                        cross_pairs.append(pair)
                        processed.add(pair)

        return intra_pairs + cross_pairs

    def _parse_domain_filter(self, domain: Optional[str]) -> Optional[List[str]]:
        """解析 domain_filter 字符串"""
        if not domain:
            return None
        if domain.lower() == "all":
            return None  # None 表示不过滤，使用所有 domain
        return [d.strip() for d in domain.split(",") if d.strip()]
