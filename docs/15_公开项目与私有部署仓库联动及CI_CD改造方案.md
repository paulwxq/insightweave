# InsightWeave 公开项目与私有部署仓库联动及 CI/CD 改造方案

> 编写日期：2026-11-23；最近修订：2026-11-24。
> 状态：待实施设计。本轮只修改文档，不表示仓库、权限、Runner 或工作流已经配置完成。
> 当前实现依据：[doc 13](13_项目Docker容器化与本地CI_CD发布设计.md)、[doc 13.1](13.1_镜像与依赖安装明细.md)、[部署手册](../deploy/README.md)、[现有发布工作流](../.github/workflows/local-release.yml)。
> 本轮决定：采用方案 1，维护者合并 PR 即授权发布，main CI 成功后自动部署。按单人开发、单人发布的实际规模收敛首期范围；异常由维护者核实处理。

## 1. 目标与关键决定

项目公开用于求职展示，保留 Debian / WSL 本地构建 Docker、本地部署。当前由 `paulwxq` 一人开发、检查和合并；以后可接受其他人的 PR，但首期最终合并及部署授权仍由 `paulwxq` 负责。

| 仓库 | 可见性 | 职责 |
| --- | --- | --- |
| `OWNER/insightweave` | 公开 | 唯一源码仓库，保存业务代码、Dockerfile、部署脚本、PR、测试和设计文档 |
| `OWNER/insightweave-deploy` | 私有 | 保存固定的请求核验及部署工作流，调度 Debian Runner |

全文 `OWNER` 对应 `paulwxq`。两个仓库之间不合并分支、不双写源码；公开仓库发送部署请求，私有工作流按完整源码 SHA 拉取代码并部署。

首期决定：

1. 使用 `feat/xxx`、`fix/xxx` 等功能分支，经 PR 合入 main，不增加长期 dev。
2. PR CI 自动检查；维护者检查差异后手动合并。main 的实际合并提交再运行 CI，成功后自动通知私有部署仓库，无需另点一次部署按钮。
3. 只接受维护者产生的 main push CI，以及维护者发起的来源 CI 重跑、通知重跑和私有部署执行。身份按预先确认的稳定账号 ID 核验。
4. Debian Runner 只注册到私有部署仓库；三套应用镜像仍本地 `buildx build --load`，不引入镜像注册表。
5. 发布只使用核验时当前 main HEAD 的成功 CI，不选择更早成功提交兜底。部署前再次检查，已知过期的请求停止；已开始容器变更的操作正常收尾。
6. 保留一个执行 job 并发组和现有本机 flock、快照、预检、排空、冒烟及恢复边界。任务失败、被替换、重复或结果不明时，维护者核实后决定重试。
7. 不承诺每次合并都部署、请求恰好执行一次、异常后自动补发或最新 main 必然最终自动部署；这些不作为首期上线条件。
8. 保持 GitHub Free 和零额外付费预算，不自动升级或提高预算。

2026-11-23 既有前置核验记录：源码仓库当时为私有，旧 Runner `aithink` 在线，标签含 self-hosted/Linux/X64/insightweave-local；最近成功 local-release run 为 `44232946662`（2026-11-18，源码 `e70391f`）。这些是历史记录，不代表新仓库已接入或服务此刻在线。账户与额度见 §6.1。

## 2. 当前实现与目标的差异

### 2.1 当前发布链路

现有 `.github/workflows/local-release.yml` 监听 push main 和手动 workflow_dispatch：

```text
push main / 手动运行
    → 当前源码仓库的 self-hosted Runner
    → ci.sh：干净源码、依赖、离线测试、三镜像构建、候选预检
    → deploy.sh release：预检、维护排空、替换、两阶段冒烟
```

现有 release_id 为 `${GITHUB_SHA:0:12}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`。它依赖工作流和业务代码同仓；迁移后 GITHUB_SHA 属于私有控制仓库，必须改用明确的 source_sha。

现有工作流没有 pull_request 触发器，当前文件不能证明远端已配置分支保护。旧工作流须在首次合并新 CI 到 main 前停用并核实无未决发布，不能等到 Runner 迁移时才停用。

### 2.2 改造前后对照

| 项目 | 当前实现 | 目标实现 |
| --- | --- | --- |
| 开发入口 | 直接 push main 可发布 | 功能分支 → PR → 维护者合并 |
| 合并前检查 | 无专用 PR 工作流 | 云端 PR CI、稳定汇总检查 |
| 合并后检查 | 本地完整 CI | 云端 main CI 成功后，再运行本地完整 CI |
| Runner 归属 | 源码仓库 | 私有部署仓库 |
| 跨仓库调用 | 无 | 成功 CI 通知 + workflow_dispatch |
| 构建版本 | 默认 checkout / GITHUB_SHA | 私有端核验后的完整 source_sha |
| 本地部署机制 | 快照、deployment.json、锁和恢复 | 保留，不另建请求账本或恢复服务 |
| 异常处理 | 查看日志及本机状态 | 明确关联 run，维护者核实后重试或恢复 |

## 3. 两个仓库及本地环境的职责

### 3.1 公开仓库 insightweave

保留完整源码、Dockerfile、Compose、通用部署脚本、模板、测试和全部既有 docs 文档。新增：

```text
.github/workflows/ci.yml             # PR/main 云端检查
.github/workflows/notify-deploy.yml  # main CI 成功后通知私有仓库
.github/pull_request_template.md     # 可选：变更及验证说明
```

原 local-release 先停用，再在 §10 步骤 9 的切换 PR 中删除，不能保留公开仓库调度本地 Runner 的路径。公开展示源码 CI、PR 和设计；真实部署详细日志默认留在私有仓库。

### 3.2 私有仓库 insightweave-deploy

首期仅需：

```text
.github/workflows/deploy.yml              # 正式 verify → release → finalize
.github/workflows/verify-diagnostics.yml  # 仅托管 Runner 的手动只读诊断，无 release
scripts/verify_source_request.py          # 固定身份、来源 CI、版本核验
scripts/release_local.sh                  # 本地复核、版本追踪、调用公开脚本
tests/                                   # 核验边界和本地入口替身测试
README.md                                # Runner 接入、手动诊断、重试/恢复
```

这些是拟新增文件。私有仓库仅由维护者管理，不给普通源码贡献者写权限；不复制业务代码，不保存实际密码、API Key 或完整运行 env。首期不新增 dispatch_deploy.py、maintenance_control.py 或常驻协调服务。

`verify-diagnostics.yml` 是 §10 步骤 5、8 的唯一托管诊断载体：仅 workflow_dispatch，全部 job 固定 ubuntu-latest，使用所选私有 main 的固定提交，显式只读 permissions（contents: read、actions: read）。手动 mode 仅允许 fixtures（缺省，仅运行私有 tests 中的核验响应替身/拒绝用例子集，不运行本地发布入口测试）或 source-readonly（源码公开后，复用生产核验的只读查询逻辑验证固定公开来源）。不复制一套独立核验规则，不执行公开源码或传入的任意命令。

诊断输入约定：mode 在 YAML 中为 choice，选项仅 fixtures/source-readonly，默认 fixtures；另声明 §5.2 同名的三个 string 输入 source_sha、source_run_id、source_run_attempt，在 YAML 中 required: false、默认空字符串，以便 fixtures 无需来源参数即可运行。mode=source-readonly 时，由诊断入口校验三项全部必填、格式符合 §5.2，缺失或无效即在查询前拒绝；fixtures 不访问公开来源 API。

source-readonly 的三个值由维护者手动传入：先读取当前公开 main HEAD，取得固定 ci.yml（§7.1 的 InsightWeave CI）、event=push、对应该 HEAD 的来源 run ID 及其当前最新 attempt；该 attempt 必须已 completed/success，再填入完整 SHA、run ID 和 attempt。不能按“历史最新成功”绕过该 run 更新后失败或仍在执行的 attempt，也不选较旧 main 的成功记录；没有明确合法来源时先等待或排查，不自动搜索旧成功兜底。诊断仍按 §6.3 重新核验这些值及六个固定 job 名，不因手动填写而直接信任。

该入口的任务图中不存在 release 或 self-hosted job，不调用 release_local.sh、deploy.sh、部署用可复用工作流，也不发送部署请求；不使用 DEPLOY_TRIGGER_TOKEN 或其他写令牌，公开查询如需身份，仅使用 §6.1 获准的只读身份。即使用例意外通过，结果也只停留在托管诊断，不产生本地任务。诊断成功不是部署成功，不通过向正式 deploy.yml 增加临时跳过检查或 dry-run 参数来替代此隔离。

### 3.3 Debian 本地环境

沿用 iw-runner 账号及既有目录：

```text
/home/iw-runner/insightweave-runtime/
├── deploy.env
├── config/metaweave.env
├── config/nl2sql.env
├── control/
├── state/deployment.json
├── releases/<release_id>/
└── logs/
```

不新增 state/requests、state/dispatches 或 state/maintenance 账本。现有状态、快照和 GitHub 日志用于人工核实，不承担自动跨文件对账。

保留 Docker context、Compose 项目名 insightweave-local、数据卷、端口和维护门闩；Runner 迁移不重新 bootstrap。Runner 主动连接 GitHub，不开放公网 webhook、SSH 或 Docker API。

另指定 `/home/iw-runner/insightweave-ops/source/` 为受控手动运维的独立源码检出目录，由 iw-runner 持有，独立于 Runner 的 `_work` 和运行配置目录；具体准备及版本核验见 §8.4，不在本次文档修改中创建该目录。

## 4. 单人开发、检查与合并

### 4.1 日常命令

以下均为未来操作示例，不在本次文档任务中执行：

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/query-history

# 替换为本次实际修改路径
git add path/to/changed-file
git commit -m "新增查询历史功能"
git push -u origin feat/query-history

gh pr create --base main --head feat/query-history \
  --title "新增查询历史功能" --body "说明变更、验证方式和影响"
```

push 会创建 GitHub 上同名功能分支；提交上传到功能分支，不直接推送 main。假设 PR 编号为 12：

```bash
gh pr diff 12
gh pr checks 12 --watch
# 阅读差异和检查结果，确认发布后，再单独执行合并
gh pr merge 12 --merge

