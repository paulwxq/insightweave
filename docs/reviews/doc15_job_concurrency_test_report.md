# doc 15.1 部署并发授权门禁测试报告

> 测试日期：2026-11-23（UTC）。执行：Claude（复审方终端，用户直派）。
> 依据：[15.1 部署并发授权门禁测试需求](../15.1_部署并发授权门禁测试需求.md)，对 [doc 15](../15_公开项目与私有部署仓库联动及CI_CD改造方案.md) §6.2/§8.2 的调度配合假设做真实 GitHub 实测。
> 结论先行：**C1–C6 全部 PASS**（C3 经原始 JSON 复核改判，见 §3.3）。核心命题成立：**并发组放在执行 job 层后，被 verify 拒绝或授权绑定拦截的 C 不会替换同组中等待的 B**；C4 对照证明该保留并非「C 未到调度阶段」。

## 1. 实测环境与交付物

| 项 | 值 |
| --- | --- |
| 实验仓库 | `paulwxq/insightweave-ci-sandbox`（公开，用户授权新建；仅含实验件，无业务源码/配置/凭据） |
| 工作流提交 | `2a07b3c1d47de4cd47305db2caa121d276416966`（全程未改 YAML；C3 重跑沿用原提交） |
| 收尾仓库 HEAD | `17e50bcc24686b87edf7e6bc7be01c4fcdb294c3`（驱动等待态判据修正）；不替代上行实际运行的工作流 SHA |
| 工作流 | Concurrency Probe (release-sim) id=427847445；Concurrency Probe (maint-sim) id=427847443 |
| Runner | 全部 ubuntu-latest 标准托管 Runner；未触 self-hosted/Docker/部署脚本/本机 Runner |
| 并发组 | `insightweave-concurrency-probe-b11231341r2-<case>`（每案例独立；工作流内守卫拒绝生产组名） |
| 工具版本 | gh 2.105.0；actionlint 1.7.12；驱动/取证脚本 bash |
| 实验文件 | `docs/reviews/doc15.1-probe-kit/`（工作流×2、run_probe.sh、check_structure.py），同步至实验仓 |
| 证据目录 | `docs/reviews/doc15.1-evidence/b11231341r2/<case>/`（分阶段 run/jobs JSON、job 日志、timeline、verdict） |
| 用量与残留 | 公开仓托管 Runner 免费额度，共约 20 次运行（含首跑/探针）；**测试结束无未完成运行，无需取消** |

## 2. 静态检查结果

- actionlint 1.7.12：两个实验工作流 **0 发现**。
- 结构断言（check_structure.py，21 项/文件）：全部 OK——顶层无 concurrency；verify/finalize 处于组外；执行 job 组名固定 `inputs.group` 且 `cancel-in-progress: false`；job 级 if 完整含四条件（`needs.verify.result=='success'` + 三个授权绑定等于当前 run/attempt/actor）；组名不含 run_id；`permissions: {}`。

## 3. 调度实测结果（C1–C6）

运行链接格式：`https://github.com/paulwxq/insightweave-ci-sandbox/actions/runs/<id>`。各案例前置条件均经脚本确认（PRECONDITION-OK：A.release in_progress；B verify=success 且其 release 在组内等待）。

### 3.1 C1 verify 拒绝 —— **PASS**
A=45188624907，B=45188666900，C=45188721204（mode=verify-reject）。
- 前置成立 13:55:17Z；C 于 13:55:19Z 发出，**verify 11s 内失败**（run 11s 完成，conclusion=failure）。
- C.release=**skipped**；A.release=success（13:56:55Z 完成未被取消）；**B.release=success，started=13:56:57Z（A 完成 2 秒后接续）**——B 全程保留，未被 C 替换。

### 3.2 C2 job 级绑定拒绝 —— **PASS**
A=45189202139，B=45189255255，C=45189302285（mode=bind-mismatch：verify success 但故意输出错误 run_id）。
- C.verify=success；C.release=**skipped**（被 job 级 if 阻止，区别于 C1 的 needs 失败传播）。
- A.release=success；B.release=success，started=14:00:25Z，A 完成 14:00:23Z 后 2 秒接续。B 未受 C 影响。

### 3.3 C3 真实局部重跑复用旧输出 —— **PASS（脚本初判 INCONCLUSIVE，经原始 JSON 复核改判）**
F=45189814350（mode=exec-fail），A=45189947122，B=45189994304。
- F attempt1（14:01:47–14:02:14）：verify=success、release=failure、finalize=failure（严格检查按设计变红）。
- B 前置成立后对 F 执行 `gh run rerun --failed`（14:03:02Z）。
- **attempt2 原始 jobs JSON（F-attempt2-jobs.json）**：
  - `verify`：attempt1 job ID=`137744871634`，attempt2 登记为新 ID=`137745696332`、run_attempt=2，但 started/completed 及步骤执行时间与 attempt1 相同（14:01:47Z→14:01:50Z）；attempt2 run_started_at=`14:03:05Z`——**未重新执行**，仅在 attempt 列表中携带旧执行数据，新 ID/attempt 不证明重新运行；
  - `release`：run_attempt=2，**skipped**（14:03:09 被评估）——旧授权输出（attempt=1）被 job 级 if 与 `github.run_attempt=2` 的比对拒绝，**从未进入并发组**；
  - `finalize`：重跑并按严格策略失败（预期）。
- A=success、B=success，B 在 F 重跑期间保留并在 A 后执行。
- **改判说明**：驱动脚本以「attempt2 列表含 verify 名字」判 VERIFY-RERAN，是错误判据（在列表≠重新执行）；结合 job/步骤时间与本次 attempt 启动时间核验，原始证据完整支持未重新运行。原 `verdict.txt` 作为驱动原始输出保留 INCONCLUSIVE，本节是复核后的 PASS 判定；驱动判据尚待修正，本次只补文档，不修改脚本或原始证据。

