"""JsonLoader 共享能力用例（doc 19.1 §5/§7）：三段式 stem 共享解析与
`load_cached_object()` 缓存限定只读接口。
"""

import json
from pathlib import Path

import pytest

from metaweave.core.metadata.json_loader import (
    JsonLoader,
    JsonLoaderError,
    parse_strict_stem,
)

DB = "store_db"


def _document(obj: str = "orders", schema: str = "public", obj_type: str = "table"):
    return {
        "metadata_version": "3.0",
        "object_info": {
            "database": DB,
            "schema_name": schema,
            "object_name": obj,
            "object_type": obj_type,
            "comment": "",
            "comment_source": "",
            "total_rows": 0,
        },
        "column_profiles": {},
        "table_profile": None,
        "sample_records": {"sample_method": "none", "records": []},
    }


def _write(json_dir: Path, obj: str = "orders", schema: str = "public"):
    json_dir.mkdir(parents=True, exist_ok=True)
    (json_dir / f"{DB}.{schema}.{obj}.json").write_text(
        json.dumps(_document(obj, schema), ensure_ascii=False), encoding="utf-8"
    )


class TestParseStrictStem:
    def test_valid_three_parts(self):
        assert parse_strict_stem(f"{DB}.public.orders") == (DB, "public", "orders")

    @pytest.mark.parametrize(
        "bad", ["two.parts", "a.b.c.d", "x..y", ".a.b", "a.b.", ""]
    )
    def test_invalid_stems_return_none(self, bad):
        assert parse_strict_stem(bad) is None


class TestLoadCachedObject:
    def test_returns_metadata_from_cache_without_disk(self, tmp_path):
        json_dir = tmp_path / "json"
        _write(json_dir)
        loader = JsonLoader(json_dir, database_name=DB)
        assert [o.object_name for o in loader.list_objects("public")] == ["orders"]

        # 枚举后磁盘被删除：缓存限定接口仍可用（不回退读盘）
        (json_dir / f"{DB}.public.orders.json").unlink()
        metadata = loader.load_cached_object("public", "orders")
        assert metadata.table_name == "orders"

    def test_cache_miss_raises_without_disk_fallback(self, tmp_path):
        json_dir = tmp_path / "json"
        _write(json_dir)
        loader = JsonLoader(json_dir, database_name=DB)
        # 未枚举该 schema：缓存未命中，即使磁盘存在该文件也必须失败
        with pytest.raises(JsonLoaderError, match="缓存未命中"):
            loader.load_cached_object("public", "orders")