# 合并后同步本地 main；此步骤不影响远端自动部署启动
git switch main
git pull --ff-only origin main
```

采用 merge commit，不开自动合并或 merge queue。合并发生在 GitHub；“合并”即维护者的发布授权，之后自动检查及部署。[GitHub CLI PR 文档](https://cli.github.com/manual/gh_pr)

### 4.2 单人阶段的保护规则

公开 main 要求 PR、稳定的 ci-required 检查成功，禁止强推和删除，并将保护应用于维护者，避免日常依赖管理员绕过。单人阶段不启用必需人工批准、不要求最后一次推送由别人批准，也不要求 CODEOWNERS 批准。

维护者可以查看自己的 PR 差异、评论、修正并合并；GitHub 不允许作者批准自己的 PR，因此不设置“至少另一人 Approve”。这是有自动检查和自查记录的单人流程，不宣称独立人工审核。AI 审核可选，不替代 CI，不作为上线前提。[PR 作者批准限制](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews)

GitHub Free 支持公开仓库分支保护；源码仍私有时先验证 CI，公开后配置保护再启用自动发布。[分支保护说明](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)

### 4.3 将来加入其他开发者

其他人可提交 PR，首期仍由维护者最终合并。公开源码仓首期仅维护者有写权限；其他贡献者通过 fork PR 协作，不为提交代码额外授予 main 合并、工作流重跑或私有部署仓写权限。这样通知重跑也只有维护者能执行。

“多人可以合并 main”和“只有维护者授权自动部署”不是本版同时支持的权限模型。将来开放他人合并时，应改为单独手动发布或增加明确的发布审批；不能只保留维护者 PAT 就声称所有发布均由维护者决定。首期设计即要求私有端核验来源 CI 的原始 actor，见 §6.3；其他人的 main push 即使成功，也不会因通知使用维护者 PAT 而取得发布资格。

## 5. 自动联动时序与请求契约

### 5.1 时序

```text
维护者功能分支 → PR CI → 维护者检查并合并
    → main 实际合并提交的 CI 成功
    → 公开 notify-deploy 调用私有 workflow_dispatch
    → 私有 verify：维护者身份、固定来源、CI 成功及当前 main
    → 私有 release：本地复核 → 固定源码 → 测试/构建 → 部署前复核 → 发布
    → 私有 finalize：记录本次是否实际部署成功
```

PR CI 可使用 GitHub 的合并预备引用；实际部署仅使用 main 的真实提交，不拿 PR 临时 merge SHA 发布。[pull_request 事件语义](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)

### 5.2 三个来源参数

| 参数 | 内容及核验 |
| --- | --- |
| source_sha | 必填字符串，40 位小写十六进制；等于来源 CI head_sha 及当前 main HEAD |
| source_run_id | 必填字符串，规范正整数；属于固定公开仓库和允许的 ci.yml |
| source_run_attempt | 必填字符串，规范正整数；指定成功 attempt，且仍为该 run 最新 attempt |

来源仓库、main 分支、CI workflow ID/path 固定在私有控制程序中。输入经 env 或结构化参数传入，不直接拼进 shell，不接受外部 Git URL、脚本路径或任意命令。

三项是来源证据，不是认证凭据。本版不要求额外 dispatch_id，不开发通用请求编号、服务端幂等或自动回执恢复协议。

### 5.3 公开通知工作流

保留 workflow_run completed 通知；只接受同仓 main push 成功事件，不 checkout 上游、不运行 PR 产物、不恢复 PR 缓存。其 GITHUB_RUN_ID/ATTEMPT 属于通知，SOURCE_RUN_* 属于来源 CI，二者分开记录。

以下是拟实施模板，须替换 OWNER、配置公开 environment/Secret、完成私有端核验后再启用。ci.yml 按 §7.1 固定声明 name: InsightWeave CI，通知中的 workflows 名称须原样一致；私有默认 main 上必须已有 deploy.yml 及三个 input 声明。

```yaml
name: Notify private deployment
on:
  workflow_run:
    workflows: [InsightWeave CI]
    types: [completed]
    branches: [main]
permissions:
  contents: read
jobs:
  notify:
    if: >-
      github.event.workflow_run.conclusion == 'success' &&
      github.event.workflow_run.event == 'push' &&
      github.event.workflow_run.head_branch == 'main' &&
      github.event.workflow_run.head_repository.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 5
    environment: deployment-dispatch
    steps:
      - name: Request deployment of the successful source commit
        env:
          GH_TOKEN: ${{ secrets.DEPLOY_TRIGGER_TOKEN }}
          DEPLOY_REPOSITORY: OWNER/insightweave-deploy
          SOURCE_SHA: ${{ github.event.workflow_run.head_sha }}
          SOURCE_RUN_ID: ${{ github.event.workflow_run.id }}
          SOURCE_RUN_ATTEMPT: ${{ github.event.workflow_run.run_attempt }}
        run: |
          set -euo pipefail
          # GITHUB_RUN_ID/ATTEMPT 在此属于公开通知，SOURCE_RUN_* 属于来源 CI。
          notification_id="n${GITHUB_RUN_ID}-a${GITHUB_RUN_ATTEMPT}"
          request_file="$RUNNER_TEMP/deploy-request-$notification_id.json"
          receipt_file="$RUNNER_TEMP/deploy-receipt-$notification_id.json"
          jq -n --arg sha "$SOURCE_SHA" --arg run "$SOURCE_RUN_ID" \
            --arg attempt "$SOURCE_RUN_ATTEMPT" \
            '{ref:"main",inputs:{source_sha:$sha,source_run_id:$run,
              source_run_attempt:$attempt}}' > "$request_file"
          # 留下无凭据的来源参数，便于人工核实；不建立发送账本。
          jq -c --arg phase intent '. + {dispatch_phase:$phase}' "$request_file" \
            | tee -a "$GITHUB_STEP_SUMMARY"
          if ! gh api --method POST \
            -H 'Accept: application/vnd.github+json' \
            -H 'X-GitHub-Api-Version: 2026-03-10' \
            "repos/$DEPLOY_REPOSITORY/actions/workflows/deploy.yml/dispatches" \
            --input "$request_file" > "$receipt_file"; then
            printf '%s\n' '发送出错，先查看本次通知及私有运行，核实受理结果，不自动重发。' >&2
            exit 1
          fi
          # 不接受空响应、缺字段或指向其他仓库的 URL 作为完整回执。
          jq -e -s --arg repo "$DEPLOY_REPOSITORY" '
            (length == 1) and (.[0] | .workflow_run_id as $id |
              (($id|type) == "number") and ($id > 0) and (($id|floor) == $id) and
              (.run_url == ("https://api.github.com/repos/"+$repo+"/actions/runs/"+($id|tostring))) and
              (.html_url == ("https://github.com/"+$repo+"/actions/runs/"+($id|tostring))))
          ' "$receipt_file" > /dev/null
          jq -c --arg notification "$notification_id" \
            '. + {notification_id:$notification,dispatch_phase:"accepted"}' "$receipt_file" \
            | tee -a "$GITHUB_STEP_SUMMARY"
