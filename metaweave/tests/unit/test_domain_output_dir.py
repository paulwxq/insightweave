"""受管 Domain 目录 resolver 用例（doc 21.1 §5.5）。

与 `resolve_md_output_dir()` 同一套规范化，固定为
`output/<source_id>/<job_id>/domain/`。
"""

import pytest

from metaweave.core.clean_targets import resolve_domain_output_dir

SOURCE = "b4a87275-7c37-4feb-bf1c-e5a8446c174f"
JOB = "3af29a0b-fdbd-4074-ac29-5aa9b4dd2179"


def test_resolves_expected_domain_dir(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    assert resolve_domain_output_dir(SOURCE, JOB) == (
        tmp_path.resolve() / "output" / SOURCE / JOB / "domain"
    )


def test_rejects_non_uuid_input():
    with pytest.raises(ValueError):
        resolve_domain_output_dir("not-a-uuid", JOB)
    with pytest.raises(ValueError):
        resolve_domain_output_dir(SOURCE, "not-a-uuid")


def test_rejects_workspace_level_symlink(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    real = tmp_path / "elsewhere" / SOURCE
    real.mkdir(parents=True)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / SOURCE).symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_domain_output_dir(SOURCE, JOB)


def test_rejects_domain_level_symlink(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    workspace = tmp_path / "output" / SOURCE / JOB
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside-domain"
    outside.mkdir()
    (workspace / "domain").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        resolve_domain_output_dir(SOURCE, JOB)
