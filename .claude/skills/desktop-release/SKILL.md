---
name: desktop-release
description: 按需发布 Wyckoff Electron 桌面安装包；仅在用户明确要求发布桌面新版本时使用，不用于日常 PR 验证或 PyPI 发布
---

# Desktop Release

从 `main` 发布一次零付费桌面正式版。未指定版本策略时默认递增 patch；接受 `patch`、`minor`、`major` 或明确的 `X.Y.Z`。

## 发布边界

- 只发布桌面 GitHub Release，不发布 PyPI、不部署 Worker，也不修改策略参数和用户数据。
- 不购买或配置代码签名；Windows 保持未签名，macOS 保持 ad-hoc 签名。
- 不覆盖、移动或删除既有 tag/Release。任一前置条件不满足就停止并报告。
- 只有 `desktop-vX.Y.Z` tag 才构建三平台安装包；普通 push/PR 不应进入 package jobs。

## 执行流程

1. 在仓库根目录读取 `AGENTS.md`、`docs/DESKTOP_RELEASE.md`、`.github/workflows/desktop.yml`，并检查 `git status --short --branch`。
2. 要求工作树干净；运行 `git fetch origin`，切到 `main`，再执行 `git pull --ff-only origin main`。如果本地或远端不是快进关系，停止。
3. 读取 `desktop/package.json` 当前版本、最新 `desktop-v*` tag 和 GitHub Release。计算目标版本；若目标 tag 或 Release 已存在，停止。
4. 在 `desktop/` 运行 `npm version <目标版本> --no-git-tag-version`，只允许 `package.json` 与 `package-lock.json` 发生版本变更。
5. 运行 `npm ci`、`npm run typecheck`、`npm test`、`npm run build:ui`。任一失败就停止，不提交也不打 tag。
6. 提交 `chore(release): desktop vX.Y.Z` 并推送 `main`。记录提交 SHA；如果远端在推送前发生变化，停止并重新同步，不强推。
7. 等该 SHA 的主干 `CI` 成功。失败、取消或超时都停止；不要创建发布 tag。
8. 创建 annotated tag `desktop-vX.Y.Z`，指向刚验证的版本提交并推送。该 tag 会触发 `Desktop` workflow。
9. 等 `Desktop` workflow 终态成功，再验证 GitHub Release 非 draft、非 prerelease，并且恰好包含一个 Windows `.exe`、两个 macOS `.dmg` 和 `SHA256SUMS.txt`。
10. 返回版本、提交 SHA、Actions run、Release 下载页和资产清单。工作流失败时保留现场并报告，不自动重跑或删除 tag。