```

JSON 中 ref: main 是私有部署控制分支，source_sha 是公开应用版本。本机 paul 用户的 gh 已于 2026-11-23 验证升级至 2.105.0（/usr/bin/gh，活动账号 paulwxq）；不代表 iw-runner 或云端版本相同。各环境验证 gh/jq 可用，发送统一使用上述固定 API 版本。该版本 dispatch 成功返回 HTTP 200 及 workflow_run_id、run_url、html_url。[dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)

**仓库名配置前提：**模板将 DEPLOY_REPOSITORY 拼入回执 URL 做区分大小写的严格等值比较。启用前用 `gh api repos/OWNER/insightweave-deploy --jq .full_name` 读回目标仓库的规范完整名称，按返回值原样配置，不能随意改大小写或一律转小写；仓库改名后也须同步核验配置。API 路径接受不同大小写，不代表返回的规范 URL 会保留请求写法，因此配置不一致可能在 POST 已受理后使本地回执校验失败。遇到此情况按 §5.4 人工核实，不认定未受理、不自动重发，也不为绕过报错取消 URL 校验。

通知日志/摘要保存来源参数、通知编号及返回的私有 run ID；只表示已受理，不表示已部署。RUNNER_TEMP 不是持久账本。首期不在公开仓库回写私有部署状态，不把通知绿色当成服务已更新。

### 5.4 简化的运行追踪与人工处理

正常按回执中明确的私有 run ID 查询：

```bash
gh run view 123456 --repo OWNER/insightweave-deploy
# 编号替换为实际回执；列表只用于浏览和人工核实
gh run list --repo OWNER/insightweave-deploy --workflow deploy.yml
```

发送报错、回执/日志缺失或中断时，先查看公开通知、私有运行和本机现场。不能用“最近一条 run”自动认领，也不因命令失败就自动重复 POST。无法确认是否已受理/执行时保持未决，维护者确认后才重试；本版不实现按关联名自动分页恢复、超时补发或持久化发送账本。

成功后不通过重跑通知/部署做健康检查。明确需要重试时，先确认旧运行结束、现场 ready、来源仍为最新 main/attempt，再按 §8.3 由维护者显式操作。

## 6. 授权与私有端核验

### 6.1 权限、套餐和工具

公开默认 GITHUB_TOKEN 不能用于控制另一个私有仓库。首期用维护者的细粒度 PAT，仅选择 insightweave-deploy，授予 Actions: write；这不是“只能调用一条工作流”的权限，须保密并在到期前轮换。令牌在账号网页生成/重新生成，Secret 更新可用受限输入的 gh secret set，不把值贴入会话、命令参数或日志。[PAT 说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

PAT 保存为公开 deployment-dispatch environment 的 DEPLOY_TRIGGER_TOKEN，环境仅允许受保护 main；首期不增加人工环境审批。PR CI 不使用此环境，令牌不传给本地 Runner、业务代码或 Docker。GitHub Free 的私有源码仓不能提供本方案要求的 environment，须源码公开后再配置；不降级为无环境限制的仓库 Secret。[environment 可用范围](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)

私有仓库只保留维护者管理权限，固定维护者稳定账号 ID，不建立多人/应用授权框架。实施时查询并记录 paulwxq 的真实 ID，不能猜测或仅按可改名的登录名授权。私有 verify 需 contents: read 和 actions: read，以检出控制程序并读取自身 attempt；普通公开数据查询不假定私有 GITHUB_TOKEN 有跨仓库访问权限。若公开只读查询限流，再配置公开 CI 的只读身份，不复用触发写令牌。

公开查询若采用未认证方式，REST 主限额按来源 IP 计算为每小时 60 次，同一出口的其他请求可能共享该额度；限流可能返回 403/429，但不能把所有 403 都归为限流。此风险尚未在本项目的实际托管出口实测，启用前按 §10 步骤 8 做低频只读核验，记录实际身份方式、响应状态及必要的 x-ratelimit-limit/remaining/reset、Retry-After 等响应头，不记录凭据。一次成功不证明以后不会限流；用响应替身验证限流时停止放行，人工重试遵守服务端等待提示，不主动耗尽额度、不做压力测试。确有可用性问题时，使用经授权的公开 CI 只读身份后再核验。[REST API 限流说明](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

**账户确认记录（2026-11-23）：**维护者截图确认 GitHub Free、2,000 Actions minutes/month；维护者确认当月当时尚未使用该额度。这是历史基线，不是实时余额；之后的任务仍须计入。额度按计费周期重置，不累加，不按次数/token 计量。预算截图 Actions $0/$0、Usage blocked 是额外付费被阻止，不是免费额度为零。

| 环节 | 计算额度 |
| --- | --- |
| 源码公开后标准托管 Runner 的 PR/main CI 和通知 | 免费，不扣上述私有计算额度 |
| 私有 verify/finalize | 消耗账号包含额度，多 job 分别累计 |
| 本地测试、Docker 构建和部署 | 自托管 Runner 不消耗上述云端计算额度 |

源码公开前的私有云端 CI 也消耗额度。启用云端 CI、首次私有部署前分别核实当前用量及零额外付费阻断，记录时间；CLI 无账单权限时由维护者查看账户页面，不自动扩大令牌权限。[Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

额度耗尽时自动发布暂停，不绕过 verify；既有应用保持运行，可等待周期重置或使用受控本机手动恢复。若本地已经部署而 finalize 未运行，核对现场，不因总状态失败自动回退。恢复额度后不批量重放旧通知。

### 6.2 verify、release 和 finalize

| job | 运行位置及职责 |
| --- | --- |
| verify | GitHub 托管 Runner；固定私有 github.sha 检出核验程序，验证身份和来源 |
| release | 本地 Runner；needs verify，job 级授权绑定通过后才进入部署并发组 |
| finalize | 托管 Runner；needs verify/release，always 判断授权和实际部署结果，处于组外 |

此表是正式发布的任务图；上线前的托管诊断按 §3.2 的 verify-diagnostics.yml 执行，不 dispatch 正式 deploy.yml 来试探通过/拒绝，更不能以暂未注册本地 Runner 保证不部署。

私有 verify 仅接受 refs/heads/main。通过指定私有 run attempt API 核对原始 actor 和本次 triggering_actor 的稳定 ID，二者都必须是维护者；不能只因初次 actor 获准就允许其他人重跑。API/身份无法确认时不放行，维护者排查后重新核验。

verify 输出以下三个授权绑定，以及核验后的三个来源参数；全部作为工作流输出字符串传递：

| 输出 | 必须输出的值 | 用途 |
| --- | --- | --- |
| authorized_private_run_id | 本次 github.run_id 的规范十进制字符串 | 绑定当前私有 run，不使用来源 CI 的编号 |
| authorized_private_attempt | 本次 github.run_attempt 的规范十进制字符串 | 防止跨私有 attempt 复用旧授权 |
| authorized_triggering_actor | 本次 github.triggering_actor 的登录名字符串，例如 paulwxq | 与 job 上下文中的本次执行者匹配，不输出数字账号 ID |

稳定账号 ID 授权在 verify 内通过指定 attempt API 完成，同时核对该身份的登录名与本次 github.triggering_actor 一致，再输出上述登录名。github.triggering_actor 是登录名，上下文没有 github.triggering_actor_id；不能将 API 的 triggering_actor.id 填入 authorized_triggering_actor。job 级等值比较只绑定本次 run/attempt/执行者，不代替稳定 ID 授权，也不允许只凭登录名授权。[GitHub 上下文说明](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context)

release 在 job 级检查四个条件：verify success，以及三个授权输出分别等于本次 github.run_id、github.run_attempt、github.triggering_actor。完整结构见 §8.2，不在本地首个 step 才拒绝。既有实验的[探针写法](reviews/doc15.1-probe-kit/concurrency-probe.yml#L66)将 github.triggering_actor 原样写入授权输出；[C2 保存日志](reviews/doc15.1-evidence/b11231341r2/c2/C-verify.log#L69)记录对应环境变量 GITHUB_TRIGGERING_ACTOR 为 paulwxq，但没有单独打印最终授权输出的具体值。正式实现还须用本地替身检查输出的具体值，不能只断言条件字段存在。

私有部署首期只使用 Re-run all jobs 重跑，重新执行 verify；Re-run failed jobs/仅重跑 release 可能继承旧授权，绑定不符时必须在分配本地 Runner 前跳过。只重跑 finalize 也不能用旧授权使新 attempt 变绿。[重跑语义](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)

finalize 使用 needs: [verify, release] 和 if: always()；要求本次授权绑定完全一致、`needs.verify.result == 'success'`、`needs.release.result == 'success'`，以及 release job 的自定义输出 `needs.release.outputs.outcome == 'deployed'`，缺失/skipped/failure/cancelled 均非零。这里 outcome 是自定义业务输出，GitHub 内置 job 结果读取 result，不使用 needs.release.outcome，也不拿 step 的内置 outcome 代替实际部署结果。

只有 release_local.sh 在 deploy.sh 成功及最终现场核验后，才将 `outcome=deployed` 写入本次发布 step 的 `$GITHUB_OUTPUT`，并由 release job 显式映射为 `outputs.outcome`，供 finalize 从 needs.release.outputs.outcome 读取；仅打印普通日志、step 退出码为零或 job success 均不能代替这项输出。先记录源码 SHA、来源 run/attempt、私有 run/attempt、控制 SHA、release_id。过期而未部署同样明确显示未部署，不伪报绿色发布。强制取消或平台失效不保证 finalize 执行，最终现场仍需人工核实。

### 6.3 来源 CI 核验范围

通过固定公开仓库查询指定 attempt，完整分页获取必需 jobs：

```text
GET /repos/OWNER/insightweave/actions/runs/{source_run_id}/attempts/{source_run_attempt}
GET /repos/OWNER/insightweave/actions/runs/{source_run_id}/attempts/{source_run_attempt}/jobs
```

检查固定 ci.yml workflow ID/path、event=push、main、固定 head repository、完整 source_sha、指定 attempt completed/success。完整分页后按 jobs API 的 name 字段，对 §7.1 固定的 nl2sql、agent_core、metaweave、deploy_tests、frontend、ci-required 六个名称逐项精确匹配（含大小写），每个必需名称须唯一且 success；缺失、改名或同名多条均拒绝。不能将 API 的数字 job id 当作 YAML job ID，也不按前缀/子串猜测套件。workflow path 按精确路径处理，若返回 @ref 后缀则单独核验，不能子串匹配。证据查询前后读取通用 run 的最新 attempt，仍须等于请求值，并确认当前 main HEAD 未变；最新 attempt 失败/进行中时不借用旧成功。

原始 actor 必须为维护者，防止他人合并后借维护者 PAT 自动发布。来源 attempt>1 的 triggering_actor 也必须是维护者，无论是否与 actor 相同；身份/API 字段缺失、限流或证据不完整均停止，不自动降级。首次 push CI 不要求维护者再手动点一次发布。

**与旧设计的区别：**本版接受 GitHub 对同一源码提交的正常成功结果复用，不再要求“每个 suite 必须在本次 attempt 重新运行”，不新增逐 job/step 时间窗口证明。指定 attempt 仍须完整成功、必需 job 结果齐全并绑定相同源码；旧 SHA、旧 attempt 的独立查询不能拿来补齐缺失。维护者日常需要完整重新验证时使用 Re-run all jobs。私有部署的授权输出禁止跨 attempt 复用，仍按 §6.2 执行，不能混淆这两种复用。

C3 实验已证明指定 attempt 的 jobs 可包含新编号但旧执行时间的继承记录；本版不据新 ID 宣称重新跑过全部测试，也不把该调度实验当成来源核验程序已验收。[实验报告](reviews/doc15_job_concurrency_test_report.md)

核验控制逻辑只取私有固定提交，不能从请求指向的公开代码加载后为自己授权。已合并代码仍在本机构建时执行，维护者合并前须检查脚本、依赖及 Dockerfile。

## 7. 云端 CI 与本地脚本的改造

### 7.1 拆分测试入口，保留本地完整 CI

当前 `deploy/scripts/ci.sh` 混合了依赖准备、测试、Docker 构建和候选预检，不能原样作为云端 PR 检查入口。

**跨仓库固定名称约定：**

- ci.yml 的 workflow 顶层固定 `name: InsightWeave CI`，供 notify-deploy.yml 的 workflow_run.workflows 精确引用；私有端仍独立核验固定 workflow ID/path。
- 五个 suite 的 YAML job ID 和 job name 均固定为 `nl2sql`、`agent_core`、`metaweave`、`deploy_tests`、`frontend`；实现时显式设置同名 name，不另起展示名，不增加矩阵后缀。
- 汇总 job 的 YAML ID 为 `ci_required`，job name 固定 `ci-required`；分支保护使用这个 name。needs 引用 YAML ID，私有核验使用 jobs API 的 name，二者不可混用。

上述名称是通知、保护规则及私有核验共同依赖的接口，修改须同步相关配置并通过诊断核验；不能在各仓库各自改名。[工作流及 job 名称语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idname)

拟提取 `deploy/scripts/check.sh`，用于依赖准备、离线哑配置、受维护测试及前端类型检查。最小接口固定为 `check.sh [all|nl2sql|agent_core|metaweave|deploy_tests|frontend]`，缺省 `all`；未知参数非零退出，不用 `eval`。用 shell case/函数集中维护每个 suite 的命令、排除项、哑配置和依赖准备，不在 YAML 再写一份测试范围，不引入复杂动态矩阵框架。

`all` 按固定顺序调用 `nl2sql → agent_core → deploy_tests → metaweave → frontend` 的同一组 suite 实现，任何一步失败立即退出。云端每个测试 job 仅选择相应 suite，本地 `ci.sh` 显式执行 `bash deploy/scripts/check.sh all`，通过后继续原镜像构建与候选预检。Agent 核心套件始终使用独立 pytest 进程。提取时保留干净源码检查和版本要求，不能新增跳过干净检查的开关；版本检查按选中 suite 所需工具执行，不能让纯前端 job 被后端 Python 配置阻塞。

CI 范围以当前脚本和部署手册为基线：

| 部分 | 保留范围 |
| --- | --- |
| NL2SQL | `src/tests/unit/api`、`src/tests/unit/services`、`src/tests/unit/utils`（均相对 `nl2sql/`）；summarizer 的仓库根相对路径为 `nl2sql/tests/unit/db_deepagent/test_independent_summarizer.py`，不在 `src/tests/` 下 |
| NL2SQL Agent 核心 | 独立进程执行 `src/tests/db_deepagent -q -ra`，不传真实库/模型开关 |
| 部署 | 根目录 `deploy/tests/` |
| MetaWeave | `tests/unit`，保留现有历史产物依赖排除项及 live 测试门控 |
| 前端 | `src/` 中已有 `.test.cjs`、`.test.ts`，以及 `npm run typecheck` |

各 suite 的准备边界：

| suite | 需要的工具与依赖 |
| --- | --- |
| `nl2sql` / `agent_core` | Python 3.12、uv，按 NL2SQL 锁文件准备 `.venv` 和 dev extra；各自运行上表对应范围 |
| `deploy_tests` | Python 3.12、uv，沿用当前 CI 的 NL2SQL `.venv` 执行根 `deploy/tests/`；准备其测试实际需要的模块、哑配置及导入路径，不依赖开发机现有 venv |
| `metaweave` | Python 3.12、uv，按 MetaWeave 锁文件准备 platform/dev extras，保留历史产物排除及 live 门控 |
| `frontend` | Node 22 和 npm，使用前端 lockfile 执行 `npm ci`；不安装全部后端依赖，也不要求后端 Python 环境 |
| `all` | 上述全部工具；同一次执行可复用已准备的同锁文件依赖，不能改变 suite 定义或跳过检查 |

`nl2sql`、`agent_core`、`deploy_tests` 的哑配置集合与导入路径按现有 `ci.sh` 的导出面整体提取，随每个独立 suite 初始化，包括 `PYTHONPATH="$REPO_ROOT:$NL"`；不能只搬 pytest 命令或依赖其他 job 的环境变量。

云端固定 Python 3.12、Node 22（满足现有脚本要求）、uv 版本，并复用 `uv sync --frozen`、`npm ci`。云端不配置 WSL/Windows 代理地址，不连接业务数据库，不使用真实模型 Key。现有跳过原因仍须显示；不得把跳过数当作通过数。

`ci.yml` 同时监听 `pull_request` 到 main 和 `push` 到 main，使用 GitHub 托管 Runner、只读权限，固定拆为上述五个 suite job 并增加汇总检查 `ci-required`；分支保护只引用这个稳定门槛。该 job 显式依赖全部必需套件，使用 `if: ${{ always() }}`，避免依赖失败、取消或跳过后汇总本身按默认条件被跳过；逐项要求 `needs.<job>.result == 'success'`，任何 failure、cancelled、skipped 或缺失均失败。不允许测试 job 通过 `continue-on-error` 隐藏失败。

以下为汇总 job 模板，需放在实际定义对应套件 jobs 的 `jobs:` 下；`name` 为稳定检查名，`ci_required` 为 job ID：

```yaml
ci_required:
  name: ci-required
  needs: [nl2sql, agent_core, metaweave, deploy_tests, frontend]
  if: ${{ always() }}
  runs-on: ubuntu-latest
  timeout-minutes: 5
  steps:
    - name: Require every mandatory suite to succeed
      env:
        ALL_REQUIRED_SUCCEEDED: >-
          ${{ needs.nl2sql.result == 'success' &&
              needs.agent_core.result == 'success' &&
              needs.metaweave.result == 'success' &&
              needs.deploy_tests.result == 'success' &&
              needs.frontend.result == 'success' }}
      run: test "$ALL_REQUIRED_SUCCEEDED" = true
