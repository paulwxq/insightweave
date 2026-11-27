# InsightWeave 提交与发布执行清单（给接手的 AI）

> 适用：上下文被刷新或重启后接手本项目的 AI。覆盖会话开始的核对、功能分支开发、提交、PR、合并后的自动部署跟踪、验收和异常分流。
> 设计依据是 [doc 15](../docs/15_公开项目与私有部署仓库联动及CI_CD改造方案.md)，实测结果见[上线验收记录](../docs/reviews/doc15_release_pipeline_acceptance.md)。操作细节以[部署手册](README.md)和两个仓库里的当前代码为准；本清单与代码不一致时，以代码和手册为准，并报告差异。
> 给人类维护者的简版是 `docs/102_开发与提交备忘录.md`。

**读法**

- `<尖括号>` 是占位值，每次现场查询后替换。文中的 SHA、release_id、运行编号都是示例，不是事实，不能当作当前状态。
- 标有"现场确认"的描述（分支保护的设置、某个 Secret 有没有配置、私有仓有哪些分支、某个文件在不在）是编写时的状态，每次以现场查询为准。
- 代码块标了性质：`[只读]` 只查询，不改动仓库和线上；`[同步]` 更新本地的远端引用（`git fetch`），不改工作区也不改远端；`[变更]` 会改动仓库或线上，执行前先核对第 0 节的授权边界。
- 两个仓库：公开源码仓 `paulwxq/insightweave`（下称公开仓）和私有控制仓 `paulwxq/insightweave-deploy`（下称私有仓）。本机约定目录分别是 `/opt/projects/insightweave` 和 `/opt/projects/insightweave-deploy`，先按第 1 节确认自己在哪里。

## 0. 授权边界与硬性禁令

**需要维护者明确授权的动作**（范围以维护者的原话为准）：

- 推送功能分支、创建 PR。
- 合并到公开仓 main。**合并会触发一次真实部署，只改文档也一样。**
- 重跑任何运行（通知、来源 CI、私有部署），停用或启用通知工作流。
- 手动回退、手动发布、修改运行配置、停止或重启任何服务、操作 Docker 资源。
- 改动私有仓、Secret、分支保护、Runner。

**规则**

- 某个动作是否已被授权，按维护者原话的范围、期限和条件判断，并以维护者之后的变更为准。此前仍然有效的授权可以覆盖后面的步骤，不因为已经执行过一次命令就重新索要。只做文档、只做评审之类的授权，不扩张成提交、推送、合并或部署的授权。
- 本流程默认不读取、不打印凭据和运行配置的内容（见第 9 节），只读取需要的字段。维护者另有明确的配置维护任务时，以有效授权限定的必要操作为准，同样不泄露凭据。
- 不丢弃别人的改动和暂存，不"顺手"提交别人的文件；不用 `git reset --hard`、`git clean`、`git stash` 清场。
- 不擅自执行 `docker compose up`、`bootstrap`、`prune`，不擅自重启引擎来"修复"现场。
- 不擅自创建或修改 Secret；维护者明确授权的配置任务，只在授权范围内操作。任何时候都不让维护者在聊天里粘贴令牌。
- 不要在每次发布时重复提醒维护者查看 Actions 用量。

## 1. 会话开始：核对现场

不假定工作区干净。先弄清楚自己在哪个仓库、哪个分支，工作区里有什么，是谁的。

```bash
# [只读]，其中 git fetch 是 [同步]。公开源码仓
cd /opt/projects/insightweave
pwd; git rev-parse --show-toplevel; git branch --show-current
git status --short                               # 已修改（M）和未跟踪（??）的文件
git diff --cached --name-only                    # 开始时索引里已经暂存的内容
git log -3 --oneline
git config user.name; git config user.email      # 提交身份
git remote get-url origin | sed -E 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)##; s#\.git$##'   # 应输出 paulwxq/insightweave
git fetch origin                                 # [同步] 只更新本地的远端引用
git rev-parse --short=12 HEAD origin/main
gh pr list --state open
gh run list --limit 10                           # 只是线索。未结束的运行按第 4 节的方法确认
gh api repos/paulwxq/insightweave/actions/workflows --jq '.workflows[] | "\(.path) \(.state)"'   # notify-deploy.yml 平时应为 active（现场确认）
gh api repos/paulwxq/insightweave/branches/main/protection \
  --jq '{checks: .required_status_checks.contexts, pull_request: .required_pull_request_reviews != null, approvals: .required_pull_request_reviews.required_approving_review_count, admins: .enforce_admins.enabled}'
```

```bash
# [只读]，其中 git fetch 是 [同步]。私有控制仓
cd /opt/projects/insightweave-deploy
git status --short; git diff --cached --name-only; git branch --show-current
git remote get-url origin | sed -E 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)##; s#\.git$##'   # 应输出 paulwxq/insightweave-deploy
git fetch origin                                 # [同步]
git rev-parse --short=12 HEAD origin/main
# 注意：执行到这里，终端停在私有仓的目录里。第 2 节的命令在公开仓里执行，见每个代码块的第一行
```

逐项判断：

