# AGENTS.md - Agent Coding Guidelines

This file provides guidelines for agents operating in the nl2sql_v3 repository.

## Project Overview

nl2sql_v3 is a multi-agent SQL generation system built with LangGraph. It converts natural language questions into SQL queries using LLM-powered agents. The project requires Python 3.12+ and uses `uv` for package management.

## Virtual Environment

- **All platforms**: Use the project-root `.venv` virtual environment
- Install dependencies: `uv pip install -e ".[dev]"`

## SDK Source References

The module targets `deepagents==0.7.34` (upgraded 2026-11-09 from the `0.7.25` baseline; see `docs/1_DeepAgents_0.7.34升级评估与实施建议.md` §6). The version-exact source is the installed package in this module's environment:

| SDK | Version-exact source (matches installed lock) |
| --- | --- |
| Deep Agents | `.venv/lib/python3.12/site-packages/deepagents/` |
| LangChain | `.venv/lib/python3.12/site-packages/langchain/` |
| LangGraph | `.venv/lib/python3.12/site-packages/langgraph/` |

- Historical baseline checkouts remain at `/opt/projects/lang-stu/` (`deepagents-0.7.25`, `langchain-1.4.5`, `langgraph-1.2.15`). They match the old baseline recorded in `docs/v4/3_db_deepagent概要设计.md` / `docs/v4/5_db_deepagent详细设计.md`, NOT the current target; do not cite them for current-version behavior.
- These are reference sources; project implementation changes belong in this repository.
- Deep Agents implementation details in `docs/v4/3_db_deepagent概要设计.md` were checked against the official `0.7.25` wheel at design time. Verify the selected package version before applying source findings; installing dependencies and validating runtime behavior remain implementation tasks.

## Build/Lint/Test Commands

### Running Tests

```bash
# Run all tests
uv run pytest

# Run all unit tests
uv run pytest tests/unit/ -v

# Run all tests in a specific directory
uv run pytest src/tests/unit/nl2sql_father/ -v

# Run a single test function
uv run pytest tests/unit/nl2sql_father/test_graph.py::test_graph_compilation -v

# Run tests by marker
uv run pytest -m unit -v        # unit tests only
uv run pytest -m integration -v # integration tests only
uv run pytest -m slow -v        # slow tests only

```

### Code Formatting and Linting

```bash
# Format code with Black
uv run black src/ tests/

# Check formatting without applying
uv run black --check src/ tests/

# Lint with Ruff
uv run ruff check src/ tests/

# Auto-fix linting issues
uv run ruff check --fix src/ tests/

# Run both (recommended)
uv run black src/ tests/ && uv run ruff check src/ tests/
```

### Type Checking

```bash
# Run mypy (if installed)
uv run mypy src/
```

## Code Style Guidelines

### General Principles

- Write clean, readable, and maintainable code
- Use type hints for all function signatures and class attributes
- Add docstrings to all public functions and classes (Chinese or English)
- Keep functions focused and single-purpose

### Naming Conventions

- **Modules/Files**: `snake_case.py` (e.g., `table_schema_loader.py`)
- **Classes**: `PascalCase` (e.g., `SQLGenerationState`, `ColumnInfo`)
- **Functions/Variables**: `snake_case` (e.g., `get_column_statistics`, `table_name`)
- **Constants**: `UPPER_SNAKE_CASE` (e.g., `DEFAULT_TIMEOUT`, `MAX_RETRIES`)
- **Private methods/attributes**: `_leading_underscore` (e.g., `_build_metadata`)

### Imports

- Use absolute imports from `src` package root
- Group imports in this order: standard library, third-party, local application
- Sort imports alphabetically within each group

```python
# Correct
import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from src.modules.nl2sql_father.state import NL2SQLFatherState
from src.utils.logger import get_module_logger
```

### Type Annotations

Use full type annotations for function signatures:

```python
# Good
def process_query(query: str, thread_id: str) -> dict[str, Any]:
    ...

# Good - using modern Python 3.12+ syntax where appropriate
def get_items(ids: list[str]) -> dict[str, int]: ...
```

### Data Classes

Use `@dataclass` for simple data containers:

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ColumnInfo:
    column_name: str
    ordinal_position: int
    data_type: str
    is_nullable: bool = True
    comment: str = ""
```

### Error Handling

- Use specific exception types
- Provide meaningful error messages
- Log errors with appropriate context

```python
# Good
if not config_file.exists():
    raise FileNotFoundError(f"配置文件不存在: {config_path}")

# Good - using custom exceptions
class SQLGenerationError(Exception):
    """SQL 生成失败"""
    pass
```

### Logging

Use the project's logging utilities:

```python
from src.utils.logger import get_module_logger

logger = get_module_logger("sql_generation")
logger.info("开始生成 SQL")
logger.debug(f"Processing query: {query_text}")
```

### Testing

- Place tests in `tests/unit/` for unit tests
- Use `test_*.py` file naming convention
- Use `test_*` function naming convention
- Test one thing per test function
- Use descriptive test names

```python
def test_graph_compilation_succeeds():
    """测试图编译成功"""
    from src.modules.nl2sql_father.graph import create_nl2sql_father_graph
    graph = create_nl2sql_father_graph()
    assert graph is not None
```

### Project Structure

```
src/
├── modules/           # NL2SQL core modules
│   ├── nl2sql_father/ # NL2SQL parent graph (LangGraph)
│   └── sql_generation/# SQL generation subgraph
├── services/          # External services (vector DB, LLM, storage)
├── utils/             # Utilities
├── configs/           # Configuration files
└── prompts/           # LLM prompts

tests/
├── unit/              # Unit tests
└── integration/      # Integration tests
```

### Configuration Files

- YAML files for runtime configuration in `configs/`
- Environment variables in `.env` file (never commit secrets)
- Use `.env.example` as template for required variables

### Git Conventions

- Make meaningful commit messages
- Don't commit secrets, credentials, or `.env` files
- Run linting and tests before committing

## Common Development Tasks

### Running the NL2SQL CLI

```bash
# Single query
python scripts/nl2sql_subgraph_cli.py "查询2024年10月的销售额"

# Interactive mode
python scripts/nl2sql_subgraph_cli.py
```

## Dependencies

Key dependencies (see `pyproject.toml`):
- **LangGraph/LangChain**: Agent orchestration
- **FastAPI**: Web framework
- **psycopg/pgvector**: PostgreSQL and vector storage
- **neo4j**: Graph database
- **dashscope**: LLM API (Qwen)
- **pytest**: Testing framework
- **black/ruff**: Code formatting and linting