```

`always()` 只保证汇总具备运行条件，不把失败变成通过；该 job 不下载源码、不执行长任务，只检查依赖结果。整体 workflow 被强制取消或基础设施故障时，不能承诺汇总一定实际启动，但取消/无结果绝不解释为绿色，私有端还要求指定 attempt 完成且全套成功。避免对整个 workflow 做会让必需门槛缺席的路径过滤。[GitHub 状态表达式说明](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions)

首期不对文档改动添加 paths-ignore，文档合并同样运行 main CI 并授权发布。若代码 B 的 CI 尚未完成就合入不跑 main CI 的文档 C，B 已不再是 HEAD、C 又无成功 main CI，现有核验可能使两者都无法发布；以后确需减少文档发布时再单独设计，不只修改 push 路径过滤。

完整云端范围须首次实跑验收，不能仅凭“离线测试”名称认定可移植。

测试入口提取还会影响 `deploy/tests/test_deployment_contract.py` 中 `TestCiSh` 的脏工作区、缺失 Python、Agent 核心失败阻止构建等行为测试；需同步调整，继续调用真实脚本并通过命令替身验证：缺省等同 all、每个 selector 只执行对应 suite、all 使用同一套命令且顺序一致、未知 selector 拒绝、前端不准备后端依赖、Agent 核心失败阻止 Docker 构建。不能只匹配 YAML 或脚本文本。

### 7.2 双 checkout 和版本身份

```text
$GITHUB_WORKSPACE/
├── control/   # 本次私有 run 的固定 github.sha
└── source/    # verify 核验的公开 source_sha
```

两个 checkout 均关闭凭据持久化，Action 使用核验过的完整提交版本。私有 verify/release 固定使用同一 github.sha，重跑仍用原控制提交；修复控制代码后需要新的 dispatch，不假定旧 run 会读取修复。

公开 source 指定固定 repository/ref，核对 git rev-parse HEAD 与 source_sha，并保持源码干净。日志、请求 JSON 和 API 响应写 RUNNER_TEMP 或运行日志，不预写入 source。现有候选清单路径继续使用被 git 忽略的 deploy/candidate-*.json。

以下片段仅示意本地构建及发布调用，实际入口还须按下列分工完成核验和日志：

- 私有执行身份：由云端 verify 按 §6.2 核验私有 run 的原始 actor 和本次 triggering_actor；release 的 job 级条件在分配 Runner 前核对本次私有 run/attempt/执行者授权绑定。
- 公开来源身份及成功证据：由同一个云端 verify 按 §6.3 核验来源 CI 的原始 actor、重跑者及指定来源 attempt 的成功证据。
- 本地入口：核对来自 verify 的本次私有授权绑定，使用其可信来源参数，在本地开始及构建完成进入部署前复核公开 main HEAD 等于 source_sha，且 source_run_id 的最新 attempt 仍为 source_run_attempt，并按 §6.3 的复核规则处理变化或查询失败。这里的来源 attempt 不等于私有 github.run_attempt；本地不另建一套身份授权规则，并记录两层 run/attempt、源码及控制版本。

```bash
set -euo pipefail
cd "$GITHUB_WORKSPACE/source"
test "$(git rev-parse HEAD)" = "$SOURCE_SHA"
release_id="${SOURCE_SHA:0:12}-d${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
candidate_manifest="$PWD/deploy/candidate-${release_id}.json"
MANIFEST_OUT="$candidate_manifest" bash deploy/scripts/ci.sh "$release_id"
# 私有入口再次核对公开 main HEAD 和来源 run 的最新 attempt 后，才执行下一步
CANDIDATE_MANIFEST="$candidate_manifest" \
  bash deploy/scripts/deploy.sh release "$release_id"
```

ci.sh/deploy.sh 继续从源码 checkout 计算 SHA；APP_GIT_SHA、候选和快照 source_sha 必须是公开源码版本。GITHUB_SHA 仅表示私有控制提交，来源 CI run 与私有部署 run 分开记录。

### 7.3 保留现有部署行为

沿用 deploy.sh、release_manifest.py、init_platform.py、smoke.py 和运行时门闩，保留候选/镜像身份/配置哈希核验、上游与端口预检、配置检查、排空、两阶段冒烟、current/previous、recovery_required 及可信快照回退。

本地 state/.lock 继续由 deploy.sh 获取；外层入口不能持有同一非重入锁再调用脚本。WSL 继续要求 iw-runner 能执行 Windows PowerShell 端口检查。不得绕过预检直接 compose up，不删除数据库卷或恢复未知现场。

首期不改变 manifest 格式、不接入请求账本，也不把云端失败颜色当作容器未变更的证明。

### 7.4 已确认的 contract.json 类型问题

2026-11-23 轻量隔离探针确认：[release_manifest.py](../deploy/scripts/release_manifest.py) 的 validate_candidate() 已严格要求 manifest 的契约版本为整数 1；create_snapshot() 和 load_snapshot() 对 contract.json 仅用 != 1，错误接受 true 和 1.0。

探针使用真实临时 JSON、合法候选、有效配置哈希及 Docker/上游替身，不触及实际容器或运行状态。9 类输入结果：

| JSON 输入 | contract.json 创建/加载 | manifest |
| --- | --- | --- |
| 整数 1 | 接受 | 接受 |
| true、1.0 | 错误接受 | 拒绝 |
| 字符串 "1"、false、0、2、null、缺失字段 | 均拒绝 | 均拒绝 |

这是现状复现，不代表普通 CI 已发生绕过。拟在两个 contract.json 入口统一要求 type(version) is int 且值等于 1，不做类型转换，也不能只用 isinstance(int)。在现有 test_release_manifest.py 为三个入口保留上述 9 类参数化回归，加载用例须保持哈希有效。修复不得破坏镜像/哈希核验或重写成功状态；仅需本地替身测试，不重跑 GitHub 调度实验。本轮尚未修复代码或新增持久化测试。

### 7.5 上线前一次完整本地 CI 演练

在新脚本首次发布前，对功能分支已提交的固定 SHA 做一次完整 ci.sh 演练，先执行 check.sh all，再构建三镜像、候选预检并生成候选清单；不只跑替身或 collect-only，也不调用 deploy.sh。此项须获得 §9.1 的生产主机演练明确授权，本次文档修订不执行。

**演练后追加提交的处理：**步骤 4 合入前，比较已演练提交与最终待合入内容，包括更新 main 带来的差异，记录是否需要重演及其依据。判断的是实际测试/构建链路是否有实质变化，例如 check.sh、ci.sh、release_manifest.py、Dockerfile、.dockerignore、依赖定义/锁文件、相关测试或哑配置；清单不是封闭白名单，也不因文件仅改注释就机械重建。实质影响链路时，对最终功能分支已提交的固定 SHA 再做一次完整演练，追加的生产主机演练须另行授权，通过后才合入。仅文档、审核记录等不影响链路的变化无需重演；merge commit 的 SHA 本来就与分支提交不同，不能仅凭 SHA 改变要求重演。正式 main CI 和部署前完整本地 CI 仍验证实际合并提交，不能拿演练 SHA 替代发布 SHA，也不新增自动判断或重演机制。

优先在目标 Debian 以 iw-runner 身份、按 Runner 必需的 Python/Node/uv/Docker/代理配置运行；使用该功能分支提交的新克隆，核对 HEAD 及干净状态，不放旧/新 Runner 的 _work，也不占用 §8.4 的手动回退检出。不得复制开发机 .env、真实业务配置或凭据；只提供工具/代理等必要环境变量，不继承真实业务凭据环境变量，suite 使用共享入口的哑配置。这样可以实际验证 MetaWeave 套件的配置需求；收集成功不算运行通过，若确属缺配置，按失败原因补齐该 suite 所需哑配置，不依赖开发机已有环境。

演练前确认没有发布在途，选择维护者认可的空闲时段，并只读查看磁盘和内存余量；资源不足先报告，不通过清理生产资源腾空间。期间不合并 main、不发起其他部署。演练会在同一 Docker 主机消耗 CPU、内存、磁盘和 I/O，并启动原 ci.sh 的临时候选检查容器；虽不主动替换或停止既有应用/基础服务，也不能承诺业务完全不受影响。若只能在开发机执行，报告应注明其不能替代真实 Runner 环境验收；不要求两套环境各完整重跑，不新增自动限额或资源调度机制。

**本次资源的精确命名：**测试 release_id 须唯一，以字母或数字开头，只含字母、数字和 `_ . -`（不含空格），满足 `^[A-Za-z0-9][A-Za-z0-9_.-]*$`；例如 `preflight-a1b2c3d4e5f6-20261124T083000Z`。开始前确认下面三个完整容器名和三个完整镜像 tag 尚不存在，且不与 current/previous 引用重合，不接管已有同名资源：

| 应用 | 本次候选检查容器名 | 本次镜像 tag |
| --- | --- | --- |
| metaweave | `ci-probe-metaweave-<release_id>` | `insightweave/metaweave:<release_id>` |
| nl2sql | `ci-probe-nl2sql-<release_id>` | `insightweave/nl2sql:<release_id>` |
| frontend | `ci-probe-frontend-<release_id>` | `insightweave/frontend:<release_id>` |

**前后记录及收尾核对：**

1. 演练前，按精确 Compose 项目标签 `com.docker.compose.project=insightweave-local` 只读选择线上项目容器，记录服务名、完整容器 ID 和 `State.StartedAt`，按服务名及完整 ID 排序；同时记录 `state/deployment.json` 的 phase、current。只读取所需字段，不输出完整 inspect、业务 env 或凭据。另记录本次源码 SHA、release_id、三个 tag 和独立克隆的明确绝对路径。
2. 本次 ci.sh 及相关构建确认结束后，无论成功、失败还是中断，都再次采集上述线上字段，逐项比较。容器集合/ID/启动时间及 phase/current 应保持一致；有差异或查询失败时报告并先核实，不能宣称未重启/未替换，也不自动调用部署或回退。字段一致仅作为容器身份、启动时间及部署引用未变的证据，不证明业务性能完全不受影响。
3. ci.sh 的正常路径和已覆盖的显式失败分支都会尝试删除候选容器，并非只有失败分支清理；中断或其他未覆盖失败点仍可能留下资源。按上表三个完整容器名逐一查询、确认归属，删除本次残留；再按三个完整仓库:tag 移除本次测试引用。删除后逐项只读查询，明确确认对应容器及 tag 均不存在；Docker 查询失败不能当作无残留。不得按前缀、短 ID 或包含 release_id 的模糊匹配批量删除，也不按镜像 ID 强删共享引用、不清理 current/previous、不做全局 prune。
4. 将结果、必要的候选清单及前后对照记录保存在独立克隆以外的受限日志位置，再核对并删除本次独立克隆，读回确认该明确路径不存在；不删除 _work、手动回退目录或其他克隆。收尾后可由审核方对记录及必要现场字段做一次只读核对，不据此追加清理、重建或部署权限。

uv、npm 和 Docker 构建缓存属于共享缓存，演练后保留；它们可能增大，不要求磁盘占用回到演练前，也不将其视为本次必须清空的残留。失败或结果不明时先记录原因及现场，报告是否需要额外授权再跑，不自动反复完整重建；不为改正文档重复演练。

## 8. Runner、并发与人工恢复

### 8.1 Runner 迁移

按 §10 先停用旧发布并确认无未决任务，再迁移同一 iw-runner、安装环境和标签到私有仓库 repository-level Runner。不能只改标签，标签不是仓库权限隔离。

历史旧服务为 actions.runner.paulwxq-insightweave.aithink.service，入口 /home/iw-runner/actions-runner/runsvc.sh。先停旧服务、svc.sh uninstall、移除旧注册，再注册新仓库、安装启动新服务；按新生成服务名核验。受限 .env 含 Runner 环境，保留其权限及必要设置，不打印内容、不复制旧注册凭据。核对代理、Python、Docker、部署目录和 _work，不同时启动服务及手工 Runner。[服务管理](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application)

### 8.2 最小并发控制及既有实验

首期仅保留固定执行 job 并发组 insightweave-local-release，cancel-in-progress: false；workflow 顶层无 concurrency，verify/finalize 组外：

```yaml
release:
  needs: verify
  if: >-
    ${{ needs.verify.result == 'success' &&
        needs.verify.outputs.authorized_private_run_id == github.run_id &&
        needs.verify.outputs.authorized_private_attempt == github.run_attempt &&
        needs.verify.outputs.authorized_triggering_actor == github.triggering_actor }}
  runs-on: [self-hosted, Linux, X64, insightweave-local]
  concurrency:
    group: insightweave-local-release
    cancel-in-progress: false