- **目录**：上面 `sed` 那一行把 origin 地址里的 `https://github.com/`、`git@github.com:` 或 `ssh://git@github.com/` 去掉，再去掉末尾的 `.git`，应输出 `paulwxq/insightweave`（公开仓）或 `paulwxq/insightweave-deploy`（私有仓）。HTTPS 和 SSH 两种写法都可以。结果不对就停下报告，不要猜。
- **分支**：公开仓停在 main 或某个功能分支上。编写时私有仓只有 main（现场确认）。
- **工作区里已有的改动**：逐个确认归属。不属于自己这次任务的文件原样保留，提交时不暂存。`docs/102_开发与提交备忘录.md` 是维护者的人类备忘录，维护者已决定把它保留在仓库里：不要重写。
- **暂存区**：开始时索引里如果已经有人暂存了内容（`git diff --cached` 不为空，且不是自己这次任务的），保留并报告，和对方协调。不要 `git reset`、`git stash`，也不要把它们一起提交。
- **Git 身份**：应是维护者的身份（示例：`paulwxq`）。为空或不对时报告，不自行换成别的身份。
- **未结束的运行**：列表里有时，先分清是哪一类。本次 PR 的必需检查还没结束，或 main CI、通知、私有部署、现场维护有未决的，合并或重跑之前要等它结束并核实。无关功能分支的 PR CI、只读诊断不影响。确认方法见第 4 节。
- **分支保护**：编写时公开仓 main 只能通过 PR 合并，必需检查 `ci-required` 成功才能合并，对管理员同样生效。以现场查询为准。

**本机访问 GitHub 要带代理（现场确认）。** 普通 shell 里没有配置代理时，直连 GitHub 不通（2026-11-28 实测 `git ls-remote` 60 秒超时）。代理由维护者配置在 Runner 的 `.env` 里（`INSIGHTWEAVE_WSL_PROXY_URL`，以及同值的 `https_proxy`）。只读出这一个字段，按单条命令带上，不改全局配置：`git -c http.proxy=<代理地址> fetch origin`、`git -c http.proxy=<代理地址> push …`、`env HTTPS_PROXY=<代理地址> gh …`。不关闭 TLS 校验。Runner 自己的检出用的是它进程环境里的同一个代理，两条路径要分别探测：以 `iw-runner` 身份在临时仓库里 `git -c protocol.version=2 fetch --no-tags --depth=1 <公开仓地址> <SHA>`，再核对 `FETCH_HEAD`。

## 2. 一次改动：功能分支 → 提交 → PR

本节的命令都在**公开源码仓**里执行。第 1 节的最后一步停在私有仓的目录里，所以本节凡是操作本地仓库的代码块，第一行都是 `cd /opt/projects/insightweave`，单独复制其中一个代码块也不会跑错仓库。进入之后先确认 origin 是公开仓。PR 相关的 `gh` 命令每条都带了 `--repo paulwxq/insightweave`，只有传给它们的文件路径和当前目录有关。

公开仓不直接推送 main：main 受分支保护，只能通过 PR 合并。直接提交 main 只是私有仓的做法，不要带到公开仓来。

本节标了 `[变更]` 的代码块都是逐条执行：任何一条失败就停下报告，不清场，也不继续执行后面的命令。

### 2.1 建分支

把一批相关的修改放在同一个功能分支上。之后的修改继续提交到这个分支、推送到同一个 PR，不是每编辑一次就建新分支。相关文档默认和代码一起带上。

```bash
# [变更] 公开源码仓。进入后先确认身份：下面第二行应输出 paulwxq/insightweave；不是就停下报告，不执行后面的命令
cd /opt/projects/insightweave
git remote get-url origin | sed -E 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)##; s#\.git$##'
# 更新本地 main，然后建分支。git pull 和 git switch 都会改动本地分支与工作区。
# 三步用 && 连起来，前一步成功才执行下一步；任何一步失败都会停下，这时报告，不清场
git switch main && git pull --ff-only origin main && git switch -c <类型>/<简短名称>    # feat/…、fix/…、docs/…、chore/…
```

切换和快进都成功时，工作区里别人的未提交改动会原样留在工作区，仍然是未提交状态。这是正常的，不要动它们。如果因为别人的改动而切换或快进失败，命令已经停下了：报告，等对方处理，不要清场。

**从临时检出发布。** 工作区里有别人未提交的改动、不方便切换或快进时，用 `git worktree add -b <类型>/<简短名称> <仓库之外的目录> origin/main` 建一个干净的临时检出，在里面只带入本次任务的文件：整份属于本次改动的文件可以直接拷入并逐段核对差异；混有无关改动的文件只取相关的段落，再用 diff 证明"发布版 = 工作区版 − 无关段"。提交、推送、开 PR 都在这个检出里做。合并后仍要按 2.6 把日常工作区同步到合并提交，最后删除临时检出（不带 `--force`，不干净就不删）。

**不小心提交到了本地 main**（没有推送）：提交不会丢。做法以本地 main 为起点，分三步：先核对事实，再把这几笔提交放到功能分支上，最后让本地 main 回到远端的位置。

```bash
# [同步]/[只读] 第一步：核对事实。公开源码仓
cd /opt/projects/insightweave
git fetch origin
git branch --show-current                          # 现在在哪个分支
git log --oneline origin/main..main                # 本地 main 比远端多出的提交，就是要保留的范围
git merge-base --is-ancestor origin/main main && echo "没有分叉"
```

