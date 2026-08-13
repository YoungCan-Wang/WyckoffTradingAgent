#!/usr/bin/env bash
# =============================================================================
# Wyckoff Trading Agent - Docker 快速部署脚本
# =============================================================================
# 用法：
#   ./docker-deploy.sh                    # 交互式部署
#   ./docker-deploy.sh --mode tui         # TUI 模式
#   ./docker-deploy.sh --mode mcp         # MCP Server 模式
#   ./docker-deploy.sh --mode dashboard   # Dashboard 模式
#   ./docker-deploy.sh --mode daemon      # 定时调度模式（推荐）
#   ./docker-deploy.sh --pull             # 拉取远程镜像
# =============================================================================

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 工具函数
info()  { printf "${BLUE}==>${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}==>${NC} %s\n" "$*"; }
err()   { printf "${RED}==>${NC} %s\n" "$*" >&2; exit 1; }

# 默认配置
DOCKER_IMAGE="${DOCKER_IMAGE:-birdxs/wyckoff-trading-agent:latest}"
MODE=""
PULL_ONLY=false

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode)
            MODE="$2"
            shift 2
            ;;
        --pull)
            PULL_ONLY=true
            shift
            ;;
        --image)
            DOCKER_IMAGE="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --mode <mode>     运行模式: tui, mcp, dashboard, daemon"
            echo "  --pull            仅拉取远程镜像"
            echo "  --image <name>    镜像名称 (默认: birdxs/wyckoff-trading-agent:latest)"
            echo "  -h, --help        显示帮助信息"
            echo ""
            echo "环境变量:"
            echo "  DOCKER_IMAGE      镜像源 (默认: birdxs/wyckoff-trading-agent:latest)"
            exit 0
            ;;
        *)
            err "未知选项: $1"
            ;;
    esac
done

# 检查 Docker
if ! command -v docker &>/dev/null; then
    err "未找到 Docker。请先安装 Docker:
  Ubuntu/Debian: sudo apt install docker.io
  CentOS/RHEL:   sudo yum install docker
  macOS:         brew install --cask docker
  Windows:       https://docs.docker.com/desktop/install/windows-install/"
fi

if ! docker info &>/dev/null; then
    err "Docker 守护进程未运行。请启动 Docker:
  sudo systemctl start docker
  或者重启系统"
fi

# 检查 Docker Compose
if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
    warn "未找到 Docker Compose，将使用 docker run 命令"
    USE_COMPOSE=false
else
    USE_COMPOSE=true
fi

# 检查 .env 文件
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        warn "未找到 .env 文件，从 .env.example 创建..."
        cp .env.example .env
        ok "已创建 .env 文件，请编辑配置:"
        echo "  nano .env"
        echo ""
    else
        warn "未找到 .env 文件和 .env.example"
    fi
fi

# 拉取镜像
if [ "$PULL_ONLY" = true ]; then
    info "拉取远程镜像: ${DOCKER_IMAGE}"
    docker pull "${DOCKER_IMAGE}" || err "拉取镜像失败"
    ok "镜像拉取完成"
    exit 0
fi

# 选择运行模式
if [ -z "$MODE" ]; then
    echo ""
    echo "请选择运行模式:"
    echo "  1) daemon    - 定时调度（推荐，后台常驻）"
    echo "  2) dashboard - 可视化面板 (端口 8365)"
    echo "  3) tui       - 交互式终端 (需要 -it 参数)"
    echo "  4) mcp       - MCP Server (stdio 协议)"
    echo "  5) 退出"
    echo ""
    read -p "请输入选择 [1-5]: " choice
    
    case $choice in
        1) MODE="daemon" ;;
        2) MODE="dashboard" ;;
        3) MODE="tui" ;;
        4) MODE="mcp" ;;
        5) exit 0 ;;
        *) err "无效选择" ;;
    esac
fi

# 停止现有容器
info "停止现有容器..."
docker stop wyckoff-daemon wyckoff-dashboard wyckoff-tui wyckoff-mcp 2>/dev/null || true
docker rm wyckoff-daemon wyckoff-dashboard wyckoff-tui wyckoff-mcp 2>/dev/null || true