```

这是片段，须补齐 verify、来源输出和实际 steps 后才能启用。一个执行 job 覆盖本地构建到部署收尾，组名不含分支、workflow 或 run ID。现有 flock 约束手动命令；未来如新增其他变更工作流，再复用同组，不为首期提前建设它们。

默认 single 会让新入组请求替换已有 pending，cancel-in-progress: false 不打断正在执行的 job。入组顺序不能视为源码提交顺序，也不能仅由 verify 的完成顺序推断；verify 通过与下游 release 被调度进入并发组不是原子操作，不承诺按提交或请求发出顺序部署。[并发规则](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

例如，在另一个发布占组时，A 最后一次有效核验 main HEAD 时 main 仍为 A，但其 release 尚未入组；main 随后前进到 B，B 的完整 main CI、通知及私有 verify 完成，B.release 进入 pending；A.release 若此时才入组，仍可能替换 B，之后本地复核发现 A 已过期而停止。这不要求 A 在 main 已为 B 时通过 HEAD 核验。本例的窗口从 A 最后一次有效核验 main HEAD 到 A.release 入组，须覆盖 B 的上述完整链路，因此需要足够长的调度延迟；窗口不一定从 verify job 结束时才开始，也不据此认定平台异常。该时序是边界推导，尚未实测，不将 C1—C6 PASS 当作其已验证证据。本地复核用于阻止已知过期请求部署；出现等待任务被取消或过期跳过时，维护者按 §8.3 核实后重试，不增加自动调度补偿或专门实验。

因此不保证最新 main 最终自动发布。除上述间隔外，最新来源 attempt 失败、额度不足或执行失败也可能停止自动链路；这些均由维护者排查处理，不把已受理或排队当作最终部署保证。

云端 verify、本地开始和构建完成进入部署前复核 main 及最新来源 attempt。最后一次核验后 main 仍可能前进，已开始 deploy.sh 的请求正常收尾，不强杀。

**过期请求的预期显示：**本版沿用 §6.2 的严格结果策略，确认过期而未部署的私有运行通常以 failure（红色）结束；即使 release job 是 skipped，finalize 仍因未取得本次 deployed 而失败，不能把整次运行称为 GitHub 的 skipped。连续合并时出现这种红色“未部署”属于预期，不等于容器替换故障；较早运行也并非一律红色，已经完成的可成功，被并发组替换的可能显示 cancelled。verify 和本地复核在完成必要身份/来源核验、明确确认 main HEAD 或最新来源 attempt 已改变时，统一打印固定日志标记 `DEPLOY_STALE_REQUEST` 及脱敏原因，便于人工检索。API/网络失败、证据不完整或身份拒绝不能冒用此标记；标记仅用于日志，不改变退出策略、不新增状态机，也不将其作为实际部署成功的证据。

**已有证据保留：**2026-11-23 公开沙箱 paulwxq/insightweave-ci-sandbox 批次 b11231341r2，C1—C6 经复核均 PASS：拒绝/绑定不符不替换合法 pending，局部重跑旧授权被拦截，合法请求可替换 pending，跨 workflow 互斥，finalize 组外。本实验观察到组内等待的 jobs API 状态为 pending。20 次运行当时均 completed；被测工作流提交 2a07b3c1d47de4cd47305db2caa121d276416966，收尾 HEAD 17e50bcc24686b87edf7e6bc7be01c4fcdb294c3 是驱动修正。

**单台生产 Runner 的观察边界：**上述实验全部使用托管 Runner，不保证生产中出现相同的等待状态或替换时序。生产只有一台 self-hosted Runner，没有空闲匹配 Runner 时任务可能资源排队为 queued；资源排队与并发组 pending 是两层机制，须结合实际 run/job 和等待原因判断，不能仅凭一个状态词认定已经入组。单台 Runner 不表示并发组不会出现 pending，已进入同组 pending 的默认 single 替换规则仍适用；实际可能看到等待任务被替换，也可能看到任务先后执行、较旧请求在本地复核时因过期停止，不预先断言会发生哪一种。授权拒绝仍由 job 级条件在本地 Runner 分配前拦截，现有 flock 和过期复核继续保留。按步骤 9 的既定端到端验收记录实际观察；没有出现竞争窗口就不声称已实测该窗口，不另制造竞争或新增专门实验。[自托管 Runner 调度说明](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#routing-precedence-for-self-hosted-runners)

详见 [doc 15.1](15.1_部署并发授权门禁测试需求.md) 及 [报告](reviews/doc15_job_concurrency_test_report.md)。原实验及历史回填记录保留，适用范围说明已同步当前精简方案；旧版完整回执恢复、来源时间证明及维护框架不属首期要求。C5 只作为将来复用的证据，不要求现在实现维护工作流；C3 的原驱动误判和人工改判记录不改写。无需为了精简文档重跑六项。

### 8.3 日志、重复请求和重试

使用 GitHub 日志/摘要、明确 run ID、现有 deployment.json 和发布快照人工追踪。成功摘要至少有公开 source_sha、来源 run/attempt、私有控制 SHA、私有 run/attempt、release_id 及结果；不保存凭据，不新增请求状态机、成功去重库或跨文件事务。

首期不保证重复请求只执行一次：私有完整重跑会生成新 release_id，可能再次构建和部署同一源码。成功后不主动重跑通知/部署；如需重新发布相同源码，视为维护者明确决定，不伪称只读健康检查。旧版本不是当前 main 时仍不能自动发布。

人工恢复顺序：

1. 查明确来源/通知/私有运行，确认旧执行确已结束；有未知发送或执行时先核实，不重复启动。
2. 核对 deployment.json、维护标记、current/previous、可信快照、实际镜像及普通请求；不靠退出码或颜色判断是否变更。构建/预检失败且现场未变可排查后重试；deploying/recovery_required 或结果不明先走现有受控恢复。
3. 现场 ready 后再次确认最新 main/来源 attempt。来源 CI 失败则修复后完整重跑，源码/工作流修复须新提交，旧 run 重跑不读取新代码。
4. 通知明确未受理时由维护者重跑通知；已创建的私有运行失败时按实际情况 Re-run all jobs，重新 verify。通知是否受理未知则先查清，不自动再发 POST。
5. 只恢复当前需要发布的版本；不批量重放，不为让状态变绿而重复已经完成的部署。无法确认时继续人工处置，不虚构自动恢复结果。

对应命令如下，均完整重跑，不带 `--failed` 或 `--job`。按已核实的故障位置择一执行，不能把三条连续执行；大写占位符替换为对应工作流的实际 run ID，不混用来源、通知和私有编号。[gh run rerun](https://cli.github.com/manual/gh_run_rerun)

| 恢复场景 | 命令 |
| --- | --- |
| 私有部署失败，现场已恢复 ready 且来源仍合法，重新执行 verify | `gh run rerun PRIVATE_RUN_ID --repo OWNER/insightweave-deploy` |
| 通知明确未受理，原因已排除且来源仍合法 | `gh run rerun NOTIFY_RUN_ID --repo OWNER/insightweave` |
| 最新 main 的来源 CI 需要完整重新验证，且相应修复适合在原提交重跑 | `gh run rerun SOURCE_RUN_ID --repo OWNER/insightweave` |

私有 verify 原先成功时，`--failed` 或仅重跑 release 可能复用旧授权而被本次 attempt 绑定拦截；不能用局部重跑替代本表的私有完整重跑。来源 CI 的同 SHA 成功结果复用政策仍见 §6.3，本表仅统一人工恢复时的完整重跑方式。通知受理未知时三条都不直接执行；处于维护/回退暂停期间时，先按 §8.5 决定是否恢复自动发布。

### 8.4 回退与配置更新

首期保留本机受控手动 rollback <release_id>，目标仅 current/previous，先执行 §8.5 的简短维护程序。选用维护者已经核验、来源明确的宿主部署工具，记录其源码版本；优先使用操作开始时可信 current 对应源码，不能拿未审查的浮动 main 当恢复工具。工具必须核验目标快照、镜像、运行契约并走现有锁/预检/排空/冒烟；无法确认兼容时停止人工排查，不猜测或直接 compose up。首期不要求新 capabilities 接口，不上线私有自动 rollback 工作流。

**手动工具的固定位置：**以 iw-runner 身份在 `/home/iw-runner/insightweave-ops/source/` 保留独立检出；每次操作先从可信 deployment.json 的 current 引用及其快照 manifest 取得完整 source_sha，将该目录固定检出到这一提交，核对 HEAD 和干净状态，记录本次工具 SHA。此处按操作开始时 current 选择工具，不按待回退的 previous 选择工具；应用镜像、Compose 和相对配置仍从待回退目标的可信快照加载。current、工具来源或兼容性无法确认时停止，先人工核实已有可信恢复工具，不猜测提交。

该目录在 §10 步骤 3 完成旧任务核验后、进入步骤 4 前准备，从已有可信本地仓库建立并保留所需提交即可，不依赖源码已公开。不要把旧 Runner 的 `_work` 或新 Runner 最近一次 `source` 当作长期手动工具目录，也不在自动任务仍运行时切换它们。新版本上线后，下一次手动操作须重新按当时 current 固定检出，不能沿用旧 HEAD。

部署手册的相对命令须明确从上述目录执行，或改用下面的脚本绝对路径。脚本默认 Compose 路径按脚本自身位置解析，与调用者当前目录不同；目标快照加载后再选择目标 Compose。以下只演示已完成暂停、目标/工具及兼容性核验后的手动回退调用，不执行构建或 bootstrap；占位值必须替换为已核实值：

```bash
set -euo pipefail
OPS_SOURCE=/home/iw-runner/insightweave-ops/source
D=/home/iw-runner/insightweave-runtime
EXPECTED_TOOL_SHA=REPLACE_WITH_VERIFIED_CURRENT_SOURCE_SHA
TARGET_RELEASE_ID=REPLACE_WITH_VERIFIED_CURRENT_OR_PREVIOUS_RELEASE_ID

