"""将采集画像加载到 MetaWeave 平台 PostgreSQL 的 CLI。"""

import click


@click.command("meta_load")
@click.option(
    "--type",
    "load_types",
    multiple=True,
    type=click.Choice(["db"]),
    help="本阶段仅支持 db（仅允许传入一次）",
)
@click.option(
    "--source-id", multiple=True, help="必填：已登记数据源 UUID（仅允许传入一次）"
)
@click.option(
    "--job-id", multiple=True, help="必填：已有作业工作区 UUID（仅允许传入一次）"
)
@click.option("--clean", is_flag=True, help="画像入库成功后，清理本次 db 目录顶层文件")
def meta_load_command(
    load_types: tuple[str, ...],
    source_id: tuple[str, ...],
    job_id: tuple[str, ...],
    clean: bool,
) -> None:
    """将指定工作区的画像加载到平台库；当前仅支持 db。"""
    if len(load_types) != 1:
        raise click.UsageError("必须且仅能指定一次 --type db")
    if len(source_id) != 1:
        raise click.UsageError("必须且仅能指定一次 --source-id")
    if len(job_id) != 1:
        raise click.UsageError("必须且仅能指定一次 --job-id")

    try:
        from backend.profile_loading.db_loader import (
            MetaLoadConfigError,
            load_db_profile,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "sqlalchemy":
            raise click.UsageError(
                "meta_load 需要平台依赖，请执行 uv sync --extra platform"
            ) from None
        raise

    try:
        result = load_db_profile(source_id[0], job_id[0], clean=clean)
    except MetaLoadConfigError as exc:
        raise click.UsageError(str(exc)) from None

    suffix = f"（job_id: {result['job_id']}"
    if result.get("publish_id"):
        suffix += f"，publish_id: {result['publish_id']}"
    suffix += "）"
    if not result["success"]:
        click.echo(f"❌ {result['message']}{suffix}", err=True)
        raise click.Abort()
    click.echo(f"✅ {result['message']}{suffix}")
