import sys; sys.path.insert(0, "/opt/projects/insightweave/nl2sql")
"""旧管线 SQL 校验器 sqlparse 行为双环境探针（doc1 §6.4 补充证据）。

仅调用不触库的纯解析路径：validate()/_check_syntax/_check_security/
extract_tables_from_sql。对比输出一致性。
"""
from src.tools.validation.sql_validation import SQLValidationTool

tool = SQLValidationTool(config={"allow_multiple_statements": False, "allowed_sql_types": ["SELECT"]})
cases = [
    "SELECT a, b FROM orders WHERE dt = '2026-10-01'",
    "SELECT 1; SELECT 2",
    "DELETE FROM orders",
    "UPDATE orders SET a = 1",
    "SELECT * FROM t1 JOIN t2 ON t1.id = t2.id",
    "not a sql at all ((((",
]
for sql in cases:
    r = tool.validate(sql)
    print(repr(sql), "->", "errors=", r["errors"], "warnings=", r["warnings"])
print("tables:", tool.extract_tables_from_sql("SELECT x FROM foo JOIN bar ON foo.id=bar.id"))
