# Agent Guidelines

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
