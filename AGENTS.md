# Agent Guidelines

## 提交、PR、发布与恢复：先读清单

- 动手做以下任何一件事之前，先完整阅读 [deploy/AGENT_RELEASE_CHECKLIST.md](deploy/AGENT_RELEASE_CHECKLIST.md)：提交或推送、创建或合并 PR、跟踪或重跑部署、暂停自动发布、回退或恢复。清单以当前实现为准，占位值每次现场查询后替换。
- 合并到 main 会触发一次真实部署，只改文档也一样。合并、重跑、回退之前先核对维护者的授权范围：按原话的范围、期限和条件判断，此前仍然有效的授权可以覆盖后面的步骤，不重复索要；写文档、做评审之类的授权不包含这些动作。
- 不假定工作区干净。别人的未提交、未跟踪或已暂存的内容原样保留，不丢弃，不替他们提交。
- 本流程默认不读取或打印凭据和运行配置的内容，不擅自创建或更换 Secret。维护者另有明确的配置维护任务时，按其授权限定的必要操作执行，同样不泄露内容。任何时候都不让维护者在聊天里粘贴令牌。

## 自动发布网络故障

- 自动发布遇到 GitHub 源码检出、Docker 依赖下载或 GitHub API 复核故障时，先读部署手册的[网络故障排查与重跑](deploy/README.md#网络故障排查与重跑)。
- 按手册核对失败步骤、线上状态和重跑条件。重跑发布或临时调整 Runner 前，核对维护者的授权范围。

## Browser automation

- For any browser task (opening pages, clicking, filling forms, screenshots,
  web-app testing / QA), prefer **ego-browser** (`ego-browser` CLI — an
  AI-oriented Chromium browser) over Orca's built-in browser tab or other web
  tools.
- Before any browser task, run `ego-browser skill` and follow its printed
  guide; it matches the installed ego lite version.
- In WSL the `ego-browser` command is provided by a symlink at
  `~/.local/bin/ego-browser` pointing to the Windows install
  (`C:\Users\Paul_\.local\share\ego\active_version_dir\ego-browser.exe`).
  If the command is missing, recreate that symlink.
- Fall back to Orca's built-in browser (`orca-ide tab ... / snapshot ...`)
  only when ego-browser is unavailable, or when the result must appear as a
  tab inside Orca's own UI.
