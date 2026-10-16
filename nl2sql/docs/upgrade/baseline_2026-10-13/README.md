# 升级前依赖快照（2026-10-13）

本目录保存本次 LangChain / LangGraph 及关联组件升级前的原始文件：

| 文件 | SHA-256 |
|---|---|
| `pyproject.toml` | `bf0b613d2e112ded5bde23ab83a2d0293513a5b085e1fb809e5181b9d0571a28` |
| `uv.lock` | `cce2021941cef033aaf2bf533789d17cb19fbca6d8dd563934d20282db73065f` |
| `.python-version` | `7b55f8e67b5623c4bef3fa691288da9437d79d3aba156de48d481db32ac7d16d` |

仓库根目录中的同名文件本来就受 Git 管理。这份快照用于直接查看升级前的依赖声明与精确锁定版本；只有将本目录随本次变更提交到 Git 后，这份额外快照才会成为 Git 历史的一部分。

需要回退依赖文件时，在仓库根目录执行：

```bash
cp docs/upgrade/baseline_2026-10-13/pyproject.toml pyproject.toml
cp docs/upgrade/baseline_2026-10-13/uv.lock uv.lock
cp docs/upgrade/baseline_2026-10-13/.python-version .python-version
uv sync --locked --all-extras
```

2026-10-13 后续应用户要求，旧 `.venv` 与 `.venv-wsl` 均已删除；当前 `.venv` 按升级后的锁文件重建。升级前的软件版本保存在本目录的锁文件和 Git 历史中，不再保留旧版虚拟环境。回退时使用上面的命令即可按旧锁文件重建 `.venv`。
