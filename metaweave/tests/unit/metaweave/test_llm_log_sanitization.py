"""LLM 注释链路日志脱敏：prompt 含样例数据，异常日志只记对象名与错误类别（doc 16 §4）。"""

import logging
from types import SimpleNamespace

from metaweave.core.metadata.comment_generator import CommentGenerator
from metaweave.services.llm_service import LLMService

SECRET = "SAMPLE_SECRET must stay out of logs"


class _ExplodingChatModel:
    """llm.invoke 抛出回显 prompt 内容的异常，模拟服务端错误文本带出样例值。"""

    def invoke(self, _messages):
        raise RuntimeError(f"request rejected, body: {SECRET}")


def _metadata():
    column = SimpleNamespace(
        column_name="note",
        data_type="text",
        comment="",
        comment_source="",
    )
    return SimpleNamespace(
        table_name="users",
        full_name="public.users",
        columns=[column],
    )


def test_table_comment_failure_logs_category_only(caplog):
    """generate_table_comment 失败：日志记表名与错误类别，不回显样例值。"""
    service = object.__new__(LLMService)
    service.llm = _ExplodingChatModel()

    with caplog.at_level(logging.ERROR, logger="metaweave.llm"):
        comment = service.generate_table_comment(
            "public.users",
            [{"name": "note", "type": "text"}],
            [{"note": SECRET}],
        )

    assert comment == ""
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "public.users" in caplog.text


def test_column_comments_failure_logs_category_only(caplog):
    """generate_column_comments 失败：日志记表名与错误类别，不回显样例值。"""
    service = object.__new__(LLMService)
    service.llm = _ExplodingChatModel()

    with caplog.at_level(logging.ERROR, logger="metaweave.llm"):
        comments = service.generate_column_comments(
            "public.users",
            [{"name": "note", "type": "text", "sample_values": [SECRET]}],
            [{"note": SECRET}],
        )

    assert comments == {}
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "public.users" in caplog.text


class _FailingLLMService:
    """LLM 服务替身：调用即抛出含样例数据的异常原文。"""

    model = "fake-model"

    def generate_table_comment(self, table_name, columns, sample_data=None):
        raise RuntimeError(f"prompt rejected: {sample_data}")

    def generate_column_comments(self, table_name, columns, sample_data=None):
        raise RuntimeError(f"prompt rejected: {sample_data}")


def test_comment_generator_failure_logs_category_only(caplog):
    """注释生成器兜底 except：日志记对象名与错误类别，不回显异常原文。"""
    generator = CommentGenerator(_FailingLLMService())
    metadata = _metadata()

    with caplog.at_level(logging.ERROR, logger="metaweave.comment_generator"):
        comment = generator.generate_table_comment(metadata)
        comments = generator.generate_column_comments(metadata)

    assert comment == ""
    assert comments == {}
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "public.users" in caplog.text


def _bare_service(extra_params: dict) -> LLMService:
    service = object.__new__(LLMService)
    service.model = "fake-model"
    service.api_key = "API_KEY_SECRET"
    service.temperature = 0.3
    service.timeout = 30
    service.sdk_max_retries = 3
    service.max_tokens = None
    service.api_base = None
    service.extra_params = extra_params
    return service


def _patch_chat_openai(monkeypatch) -> dict:
    """替换 langchain_openai.ChatOpenAI，返回捕获到的初始化参数。"""
    import langchain_openai

    captured: dict = {}

    def fake_chat_openai(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", fake_chat_openai)
    return captured


def test_deepseek_extra_param_values_stay_out_of_debug_logs(caplog, monkeypatch):
    """DeepSeek 额外参数仍传给客户端，但 debug 日志只记参数名。"""
    captured = _patch_chat_openai(monkeypatch)
    service = _bare_service({"wibble": SECRET})

    with caplog.at_level(logging.DEBUG, logger="metaweave.llm"):
        service._init_deepseek()

    assert captured["wibble"] == SECRET
    assert "wibble" in caplog.text
    assert SECRET not in caplog.text


def test_qwen_extra_param_values_stay_out_of_debug_logs(caplog, monkeypatch):
    """Qwen 顶层与 extra_body 参数仍传给客户端，但 debug 日志只记参数名。"""
    captured = _patch_chat_openai(monkeypatch)
    service = _bare_service({"wibble": SECRET, "enable_thinking": True})

    with caplog.at_level(logging.DEBUG, logger="metaweave.llm"):
        service._init_qwen()

    assert captured["wibble"] == SECRET
    assert captured["extra_body"] == {"enable_thinking": True}
    assert SECRET not in caplog.text
    assert "enable_thinking" in caplog.text
