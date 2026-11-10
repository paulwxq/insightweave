"""Streamlit 1.67.0 无头页面冒烟：AppTest 执行 frontend/app.py 全脚本。"""
import sys
sys.path.insert(0, "/opt/projects/insightweave/nl2sql/frontend")
from streamlit.testing.v1 import AppTest

at = AppTest.from_file("/opt/projects/insightweave/nl2sql/frontend/app.py", default_timeout=30)
at.run()
exceptions = [str(e.value)[:200] for e in at.exception]
print("exceptions:", exceptions)
warnings = [w.value for w in at.warning]
print("warnings:", warnings)
print("titles:", [t.value for t in at.title][:3])
assert not exceptions, f"页面执行异常: {exceptions}"
assert any("无法连接后端服务" in w for w in warnings), "后端未启动时应有连接警告（v1 8008 未运行为预期）"
print("AppTest smoke passed")
