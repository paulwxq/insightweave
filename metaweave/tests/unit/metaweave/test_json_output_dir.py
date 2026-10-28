"""受管 JSON 目录 resolver 用例（doc 18 §4、doc 6 §2）。

与 `resolve_db_output_dir()` / `resolve_ddl_output_dir()` 同一套规范化：
短 ID + 工作区、逐级符号链接拒绝、项目根边界与预期路径核对，固定为
`output/<source_id>/<workspace_id>/json/`。
"""

import pytest

from metaweave.core.clean_targets import resolve_json_output_dir

SOURCE = "db_src_1"
WORKSPACE = "ws_1"


def test_resolves_expected_json_dir(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    target = resolve_json_output_dir(SOURCE, WORKSPACE)
    assert target == (tmp_path.resolve() / "output" / SOURCE / WORKSPACE / "json")


def test_rejects_non_short_id_input():
    with pytest.raises(ValueError):
        resolve_json_output_dir("not-a-short-id", WORKSPACE)
    with pytest.raises(ValueError):
        resolve_json_output_dir(SOURCE, "not-a-short-id")


def test_rejects_symlink_in_any_workspace_level(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    real_workspace = tmp_path / "elsewhere" / SOURCE
    real_workspace.mkdir(parents=True)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / SOURCE).symlink_to(real_workspace, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_json_output_dir(SOURCE, WORKSPACE)


def test_rejects_json_level_symlink(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    workspace = tmp_path / "output" / SOURCE / WORKSPACE
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside-json"
    outside.mkdir()
    (workspace / "json").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_json_output_dir(SOURCE, WORKSPACE)
