"""可由 Web 或 CLI 调用的 Question-SQL 生成与可选校验流程。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from metaweave.core.sql_rag.generator import QuestionSQLGenerator
from metaweave.core.sql_rag.models import GenerationResult
from metaweave.core.sql_rag.validator import SQLValidator


@dataclass
class SqlRagRunResult:
    generation: GenerationResult
    validation: dict[str, Any] | None


def run_sql_rag_generation(
    config_snapshot: dict,
    *,
    domains_path: Path,
    md_dir: Path,
    rel_dir: Path,
    output_dir: Path,
    llm_service: Any,
    connector: Any = None,
) -> SqlRagRunResult:
    """仅使用显式路径；目录清理由调用方完成。"""
    sql_cfg = config_snapshot["sql_rag"]
    generation_cfg = dict(sql_cfg["generation"])
    generation_cfg["output_dir"] = str(output_dir)
    generator = QuestionSQLGenerator(llm_service, generation_cfg)
    if generator.output_dir != output_dir:
        raise ValueError("SQL-RAG 输出目录与受管作业目录不一致")
    generation = generator.generate(
        domains_config_path=str(domains_path),
        md_dir=str(md_dir),
        rel_dir=str(rel_dir),
    )
    validation_cfg = sql_cfg["validation"]
    if not generation.success or not validation_cfg.get("enabled", True):
        return SqlRagRunResult(generation, None)
    if connector is None:
        raise ValueError("已启用 SQL 校验，但没有源库连接")
    validator = SQLValidator(
        connector=connector,
        config=validation_cfg,
        llm_service=(
            llm_service if validation_cfg.get("enable_sql_repair", False) else None
        ),
        md_dir=str(md_dir),
        rel_dir=str(rel_dir),
    )
    stats = validator.validate_file(
        input_file=generation.output_file,
        enable_repair=validation_cfg.get("enable_sql_repair", False),
    )
    return SqlRagRunResult(generation, stats)