多出的提交正是要保留的那几笔，并且打印了"没有分叉"，才做第二步。本地 main 与远端分叉，或者看不懂多出的提交是什么时，不要动 main，停下报告。

```bash
# [变更] 公开源码仓。第二步（从本地 main 建分支，带走这几笔提交）和第三步（本地 main 指回远端）
# 用 && 连起来，前一步成功才执行下一步。因别人的未完成改动而无法切换时会停下：
# 这时报告，不清场，也不执行后面的命令
cd /opt/projects/insightweave
git switch main && git switch -c <类型>/<简短名称> && git branch -f main origin/main
# [只读] 核对
git log --oneline origin/main..main                # 应为空
git log --oneline origin/main..<类型>/<简短名称>     # 应列出保留的提交
```

不要用 `git reset --hard`。

### 2.2 提交前的相关检查

按改动范围选择，不是每次全跑。

```bash
# [只读] 公开源码仓
cd /opt/projects/insightweave
git status --short
git diff                                         # 逐段看自己的改动
git diff --check                                 # 空白错误
```

- **改了工作流**（`.github/workflows/*.yml`）：本机有 actionlint 时，对改动的文件运行 `actionlint <文件>`。
- **改了部署脚本或测试**：用 nl2sql 的虚拟环境跑直接相关的测试，例如 `(cd nl2sql && .venv/bin/python -m pytest ../deploy/tests/<测试文件> -q)`。外层的括号让命令在子 shell 里执行，结束后终端仍在仓库根目录。
- **改了文档**：核对链接目标，以及文中的命令、路径与当前代码一致。
- **完整检查由 PR 的 CI 运行**。`deploy/scripts/check.sh` 要求工作区完全干净，未跟踪文件也算，还需要 `INSIGHTWEAVE_CI_PYTHON`（Python 3.12 的绝对路径）和 uv，没有跳过开关。主工作区有别人未提交的文件时它会被拒绝；不要为了运行它去清理别人的文件。

### 2.3 精确暂存、提交、推送

需要授权。

```bash
# [变更] 公开源码仓。推送和提交需要授权
cd /opt/projects/insightweave
git add -- <路径1> <路径2>                        # 只写自己这次任务的明确路径
git status --short                               # [只读] 别人的文件应仍是未暂存或未跟踪
git diff --cached --stat                         # [只读]
git diff --cached --check                        # [只读] 暂存内容的空白错误，新文件也查得到
git diff --cached                                # [只读] 完整的暂存差异，提交前逐段看完
git commit -m "<类型>(<范围>): <说明>"
git push -u origin <类型>/<简短名称>
```

只暂存自己的路径，并不能把别人已经暂存的内容挡在提交之外：`git commit` 会提交索引里的全部内容。所以提交前要看完整的 `git diff --cached`，确认里面只有本次任务授权的内容。发现别人的暂存项时停下，保留并协调；不要提交，也不要 `git reset` 或 `git stash`。

推送功能分支不触发部署。按编写时的分支保护设置，直接推送 main 会被拒绝（现场确认）。

### 2.4 创建和更新 PR

创建 PR 需要授权。

```bash
# [变更] 公开源码仓。创建 PR 需要授权；--body-file 的路径相对当前目录
cd /opt/projects/insightweave
gh pr create --repo paulwxq/insightweave --base main --head <类型>/<简短名称> \
  --title "<标题>" --body-file <PR说明文件>
```

PR 说明写清四件事：改了什么、为什么；跑了哪些检查、结果如何；是否影响部署、配置或数据；没有验证的部分。

```bash
# [只读]
gh pr view <PR编号> --repo paulwxq/insightweave
gh pr diff <PR编号> --repo paulwxq/insightweave
gh pr checks <PR编号> --repo paulwxq/insightweave --watch --required     # 只看必需检查，等到结束
gh pr view <PR编号> --repo paulwxq/insightweave --json state,mergeStateStatus,headRefOid
```

`ci-required` 在五个套件结束后才出现。它出现之前，`gh pr checks --watch --required` 会以 `no required checks reported` 退出（退出码 1），这不表示检查失败：改用 `gh run watch <PR CI 运行ID> --repo paulwxq/insightweave --exit-status` 等整个运行结束，再看 `ci-required` 的结论。

PR CI 失败时，在原分支修复，提交后推送同一个分支，等待新的检查，不另建 PR。`mergeStateStatus` 为 `CLEAN` 表示分支保护的要求已经满足。

### 2.5 合并

需要维护者对"合并并部署"的明确授权。合并前逐项确认：

1. 授权覆盖这次合并——来自本次指示，或此前仍然有效的指示。有就不重复问，没有就先问。
2. PR 的必需检查全部通过，`gh pr diff` 看过的内容就是要上线的。
3. 没有未决的相关任务：main CI、通知、私有部署和现场维护（核对方法见第 4 节），第 4 节的必要条件成立。无关功能分支的 PR CI 和只读诊断不影响合并。
4. 从合并到部署验证完成期间，没有其他人合并 main。

```bash
# [变更]
gh pr merge <PR编号> --repo paulwxq/insightweave --merge --match-head-commit <PR头提交的完整SHA>
```

