#!/usr/bin/env bash
set -euo pipefail

# 卸载 Wyckoff 定时调度 daemon。日志和待批准队列保留。

LABEL="com.wyckoff.daemon"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

info() { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m==>\033[0m %s\n" "$*"; }

if launchctl list "$LABEL" &>/dev/null; then
    info "停止服务..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
else
    info "服务未在运行"
fi

if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    info "已删除 $PLIST"
fi

ok "已卸载。定时任务现在只在 UI 打开时运行。"
echo "日志保留在 ~/.wyckoff/logs/，待批准队列保留在 ~/.wyckoff/approvals.db"
