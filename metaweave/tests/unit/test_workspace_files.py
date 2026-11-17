"""workspace_files：marker、两类备份与点号文件过滤（doc 6 §6、6.2 §5.2–§5.3）。"""

from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

from backend.workspace_files.backups import (
    EditBackupFailedError,
    backup_directory_before_update,
    backup_files_before_edit,
)
from backend.workspace_files.markers import (
    clear_collection_markers,
    read_collection_marker,
    write_collection_marker,
)
from backend.workspace_files.scan import list_visible_files
from backend.workspace_files.timestamps import (
    SHANGHAI_TZ,
    backup_dir_name,
    marker_timestamp,
    parse_marker_timestamp,
)

RUN = UUID("11111111-1111-4111-8111-111111111111")
RUN2 = UUID("22222222-2222-4222-8222-222222222222")


# ---- 时间戳 ----


def test_timestamp_format_is_shanghai_24h_millis():
    moment = datetime(2026, 10, 21, 15, 30, 5, 123456, tzinfo=SHANGHAI_TZ)
    assert backup_dir_name(moment) == "bak_20261021_153005_123"
    assert marker_timestamp(moment) == "20261021-153005123"


def test_marker_timestamp_parse_roundtrip():
    moment = datetime(2026, 1, 2, 3, 4, 5, 67000, tzinfo=SHANGHAI_TZ)
    parsed = parse_marker_timestamp(marker_timestamp(moment))
    assert parsed == moment
    assert parsed.utcoffset().total_seconds() == 8 * 3600


def test_marker_timestamp_rejects_garbage():
    with pytest.raises(ValueError):
        parse_marker_timestamp("20261021-1530012")
    with pytest.raises(ValueError):
        parse_marker_timestamp("garbage")


# ---- 采集 marker ----


def test_write_marker_clears_previous(tmp_path):
    step = tmp_path / "rel"
    step.mkdir()
    first = write_collection_marker(step, RUN, "success")
    second = write_collection_marker(step, RUN2, "failed")
    marker, error = read_collection_marker(step)
    assert error is None
    assert marker is not None
    assert marker.run_id == str(RUN2)
    assert marker.status == "failed"
    assert marker.file_name == second.file_name != first.file_name
    # 顶层只有一个 marker
    assert len(list(step.glob(".coll_run_id_*"))) == 1


def test_read_marker_states(tmp_path):
    empty = tmp_path / "db"
    empty.mkdir()
    assert read_collection_marker(empty) == (None, None)

    step = tmp_path / "dim"
    step.mkdir()
    write_collection_marker(step, RUN, "success")
    marker, error = read_collection_marker(step)
    assert error is None and marker is not None
    assert marker.run_id == str(RUN) and marker.status == "success"

    # 多个 marker：状态不明，不按时间挑一个
    (
        step
        / ".coll_run_id_33333333-3333-4333-8333-333333333333_20260101-000000000_success"
    ).touch()
    marker, error = read_collection_marker(step)
    assert marker is None and "多个采集 marker" in error


def test_write_marker_rejects_nonterminal_status(tmp_path):
    step = tmp_path / "md"
    step.mkdir()
    with pytest.raises(ValueError):
        write_collection_marker(step, RUN, "blocked")
    with pytest.raises(ValueError):
        write_collection_marker(step, RUN, "running")
    assert list(step.glob(".coll_run_id_*")) == []


def test_clear_markers_keeps_other_dotfiles(tmp_path):
    step = tmp_path / "json"
    step.mkdir()
    write_collection_marker(step, RUN, "success")
    other = step / ".hidden.txt"
    other.write_text("x")
    removed = clear_collection_markers(step)
    assert removed == 1
    assert other.exists()
    assert list(step.glob(".coll_run_id_*")) == []


# ---- 整目录备份（生成场景，A09/A10） ----


def _populate(step: Path):
    step.mkdir(parents=True)
    (step / "a.json").write_text("old-a")
    (step / ".hidden").write_text("dotfile")
    write_collection_marker(step, RUN, "success")
    backup = step / ".backup" / "bak_20260101_000000_000"
    backup.mkdir(parents=True)
    (backup / "old.json").write_text("history")
    (step / "sub").mkdir()
    (step / "sub" / "b.json").write_text("old-b")


def test_directory_backup_moves_all_including_dotfiles(tmp_path):
    step = tmp_path / "json"
    _populate(step)
    result = backup_directory_before_update(step)
    assert result.complete and result.message is None
    # 全部旧内容（含点号文件与旧 marker）移入唯一新备份；.backup 不嵌套
    backups = sorted((step / ".backup").iterdir())
    assert [p.name for p in backups] == [
        "bak_20260101_000000_000",
        result.backup_dir.name,
    ]
    new_backup = result.backup_dir
    assert (new_backup / "a.json").read_text() == "old-a"
    assert (new_backup / ".hidden").read_text() == "dotfile"
    assert (new_backup / "sub" / "b.json").read_text() == "old-b"
    assert list(new_backup.glob(".coll_run_id_*"))
    # 旧备份目录原样保留
    assert ((step / ".backup" / "bak_20260101_000000_000") / "old.json").exists()
    # 顶层只剩 .backup
    assert [child.name for child in step.iterdir()] == [".backup"]