用 `--merge`（合并提交），不用 `--squash`、`--rebase`、`--auto`、`--admin`。`--match-head-commit` 保证合并的是检查过的那个提交。

### 2.6 合并后同步本地

```bash
# [变更] 公开源码仓，只动本地。无法快进时停下报告
cd /opt/projects/insightweave
git switch main && git pull --ff-only origin main
```

已合并的分支，等确认内容都在 main 里之后再删（`git push origin --delete <分支>`、`git branch -d <分支>`）。这是可选的整理。

**发布完成的标准包括日常工作区与合并提交一致。** 只让临时检出干净，或者 PR、私有运行是绿色的，都不算完成。日常工作区（本机是 `/opt/projects/insightweave`）没有同步时，已经发布的改动会在那里继续显示为未提交，越积越多：2026-11-28 PR #8 从临时检出发布后，那个工作区里一度显示 30 项。

无法快进的常见原因是工作区里有未提交的改动，而其中一部分其实已经随发布进了远端（内容和远端相同）。这时不要清场，按下面的顺序做；做法已在临时仓库里验证过：

1. 留可恢复记录：把 `git diff --cached --binary`、`git diff --binary`、`git status --porcelain=v1 --untracked-files=all`、分支头（`git for-each-ref`）和未跟踪文件的副本存到仓库之外。不复制含凭据的文件。
2. 逐个路径核对 `git diff --name-status --no-renames HEAD origin/main`：
   - 内容已经与远端相同的，`git add` 它，让索引持有目标内容。
   - 内容不同、并且确定以远端为准的，先另存工作副本，再换成远端内容后 `git add`。
   - 远端新增、工作区没有的，`git checkout origin/main -- <路径>`。
   - 远端已删除、本地还要保留的物理文件，先确认它已被忽略规则覆盖，并且删除已经暂存（`git rm --cached` 过）。
   - 未跟踪的实验产物移到仓库之外归档，不删除，也不用忽略规则去遮盖真正没提交的代码。
3. 合并前断言 `git write-tree` 的结果等于 `git rev-parse origin/main^{tree}`，再执行 `git merge --ff-only origin main`。索引已经是目标内容的条目，git 快进时不会改写这些文件。
4. 以 `git status --porcelain=v1 --untracked-files=all` 输出为空、本地 main 等于 `origin/main` 作为完成标准。

## 3. 两个仓库的边界与术语

| | 公开源码仓 `paulwxq/insightweave` | 私有控制仓 `paulwxq/insightweave-deploy` |
| --- | --- | --- |
| 内容 | 业务源码、测试、文档、公开 CI、通知工作流 | 部署任务图、核验程序、本地发布入口 |
| main | 受保护，只能通过 PR 合并 | 直接提交，没有 PR 和保护 |
| 什么时候有运行 | PR 触发 PR CI；推送到 main 触发 main CI；main CI 成功后触发通知 | 只有手动触发的工作流；推送不触发任何运行 |
| 部署哪份源码 | 由公开仓 main 的完整提交决定 | 不保存业务源码 |

表中是编写时的约定和现状（现场确认）。

- 两个仓库不互相合并，不在两边各写一份业务源码。
- **来源提交**（`source_sha`）是公开仓 main 上的提交；**控制提交**是私有仓 main 上的提交。两者分开记录，不混用。
- **PR CI** 检查的是 PR 的合并预备引用，**main CI** 检查 main 上的真实提交。部署只用后者。
- **release_id** 的格式是 `<来源提交前12位>-d<私有运行ID>-<attempt>`，示例 `b750cef7afa4-d45330068597-1`。
- 合并只改文档的 PR 也会运行 main CI 并触发部署，没有按路径跳过。

## 4. 发布前的环境核对

下面的命令以 `iw-runner` 身份读取（本机通过 `sudo -u iw-runner`）。没有这个权限就报告，不绕过。只读需要的字段，不要输出 `deploy.env`、`config/*.env` 或 Runner 的 `.env`。

```bash
# [只读]
PUB=paulwxq/insightweave; PRIV=paulwxq/insightweave-deploy
D=/home/iw-runner/insightweave-runtime

# 自动发布入口和 Runner
gh api repos/$PUB/actions/workflows --jq '.workflows[] | "\(.path) \(.state)"'        # notify-deploy.yml 应为 active（现场确认）
gh api repos/$PRIV/actions/runners --jq '.runners[] | "\(.name) \(.status) busy=\(.busy)"'

# 未结束的运行：按状态筛选，并由 --paginate 翻完所有页。
# 任何一次查询失败都会打印"查询失败"；出现它时结果不可信，要重查，不能当作没有
unfinished() {
  local s
  for s in requested queued in_progress waiting pending action_required; do
    gh api "repos/$1/actions/runs?status=$s&per_page=100" --paginate \
      --jq '.workflow_runs[] | "\(.status)\t\(.name)\t\(.head_branch)\t\(.id)"' || echo "查询失败：$1 status=$s"
  done
}
unfinished $PUB
unfinished $PRIV

# 本机：引擎、部署状态、容器、维护标记、站点
sudo -u iw-runner docker --context default version --format '{{.Server.Version}}'
sudo -u iw-runner python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("phase"), d.get("current"), d.get("previous"))' $D/state/deployment.json
sudo -u iw-runner docker --context default ps -a --filter label=com.docker.compose.project=insightweave-local --format '{{.Names}}\t{{.Status}}'
sudo -u iw-runner ls -A $D/control                    # 维护标记，应为空
curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/   # 期望 200。--noproxy 不能省：shell 里设了代理时，不加会得到 502 的误报
```

