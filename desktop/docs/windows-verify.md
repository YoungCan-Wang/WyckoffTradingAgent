# Windows 验证手册

这个应用有一整套**只在 Windows 上执行**的代码：`taskkill` 回收进程树、
`.venv/Scripts/python.exe` 的解释器布局、`wyckoff-ipc.exe` 的二进制名。开发全在
macOS 上做，那些分支从来没真正跑过 —— 已经因此漏过一个会崩主进程的 bug
（三处 `spawn('taskkill')` 没挂 `error` 监听，见 `test/windowsReap.test.js`）。

## 三层验证，各自能保证什么

| 层 | 保证 | 保证不了 |
|---|---|---|
| `npm test`（本机） | 平台分支的静态形状对 | 那些分支真的跑得通 |
| Windows CI（`.github/workflows/desktop.yml`） | 在真 Windows 上**打得出包** | 窗口能不能出来（runner 无交互式桌面会话） |
| 本地 Windows 虚拟机 | 装上、点一遍、退出无残留 | 真实 x64 机器的性能（见下面的架构说明） |

## 架构：Apple Silicon 上只能测 arm64

M 系列 Mac 上的 Windows 虚拟机**一律是 Windows on ARM**。所以：

- 要测的是 **arm64 安装包**。`package.json` 的 `build.win.target` 已经配成
  `arch: ["x64", "arm64"]`，两个都出。
- x64 包在 ARM Windows 上能跑，但走 Prism 模拟 —— 性能和部分原生行为不代表真实
  x64 机器。**别用它下性能结论。**
- 真实 x64 的验证只能靠一台 Intel/AMD Windows 机器，或云上的 x64 Windows 实例。

## 建虚拟机（UTM，免费）

```bash
brew install --cask utm crystalfetch
```

1. 打开 **CrystalFetch** → 选 Windows 11 → ARM64 → 下载 ISO（走微软官方源）。
2. 打开 **UTM** → Create a New Virtual Machine → **Virtualize**（不是 Emulate，
   ARM 上虚拟化才有原生速度）→ Windows → 选刚下的 ISO。
3. 分配 4 核 / 8GB 内存 / 64GB 磁盘。装机约 1~2 小时（大部分时间在等 Windows）。
4. 装完在 Windows 里装 **UTM Guest Tools**（剪贴板与共享文件夹要靠它）。

## 拿安装包：在 macOS 上直接打就行

实测过：**Windows 安装包可以在 macOS 上打出来**，不需要进虚拟机构建。
electron-builder 会自己下载 wine 和 NSIS 工具链。

```bash
cd desktop
npm run build:ui

# extraResources 指向 ../dist/python/wyckoff-ipc，不存在会报错。
# 只验 Electron 侧的话放个占位文件就够（CI 就是这么做的）。
mkdir -p ../dist/python/wyckoff-ipc
echo placeholder > ../dist/python/wyckoff-ipc/wyckoff-ipc.exe

npx electron-builder --win --publish never
```

产出 `dist/Wyckoff Setup <版本>.exe`（约 307MB）。注意 **NSIS 把 x64 和 arm64 合进
同一个安装包**，安装时按机器架构自动选，所以不会有单独的 `*arm64*.exe`。想确认
arm64 真的打出来了，看 `dist/win-arm64-unpacked/Wyckoff.exe`：

```bash
file dist/win-arm64-unpacked/Wyckoff.exe
# PE32+ executable (GUI) Aarch64, for MS Windows
```

把那个 `.exe` 拖进虚拟机装上即可 —— 虚拟机只用来**运行**，不用来构建。这比在
Windows 里配 Node/Python/Git 快得多。

要连 Python 侧一起验，就得在 Windows 里跑一次 PyInstaller
（`scripts/build_python_ipc.sh` 是 bash 脚本，Windows 上在 Git Bash 里跑，
或写一份 PowerShell 版本）。占位文件只能验 Electron 的打包与启动结构。

## 手动冒烟清单

装完那个 `Wyckoff Setup <版本>.exe` 之后，按顺序点：

- [ ] **窗口出现**，标题「Wyckoff 读盘室」，不是白屏
- [ ] **左下角状态不卡在「连接中…」** —— 卡住说明 Python 桥没握手成功，
      去看 `%APPDATA%\Wyckoff\logs`（或从终端启动看 stderr）
- [ ] 七个页面都能进：对话 / 任务 / 审批 / 持仓 / 定时 / 跟踪 / 归因
- [ ] 打开菜单 → **K 线图**，输入 `600519`，图能画出来
- [ ] 打开菜单 → **浏览器**，导航一个网址，然后点 ✕ 能关掉
- [ ] 持仓页进编辑模式，改一次现金 → 能保存
- [ ] **退出应用后打开任务管理器，确认没有残留的 `wyckoff-ipc.exe`**

最后一条最关键 —— 那正是 `killTreeWindows()` 修的东西，而它在 macOS 上根本不执行。

## 已知的 Windows 专属风险点

排查时优先看这几处：

- `src/python-bridge.js` `killTreeWindows()` / `venvPython()` / `bundledBinary()`
- `src/daemon-runner.js` `stop()` 的 Windows 分支
- 路径分隔符：渲染层的静态断言测试在 CRLF 与 `\` 下可能表现不同
