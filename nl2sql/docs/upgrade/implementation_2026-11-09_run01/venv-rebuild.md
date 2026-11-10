# 正式模块环境删除重建记录（doc1 §6.3，2026-11-09 18:36:57)

```
rm -rf -- .venv  # 删除前确认：基线已存（本目录）、独立旧环境已建（var/upgrade_old_env/.venv，deepagents 0.7.25）、候选已验证（/tmp/nl2sql-upgrade-impl-run01）
uv sync --locked --all-extras --no-install-project --python 3.12.13  # exit=0，全新安装 131 包
 + yarl==1.25.1
 + zstandard==0.25.0
uv run --locked --extra dev python -c 'import sys; ...'
/opt/projects/insightweave/nl2sql/.venv/bin/python3
3.12.13 (main, Mar 24 2026, 22:49:22) [Clang 22.1.1 ]
uv lock --check  # Resolved 134 packages, OK
uv pip check --python .venv/bin/python  # Checked 131 packages, All installed packages are compatible
```