**未结束的运行怎么看。** 在"最近 N 条"里筛不出未完成的运行，不能证明没有：长时间排队的旧运行可能排在列表之外。所以用上面的函数按状态查。有输出时逐条分清：

- 属于相关任务，合并、重跑、维护之前要等它结束并核实：main 上的 `InsightWeave CI`、`Notify private deployment`，私有仓的 `Deploy verified source`，以及本机 `phase` 不是 `ready` 或有维护标记。
- 不属于阻塞项：别的功能分支上的 PR CI，`Verify diagnostics` 这类只读诊断。

没有输出，也没有"查询失败"，才说明查询时没有未结束的运行。合并或重跑真正执行之前，再把相关任务的状态核实一遍。

| 类别 | 条件 |
| --- | --- |
| **必要**。正常 release 的这些检查在排空和替换容器之前拒绝；Runner 离线时分不到执行机，会长时间排队。检查和构建期间仍可能产生候选镜像、临时容器、快照或缓存，所以不据此保证本机资源没有变化，失败后以现场为准 | Docker Desktop 的 Linux 引擎就绪，`iw-runner` 能通过既定的 Docker context 访问它；本机 Runner 在线；`phase` 为 `ready`，没有维护标记，当前版本的可信快照可用；`postgres`、`neo4j`、`milvus`、`attu` 四个基础服务和 `metaweave`、`nl2sql` 两个后端在运行并通过预检 |
| **推荐** | 七个项目容器都健康运行，站点返回 200，没有未结束的运行 |

- 镜像是构建产物，没有"启动"一说。`frontend` 没在运行不是普通新版本发布的硬性前提，发布会按目标版本保留或启动它。
- **发布会读取开发工作区的配置（doc 16 起）。** Runner 的 `.env` 提供 `INSIGHTWEAVE_DEV_PROJECT_ROOT` 和 `INSIGHTWEAVE_ENV_SYNC_PYTHON`；发布时读取开发工作区的 `metaweave/.env`、`nl2sql/.env`（生成两个后端的正式 env）和 `frontend/.env.docker.local`（两个 API 地址作为构建参数进入前端镜像）。合并前只核对这些文件对 `iw-runner` 可读：`sudo -u iw-runner test -r <文件>`。需要确认某个取值时只读那一个字段。读不到时发布会在构建之前停止，不替换容器。详见部署手册「修改配置」。
- 其他项目的停止容器不需要启动。
- 只启动 Docker Desktop 不会让发布脚本恢复整套已停止的服务。容器没有自己起来时先报告，由维护者决定怎么恢复；自动启动设置不是必须开启的功能。
- 必要条件不满足时不要自行 `compose up`、`bootstrap`、`prune` 或重启引擎。报告，等维护者决定。

完整说明见[部署手册](README.md)的"日常自动发布前置条件"一节。

## 5. 跟踪一次部署

合并之后按顺序做，每一步用上一步得到的编号。不要用"最近一条运行"猜关联。

```bash
# [只读]
PUB=paulwxq/insightweave; PRIV=paulwxq/insightweave-deploy
M=<合并提交的完整SHA>

# ① main CI：用固定的工作流，查这个合并提交在 main 上的 push 运行
gh api "repos/$PUB/actions/workflows/ci.yml/runs?branch=main&event=push&head_sha=$M&per_page=10" \
  --jq '.workflow_runs[] | "\(.id) \(.status)/\(.conclusion) event=\(.event) branch=\(.head_branch) head=\(.head_sha) attempt=\(.run_attempt)"'
gh run view <mainCI运行ID> --repo $PUB --json status,conclusion,jobs
git -C /opt/projects/insightweave ls-remote origin refs/heads/main    # $M 仍是 main 的 HEAD
#    核对：event=push、branch=main、head 等于 $M、conclusion=success，
#    run_attempt 是这个运行的最新 attempt，六个 job（含 ci-required）都成功。

# ② 通知：下面的列表只用来找候选，逐个确认之后才算对应上
gh api "repos/$PUB/actions/workflows/notify-deploy.yml/runs?event=workflow_run&per_page=100" --paginate \
  --jq '.workflow_runs[] | "\(.id) attempt=\(.run_attempt) \(.status)/\(.conclusion) \(.created_at)"'
gh run view <候选的通知运行ID> --repo $PUB --attempt <attempt号> --log \
  | grep -E 'dispatch_phase":"|gh: |HTTP [0-9]{3}|##\[error\]'
#    "dispatch_phase":"intent"    这个 attempt 发出的三个来源参数
#    "dispatch_phase":"accepted"  私有仓已受理，其中的 workflow_run_id 就是私有运行 ID
#    gh: …、HTTP 4xx/5xx、##[error] 只在出错时出现，处理见第 6 节

# ③ 私有部署：verify → release → finalize
gh run view <私有运行ID> --repo $PRIV
gh api repos/$PRIV/actions/runs/<私有运行ID> --jq '{status, conclusion, run_attempt, head_sha}'
gh api repos/$PRIV/actions/runs/<私有运行ID>/jobs --jq '.jobs[] | "\(.name): \(.conclusion // .status)  runner=\(.runner_name)"'
gh run view <私有运行ID> --repo $PRIV --attempt <attempt号> --log
```

