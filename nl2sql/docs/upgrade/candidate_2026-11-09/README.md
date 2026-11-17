# NL2SQL 扩大范围升级候选快照（2026-11-09）

本目录是文档评估的候选输入，不是项目已经实施的依赖文件。

审核后用户确认不使用 Redis、pytest-asyncio、pytest-cov，项目实际声明、锁文件和环境已删除这三包及不再需要的 coverage，并将 pytest 仅保留在 dev extra。本目录保留清理前的候选和原始证据以便复核历史结果；正式升级须从当前项目声明重新生成候选，不得照搬此快照并重新引入已删除的包。

当前项目另已显式声明主依赖 starlette、cryptography 和 web extra 的 pandas；此历史候选早于补齐声明，没有这三行，并仍将 pytest 同时列在主依赖与 dev 中。因此该快照只供复核历史组合，不能作为补丁或覆盖来源。三包应保留直接依赖身份，按主评估文档 §2.6 决定精确目标，再从当前项目声明重新解析；仅执行 `uv lock --upgrade` 不会越过当前精确版本约束。

- 目标：DeepAgents 使用核对日最新稳定版 0.7.34，其余依赖在兼容条件下尽量更新。
- `pyproject.toml` / `uv.lock`：已在 Python 3.12.13 的全新临时虚拟环境中解析、安装并验证的候选；包含 dev、monitoring、web 全部可选组。
- PyMilvus 采用 2.6.18 作为原 2.6 系列的候选；正式使用前仍须确认 Milvus 服务端版本。最新 3.0.2 的全最新版组合也解析成功，但未安装或做业务验证。
- `latest_versions.json`：41 项直接依赖、显式约束及重点联动包的 PyPI 当前/最新稳定版本清单。
- `resolved_changes.csv`：相对评估时原锁文件的全部版本变更，包含间接依赖；空 old/new 分别表示新增/移除。
- `validation_summary.json`：两环境同范围回归结果及失败用例 ID、内存 checkpoint 跨版本探针结果。
- [evidence/README.md](evidence/README.md)：原始 pytest XML（日志为本地记录）、checkpoint 探针脚本/结果与安装日志，以及本轮新增的 pip check / doctor 复核记录。历史回归使用同一份当时工作区源码，仅依赖环境不同。

完整范围与限制见 [主评估文档](../../1_DeepAgents_0.7.34升级评估与实施建议.md)。不能直接覆盖项目文件并据此认定可上线，实施时还需迁移公开权限参数和 SQLGlot doctor 版本常量，并完成真实服务及旧 PostgreSQL 会话验收。

候选复现方式（在本目录或其隔离副本中）：`uv sync --locked --all-extras --no-install-project`。请明确使用隔离环境；不要把快照当作已部署状态。
