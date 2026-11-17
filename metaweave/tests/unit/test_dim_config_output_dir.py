"""受管 Dim 目录 resolver 用例（doc 6 §2、6.2 §5.1）。

步骤标识 `dim_config` 已随断代改造更名为 `dim`，目录固定为
`output/<source_id>/<workspace_id>/dim/`（`resolve_dim_output_dir`）。
"""

import pytest

from metaweave.core.clean_targets import (
    resolve_dim_output_dir,
    resolve_step_output_dir,
)

SOURCE = "db_src_1"
WORKSPACE = "ws_1"


def test_resolves_expected_dim_dir(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    assert resolve_dim_output_dir(SOURCE, WORKSPACE) == (
        tmp_path.resolve() / "output" / SOURCE / WORKSPACE / "dim"
    )


def test_rejects_non_short_id_input():
    with pytest.raises(ValueError, match="数据源"):
        resolve_dim_output_dir("b4a87275-7c37-4feb-bf1c-e5a8446c174f", WORKSPACE)
    with pytest.raises(ValueError, match="工作区"):
        resolve_dim_output_dir(SOURCE, "not-a-workspace")
    # 旧 UUID 入口不再合法（doc 6 §1：短 ID 与 UUID 分开校验）
    with pytest.raises(ValueError):
        resolve_dim_output_dir("not-a-uuid", "ws_x")


def test_rejects_workspace_level_symlink(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    real = tmp_path / "elsewhere" / SOURCE
    real.mkdir(parents=True)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / SOURCE).symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_dim_output_dir(SOURCE, WORKSPACE)


def test_rejects_dim_level_symlink(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    workspace = tmp_path / "output" / SOURCE / WORKSPACE
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside-dim"
    outside.mkdir()
    (workspace / "dim").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_dim_output_dir(SOURCE, WORKSPACE)


def test_step_output_dir_rejects_unknown_step():
    with pytest.raises(ValueError, match="未知的受管步骤目录"):
        resolve_step_output_dir(SOURCE, WORKSPACE, "dim_config")
    # 更名后的白名单：dim 与 sql 均为合法步骤目录（doc 6 §2）
    assert resolve_step_output_dir(SOURCE, WORKSPACE, "dim").name == "dim"
    assert resolve_step_output_dir(SOURCE, WORKSPACE, "sql").name == "sql"
