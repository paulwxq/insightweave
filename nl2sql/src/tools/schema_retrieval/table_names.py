"""表名显示转换工具（§4.2 唯一翻译点）。

内部全链路使用三段式表键（db.schema.table，与 Neo4j `id` 同空间）；
仅在 LLM 提示词边界经 to_display_name 截断为两段式显示名（schema.table）。

本模块只包含表名转换的纯函数，不依赖 Agent 或数据库客户端，
供 sql_generation.py / join_planner.py / value_matcher.py 共同导入，
保持 modules → tools 的依赖方向。
"""


def to_display_name(s: str) -> str:
    """三段表键 → 两段式显示名（剥掉第一段 db 名）。

    段数感知防误剥：仅当分段数 >= 3 时剥首段；两段及以下原样返回（幂等），
    非 public schema（如 orders.sales.orders → sales.orders）得以保留。
    """
    parts = s.split(".")
    return ".".join(parts[1:]) if len(parts) >= 3 else s
