#!/usr/bin/env bash
# =============================================================================
# Wyckoff Trading Agent - Docker 快速部署脚本
# =============================================================================
# 用法：
#   ./docker-deploy.sh                    # 交互式部署
#   ./docker-deploy.sh --mode tui         # TUI模式
#   ./docker-deploy.sh --mode mcp         # MCP Server模式
#   ./docker-deploy.sh --mode dashboard   # Dashboard模式
#   ./docker-deploy.sh --mode cron        # 定时任务模式
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
IMAGE_NAME="wyckoff-trading-agent"
IMAGE_TAG="latest"
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
            IMAGE_NAME="$2"
            shift 2
            ;;
        --tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --mode <mode>     运行模式: tui, mcp, dashboard, cron"
            echo "  --pull            仅拉取远程镜像"
            echo "  --image <name>    镜像名称 (默认: wyckoff-trading-agent)"
            echo "  --tag <tag>       镜像标签 (默认: latest)"
            echo "  -h, --help        显示帮助信息"
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
    info "拉取远程镜像..."
    docker pull ghcr.io/birdxs/wyckoff-trading-agent:latest || \
    docker pull wyckoff-trading-agent:latest || \
    err "拉取镜像失败"
    ok "镜像拉取完成"
    exit 0
fi

# 构建镜像
info "构建 Docker 镜像..."
if [ "$USE_COMPOSE" = true ]; then
    docker compose build
else
    docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" .
fi
ok "镜像构建完成"

# 选择运行模式
if [ -z "$MODE" ]; then
    echo ""
    echo "请选择运行模式:"
    echo "  1) TUI - 交互式终端 (需要 -it 参数)"
    echo "  2) Dashboard - 本地可视化面板 (端口 8765)"
    echo "  3) Cron - 定时任务 (后台运行)"
    echo "  4) MCP - MCP Server (stdio协议)"
    echo "  5) 退出"
    echo ""
    read -p "请输入选择 [1-5]: " choice
    
    case $choice in
        1) MODE="tui" ;;
        2) MODE="dashboard" ;;
        3) MODE="cron" ;;
        4) MODE="mcp" ;;
        5) exit 0 ;;
        *) err "无效选择" ;;
    esac
fi

# 停止现有容器
info "停止现有容器..."
docker stop wyckoff-tui wyckoff-dashboard wyckoff-funnel 2>/dev/null || true
docker rm wyckoff-tui wyckoff-dashboard wyckoff-funnel 2>/dev/null || true

# 根据模式运行
case $MODE in
    tui)
        info "启动 TUI 模式..."
        if [ "$USE_COMPOSE" = true ]; then
            docker compose run --rm -it wyckoff
        else
            docker run -it --rm \
                --name wyckoff-tui \
                --env-file .env \
                -v "$(pwd)/data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/logs:/home/wyckoff/.wyckoff/logs" \
                "${IMAGE_NAME}:${IMAGE_TAG}"
        fi
        ;;
    dashboard)
        info "启动 Dashboard 模式..."
        if [ "$USE_COMPOSE" = true ]; then
            docker compose --profile dashboard up -d
            ok "Dashboard 已启动，访问 http://localhost:8765"
        else
            docker run -d \
                --name wyckoff-dashboard \
                --restart unless-stopped \
                --env-file .env \
                -p 8765:8765 \
                -v "$(pwd)/data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/logs:/home/wyckoff/.wyckoff/logs" \
                "${IMAGE_NAME}:${IMAGE_TAG}" \
                dashboard
            ok "Dashboard 已启动，访问 http://localhost:8765"
        fi
        ;;
    cron)
        info "启动定时任务模式..."
        if [ "$USE_COMPOSE" = true ]; then
            docker compose --profile cron up -d
            ok "定时任务已启动"
        else
            # A股漏斗
            docker run -d \
                --name wyckoff-funnel-a-share \
                --restart unless-stopped \
                --env-file .env \
                -v "$(pwd)/data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/logs:/home/wyckoff/.wyckoff/logs" \
                "${IMAGE_NAME}:${IMAGE_TAG}" \
                -c "python -m scripts.daily_job"
            
            # 持仓诊断
            docker run -d \
                --name wyckoff-holding-diagnosis \
                --restart unless-stopped \
                --env-file .env \
                -v "$(pwd)/data:/home/wyckoff/.wyckoff/data" \
                -v "$(pwd)/logs:/home/wyckoff/.wyckoff/logs" \
                "${IMAGE_NAME}:${IMAGE_TAG}" \
                -c "python -m scripts.holding_diagnosis_job"
            
            ok "定时任务已启动"
        fi
        ;;
    mcp)
        info "启动 MCP Server 模式..."
        if [ "$USE_COMPOSE" = true ]; then
            docker compose --profile mcp up -d
            ok "MCP Server 已启动"
        else
            docker run -d \
                --name wyckoff-mcp \
                --restart unless-stopped \
                --env-file .env \
                -v "$(pwd)/data:/home/wyckoff/.wyckoff/data" \
                "${IMAGE_NAME}:${IMAGE_TAG}" \
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