**怎样把通知和这次部署对应上。** 上面的列表只用来找候选。对每个候选，按它的 attempt 号看日志，确认 intent 行里的三个来源参数与 ① 逐项相等：`source_sha` 是合并提交，`source_run_id` 是 main CI 运行，`source_run_attempt` 是它的最新 attempt。三项都一致，才取同一个 attempt 的 accepted 行里的 `workflow_run_id`。

候选里找不到时，扩大范围再查（加时间条件，例如 `…/runs?event=workflow_run&created=>=<ISO时间>&per_page=100`，或继续用 `--paginate`）。不能因为列表里没有，就断定通知没有发出。

通知日志的开头会回显一遍脚本原文，其中的文字不是实际输出。上面那条 `grep` 只保留 intent、accepted 和真正的出错行。

等待某个运行结束，可以用 `gh run watch <运行ID> --repo <仓库> --exit-status --interval 30`。

怎么读结果：

- 通知成功只表示**已受理**，不表示服务已更新。
- **部署成功**要同时成立：私有运行的 `finalize` 成功（它要求本次运行已授权，且 release 的输出 `outcome` 为 `deployed`）；release 日志里的来源提交、来源 run/attempt 与预期一致；第 5.1 节的现场核对通过。
- GitHub 上的红或绿本身都不能证明容器有没有被动过。失败或中途中断时，要看现场。

### 5.1 现场核对

```bash
# [只读]
D=/home/iw-runner/insightweave-runtime
sudo -u iw-runner python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("phase"), d.get("current"), d.get("previous"))' $D/state/deployment.json
# 期望：ready  <release_id>  <上一个版本>。其中 <release_id> = <来源提交前12位>-d<私有运行ID>-<attempt>

sudo -u iw-runner docker --context default ps --filter label=com.docker.compose.project=insightweave-local --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
# 期望：frontend、metaweave、nl2sql 的镜像标签是 insightweave/<应用>:<release_id>，状态 healthy

sudo -u iw-runner python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("source_sha"))' $D/releases/<release_id>/manifest.json
# 期望：等于合并提交

curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/   # 期望 200
```

doc 16 起 `deployment.json` 还可能有 `pending_config_release_id`：非空表示更新正式配置时被打断，不算就绪，处理见部署手册「追踪、重试与暂停」。

改过前端构建配置（`frontend/.env.docker.local`）时，另核对两个地址确实进入了镜像。构建参数的实际取值看私有运行 release 日志里前端构建的那一步，BuildKit 会把 `RUN` 行里的变量展开成实际值；再从入口取 `index.html` 引用的 JS，核对其中的地址和构建信息里的提交前 7 位。

一次普通发布前后，四个基础服务的容器不应该被重建。

## 6. 异常分流

先弄清是哪一环出的问题，再决定要不要重跑、重跑哪一个。遇到任何一种失败，先做三件事（都是只读）：确认旧的运行已经结束，并且没有未决的相关任务（按第 4 节的方法查，不看最近几条）；按第 4 节查看现场；查公开仓 main 当前的 HEAD 和它最新一次 attempt 的状态。

