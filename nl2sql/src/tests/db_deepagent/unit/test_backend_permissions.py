"""后端权限单元测试（docs/v4/5 §18.2 P09）

允许路径可读；根目录、未列路径、隐藏文件、越界符号链接被拒；
写／删除不在可分派工具中；结果目录不经 read_file 访问。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.agent import (
    build_backend,
    build_filesystem_middleware,
    build_permissions,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def _env(db_agent_config_module, tmp_path_factory):
    cfg = db_agent_config_module
    session_dir = tmp_path_factory.mktemp("session")
    (session_dir / "artifacts").mkdir()
    (session_dir / "empty_root").mkdir()
    backend = build_backend(session_dir, cfg)
    permissions = build_permissions(cfg)
    middleware = build_filesystem_middleware(backend, permissions, cfg)
    return backend, middleware, session_dir


@pytest.fixture(scope="module")
def db_agent_config_module(db_agent_config):
    return db_agent_config


def _read(middleware, path: str):
    """经 ToolNode 完整调用 read_file（含 runtime 注入），返回 (ok, 文本)。"""
    from langgraph.prebuilt import ToolNode

    from src.tests.db_deepagent.conftest import invoke_tool_node

    read_tool = next(t for t in middleware.tools if t.name == "read_file")
    node = ToolNode([read_tool])
    try:
        _updates, msg = invoke_tool_node(
            node,
            [("read_file", {"file_path": path}, "tc-read")],
            context=None,
        )
    except Exception as exc:  # noqa: BLE001（权限拒绝以错误 ToolMessage 呈现）
        return False, str(exc)
    text = msg.content if isinstance(msg.content, str) else str(msg.content)
    ok = (
        getattr(msg, "status", None) != "error"
        and "denied" not in text.lower()
        and "permission" not in text.lower()
    )
    return ok, text


def _dispatchable_names(middleware) -> set[str]:
    return {getattr(tool, "name", "") for tool in (middleware.tools or [])}


class TestP09Permissions:
    def test_skills_readable(self, _env):
        backend, middleware, _ = _env
        ok, content = _read(middleware, "/skills/query-planning/SKILL.md")
        assert ok and "查询规划" in str(content)

    def test_root_and_unlisted_denied(self, _env):
        backend, middleware, _ = _env
        ok, _ = _read(middleware, "/some/unlisted.txt")
        assert not ok

    def test_hidden_files_denied(self, _env):
        backend, middleware, session_dir = _env
        (session_dir / "artifacts" / ".env").write_text("secret", encoding="utf-8")
        ok, _ = _read(middleware, "/artifacts/.env")
        assert not ok

    def test_out_of_root_symlink_denied(self, _env, tmp_path):
        backend, middleware, session_dir = _env
        target = tmp_path / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        link = session_dir / "artifacts" / "link.txt"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("环境不支持符号链接")
        ok, _ = _read(middleware, "/artifacts/link.txt")
        assert not ok

    def test_no_write_delete_execute_in_dispatchable(self, _env):
        _backend, middleware, _ = _env
        names = _dispatchable_names(middleware)
        assert {"write_file", "edit_file", "delete", "execute"} & names == set()
        assert {"ls", "read_file", "glob", "grep"} <= names

    def test_results_not_via_read_file(self, _env):
        """结果目录不在任何路由下，read_file 无法触达（§10.3）。"""
        backend, middleware, _ = _env
        ok, _ = _read(middleware, "/results/res_abc/rows.jsonl")
        assert not ok
