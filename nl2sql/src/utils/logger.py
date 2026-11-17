"""日志工具模块"""

import logging
import logging.config
import os
from pathlib import Path

import yaml


def get_module_logger(module_name: str) -> logging.Logger:
    """
    获取模块日志器（名称形如 nl2sql.<module>）。
    如未先显式 setup，该函数将返回同名 Logger，但不会自动添加处理器。
    """
    return logging.getLogger(f"nl2sql.{module_name}")


class QueryLoggerAdapter(logging.LoggerAdapter):
    """简单的查询上下文适配器：将 [query_id] 前缀加入消息。"""

    def process(self, msg, kwargs):
        query_id = self.extra.get("query_id")
        if query_id:
            msg = f"[{query_id}] {msg}"
        return msg, kwargs


def with_query_id(logger: logging.Logger, query_id: str) -> logging.LoggerAdapter:
    """基于给定 logger 创建带 query_id 前缀的适配器。"""
    return QueryLoggerAdapter(logger, {"query_id": query_id})


def setup_logging_from_yaml(yaml_path: str) -> None:
    """
    从 YAML 文件加载 logging 配置（dictConfig）。
    支持同时输出到控制台与文件，并允许通过 YAML 设置日志级别。

    NL2SQL_LOG_LEVEL 环境变量（可选）：设置后覆盖业务 logger（nl2sql.*）
    和 nl2sql_file handler 的级别。未设置时沿用 YAML 默认（本地开发
    为 DEBUG）。Docker 镜像默认 INFO（见 Dockerfile ENV）。非法值启动
    时明确报错。nl2sql.api 和 console 不受影响。
    """
    if not os.path.exists(yaml_path):
        raise FileNotFoundError(f"Logging config YAML 不存在: {yaml_path}")

    with open(yaml_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 确保文件型 handler 的目录存在
    for handler_name, handler_cfg in (cfg.get("handlers", {}) or {}).items():
        filename: str | None = handler_cfg.get("filename")
        if filename:
            log_path = Path(filename)
            log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.config.dictConfig(cfg)

    # NL2SQL_LOG_LEVEL 覆盖（仅影响 nl2sql 业务 logger + nl2sql_file）
    env_level = os.environ.get("NL2SQL_LOG_LEVEL")
    if env_level:
        env_level = env_level.strip().upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if env_level not in valid:
            raise ValueError(
                f"NL2SQL_LOG_LEVEL 非法值 {env_level!r}，可选：{', '.join(sorted(valid))}"
            )
        level = getattr(logging, env_level)
        logging.getLogger("nl2sql").setLevel(level)
        for handler in logging.getLogger("nl2sql").handlers:
            if handler.name == "nl2sql_file":
                handler.setLevel(level)
