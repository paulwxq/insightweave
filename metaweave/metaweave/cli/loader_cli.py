"""RAG 数据加载 CLI 命令（rag_load，设计见 docs/update/24、docs/update/27 与 docs/data_gov/20.1）

将生成产物加载到向量库（Milvus）与图库（Neo4j）：
- --type 必选（仅允许传入一次）：table_schema / cql 单类（dim_value 已随 doc 22.2
  移除、sql 已随 doc 25.2 移除，均由 Web 发布替代）
- table_schema 仅供开发调试：必须同时传 --source_id / --workspace_id，直接读取该工作区的
  JSON/MD 并按 source_id 替换共用集合中的记录；不访问平台库、不登记 publish_info、
  不改变正式 current（见 doc 20.1 §10.7）
- 任一类型失败 → 退出码非 0
"""

import click
import logging
import time
from pathlib import Path
from typing import Dict, Tuple

from metaweave.core.loaders.factory import LoaderFactory
from metaweave.utils.file_utils import resolve_project_path
from services.config_loader import ConfigLoader

logger = logging.getLogger("metaweave.cli")

# 唯一类型枚举源（--type 的 Choice）
RAG_LOAD_TYPES = ("table_schema", "cql")


def _fail_result(message: str, elapsed: float) -> Dict[str, object]:
    """单类型加载失败结果（契约见 docs/update/24 §3.1）。"""
    return {"success": False, "execution_time": elapsed, "message": message}