ACTUAL_TOOL_SHA=$(sudo -u iw-runner git -C "$OPS_SOURCE" rev-parse HEAD)
test "$ACTUAL_TOOL_SHA" = "$EXPECTED_TOOL_SHA"
OPS_STATUS=$(sudo -u iw-runner git -C "$OPS_SOURCE" status --porcelain)
test -z "$OPS_STATUS"
sudo -u iw-runner env DEPLOY_ROOT="$D" ENV_FILE="$D/deploy.env" \
  PROJECT=insightweave-local \
  PYTHON_BIN=/home/iw-runner/.venv-smoke/bin/python \
  bash "$OPS_SOURCE/deploy/scripts/deploy.sh" rollback "$TARGET_RELEASE_ID"
```

上述 Python 路径沿用既有部署手册，准备目录时还须确认 iw-runner 可读取工具、执行该 Python 及现有 Docker/端口检查；目录存在或 HEAD 匹配不等于回退已验收。这里只核对源码目录，不将运行凭据复制进检出，也不在外层持有 deploy.sh 的非重入部署锁。

现有 release <current> 和 ready 下 rollback <current> 成功时仅只读核验，不能依此让新 env 生效。配置修改需要真正重新创建后端。首期如由维护者明确选择对最新 main 再执行一次完整发布，新 release_id 的构建/发布会重建后端并读取 env；这也会发布最新 main 的代码，不能称为只配置重发。当前版本与 main 不同且只想改配置时，不自动升级代码。

独立的 reconfigure <expected_current>、纯 capabilities 及固定维护工具自动选择延后，另行实现后才开放配置专用入口；届时仍须固定当前镜像/Compose、锁内复核目标、排空、强制重建、两阶段冒烟，保持 current/previous，不因使用同镜像就声称旧配置已恢复。当前快照不保存旧凭据，配置失败没有自动旧配置回退保证。不得制造维护标记、篡改 phase 或跳过预检来强迫现有只读分支变更。

### 8.5 维护者的简短操作程序

由于只有维护者能合并和部署，维护时由同一人停止合并、通知重跑及私有重跑，使用 gh 管理身份操作公开通知。无需 maintenance_control.py 或维护操作账本：

1. 停用 notify-deploy，读回确认 disabled_manually，核对已有公开通知和私有部署（含 queued、waiting、pending、in_progress），等正在发布的任务正常收尾。已完成通知也核对其回执对应的私有 run；有未决发送/执行则先查清。停用不取消已有运行。
2. 确认旧执行结束后，保存本次所需 run 编号、current/previous 和操作记录，使用既有部署锁执行维护/恢复；外层不要重复持有 deploy.sh 的锁。不开另一个发布，不强杀维护中的任务。
3. 核验维护结果及现场 ready。失败、崩溃或未知时通知继续停用，维护者恢复后再决定启用，不在 finally/trap 无条件打开。
4. 普通维护结束后可启用通知；暂停期间完成的 CI 不保证补发。需要发布时由维护者确认最新 main 后，使用 §8.3 的来源 CI 完整重跑命令，使新成功事件通知部署；不逐条补历史通知。
5. 实际回退后默认保持通知停用。维护者决定重新发布时，确认当前 main、核实既有 CI/通知/部署已结束且无未决发送，再启用通知并按 §8.3 的来源 CI 命令完整重跑该 main 的 CI；若需要源码修复，则启用后合并修复 PR，走新的 main CI。恢复自动发布表示维护者同意当前 main 再次上线，可能高于回退后的版本；不批量补发旧通知，不重跑回退前的旧私有部署，期间不合并无关变更。

```bash
# 暂停前先记下原 state；原先已停用则查明原因，不自动启用
gh api repos/OWNER/insightweave/actions/workflows/notify-deploy.yml --jq '{id,path,state}'
gh workflow disable notify-deploy.yml --repo OWNER/insightweave
gh api repos/OWNER/insightweave/actions/workflows/notify-deploy.yml --jq '{id,path,state}'
# 以下启用命令只在人工确认维护完成且应恢复自动发布后单独执行
gh workflow enable notify-deploy.yml --repo OWNER/insightweave
gh api repos/OWNER/insightweave/actions/workflows/notify-deploy.yml --jq '{id,path,state}'
```

仅操作通知，不停 CI，也不恢复已退役 local-release；原来被停用的通知须由维护者确认原因再启用。迁移前通知尚未创建时不调用不存在的入口：改为确认旧 local-release 已停用、无未决任务且新通知尚未上线，保留受控本机手动路径。若平台/API 故障无法确认旧任务状态，先核实进程及现场，不能假定已安全清空。

## 9. 基于当前代码的首期改动清单

| 位置 | 改动及约束 |
| --- | --- |
| 公开 local-release.yml | 任何准备性改动合入 main 前先停用并核实旧发布结束；在 §10 步骤 9 的切换 PR 中删除，不恢复公开 self-hosted 路径 |
| 公开 ci.yml、check.sh、ci.sh | 提取共享五套件检查；PR/main 云端汇总；本地 all 后继续原构建/候选预检 |
| 公开 notify-deploy.yml | 三来源参数、固定 dispatch API、日志/摘要保存明确私有 run ID，DEPLOY_REPOSITORY 按目标 API 的 full_name 原样配置；无自动重试或回执恢复程序 |
| 私有 deploy.yml、verify_source_request.py、release_local.sh | 单维护者稳定 ID、固定来源/当前 main/最新 attempt、job 级授权绑定、双 checkout、本地复核及 finalize；明确过期时输出统一日志标记，技术故障及未授权拒绝不用该标记 |
| 私有 .github/workflows/verify-diagnostics.yml | 新增仅手动触发的托管只读诊断，fixtures/source-readonly 复用核验逻辑；source-readonly 手填三个来源参数并重验当前 main、最新 attempt 的 success 状态和六个固定 job 名，fixtures 无需来源参数；步骤 5、8 专用，无 release/self-hosted/部署调用或写令牌 |
| 私有 tests/README | 响应替身（含 API 限流停止放行）、身份和版本拒绝分支、重跑绑定、日志字段及人工恢复程序 |
| 公开 release_manifest.py、test_release_manifest.py | 补齐 contract.json 两入口严格整数校验，9 类输入本地回归，保留既有 manifest/镜像/哈希约束 |
| 公开 test_deployment_contract.py | 更新 TestCiSh 及 suite 真实行为测试，保留 Agent 核心失败阻止构建、干净源码拒绝 |
| 公开 deploy/README、doc 13/13.1 | 随步骤 9 切换 PR 更新开发、Runner、追踪、完整重跑命令和配置更新边界，移除 README 配置生效及日志级别说明中的旧 Run workflow 指引；手动回退明确独立检出目录、current 工具 SHA 及绝对脚本路径，最小运维说明在步骤 3 即可用；保留部署状态/数据卷/恢复机制 |
| GitHub 设置 | 单人 PR/CI 保护、公开受限 environment/Secret、私有写权限及 Runner 归属 |

首期不为拆仓而修改 deploy.sh 的状态协议，不接入请求账本；独立 reconfigure/capabilities 不列为核心发布上线依赖。提取测试时检查 git show HEAD 读取已提交脚本的测试，不能只改源码布局而不更新行为测试。

### 9.1 实施分工与授权边界

本轮仍仅授权文档修订，下面是建议的开工范围，不代表已授权改代码、推送 PR 或在生产主机构建。由一个实施者负责主线，现有审核 AI 复核关键差异；这不要求另建 Agent、发送消息或新增协调框架。

维护者明确确认第一阶段后，实施者才可在该范围内连续完成：

| 第一阶段待授权事项 | 范围及影响 |
| --- | --- |
| 功能分支代码、测试和文档 | 只修改功能分支、提交变更及运行本地测试；首个 PR 带上已裁定随仓公开的 docs/14_*.md、docs/15_*.md、docs/15.1_*.md 和本次 docs/reviews 报告/证据/探针，文档可独立提交便于复核，无需等永久定稿；只加入明确路径，不捎带其他无关未跟踪文件 |
| 推送功能分支并创建 PR | 不推送或合并 main；当前源码仓仍私有时，PR CI 会消耗包含额度，按 §6.1 核实实际用量和零付费阻断 |
| 生产主机一次完整 ci.sh 演练 | 必须在授权中明确写入以 iw-runner 身份执行 §7.5，不能从普通本地测试授权推定；涉及同机镜像构建、临时候选容器及本次资源收尾，只到候选清单，不调用 deploy.sh |

以下均不包含在第一阶段，执行前分别取得维护者对具体目标和范围的确认：推送或合并 main；停用旧入口；停止、卸载、注册或迁移 Runner 服务；修改仓库可见性、Secret、environment 或分支保护；调用 deploy.sh、正式部署工作流或停止既有服务。后续私有仓准备及托管诊断也按相应迁移步骤另行明确授权，不能因“PR 准备”提前执行。已授权动作范围内无需逐条命令重复确认；发现状态不明、超出范围或需要追加生产演练时，先报告再继续。

审核重点是 ci.yml 固定名称、触发器和只读权限，check.sh 套件隔离和 all 顺序，TestCiSh 真实脚本行为及 contract.json 实际创建/加载/manifest 校验路径，以及变更是否局限于功能分支；不能用文本匹配替代行为验证。步骤 3 起的生产切换不随第一阶段一起授权；本节是操作授权，§4 的“合并即授权发布”是系统启用后的发布规则，二者不能混用。

## 10. 迁移顺序

1. **公开准备及额度核验。**初步核查当时版本及已有 Git 历史的凭据/业务数据，.gitignore 不清历史。公开前的最终扫描在步骤 7 完成。既有 gitleaks v8.30.1 扫描 310 个提交/141.75 MB 零发现和两幅 chinook 样例截图抽查保留为历史证据，不代替最终复核。维护者已决定全部既有 docs 随仓公开，doc 13/13.1 的 WSL 网关代理实值保留，不为此剔除文档/重写历史；当前版本可清理六个已跟踪 Zone.Identifier 并补忽略，真实凭据泄露才先轮换再处理历史。核实当时 Actions 用量及零付费阻断。本步只做核查和准备：清理及忽略规则等所有准备性代码/文档改动均放入步骤 2 的功能分支，步骤 3 停用并确认旧发布结束前，不得直接推送或合入 main，以免触发旧发布。
2. **功能分支验证 PR CI。**按 §9.1 明确授权后，实现共享检查及 ci.yml，纳入步骤 1 的清理改动及已裁定随首个 PR 提交的文档/审核产物，推送功能分支并开 PR 验证，尚不合并 main。新脚本提交后，取得生产演练明确授权，再按 §7.5 从该提交的新克隆完成一次只到候选清单的完整本地 ci.sh，记录结果及本次资源收尾，不能等步骤 9 才首次完整验证。Free 私有阶段的 PR CI 消耗包含额度，保护和 environment 公开后配置。contract.json 类型修复可在此阶段纳入并做本地回归，不混入自动维护框架；步骤 3 起的切换动作不包含在本阶段授权中。
3. **首次 main 更新前停旧入口并准备手动路径。**gh workflow disable local-release.yml，读回 disabled_manually，核对全部排队/等待/执行中的旧发布，等完成并核验现场。此要求也覆盖清理、忽略规则和文档等准备性提交，不能在停用前先将它们落入 main。不在同次 main 提交中删文件来代替前置停用。完成旧任务核验后，按 §8.4 以 iw-runner 身份准备独立运维目录、固定可信 current 的完整 source_sha，核对 HEAD/干净状态和 Python/Docker/端口检查环境；最小操作说明当场可用，无需等步骤 10 交付。准备不完整时不进入步骤 4；本步不通过真实回退来试探路径是否可用。
4. **合并并验证 main CI。**合入前按 §7.5 核对演练之后的实际差异（含 main 更新引入的内容）；若实质影响测试/构建链路，先另行取得追加演练授权，对最终功能分支提交重演通过，不能沿用旧结果直接合入；纯文档/审核记录或仅合并造成的 SHA 差异不机械重演。确认后合入已通过 PR 检查的共享检查、ci.yml 及准备性清理改动。此时旧入口停用、新通知尚未合入，只有云端检查，不构建部署镜像、不替换现有应用。期间有需要使用已核验的受控本机手动路径。
5. **准备私有仓库及隔离诊断入口。**准备最小 verify/release/finalize 和日志字段，但本步不 dispatch 或重跑正式 deploy.yml。先在本地做响应替身测试，再只通过 §3.2 的 verify-diagnostics.yml（mode=fixtures）运行托管 Runner 拒绝用例，结构上无 release；不以“尚无 Runner 接单”代替隔离。固定维护者 ID、公开 CI ID/path 和私有控制提交。正式 deploy.yml 的首次真实执行留到步骤 9；步骤 5、8 的用例均不得经其正向或负向 dispatch 发出。复用既有 C1—C6，不重跑整套实验。
6. **迁移 Runner。**按 §8.1 复核旧入口仍停用、无未决任务。注册到私有仓前，还须完整核对正式 deploy.yml 的全部非终态 run/job，包括 queued、waiting、pending、in_progress 的 release 和仍可能产生 release 的 verify；误触发且尚未开始的部署任务须处理并读回终态，有已开始或状态不明的任务先核实，不强杀。不能仅凭 Runner 空闲、暂未注册或一次 release 队列为空继续；旧执行均已明确结束且没有新正式 dispatch 后，才迁移账号/服务并验证运行环境。保留步骤 3 准备的 `/home/iw-runner/insightweave-ops/source/` 及所需提交，确认 iw-runner 权限、手动工具与环境仍可用，不随 `_work` 清理或注册迁移删除。沿用获准的管理身份取得 Runner 注册/移除所需的短期令牌，不为迁移提前生成跨仓部署 PAT（DEPLOY_TRIGGER_TOKEN）；该 PAT 留到步骤 8 即时生成并写入 Secret。不打印令牌或自动加权限，不同时运行两个 Runner 控制现场。
7. **最终扫描、确认旧关联已移除后公开。**公开前，以当时 main 的完整当前 HEAD 为扫描终点，重跑 gitleaks，覆盖该版本的可达提交历史及当前跟踪内容，包含步骤 2—4 新增提交；记录 HEAD、工具版本和脱敏扫描结果，不能用步骤 1 的初查或旧零发现报告替代。扫描后若 main 在公开前又更新，须按新 HEAD 重新核验；发现真实凭据时先轮换并处理，未完成核验不公开。确认没有本地 Runner 暴露给源码仓、公开准备完成，并经维护者授权后再公开。公开传播不可保证收回，沿用已确认公开范围；本次文档不执行扫描或可见性操作。
8. **配置单人保护及 environment。**要求 PR/ci-required、不要求他人批准、保护应用于维护者；先在已公开源码仓配置环境并只允许 main。核实私有额度、gh/jq、Runner 和身份控制，按 §3.2 手动填写三个来源参数，指向当前 main 对应合法 run 的当前最新 attempt（须 completed/success），仅通过 verify-diagnostics.yml（mode=source-readonly）在实际 GitHub 托管 Runner 使用拟上线的公开只读查询方式，低频验证 main、来源 run/attempt 和必需 jobs 的完整分页。不得用正式 deploy.yml 的正向 dispatch 或重跑代替；此时 Runner 已注册，正式 verify 通过就可能部署。记录时间、是否认证、响应状态及 §6.1 的必要限流响应头；不以开发机已登录 gh 的查询结果替代，诊断任务图不包含本地 release。若限流或无法完整取得证据，先排查或配置获准只读身份并复核，未完成核验不进入步骤 9；限流拒绝分支用响应替身验证，不通过大量真实请求触发。诊断通过、准备启用前，再由维护者按 §6.1 完成必要认证/MFA，生成仅限私有部署仓 Actions: write 的跨仓部署 PAT，并立即经受限输入写入公开 deployment-dispatch 环境的 DEPLOY_TRIGGER_TOKEN Secret，不提前在步骤 6 生成或在迁移步骤间暂存，也不打印令牌值。准备好切换 PR 及操作说明；本步骤不合入新通知，通知入口的合并统一留到步骤 9，避免提前触发真实部署。
9. **合并切换 PR 并首次真实部署。**维护者确认上线条件就绪后，合并同一个受控 PR：新增 notify-deploy.yml、删除旧 local-release.yml，并同步更新 deploy/README 和 doc 13/13.1 中的旧发布指引，特别是 README 配置生效及日志级别两处 Run workflow 说明。该合并提交自己的 main CI 成功后，新通知即会请求首次真实部署，不需要另点部署按钮。核验 main CI → 通知回执 → 私有核验 → 本地构建部署 → finalize；如出现等待，按 §8.2 记录单台 Runner 的实际 run/job 状态和可确认的等待原因，不要求专门制造排队竞争。从本次合并起到验证完成，暂停其他 main 合并/推送及无关发布重跑。此阶段预期真实变更容器，与步骤 4 明确区分；旧文件删除不替代步骤 3 的前置停用，Runner 迁走也不能代替删除，否则旧入口若被重新启用仍可能产生无人接单的排队任务。不要求先实现 reconfigure/capabilities/自动回退或维护程序。
10. **核对并交付操作说明。**核对步骤 9 已删除旧文件、操作文档不再指向旧入口，记录源码/镜像身份及验证范围，交付失败重试、人工回退/通知暂停说明，按已验收结果更新求职展示；不把旧入口说明的替换拖到切换完成后。后续确需修改仓库文档时，仍走正常 PR/main CI，并明确启用后的 main 合并也会触发自动发布。切换失败保持旧应用或受控手动恢复，不重新挂接公开 Runner、不 down -v、不删卷。

上述 GitHub 设置、Secret 写入、Runner 管理及日志查询优先 CLI/API；PAT 网页生成、认证及缺少 sudo/管理权限时维护者介入。所有示例须替换 OWNER；本次无远端写入、部署或服务变更。
## 11. 首期验收清单

实验、旧本地基线与生产验收分开。2026-11-23 复审方曾报告 nl2sql 355 passed、agent_core 1177 passed/36 skipped、deploy_tests 264 passed、metaweave 2716 passed/28 skipped/4 xfailed、前端 110 tests 及 typecheck 通过；这不是本轮重新运行的结果，也不代表干净云端安装/新控制链已通过。

| 场景 | 预期 |
| --- | --- |
| 单人 PR | 可自查、评论和合并；不因缺另一个 Approve 阻塞，CI 必须通过 |
| 开工及授权边界 | 本次仅改文档不等于开工；第一阶段功能分支修改、推送/开 PR 及生产一次完整 CI 演练均按 §9.1 明确授权，main/旧入口/Runner/权限设置/部署等动作分别确认，不扩大授权 |
| 功能分支、PR、fork PR | 只跑托管 CI，不取得触发 Secret、不调度本地 Runner |
| 仅文档合并 | 首期不加 paths-ignore，仍跑完整 main CI 并自动发布；不以只跳过 main 文档 CI 的方式破坏当前 HEAD 发布资格 |
| 跨仓库 CI 名称约定 | workflow name 为 InsightWeave CI；五个 suite 的 job ID/name 同名固定，汇总 ID 为 ci_required、name 为 ci-required；通知、保护和私有 jobs API name 精确匹配一致，改名/缺失/同名多条拒绝 |
| 他人提交、维护者合并 | 来源 CI 原始 actor 对应维护者，通过后可自动发布 |
| 非维护者 main push/来源重跑/私有触发或重跑 | 身份拒绝，不能仅靠维护者 PAT 通过 |
| Zone.Identifier、忽略规则和文档等准备性提交 | 只在功能分支准备；旧发布未停用或未确认结束前，不得合入/直接推送 main |
| 首次 main CI 合并 | 旧入口已停用且无未决发布，新通知未上线，容器及 current/previous 保持 |
| 步骤 5、8 的托管诊断载体 | 只运行 verify-diagnostics.yml 的对应 mode；全部 job 为托管 Runner，任务图无 release/self-hosted，不间接调用或 dispatch 部署，不使用写令牌；误放行只影响诊断结果，不产生本地任务 |
| 诊断来源输入 | fixtures 不要求来源参数；source-readonly 三项手填、运行时必填并按生产规则重验；仅接受当前 main 对应 run 的当前最新成功 attempt，不能借旧 main/旧成功 attempt 兜底 |
| 步骤 6 私有 Runner 注册前 | 正式 deploy.yml 全部非终态 run/job（含在途 verify）已核实并处理至终态，无新正式 dispatch；空闲 Runner 或一次 release 空列表不足以通过 |
| 步骤 7 公开前最终扫描 | gitleaks 扫描覆盖当时 main HEAD 的可达历史及当前跟踪内容，记录 SHA/版本/结果；步骤 1 初查不能替代，公开前 HEAD 再变化须重新核验 |
| 步骤 8 配置完成 | 通知仍未合入 main，尚不触发新链路部署；上线触发动作只在步骤 9 执行 |
| 部署 PAT 生成时点 | Runner 迁移仅使用注册/移除令牌，不提前生成跨仓部署 PAT；步骤 8 已具备公开受限 environment、诊断通过后才生成 PAT 并立即写入 Secret，无明文中转或输出 |
| 步骤 8 托管 Runner 公开只读查询 | 通过 verify-diagnostics.yml 的 source-readonly mode 使用拟上线身份方式低频读取 main、指定来源 attempt 及完整 jobs 分页，记录响应与必要限流头；不用正式 deploy.yml dispatch，本机已登录 gh 的结果不能替代，核验未完成不启用通知、不调度本地 release |
| 公开 API 返回限流响应 | 以响应替身验证 403/429 限流时不放行、不冒称来源过期或身份永久不合法；人工重试遵守等待提示，不主动打满额度、复用触发写令牌或自动提升权限 |
| 步骤 9 切换 PR | 同时新增通知、删除旧工作流并更新旧操作指引；该提交 main CI 成功即触发首次真实部署，验证完成前无其他 main 更新或无关重跑 |
| main CI 失败，或必需 suite 失败/取消/跳过/缺失 | 不取得发布资格；ci-required 不能靠 skipped/continue-on-error 变绿 |
| 正常自动发布 | 合并后无需另点部署，明确 SHA 经云端与本地检查并实际部署 |
| release 结果与自定义部署输出 | finalize 同时检查 needs.release.result 成功及 needs.release.outputs.outcome 为 deployed；发布 step 写入 GITHUB_OUTPUT 并经 job 映射，只有日志、零退出码或内置 step outcome 不足以判定部署完成 |
| 来源错误、参数注入、非 main 私有 ref | verify 在本地分配前拒绝 |
| verify 授权输出的具体值 | run_id/attempt 为本次私有上下文的规范十进制字符串，triggering_actor 为经稳定 ID 授权并与上下文核对一致的登录名；误填数字账号 ID 或其他执行绑定时不得放行，不能只做字段存在性检查 |
| 最新来源 attempt 失败/进行中 | 不使用旧成功 attempt，维护者排查后重跑/提交修复 |
| 同 SHA 成功套件由 GitHub 继承 | 按 §6.3 可接受；不声称该 attempt 全套重新执行 |
| 私有局部重跑继承旧 verify | job 绑定拒绝入组，finalize 不用旧输出变绿 |
| 人工选择重试入口 | 私有/通知/来源三个 run ID 与仓库分开，按故障位置择一完整重跑，不加 --failed/--job；受理未知或维护暂停未处理时不执行 |
| 未授权/绑定不符请求与 pending 发布交错 | 不替换合法等待者；沿用 C1—C3 证据，正式结构必须一致 |
| 合法请求入组及部署前过期复核 | 默认 single 可替换 pending，入组顺序不当作源码提交顺序；核验到入组的间隔及 A/B 时序见 §8.2，该时序尚未实测，不计入 C1—C6 PASS；本地替身验证已知过期请求停止，被取消/跳过由维护者核实后恢复 |
| 单台 self-hosted Runner 的等待状态 | 在既定端到端验收中记录实际 queued/pending 等状态及可确认的原因，不仅按状态词认定入组；不把托管实验的替换时序直接当作生产实证，无竞争窗口时如实说明，不新增专门实验 |
| 已核验来源在 verify 或本地复核确认过期 | 打印 DEPLOY_STALE_REQUEST 和脱敏原因，不部署；严格 finalize 策略下整次运行失败是预期，release skipped 不等于运行 skipped；API/身份故障不冒用该标记 |
| 本地手动命令与自动发布 | deploy.sh flock 继续约束，同锁不重复嵌套；运行中部署不被新合并强杀 |
| 回执完整/丢失/发送中断 | 明确 run ID 可追踪；未知不自动重发，不按最近一条认领 |
| 仓库名大小写与回执 URL | 启用前按 API full_name 原样配置；POST 已受理但大小写等配置导致回执校验失败时，先人工核实，不当作未受理重发 |
| 重复通知/成功后私有完整重跑 | 不承诺只部署一次；人工决定且日志明确，不能当健康检查 |
| 构建失败、预检失败 | 不进入替换；原服务保持；核验现场后再重试 |
| 部署失败、取消、中断或 finalize 未运行 | 依据现有状态和现场恢复，不凭颜色认定未部署，不自动回退或补账 |
| 最新 main 在部署前变化 | 未进入变更的旧请求停止；已经开始的 deploy.sh 走完收尾 |
| 本地成功 | source_sha、APP_GIT_SHA、候选、快照和实际镜像一致，日志绑定私有控制/部署版本 |
| contract.json 类型 | 本地探针已确认问题；实施后仅整数 1 通过，其他 8 类拒绝，合法哈希/镜像路径不退化 |
| suite 提取 | 五套件/all 共用定义、前端不装后端依赖、未知 selector 拒绝、Agent 失败阻止构建 |
| 首次上线前完整 ci.sh 演练 | 功能分支已提交 SHA 的独立干净新克隆，以获准 iw-runner 环境运行 check.sh all/三镜像构建/候选预检并生成清单，不调用 deploy.sh；MetaWeave 实际执行、不是仅收集，无开发机 .env 依赖 |
| 演练后又追加提交或更新 main | 合入前比较实际差异并记录依据；实质影响测试/构建链路则另行授权并对最终功能分支提交重演，纯文档/审核记录及仅合并 SHA 差异无需机械重演；正式 CI 仍核验实际合并提交 |
| 演练资源和收尾 | release_id 格式及六个精确资源名符合 §7.5，开始前不存在；结束后只删除本次容器/tag/独立克隆并读回确认不存在，查询失败不算清干净；uv/npm/Docker 构建共享缓存保留，不全局 prune、不误删 current/previous |
| 演练前后线上对照 | 同一 insightweave-local 项目容器的完整 ID/State.StartedAt 及 deployment.json phase/current 前后逐项一致；差异或读不到字段则先报告核实，不以此自动修复，也不由字段一致推断业务零影响 |
| Runner 切换及公开 | 旧入口不恢复，Runner 仅私有仓，服务/环境/权限实测 |
| GitHub Free/预算 | 用量启用时核验、额外付费被阻止；额度不足暂停发布，不绕过 verify |
| 手动维护/回退 | 先暂停/核实既有任务，共用锁，目标快照可信，保留数据卷；回退后不自动重放旧通知 |
| 手动工具准备及 Runner 迁移 | 步骤 3 即有独立运维检出和最小说明，步骤 6 保留；工具 HEAD 为操作开始时可信 current source_sha，目录干净，使用 iw-runner 和绝对脚本路径，不依赖 `_work` 或最近一次自动任务 checkout |
| 新 env 生效 | 实际重建并冒烟；同版本只读成功不算配置生效；配置专用入口延期明确 |

正式 YAML 做解析/actionlint 和 job 级条件/并发检查，私有核验用 API 替身测试，脚本改动运行现有部署测试及受影响套件；最后按 §10 完成一次受控真实链路。既有实验 actionlint 1.7.12 零发现、42 项结构检查及 C1—C6 PASS 不等于正式工作流已验收，不因文档变化重复长时间实验。

## 12. 延后能力与本轮变更范围

以下不作为首期交付：请求账本及永久成功去重、自动崩溃对账、dispatch_id/回执自动恢复、maintenance_control.py、自动补发/队列补偿、私有自动 rollback、独立 reconfigure/capabilities、旧应用配新 tooling_sha，以及多人可合并但单人独立批准部署的权限模型。

这些内容从原版首期要求中移出；旧实验及原始证据保留，技术事实不因方案精简而作废。若后续确需某项，再单独定义范围及验收，不把暂未实现能力写入求职材料。

首期完成标准：维护者功能分支 → PR 自查和 CI → 维护者合并 → main CI → 私有核验 → Debian 本地构建部署，异常有可执行的人工处理步骤。长期 dev、预发布环境、Kubernetes、云端镜像注册表、AI 审核订阅及公开部署状态回写均可后续另行增加。
