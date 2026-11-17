# 验证证据归档

本目录在 2026-11-09 的 GLM 第 1 轮审核后补充，避免原始记录随 `/tmp` 清理而丢失。它不表示项目已经升级或完成真实服务验收。

## 历史原件

以下 7 个文件原样复制自 `/tmp/insightweave-full-upgrade-amqb1psp/`，归档时逐文件比对 SHA-256 一致：

- `baseline-tests.xml` / `candidate-tests.xml`：两环境原始 JUnit XML，各 1166 项、1152 通过、10 失败、4 跳过；失败 ID 一致。
- `baseline-tests.log` / `candidate-tests.log`：对应 pytest 控制台记录，头部记录 Python、插件版本及相同源码 rootdir。测试范围见上级目录 `validation_summary.json` 的 `test_paths`。
- `checkpoint_probe.py` / `checkpoint-probe.log`：原始无模型、无数据库跨版本内存探针及三步结果；脚本断言消息数为 2、4、4。
- `service-compatible-sync.log`：主候选安装日志，记录安装 135 个包。该文件不是 pip check 输出。

> 版本控制说明：本目录仅提交 XML、脚本与文本记录；`.log` 为原始运行日志，按根 `.gitignore` 的 `*.log` 规则保留在工作区，不纳入版本控制，克隆仓库后不存在。

历史回归针对同一份当时的工作区源码，仅依赖环境不同。本目录未保存当时完整源码快照或虚拟环境，不能据此保证后续复跑结果相同。

## 审核后新增检查

这些记录由主协调者在本轮重新执行，不能冒充历史运行输出：

- `review-pip-check.log`：对仍存在的主候选虚拟环境重新执行 `uv pip check`。
- `review-dependency-report.log`：在候选 Python 中调用 `_collect_dependency_report()`，确认 SQLGlot doctor 精确版本不匹配；没有运行完整 doctor 或访问数据库。
- `review-checkpoint-probe.log`：使用原探针脚本和全新本地样本，按旧环境写入、新环境恢复并追加、旧环境读取的顺序重新执行。

探针复跑方式：用旧环境 Python 执行 `checkpoint_probe.py write-old <全新样本路径>`，用候选 Python 执行 `checkpoint_probe.py read-and-write-new <相同样本路径>`，再用旧环境 Python 执行 `checkpoint_probe.py read-old <相同样本路径>`。脚本只应读取自己生成的本地 pickle，不读取外部 checkpoint 文件。没有归档二进制 pickle；复跑时生成新的样本。

原始 PyPI 响应、发布记录和安装包源码未整体复制；候选精确版本与分发文件哈希见上级 `uv.lock`。本次不补造缺失的历史 Fake 模型构造检查输出，相关 API 结论仍应以固定版本源码及实施时验证为准。