### 3.4 C4 合法 C 对照 —— **PASS**
A=45190525273，B=45190576024，C=45190645968（mode=ok，全部门槛通过）。
- **B.release=cancelled**（被合法 C 的 pending 替换——默认 single 策略如 doc 15 §8.2 所述）；A 未被取消（success）；C.release=success，started=14:08:04Z（A 完成 14:08:02Z 后接续）。
- 对照成立：C1/C2/C3 的 B 保留证明是「被拒的 C 不入组」，而非「C 从未到达调度阶段」。

### 3.5 C5 跨 workflow 互斥 —— **PASS**
rel=45191164619（release-sim），mnt=45191211126（maint-sim），共享组 `…-c5`。
- 执行区间：rel=[14:09:35Z, 14:11:37Z]，mnt=[14:11:39Z, 14:12:11Z]——**不重叠**（mnt 起点 > rel 终点 2 秒）。

### 3.6 C6 finalize 不占组 —— **PASS**
A=45191763927（exec 30s + finalize hold 75s），B=45191925656。
- A.release 于 14:13:30Z 完成，A.finalize=[14:13:32Z,14:14:51Z]；**B.release=[14:13:52Z,14:14:14Z]，在 A finalize 持有期间开始执行并 success**——锁定范围确认为执行 job，非整个 workflow。

## 4. 平台实证（对 doc 15 的增量事实）

1. **job 级并发组的等待态在 jobs API 中为 `pending`**（非 `queued`）——首跑驱动即因假设 `queued` 而误判（附录 A）。
2. **被拒/被 job-if 拦截的执行 job 不进入并发组、不产生 pending 替换**（C1/C2/C3）。
3. **合法进入的执行 job 会替换同组 pending**（C4，B.release=cancelled），证实 §8.2「首期允许跳过尚未开始旧请求」的平台基础。
4. attempt 级 jobs 列表**包含未重跑的 job 并携带其旧执行数据**（时间戳不变），同时可登记新的 job ID 和 run_attempt。判断「是否本次重跑」须结合 job/步骤执行时间及该 attempt 的启动时间，不能只看名字、ID 或 attempt 数字（C3 改判依据）。
5. 同组跨 workflow 的 job 级互斥与「finalize 在组外不阻塞后续执行」均按设计成立（C5/C6）。

## 5. 对 doc 15 的建议及回填记录

以下为实测完成时的建议及历史回填记录。2026-11-23 独立复核原始 JSON、实验文件及当时 GitHub 运行后，C1—C6 的结论均获确认；当时已回填 doc 15 §6.2/§6.3/§8.2/§9/§10/§11，并更新 doc 15.1 的状态。2026-11-24 同步当前精简方案的适用范围，修改仅涉及说明，不代表正式工作流或来源核验程序已经实现，也不改写原始响应、日志、驱动输出或案例判定。

- §8.2 建议补注：执行 job 层并发组的等待状态在 jobs API 中为 `pending`；被授权门槛拦下（verify 失败或绑定不符）的执行 job 为 skipped、不进入组、不替换 pending；该行为已经 sandbox 实测（本报告 C1–C4）。
- §11 建议新增验收行：「verify 拒绝/绑定不符的 C 到达时同组 pending 的 B 不被替换并在 A 后执行（对照：合法 C 替换 pending B）」「局部重跑复用旧 verify 输出时执行 job 被 attempt 绑定拦截且不影响同组 pending」。
- 判定本实验 verify 是否重跑的取证规范：仍按 §4 第 4 条结合实际执行时间，不把新 job ID 或 run_attempt 当作重新执行证明。2026-11-23 曾据此向旧版 doc 15 §6.3 回填来源 CI 的继承旧结果拒绝要求；当前精简版已改为接受同 SHA 的正常成功结果复用，不再要求逐来源 job/step 时间窗口证明。生产来源核验按 doc 15 当前 §6.3 实施验收；私有 verify 授权输出仍须绑定本次 run/attempt/执行者，旧授权不得跨 attempt 放行。

本报告的运行编号直接取自实验发送/采集记录；当前追踪说明对应 doc 15 §5.4 的明确 run ID 及人工核实，不要求已延后的 dispatch_id、自动回执查找或维护协调程序。上述范围调整不影响 C1—C6 的调度结论，也不触发重跑实验。

## 6. 边界声明

- 本实测验证的是**授权门槛与 GitHub 调度的配合**；绑定输出由测试模式从上下文生成或故意造假，**未验证真实维护者名单、来源 CI 核验或任何真实部署**（§1/§3 of 15.1）。
- 未修改 InsightWeave 仓库任何工作流；未连接本地 Runner；未执行 Docker/部署脚本；未提升付费预算（公开仓免费）。
- 测试后实验仓保留全部运行记录供复核；本报告与证据保存在源码仓 `docs/reviews/`（未提交，随现有 docs/14/15 一样待维护者处置）。

## 附录 A：驱动缺陷与排查记录（诚实记录）

- 首跑（batch b11231341，c1）：前置循环假设组等待态为 `queued`，实际为 `pending`，循环空转至 INCONCLUSIVE；GitHub 侧行为本身正确（B 等待 109s 后接续，见 b11231341/c1 证据）。修正判据后以新批次 b11231341r2 重跑全部案例。
- 诊断探针（组 `…-probe`，P/Q 两运行）：确认 `pending` 状态名，数据保留于任务输出与 gh 运行记录。
- C3 初判脚本误判（见 §3.3 改判说明）。