# 根据模式运行
case $MODE in
    daemon)
        info "启动定时调度模式 (daemon)..."
        if [ "$USE_COMPOSE" = true ]; then
            # 设置环境变量
            export DOCKER_IMAGE
            docker compose up -d daemon
            ok "定时调度已启动"
            echo ""
            echo "配置定时任务："
            echo "  nano ./schedules.json"
            echo ""
            echo "查看日志："
            echo "  docker logs -f wyckoff-daemon"
        else
            docker run -d \
                --name wyckoff-daemon \
                --restart unless-stopped \
                --env-file .env \
                -e TZ=Asia/Shanghai \
                -v "$(pwd)/wyckoff_data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/wyckoff_logs:/home/wyckoff/.wyckoff/logs" \
                -v "$(pwd)/.env:/home/wyckoff/.wyckoff/.env:ro" \
                -v "$(pwd)/schedules.json:/home/wyckoff/.wyckoff/schedules.json" \
                "${DOCKER_IMAGE}" \
                daemon --foreground
            ok "定时调度已启动"
        fi
        ;;
    dashboard)
        info "启动 Dashboard 模式..."
        if [ "$USE_COMPOSE" = true ]; then
            export DOCKER_IMAGE
            docker compose --profile dashboard up -d
            ok "Dashboard 已启动，访问 http://localhost:8365"
        else
            docker run -d \
                --name wyckoff-dashboard \
                --restart unless-stopped \
                --env-file .env \
                -e TZ=Asia/Shanghai \
                -p 8365:8765 \
                -v "$(pwd)/wyckoff_data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/wyckoff_logs:/home/wyckoff/.wyckoff/logs" \
                -v "$(pwd)/.env:/home/wyckoff/.wyckoff/.env:ro" \
                "${DOCKER_IMAGE}" \
                dashboard
            ok "Dashboard 已启动，访问 http://localhost:8365"
        fi
        ;;
    tui)
        info "启动 TUI 模式..."
        if [ "$USE_COMPOSE" = true ]; then
            export DOCKER_IMAGE
            # 启动 TUI 容器（保持运行）
            docker compose up -d wyckoff
            ok "TUI 容器已启动，进入方式："
            echo "  docker exec -it wyckoff-tui bash"
            echo ""
            echo "进入容器后运行 TUI："
            echo "  python -m cli"
        else
            docker run -it --rm \
                --name wyckoff-tui \
                --env-file .env \
                -e TZ=Asia/Shanghai \
                -v "$(pwd)/wyckoff_data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/wyckoff_logs:/home/wyckoff/.wyckoff/logs" \
                -v "$(pwd)/.env:/home/wyckoff/.wyckoff/.env:ro" \
                "${DOCKER_IMAGE}"
        fi
        ;;
    mcp)
        info "启动 MCP Server 模式..."
        if [ "$USE_COMPOSE" = true ]; then
            export DOCKER_IMAGE
            docker compose --profile mcp up -d
            ok "MCP Server 已启动"
        else
            docker run -d \
                --name wyckoff-mcp \
                --restart unless-stopped \
                --env-file .env \
                -e TZ=Asia/Shanghai \
                -v "$(pwd)/wyckoff_data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/wyckoff_logs:/home/wyckoff/.wyckoff/logs" \
                -v "$(pwd)/.env:/home/wyckoff/.wyckoff/.env:ro" \
                "${DOCKER_IMAGE}" \
                mcp
            ok "MCP Server 已启动"
        fi
        ;;
    *)
        err "未知模式: $MODE"
        ;;
esac

# 显示状态
echo ""
info "容器状态:"
docker ps -a --filter "name=wyckoff" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
ok "部署完成!"
echo ""
echo "常用命令:"
echo "  查看日志:    docker logs -f wyckoff-*"
echo "  停止服务:    docker stop wyckoff-*"
echo "  重启服务:    docker restart wyckoff-*"
echo "  清理容器:    docker rm wyckoff-*"
echo "  查看状态:    docker ps -a | grep wyckoff"
echo ""
echo "进入 TUI:     docker exec -it wyckoff-tui bash"
echo "  (进入后运行 python -m cli)"
