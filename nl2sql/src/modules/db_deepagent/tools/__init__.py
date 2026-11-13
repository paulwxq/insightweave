"""业务工具装配（docs/v4/5 §7.1；doc 6.3 §10）

build_business_tools(services) 用闭包注入服务：步骤 2 检索三件套、
步骤 3 校验执行五件套、步骤 5 结果两件套、doc 6.3 任务登记一件，
共 11 件业务工具。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from src.modules.db_deepagent.services import DbAgentServices
from src.modules.db_deepagent.tools.guard_tools import (
    make_probe_dimension_values_tool,
    make_resolve_time_range_tool,
    make_reuse_evidence_tool,
)
from src.modules.db_deepagent.tools.schema_tools import (
    make_inspect_tables_tool,
    make_plan_joins_tool,
    make_search_schema_tool,
)
from src.modules.db_deepagent.tools.result_tools import (
    make_analyze_result_tool,
    make_read_result_tool,
)
from src.modules.db_deepagent.tools.sql_tools import (
    make_execute_sql_tool,
    make_validate_sql_tool,
)
from src.modules.db_deepagent.tools.task_tools import make_record_sub_tasks_tool


def build_business_tools(services: DbAgentServices) -> list[BaseTool]:
    """构造检索 + 校验执行闭环工具集（§7.2–§7.8、§7.12；doc 6.3 §5）。"""
    return [
        # 检索（步骤 2）
        make_search_schema_tool(services),
        make_inspect_tables_tool(services),
        make_plan_joins_tool(services),
        # 时间／维度证据（步骤 3）
        make_resolve_time_range_tool(services),
        make_probe_dimension_values_tool(services),
        make_reuse_evidence_tool(services),
        # 校验与执行（步骤 3）
        make_validate_sql_tool(services),
        make_execute_sql_tool(services),
        # 结果读取与分析（步骤 5）
        make_read_result_tool(services),
        make_analyze_result_tool(services),
        # 业务任务登记（doc 6.3）
        make_record_sub_tasks_tool(services),
    ]


__all__ = ["build_business_tools"]