def _run_single_load(
    load_type: str,
    config_path: Path,
    clean: bool,
    debug: bool,
    expected_clean_target: object = None,
    emit_clean_warning: bool = True,
    source_id: str | None = None,
    workspace_id: str | None = None,
) -> Dict[str, object]:
    """执行单个类型的完整加载（原 load 命令主体提取，契约见 docs/update/24 §3.1）。

    - 永远只返回结果 dict，不抛 click.Abort（Abort 由命令函数统一发出）；
    - execution_time 由入口 perf_counter 统一计时（含配置解析/校验/加载），
      覆盖 validate / 工厂创建 / 异常等无 loader 耗时返回的失败路径；
    - 各失败出口的错误输出原位保留在本函数内；
    - expected_clean_target / emit_clean_warning：clean 前运行期目标一致性复核
      与通用清空警告开关；命令入口均取默认值（None / True）。
    """
    start = time.perf_counter()
    try:
        # 设置日志级别
        if debug:
            logging.getLogger("metaweave").setLevel(logging.DEBUG)

        # 解析配置文件路径
        config_path = resolve_project_path(config_path)

        click.echo(f"📋 加载配置: {config_path}")

        # 检查加载类型是否已实现
        supported_types = LoaderFactory.get_supported_types()
        if load_type not in supported_types:
            click.echo(f"⚠️  加载类型 '{load_type}' 尚未实现", err=True)
            click.echo(f"当前支持的类型: {', '.join(supported_types)}")
            return _fail_result(
                f"加载类型 '{load_type}' 尚未实现", time.perf_counter() - start
            )

        # 通过 ConfigLoader 加载主配置文件（支持环境变量替换）
        full_config = ConfigLoader(str(config_path)).load()

        # clean 目标解析与运行期复核（doc 26 §6.2）：clean=true 时**始终先经
        # 共享 resolver**（独立 CLI 共用安全边界）；必须位于派生/操作 loaders
        # 之前——loaders 形态非法（如 []）时由 resolver 给出带完整配置路径的
        # 友好错误，而非 TypeError（用户审核第四轮第 3 条）。
        # 传入 expected 时再比较 identity；解析失败或目标不一致 →
        # 不清理任何目标，任务失败（exit 1）
        # table_schema 不清空集合（doc 20.1 §10.7）：--clean 与否都只按当前 source_id 替换，
        # 因此既不解析 clean 目标，也不提示清空整张集合。
        # cql 同理（doc 4 §4.4）：普通发布按源替换，全库清空仅属一次性维护
        # 操作，--clean 对 cql 无额外清理。
        if clean and load_type not in ("table_schema", "cql"):
            from metaweave.core.clean_targets import resolve_clean_target

            try:
                _current = resolve_clean_target("rag_load", load_type, full_config)
            except ValueError as e:
                _msg = f"clean 当前目标无法解析，未执行任何清理: {e}"
                click.echo(f"❌ {_msg}", err=True)
                return _fail_result(_msg, time.perf_counter() - start)
            if expected_clean_target is not None and _current[
                "identity"
            ] != expected_clean_target.get("identity"):
                _msg = (
                    "clean 目标与预警不一致，未执行任何清理: "
                    f"预警={expected_clean_target.get('display')} "
                    f"当前={_current['display']}"
                )
                click.echo(f"❌ {_msg}", err=True)
                return _fail_result(_msg, time.perf_counter() - start)

        # 主配置与依赖节点形态统一校验（用户审核第五轮第 2 条）：任何节点
        # 类型错误转为带完整配置路径的结构化失败，不让 TypeError/AttributeError
        # 逃逸（空文件 → None、顶层列表、database/sql_rag 非 mapping 等）。
        from metaweave.utils.file_utils import require_mapping

        try:
            if not isinstance(full_config, dict):
                raise ValueError(
                    f"主配置必须是对象（mapping），实际为 "
                    f"{type(full_config).__name__}"
                )
            loaders_cfg = (
                require_mapping(full_config.get("loaders"), "loaders")
                if "loaders" in full_config
                else {}
            )
            _sub_key = {
                "table_schema": "table_schema_loader",
                "cql": "cql_loader",
            }[load_type]
            if _sub_key in loaders_cfg:
                require_mapping(loaders_cfg[_sub_key], f"loaders.{_sub_key}")
            if source_id is not None:
                # 必须在节点形态校验之后：loaders 非 mapping（如 []）时由上面给出
                # “配置错误: loaders 必须是对象（mapping）”，而非 AttributeError。
                from metaweave.core.table_schema.managed_input import (
                    configure_managed_loader,
                )

                if "loaders" not in full_config:
                    full_config["loaders"] = loaders_cfg
                configure_managed_loader(loaders_cfg, source_id, workspace_id)
        except ValueError as e:
            click.echo(f"❌ 配置错误: {e}", err=True)
            return _fail_result(str(e), time.perf_counter() - start)

        # 从主配置快照派生 loader 配置（doc 26 §6.2：深拷贝派生副本，
        # 不污染 full_config["loaders"] 引用；注入的 loader 不再读盘）
        import copy as _copy

        loader_config = _copy.deepcopy(loaders_cfg)
        loader_config["metadata_config_file"] = str(config_path)

        # 显示警告（如果需要清空数据库）
        if clean and load_type == "table_schema":
            click.echo(
                "ℹ️  table_schema 不会清空集合：将先写入本批记录，再仅删除当前 source_id "
                "下不属于本批次的旧记录（--clean 对该类型无额外清理）"
            )
        elif clean and load_type == "cql":
            click.echo(
                "ℹ️  cql 不再清空整库（doc 4）：按脚本头 source_id 整源替换，"
                "单一数据事务内删除→写入→按源核验（--clean 对该类型无额外清理）"
            )
        elif clean and emit_clean_warning:
            click.echo("")
            click.echo("⚠️  警告: 您即将清空目标数据库的所有数据！")
            click.echo("⚠️  此操作不可逆，请确认您知道自己在做什么。")
            click.echo("")

        # 创建加载器
        click.echo(f"🔧 创建加载器: {load_type.upper()}Loader")
        loader = LoaderFactory.create(
            load_type, loader_config, metadata_config=full_config
        )

        # 执行加载（传递 clean 参数）
        click.echo("")
        click.echo("=" * 60)
        click.echo(f"🚀 开始加载 {load_type.upper()} 数据")
        click.echo("=" * 60)
        click.echo("")

        # 支持 clean 参数的加载器（dim 别名已从 CLI 移除，doc 27 §3）
        if load_type == "table_schema":
            # 调试批次 UUID 仅作 Milvus publish_run_id 标记，不登记平台库（doc 20.1 §10.7）
            click.echo(f"🏷️  调试批次 UUID: {getattr(loader, 'publish_run_id', '')}")
            try:
                if not loader.validate():
                    click.echo("❌ 验证失败", err=True)
                    return _fail_result("验证失败", time.perf_counter() - start)
                # 不论是否传 --clean，table_schema 都只按当前 source_id 替换
                result = loader.load(clean=False)
            finally:
                loader.close()
        elif load_type == "cql":
            if not loader.validate():
                click.echo("❌ 验证失败", err=True)
                return _fail_result("验证失败", time.perf_counter() - start)
            # 按源替换加载（doc 4）：CLI 调试路径未传 source_id 时由脚本头
            # ``// source_id:`` 决定删除范围；旧格式脚本会被拒绝
            result = loader.load()
        else:
            result = loader.execute()

        # 显示结果
        click.echo("")
        click.echo("=" * 60)
        click.echo("📊 加载结果")
        click.echo("=" * 60)

        if result.get("success"):
            click.echo("✅ 状态: 成功")
            click.echo(f"💬 消息: {result.get('message', '加载完成')}")

            # 显示统计信息（根据加载类型不同）
            if load_type == "table_schema":
                empty = result.get("empty_desc_columns") or []
                click.echo(
                    f"📦 加载对象 {result.get('objects_loaded', 0)} 个，"
                    f"空描述跳过字段 {len(empty)} 个"
                )
                click.echo("ℹ️  仅 Milvus 调试加载，未写平台库，不改变正式发布 current")
            if load_type == "cql":
                # CQL 加载器的统计信息
                sections = result.get("sections", [])
                if sections:
                    click.echo(f"📦 执行章节: {len(sections)} 个")
                    for i, section in enumerate(sections, 1):
                        status = "✅" if section.get("success") else "❌"
                        click.echo(f"  {status} [{i}] {section.get('name')}")

            # 显示执行时间
            execution_time = result.get("execution_time")
            if execution_time is not None:
                click.echo(f"⏱️  执行时间: {execution_time}s")

        else:
            click.echo("❌ 状态: 失败", err=True)
            click.echo(f"💬 消息: {result.get('message', '未知错误')}", err=True)

            # 显示错误列表
            errors = result.get("errors", [])
            if errors:
                click.echo("")
                click.echo("⚠️  错误详情:")
                for i, error in enumerate(errors[:5], 1):
                    click.echo(
                        f"  [{i}] {error.get('section')}: {error.get('error_message')}",
                        err=True,
                    )

                if len(errors) > 5:
                    click.echo(f"  ... 还有 {len(errors) - 5} 个错误", err=True)

        click.echo("=" * 60)

        if result.get("success"):
            click.echo("✨ 加载完成！")
            out = {
                "success": True,
                "execution_time": time.perf_counter() - start,
                "message": result.get("message", "加载完成"),
            }
            return out
        else:
            click.echo("⚠️  加载失败", err=True)
            return _fail_result(
                result.get("message", "未知错误"), time.perf_counter() - start
            )

    except FileNotFoundError as e:
        logger.error(f"文件不存在: {e}")
        click.echo(f"❌ 错误: 文件不存在 - {e}", err=True)
        return _fail_result(f"文件不存在 - {e}", time.perf_counter() - start)

    except ValueError as e:
        logger.error(f"配置错误: {e}")
        click.echo(f"❌ 错误: {e}", err=True)
        return _fail_result(str(e), time.perf_counter() - start)

    except Exception as e:
        logger.error(f"加载失败: {e}", exc_info=debug)
        click.echo(f"❌ 错误: {e}", err=True)
        if debug:
            import traceback

            click.echo("\n" + traceback.format_exc(), err=True)
        return _fail_result(str(e), time.perf_counter() - start)