| 现象 | 在哪里看 | 含义 | 处理 |
| --- | --- | --- | --- |
| PR CI 失败 | `gh pr checks`、失败 job 的日志 | PR 的代码没有通过检查，不会产生部署 | 在原功能分支修复并推送，等新的检查。不合并 |
| main CI 失败 | 合并提交在 main 上的运行 | 没有发布资格，通知不会发出部署请求 | 新建修复分支和 PR，不直接推送 main。修复适合在原提交上重跑时，才重跑来源 CI |
| 通知失败，或回执缺失、无效 | 通知运行的日志；私有仓的运行；现场 | "发送出错"不等于未受理：它只说明 POST 命令返回了错误，而网络错误可能发生在 GitHub 已经受理之后。回执缺失或不符合预期时，POST 也可能已经被受理。只有日志里有 GitHub 明确拒绝请求的记录，才是未受理 | 一律先按"受理结果不明"处理，按 6.1 核实。有明确的拒绝证据、确认没有创建运行、来源仍有效，才重跑通知。无法确认就报告，不重发 |
| 来源过期 | 私有 verify 日志里的 `DEPLOY_STALE_REQUEST` | 核验时公开 main 或来源 attempt 已经更新 | 不部署旧请求，这是预期结果。以最新的 main 为准重新走流程 |
| 核验拒绝 | 私有 verify 日志里的 `VERIFY_REJECTED` | 参数、身份或证据不符 | 不重跑。把日志交给维护者，查明原因 |
| 接口限流 | 私有 verify 日志里的 `VERIFY_UNAVAILABLE` 和 `API rate limit` | 不带身份的公开查询达到了限额（托管 Runner 的出口 IP 与别人共用） | 等日志给出的重置时间过去（`date -d @<x-ratelimit-reset的值>`），核对现场和来源后，完整重跑这个私有运行一次。没有自动等待，也没有自动重试；再次被限流就停下报告 |
| 网络或其他无法确认的原因 | `VERIFY_UNAVAILABLE`，但不是限流 | 查询失败。不是过期，也不是身份不合法 | 查明原因后完整重跑私有运行 |
| release 失败、取消或中断 | 私有运行的 release job；本机的 `deployment.json` 和容器 | 可能停在部署中途。现场可能已经变了，也可能没动 | 先看现场的 `phase`、维护标记、容器和站点。`ready` 且来源仍有效时可以重跑；`deploying` 或 `recovery_required` 时按部署手册的恢复程序处理，需要维护者授权 |
| finalize 失败 | finalize job 的断言 | 本次运行没有被授权，或 release 没有输出 `deployed` | 不代表一定没有部署。按上一行核对现场，不自动补账 |
| release 中断后 `pending_config_release_id` 非空 | `deployment.json` | 更新正式配置时被打断（doc 16） | 需要维护者授权：核实后执行 `deploy.sh rollback <current>`，它先恢复配置备份，再恢复当前版本。不手工删除被引用的 `.pending/` 目录 |
| 源码检出、依赖下载或来源复核的网络故障：`early EOF`、下载超时、TLS 错误，或不是限流的 `VERIFY_UNAVAILABLE` | 该次 attempt 日志里的第一处失败 | Runner 访问 GitHub、Docker 构建环境或 GitHub API 的线路不稳定 | 按部署手册「网络故障排查与重跑」分别探测三条路径。确认旧运行已结束、现场 `ready`，与维护者协调后完整重跑私有部署一次；不连续盲重跑 |

### 6.1 核实通知是否已被受理

通知运行失败时，先确认私有仓里有没有已经被创建的运行。只有确认没有，才谈得上重跑通知。

```bash
# [只读]
PUB=paulwxq/insightweave; PRIV=paulwxq/insightweave-deploy
gh run view <通知运行ID> --repo $PUB --attempt <attempt号> --log \
  | grep -E 'dispatch_phase":"|gh: |HTTP [0-9]{3}|##\[error\]'          # 有没有 GitHub 明确拒绝请求的记录
gh api "repos/$PRIV/actions/workflows/deploy.yml/runs?event=workflow_dispatch&created=>=<通知这个attempt的开始时间>&per_page=100" --paginate \
  --jq '.workflow_runs[] | "\(.id) \(.status)/\(.conclusion) \(.created_at)"'    # 通知之后创建的私有部署运行
gh run view <候选的私有运行ID> --repo $PRIV --log | grep -E 'SOURCE_SHA:|SOURCE_RUN_ID:|SOURCE_RUN_ATTEMPT:'    # 它收到的三个来源参数
```

- 私有仓里有一个在通知之后创建的运行，而且它收到的三个来源参数与这次通知的 intent 一致：已经受理。转为跟踪那个运行（第 5 节），不重发。
- 私有仓里没有对应的运行，通知日志里又有 GitHub 明确拒绝请求的记录：未受理。原因排除、来源仍有效之后，才可以重跑通知（第 7 节）。
- 两种证据对不上，或者现场显示部署正在进行：不要重发，报告并等维护者决定。

没有看到 accepted 行，不能当作未受理的证据；日志里的"发送出错"也不能。

## 7. 重跑

三条命令都是完整重跑，不加 `--failed` 或 `--job`。每次只选**一个**环节，不能连着全部重跑。重跑部署会再次构建，可能再次部署同一份源码，不是健康检查。

| 重跑哪一个 | 什么时候用 | 命令 |
| --- | --- | --- |
| 私有部署 | verify 因限流或网络问题停止；或 release 失败、取消后现场已经回到 `ready`。前提是来源仍是公开仓 main 最新且成功的 attempt | `gh run rerun <私有运行ID> --repo paulwxq/insightweave-deploy` |
| 通知：未受理后的重试 | 有 GitHub 明确拒绝请求的证据，并确认私有仓里没有创建运行（第 6.1 节）；原因已排除，来源仍有效 | `gh run rerun <通知运行ID> --repo paulwxq/insightweave` |
| 通知：控制代码修复后的新请求 | 另一种用途，需要维护者的授权。前提全部成立：上一次请求的受理已经确认；它对应的私有运行已经结束；现场是 `ready`，没有未决的任务；来源仍是公开仓 main 最新且成功的 attempt；私有仓的控制修复已经推送 | 同一条命令。它会生成一个绑定新控制提交的全新私有请求 |
| 来源 CI | main 的 CI 需要完整重新验证，且修复适合在原提交上重跑；或恢复自动发布后需要让当前的 main 重新发布 | `gh run rerun <来源CI运行ID> --repo paulwxq/insightweave` |

重跑通知有两个用途，不要混淆；它们和"受理结果不明"时的规则也不是一回事：

