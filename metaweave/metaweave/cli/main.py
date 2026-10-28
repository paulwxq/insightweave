"""MetaWeave CLI 主入口

提供命令行接口访问 MetaWeave 各个功能模块。
"""

import click
import logging
from typing import Optional

from metaweave.cli.metadata_cli import metadata_command
from metaweave.cli.loader_cli import rag_load_command
from metaweave.utils.logger import setup_metaweave_logging


@click.group()
@click.option("--debug", is_flag=True, help="启用调试模式")
@click.option(
    "--log-config", type=str, help="日志配置文件路径（相对路径基于项目根解析）"
)
def cli(debug: bool, log_config: Optional[str]):
    """MetaWeave - 数据库元数据自动生成和增强平台

    使用子命令访问不同的功能模块。
    """
    # 根 context 保存全局调试状态（子命令经 ctx.find_root().obj 读取，不各自声明 --debug）
    ctx = click.get_current_context()
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    # 初始化日志系统（doc 15 §3.1/§3.3：--log-config 不再用
    # click.Path(exists=True) 按 cwd 预检，改由共享入口基于项目根解析并
    # 检查"存在、可读、普通文件"）
    try:
        setup_metaweave_logging(log_config)
    except FileNotFoundError as exc:
        if log_config is None:
            # 默认配置缺失：明确警告并降级为控制台 basicConfig
            logging.basicConfig(
                level=logging.DEBUG if debug else logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            )
            click.echo(f"⚠️  日志配置文件不存在: {exc}", err=True)
        else:
            raise click.BadParameter(str(exc), param_hint="--log-config") from exc
    except ValueError as exc:
        # 配置存在但非法（非普通文件/非 mapping/坏 YAML/装载失败）：
        # 默认与显式一律明确失败，不静默降级（exit 2）
        if log_config is None:
            raise click.UsageError(f"默认日志配置非法: {exc}") from exc
        raise click.BadParameter(str(exc), param_hint="--log-config") from exc
    else:
        # 根据 --debug 动态提升控制台输出等级
        if debug:
            root_logger = logging.getLogger()
            root_logger.setLevel(logging.DEBUG)
            for handler in root_logger.handlers:
                handler.setLevel(logging.DEBUG)


# 注册子命令
cli.add_command(metadata_command)
cli.add_command(rag_load_command)


if __name__ == "__main__":
    cli()