@click.command(name="rag_load")
@click.option(
    "--type",
    "-t",
    "load_type",
    type=click.Choice(RAG_LOAD_TYPES, case_sensitive=False),
    multiple=True,
    help="加载类型（仅允许传入一次）：table_schema / cql",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    default="configs/metadata_config.yaml",
    show_default=True,
    help="主配置文件路径（metadata_config.yaml）",
)
@click.option(
    "--clean",
    is_flag=True,
    help="（已失效保留兼容）cql/table_schema 均按 source_id 替换，"
    "不再清空整库或整集合",
)
@click.option(
    "--source_id",
    type=str,
    help="table_schema 数据源短 ID db_src_N（必填，与 --workspace_id 同时传入）",
)
@click.option("--workspace_id", type=str, help="table_schema 工作区短 ID ws_N（必填）")
@click.option("--debug", is_flag=True, help="启用调试模式")
def rag_load_command(
    load_type: Tuple[str, ...],
    config: str,
    clean: bool,
    debug: bool,
    source_id=None,
    workspace_id=None,
):
    """RAG 数据加载：将生成产物加载到向量库（Milvus）与图库（Neo4j）

    加载类型（仅允许传入一次）：

    \b
    - table_schema : 表结构语义（仅供调试）→ 共用向量集合（MD + 同名 JSON 产物），
                     必须传 --source_id / --workspace_id，不访问平台库、不写发布记录
    - cql          : Cypher 脚本   → Neo4j 图库（cql 产物）

    任一失败最终退出码非 0。

    示例:

    \b
        # 调试加载某个采集作业的 MD + JSON（按 source_id 替换共用集合中的记录）
        metaweave rag_load --type table_schema --source_id <db_src_N> --workspace_id <ws_N>
    """
    # 归一化与重复/缺省检查（doc 27 §2/§3.1：multiple 元组 → 单值，
    # 重复传入即报错，缺省中文提示；均先于任何加载动作）
    if len(load_type) > 1:
        raise click.UsageError(
            f"--type 仅允许传入一次，检测到 {len(load_type)} 次: {', '.join(load_type)}"
        )
    selected_type = load_type[0] if load_type else None
    if not selected_type:
        raise click.UsageError("必须指定 --type（table_schema/cql）")
    if (source_id is None) != (workspace_id is None):
        raise click.UsageError("--source_id 和 --workspace_id 必须同时传入")
    if source_id is not None and selected_type != "table_schema":
        raise click.UsageError(
            "--source_id / --workspace_id 当前仅用于 --type table_schema"
        )
    if source_id is None and selected_type == "table_schema":
        raise click.UsageError(
            "--type table_schema 必须同时传入 --source_id 和 --workspace_id（不再读取全局 output/md、output/json）"
        )

    config_path = Path(config)

    if source_id is None:
        result = _run_single_load(selected_type, config_path, clean, debug)
    else:
        result = _run_single_load(
            selected_type,
            config_path,
            clean,
            debug,
            source_id=source_id,
            workspace_id=workspace_id,
        )

    # Abort 一律由命令函数发出（helper 只返回结果）；退出码 1
    if not result["success"]:
        raise click.Abort()
