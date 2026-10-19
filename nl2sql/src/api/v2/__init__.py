"""NL2SQL Web API v2（doc 2 §4.2）：db_deepagent 的新链 Web 入口。

仅挂载 /api/v2 路由；旧 /api/v1 query/history 不在本应用挂载（旧脚本独立
管理自己的生命周期）。统一封装 {code, message, data, trace_id}，错误码为
闭集，不能由异常类名/中文消息临时生成。
"""