- **受理结果不明**：规则是防止重复（第 6.1 节）。查不清就不重发。
- **用途一，未受理后的重试**：有明确的拒绝证据，并确认没有创建运行，才重试通知。
- **用途二，控制代码修复后的新请求**：旧的私有运行固定在旧的控制提交上，重跑它不会读到新代码。上表"控制代码修复后的新请求"一行的前提全部成立后，由维护者授权重跑通知运行，让它发出一个绑定新控制提交的全新请求。这不是给结果不明的请求补发：旧请求已经确认受理，并且执行完毕。

## 8. 维护暂停、回退与恢复

这些操作都会改动线上，都需要维护者的明确授权。具体步骤以既有手册为准，这里只写顺序和切换条件，不复制脚本。

| 要做的事 | 顺序与切换条件 | 依据 |
| --- | --- | --- |
| 暂停自动发布 | 停用通知工作流，读回 `disabled_manually` → 按第 4 节的方法查公开仓的通知和私有仓的部署里有没有未结束的运行，排队中的也算 → 已经发出的请求要等它正常收尾，停用不会取消正在执行的部署 → 然后才能开始维护 | [部署手册](README.md)的"追踪、重试与暂停"；doc 15 §8.5 |
| 手动回退 | 先暂停自动发布 → 按运维说明，把回退工具检出固定到**操作开始时** current 的来源提交并核对 → 用那份工具里的 `deploy.sh rollback` 回退，目标只能是 current 或 previous → 核对现场 | [部署手册](README.md)的"手动发布/回退"；doc 15 §8.4；本机 `/home/iw-runner/insightweave-ops/README.md` |
| 恢复自动发布 | 确认维护已经结束、现场是 `ready`、main 没有变，按第 4 节的方法确认没有未结束的运行，也没有未决的发送 → 启用通知，读回 `active` → 需要发布时，重跑 main 的来源 CI，或合并修复 PR | doc 15 §8.5 |
| 修改运行配置 | doc 16 起，运行目录里的 `config/*.env` 由每次发布从开发工作区的 `.env` 和 `config/overrides/` 生成，不要手工编辑。改了开发 `.env`、覆盖文件或 `frontend/.env.docker.local` 之后，后端容器或前端镜像要重新构建才生效：经授权按手册里只改配置时的发布重跑私有部署，或者随下一次代码发布。只改配置也会重新构建，不升级代码 | [部署手册](README.md)的"修改配置" |

- 回退之后默认保持通知停用，由维护者决定何时恢复。暂停期间完成的 CI 不保证补发。
- 现场是 `deploying`、`recovery_required` 或状态不明时，先核实再处理。不删除数据卷，不强行放开维护状态。
- 七个容器全部停止时，不要用 `rollback` 来恢复，它不启动基础服务。
- 不新增调度、账本或自动补发机制。

## 9. 凭据

| 凭据 | 在哪里 | 用途 | 你需要做的 |
| --- | --- | --- | --- |
| 本机的 gh 登录 | 维护者账号在 gh 里的登录 | 执行 `gh` 命令 | 直接用，不需要每次发布手工提供 |
| `DEPLOY_TRIGGER_TOKEN` | 公开仓环境 `deployment-dispatch` 的 Secret | 通知工作流用它请求私有仓部署 | 本流程不读取，不更换，不索要 |
| `GITHUB_TOKEN` | 每个 job 自动生成 | 私有 verify 读取本次运行的记录；检出代码 | 由工作流自动提供，无需配置 |
| `SOURCE_READ_TOKEN` | 私有仓的可选 Secret | 公开查询被限流时使用的只读身份 | 编写本清单时没有配置（现场确认），限流靠事后重跑处理。要配置须由维护者决定，不自行创建或修改 |
| 开发工作区的 `metaweave/.env`、`nl2sql/.env`、`frontend/.env.docker.local` | 开发工作区（被 Git 和构建上下文忽略） | 每次发布在 Runner 上读取：前两份生成后端的正式 env，后一份提供前端构建参数 | 本流程不读取、不打印内容，只核对这些文件对 `iw-runner` 可读。需要确认某个取值时只读那一个字段，不改这些文件 |

本流程默认不打开 `deploy.env`、`config/*.env` 或 Runner 的 `.env`，不运行会输出令牌的命令。维护者另有明确的配置维护任务时，按其授权限定的必要操作执行，同样不泄露内容。任何时候都不让维护者在聊天里粘贴令牌。

## 10. 完成时报告

写清五项：改了哪些文件；跑了哪些检查、结果如何；当前在哪个分支；是否已经提交、推送、合并；没有验证的边界。

合并或重跑之后，再附上合并提交、main CI 运行、通知运行、私有运行、release_id，以及现场核对的结论。从临时检出发布时，再附上日常工作区同步后的 `git status --porcelain=v1 --untracked-files=all` 结果，以及移到仓库之外归档的东西的清单和位置。

## 相关实现

- 公开仓：[部署手册](README.md)、[ci.yml](../.github/workflows/ci.yml)、[notify-deploy.yml](../.github/workflows/notify-deploy.yml)、`deploy/scripts/check.sh`。
- 私有仓 `paulwxq/insightweave-deploy`（需要维护者权限）：`README.md`、`.github/workflows/deploy.yml`、`.github/workflows/verify-diagnostics.yml`、`scripts/verify_source_request.py`、`scripts/release_local.sh`。
