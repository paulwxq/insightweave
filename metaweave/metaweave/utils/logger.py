"""MetaWeave 日志工具模块（CLI 兼容包装与步骤分流）

配置装载委托给共享入口 ``services/logging_manager.py`` 的 ``script``
运行配置（doc 15 §3.1）：顶层 YAML 就是 script 配置原形，本模块只保留
CLI 兼容签名、步骤分流（``StepFilter`` / ``set_current_step()``）与
步骤记录工厂。
"""

import logging
from typing import Optional, Iterable

from services.logging_manager import SCRIPT_PROFILE, configure_logging


_CURRENT_STEP: str = "-"
_STEP_RECORD_FACTORY_INSTALLED: bool = False
_ORIGINAL_RECORD_FACTORY = logging.getLogRecordFactory()


def set_current_step(step: Optional[str]) -> None:
    """设置当前运行 step（用于按 step 路由日志文件）"""
    global _CURRENT_STEP
    _CURRENT_STEP = (step or "-").strip().lower() or "-"


def get_current_step() -> str:
    """获取当前运行 step（默认 '-'）"""
    return _CURRENT_STEP


def _install_step_log_record_factory() -> None:
    """全局注入 LogRecord.step 字段（只安装一次）。

    说明：
    - logging 的 logger-level filter 不会在 propagate 链路的父 logger 上执行，
      因此无法依赖“只在 root/metaweave logger 挂一次 filter”来注入字段。
    - 使用 LogRecordFactory 是最稳定的“全局注入一次”的方式，避免每个 handler 重复注入。
    """
    global _STEP_RECORD_FACTORY_INSTALLED
    if _STEP_RECORD_FACTORY_INSTALLED:
        return

    base_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        if not hasattr(record, "step"):
            record.step = get_current_step()
        else:
            record.step = get_current_step()
        return record

    logging.setLogRecordFactory(factory)
    _STEP_RECORD_FACTORY_INSTALLED = True


class StepFilter(logging.Filter):
    """按 current_step 放行的过滤器（供 logging.config.dictConfig 引用）"""

    def __init__(self, allowed_steps: Optional[Iterable[str]] = None):
        super().__init__()
        self.allowed_steps = {
            str(s).strip().lower() for s in (allowed_steps or []) if str(s).strip()
        }

    def filter(self, record: logging.LogRecord) -> bool:
        return get_current_step() in self.allowed_steps


def setup_metaweave_logging(config_path: Optional[str] = None) -> None:
    """初始化 MetaWeave 日志系统（CLI 兼容包装，doc 15 §3.1）

    委托共享入口装载 ``script`` 运行配置：整段沿用 YAML 顶层标准配置，
    相对日志路径统一经 ``resolve_project_path()`` 解析；随后安装步骤
    记录工厂（仅 script 进程使用，API 运行配置不经过本函数）。

    Args:
        config_path: 日志配置文件路径（可选）；缺省 configs/logging.yaml

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置文件不是普通文件或内容非法
    """
    configure_logging(SCRIPT_PROFILE, config_path)
    _install_step_log_record_factory()


def get_metaweave_logger(module_name: str) -> logging.Logger:
    """获取 MetaWeave 模块日志器

    Args:
        module_name: 模块名（如 "metadata", "relationships"）

    Returns:
        logging.Logger: 日志器实例（命名: metaweave.<module_name>）

    Examples:
        >>> logger = get_metaweave_logger("metadata")
        >>> logger.info("开始生成元数据")

        >>> logger = get_metaweave_logger("relationships.scorer")
        >>> logger.debug("计算候选评分")
    """
    logger_name = f"metaweave.{module_name}"
    return logging.getLogger(logger_name)
