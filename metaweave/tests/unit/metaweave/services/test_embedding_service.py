"""EmbeddingService 初始化的 dashscope base 地址配置行为测试。

背景：原实现将 api_base 写入 dashscope.base_url（SDK 从不读取的死属性），
导致配置的 api_base 被静默丢弃；官方覆盖属性为 dashscope.base_http_api_url。
"""

import dashscope
import pytest

from metaweave.services.embedding_service import EmbeddingService

BASE_CONFIG = {
    "active": "qwen",
    "providers": {"qwen": {"model": "text-embedding-test", "api_key": "sk-test"}},
}


@pytest.fixture(autouse=True)
def _preserve_dashscope_globals():
    """dashscope 模块级全局变量在测试前后保持原值，避免泄漏到其他测试。"""
    saved_key = dashscope.api_key
    saved_base = getattr(dashscope, "base_http_api_url", None)
    yield
    dashscope.api_key = saved_key
    dashscope.base_http_api_url = saved_base
    # 历史版本会误设 base_url 死属性，测试后清理避免污染
    if hasattr(dashscope, "base_url"):
        del dashscope.base_url


class TestApiBaseConfig:
    def test_api_base_sets_official_base_http_api_url(self):
        """配置 api_base 时应写入 SDK 官方属性 base_http_api_url。"""
        cfg = {
            "active": "qwen",
            "providers": {
                "qwen": {
                    "model": "text-embedding-test",
                    "api_key": "sk-test",
                    "api_base": "https://example.com/api/v1",
                }
            },
        }

        EmbeddingService(cfg)

        assert dashscope.base_http_api_url == "https://example.com/api/v1"

    def test_no_api_base_keeps_default(self):
        """未配置 api_base 时不应覆盖 SDK 默认端点。"""
        default_base = dashscope.base_http_api_url

        EmbeddingService(BASE_CONFIG)

        assert dashscope.base_http_api_url == default_base