def test_directory_backup_no_old_content_no_empty_backup(tmp_path):
    step = tmp_path / "db"
    step.mkdir()
    result = backup_directory_before_update(step)
    assert result.backup_dir is None and result.complete
    assert not (step / ".backup").exists()


def test_directory_backup_failure_deletes_leftover_and_continues(
    tmp_path, monkeypatch, caplog
):
    step = tmp_path / "rel"
    _populate(step)

    real_move = Path.rename

    def failing_move(src, dst, *args, **kwargs):
        src_path = Path(str(src))
        if src_path.name == "a.json":
            raise OSError("simulated move failure")
        return real_move(src_path, Path(str(dst)))

    import backend.workspace_files.backups as backups_module

    monkeypatch.setattr(backups_module.shutil, "move", failing_move)
    with caplog.at_level("WARNING"):
        result = backup_directory_before_update(step)
    assert not result.complete
    assert "备份不完整" in result.message
    # 失败项 a.json 被删除（残留旧内容清除），其余内容已移入备份
    assert not (step / "a.json").exists()
    assert (result.backup_dir / "sub" / "b.json").exists()
    assert (result.backup_dir / ".hidden").exists()
    assert any("备份不完整" in record.message for record in caplog.records)


def test_directory_backup_same_timestamp_unique_suffix(tmp_path):
    from backend.workspace_files.timestamps import SHANGHAI_TZ as TZ

    step = tmp_path / "md"
    step.mkdir()
    (step / "a.md").write_text("1")
    moment = datetime(2026, 10, 21, 12, 0, 0, 0, tzinfo=TZ)
    first = backup_directory_before_update(step, moment=moment)
    (step / "a.md").write_text("2")
    second = backup_directory_before_update(step, moment=moment)
    assert first.backup_dir.name == "bak_20261021_120000_000"
    assert second.backup_dir.name != first.backup_dir.name
    assert (first.backup_dir / "a.md").read_text() == "1"
    assert (second.backup_dir / "a.md").read_text() == "2"


# ---- 文件级备份（人工编辑，A28/A30） ----


def test_file_backup_copies_only_listed_existing_files(tmp_path):
    step = tmp_path / "domain"
    step.mkdir()
    (step / "db_domains.yaml").write_text("old")
    (step / "other.yaml").write_text("untouched")
    write_collection_marker(step, RUN, "success")
    backup_dir = backup_files_before_edit(step, ["db_domains.yaml", "missing.yaml"])
    assert backup_dir is not None
    assert (backup_dir / "db_domains.yaml").read_text() == "old"
    # 不移动原文件、不备份其他产物、不触碰 marker
    assert (step / "db_domains.yaml").exists()
    assert not (backup_dir / "other.yaml").exists()
    marker, _ = read_collection_marker(step)
    assert marker is not None and marker.status == "success"


def test_file_backup_failure_keeps_everything(tmp_path, monkeypatch):
    step = tmp_path / "rel"
    step.mkdir()
    (step / "orders.relationships_global.json").write_text("old-json")
    (step / "orders.relationships_global.md").write_text("old-md")
    write_collection_marker(step, RUN, "success")

    import backend.workspace_files.backups as backups_module

    real_copy2 = backups_module.shutil.copy2

    def failing_copy(src, dst, *args, **kwargs):
        if Path(str(dst)).name.endswith(".md"):
            raise OSError("simulated copy failure")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(backups_module.shutil, "copy2", failing_copy)
    with pytest.raises(EditBackupFailedError) as excinfo:
        backup_files_before_edit(
            step,
            ["orders.relationships_global.json", "orders.relationships_global.md"],
        )
    # 原文件、marker 全部保持原状；不删除任何现有内容
    assert (step / "orders.relationships_global.json").read_text() == "old-json"
    assert (step / "orders.relationships_global.md").read_text() == "old-md"
    marker, _ = read_collection_marker(step)
    assert marker is not None
    # 不完整备份保留但仅为人工核对；消息说明失败
    assert "文件级备份失败" in str(excinfo.value)


def test_file_backup_rejects_escape_paths(tmp_path):
    step = tmp_path / "dim"
    step.mkdir()
    with pytest.raises(EditBackupFailedError):
        backup_files_before_edit(step, ["../outside.yaml"])
    with pytest.raises(EditBackupFailedError):
        backup_files_before_edit(step, ["/etc/passwd"])
    with pytest.raises(EditBackupFailedError):
        backup_files_before_edit(step, [".backup/db_domains.yaml"])


# ---- 点号文件过滤（普通扫描） ----


def test_list_visible_files_skips_hidden_at_every_level(tmp_path):
    root = tmp_path
    (root / "a.json").write_text("1")
    (root / ".coll_run_id_x").write_text("marker")
    (root / "sub").mkdir()
    (root / "sub" / "b.json").write_text("2")
    (root / "sub" / ".tmp123").write_text("tmp")
    (root / ".backup").mkdir()
    (root / ".backup" / "c.json").write_text("backup")
    (root / ".hiddendir").mkdir()
    (root / ".hiddendir" / "d.json").write_text("hidden")
    assert list_visible_files(root) == ["a.json", "sub/b.json"]
