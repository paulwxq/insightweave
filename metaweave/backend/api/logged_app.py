"""部署专用应用工厂（doc 15 §3.3）。

先加载 ``api`` 运行配置取得校验后的模块注册表，再构造现有 FastAPI
应用，最后包上外层纯 ASGI 请求日志包装器。启动命令::

    uv run --extra platform uvicorn backend.api.logged_app:create_logged_app \
        --factory --no-access-log --host 127.0.0.1 --port 8001

继续使用 ``backend.api.main:create_app`` 入口不会自动得到
``logs/api.log`` 与 ``logs/backend/*.log``。
"""

from backend.api.main import create_app
from backend.api.request_logging import RequestLoggingApp
from services.logging_manager import configure_logging


def create_logged_app():
    setup = configure_logging("api")
    app = create_app()
    return RequestLoggingApp(app, setup.modules)
