"""受管 cql 目录 resolver 用例（doc 23.2 §5.1、doc 6 §2）。

与 `resolve_md_output_dir()` / `resolve_rel_output_dir()` 同一套规范化，
固定为 `output/<source_id>/<workspace_id>/cql/`（短 ID + 工作区）——
cql 产物目录由发布过程创建，无采集步骤行。
"""

import pytest

from metaweave.core.clean_targets import resolve_cql_output_dir

SOURCE = "db_src_1"
WORKSPACE = "ws_1"


def test_resolves_expected_cql_dir(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    assert resolve_cql_output_dir(SOURCE, WORKSPACE) == (
        tmp_path.resolve() / "output" / SOURCE / WORKSPACE / "cql"
    )


def test_rejects_non_short_id_input():
    with pytest.raises(ValueError):
        resolve_cql_output_dir("not-a-short-id", WORKSPACE)
    with pytest.raises(ValueError):
        resolve_cql_output_dir(SOURCE, "not-a-short-id")


def test_rejects_workspace_level_symlink(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    real = tmp_path / "elsewhere" / SOURCE
    real.mkdir(parents=True)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / SOURCE).symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_cql_output_dir(SOURCE, WORKSPACE)


def test_rejects_cql_level_symlink(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    workspace = tmp_path / "output" / SOURCE / WORKSPACE
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside-cql"
    outside.mkdir()
    (workspace / "cql").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_cql_output_dir(SOURCE, WORKSPACE)
