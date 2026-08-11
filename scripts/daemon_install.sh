#!/usr/bin/env bash
set -euo pipefail

# 安装 Wyckoff 定时调度 daemon 为 launchd 用户级服务。
# 装完后关掉 UI，定时任务仍会跑。

LABEL="com.wyckoff.daemon"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/.wyckoff/logs"

info() { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m==>\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m==>\033[0m %s\n" "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || err "launchd 只在 macOS 可用。Linux 请用 systemd --user。"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHON="$REPO_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
[ -x "$PYTHON" ] || err "未找到 Python。先建 venv：uv venv && uv sync"

info "仓库:   $REPO_DIR"
info "Python: $PYTHON"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

# 已装则先卸，否则 bootstrap 会因重复 label 失败
if launchctl list "$LABEL" &>/dev/null; then
    info "卸载已有服务..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
fi

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>-m</string>
        <string>cli</string>
        <string>daemon</string>
        <string>--foreground</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>

    <key>RunAtLoad</key>
    <true/>

    <!-- 只在异常退出时重启：SIGTERM 干净退出后不该被拉起来 -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/daemon.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/daemon.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

info "已写入 $PLIST"

launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

sleep 2
if launchctl list "$LABEL" &>/dev/null; then
    ok "daemon 已启动"
    "$PYTHON" -m cli daemon --status || true
    echo
    echo "查看日志:  tail -f $LOG_DIR/daemon.log"
    echo "待批准项:  $PYTHON -m cli approve list"
    echo "卸载:      scripts/daemon_uninstall.sh"
else
    err "启动失败。查看 $LOG_DIR/daemon.err.log"
fi
