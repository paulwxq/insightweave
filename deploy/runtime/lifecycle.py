"""共享部署生命周期门闩（doc 13 §7.5、§8.1）。

仅当部署开关启用（``DEPLOY_CONTROL_ENABLED=1``）时由两个 API 惰性导入并装配；
默认开发启动不依赖本模块所在路径。语义：

- 控制操作 ``<prefix>/{enter,state,leave}``：``state`` 为 GET，``enter``/``leave``
  为 POST，须携带 ``X-InsightWeave-Deploy-Token``；不经 Nginx 代理（doc 13 §7.5）。
- 维护期：普通业务 API 返回 503 + ``Retry-After``，不计入活动数；健康路径与
  控制路径放行且不计入；携有效令牌的 GET/HEAD 读取冒烟放行且计入。
- 活动计数覆盖 ASGI 调用开始到 ``finally``（含响应发送后的 BackgroundTasks）；
  executor future 由 ``submit_tracked()`` 在提交前登记、完成后释放。
- 启动时读取维护标记文件（只读挂载的 ``control/`` 目录），存在则初始处于维护。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONTROL_TOKEN_HEADER = "x-insightweave-deploy-token"
RETRY_AFTER_SECONDS = "5"
DEFAULT_CONTROL_DIR = "/run/insightweave-deploy"

_CONTROL_METHOD = {"state": "GET", "enter": "POST", "leave": "POST"}


def load_contract_version() -> int:
    """读取 deploy/contract.json 并校验为唯一受支持的版本 1。"""
    contract_path = Path(__file__).resolve().parents[1] / "contract.json"
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    version = data["deployment_contract_version"]
    if version != 1:
        raise ValueError(f"不支持的 deployment_contract_version: {version!r}")
    return version


async def _json_response(
    send: Callable[..., Any],
    status: int,
    payload: dict[str, Any],
    *,
    allow: str | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if allow is not None:
        headers.append((b"allow", allow.encode("ascii")))
    if status == 503:
        headers.append((b"retry-after", RETRY_AFTER_SECONDS.encode("ascii")))
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header(scope: dict[str, Any], name: str) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.decode("latin-1").lower() == name:
            return value.decode("latin-1")
    return None


class DeploymentGate:
    """进程内部署门闩：维护状态、业务活动计数与 future 追踪。"""

    def __init__(
        self,
        *,
        token: str,
        marker_path: str | Path,
        health_paths: tuple[str, ...] | frozenset[str] = (),
        contract_version: int = 1,
    ) -> None:
        if not token:
            raise ValueError("DeploymentGate 缺少控制令牌")
        self._token = token
        self._marker_path = Path(marker_path)
        self._health_paths = frozenset(health_paths)
        self._contract_version = contract_version
        self._lock = threading.Lock()
        self._maintaining = self._marker_path.exists()
        self._active = 0

    # ------------------------------------------------------------ 状态

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "deployment_contract_version": self._contract_version,
                "maintaining": self._maintaining,
                "active": self._active,
            }

    def enter(self) -> None:
        with self._lock:
            self._maintaining = True

    def leave(self) -> None:
        with self._lock:
            self._maintaining = False

    def is_maintaining(self) -> bool:
        with self._lock:
            return self._maintaining

    # ------------------------------------------------------------ 计数

    def _acquire(self) -> None:
        with self._lock:
            self._active += 1

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    def try_admit(self, *, during_maintenance: bool = False) -> bool:
        """同一把锁内完成"维护判定 + 计数登记"（doc 13 §7.5、审核 M1）。

        返回 False 表示处于维护且不属于受控读取：请求被拒且不计入活动数。
        ``during_maintenance=True`` 供携令牌读取冒烟使用：维护期仍放行并计数。
        风险边界：当前单 worker 事件循环下两步分离并无实际抢占窗口；收敛为
        同锁原子是为兑现 §7.5 的字面契约并防住跨线程 enter（如测试/工具）。
        """
        with self._lock:
            if self._maintaining and not during_maintenance:
                return False
            self._active += 1
            return True

    def submit_tracked(
        self, submit: Callable[[Callable[[], Any]], Any], fn: Callable[[], Any]
    ) -> Any:
        """提交前登记、future 完成释放；提交失败立即回滚（doc 13 §7.5）。"""
        self._acquire()
        try:
            future = submit(fn)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(lambda _f: self._release())
        return future

    # ------------------------------------------------------------ ASGI

    @property
    def middleware_class(self) -> type[DeploymentGateMiddleware]:
        return DeploymentGateMiddleware


class DeploymentGateMiddleware:
    """纯 ASGI 中间件：控制分派 → 健康/维护判定 → 业务计数。"""

    def __init__(self, app: Any, *, gate: DeploymentGate, control_prefix: str) -> None:
        self.app = app
        self.gate = gate
        self.control_prefix = control_prefix.rstrip("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        method: str = scope["method"].upper()

        if path == self.control_prefix or path.startswith(self.control_prefix + "/"):
            await self._dispatch_control(path, method, scope, send)
            return

        if path in self.gate._health_paths:
            await self.app(scope, receive, send)
            return

        maintenance = self.gate.is_maintaining()
        if maintenance:
            token_ok = _header(scope, CONTROL_TOKEN_HEADER) == self.gate._token
            read_smoke = token_ok and method in ("GET", "HEAD")
            if not read_smoke:
                await _json_response(send, 503, {"error": "maintenance_mode"})
                return
        # 放行的业务请求（含携令牌读取冒烟）计入活动数；同锁原子准入，
        # 关闭 is_maintaining 判定与登记之间的竞态窗口（审核 M1）
        if not self.gate.try_admit(during_maintenance=maintenance):
            await _json_response(send, 503, {"error": "maintenance_mode"})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate._release()

    async def _dispatch_control(
        self, path: str, method: str, scope: dict[str, Any], send: Any
    ) -> None:
        operation = (
            path[len(self.control_prefix) + 1 :] if path != self.control_prefix else ""
        )
        expected = _CONTROL_METHOD.get(operation)
        if expected is None:
            await _json_response(send, 404, {"error": "deploy_control_not_found"})
            return
        if method != expected:
            await _json_response(
                send,
                405,
                {"error": "deploy_control_method_not_allowed"},
                allow=expected,
            )
            return
        if _header(scope, CONTROL_TOKEN_HEADER) != self.gate._token:
            await _json_response(send, 403, {"error": "deploy_control_forbidden"})
            return
        if operation == "enter":
            self.gate.enter()
        elif operation == "leave":
            self.gate.leave()
        await _json_response(send, 200, self.gate.state())


def install_deployment_gate(
    app: Any,
    *,
    control_prefix: str,
    health_paths: tuple[str, ...] = (),
) -> DeploymentGate:
    """在部署开关启用时装配门闩；缺少令牌直接失败，不静默放开。"""
    token = os.environ.get("DEPLOY_CONTROL_TOKEN")
    if not token:
        raise RuntimeError(
            "DEPLOY_CONTROL_ENABLED=1 但缺少 DEPLOY_CONTROL_TOKEN，拒绝启动"
        )
    control_dir = os.environ.get("DEPLOY_CONTROL_DIR", DEFAULT_CONTROL_DIR)
    gate = DeploymentGate(
        token=token,
        marker_path=Path(control_dir) / "maintenance",
        health_paths=health_paths,
        contract_version=load_contract_version(),
    )
    app.add_middleware(
        DeploymentGateMiddleware, gate=gate, control_prefix=control_prefix
    )
    return gate
