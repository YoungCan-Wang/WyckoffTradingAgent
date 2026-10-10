# 会员车道的 Agent Runtime 服务镜像。构建上下文是仓库根目录：
#   docker build -f packaging/agent-service/Dockerfile -t wyckoff-agent-service .
# Cloudflare Containers 只收 linux/amd64；在 Apple Silicon 上要显式加 --platform linux/amd64。
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

WORKDIR /app

# 依赖层单独缓存：只有 lock 变了才重装。
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agents ./agents
COPY cli ./cli
COPY core ./core
COPY integrations ./integrations
COPY tools ./tools
COPY utils ./utils
COPY workflows ./workflows
COPY llmdoc ./llmdoc
COPY config ./config
COPY data ./data

ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /home/app/.wyckoff \
    && chown -R app:app /home/app
USER app

# matplotlib 首次导入要建字体缓存（本机实测 15s），烤进镜像免得冷启动的第一轮吃掉。
RUN python -c "import matplotlib.pyplot"

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

# AGENT_SERVICE_TOKEN 必须由运行环境注入，没有它进程直接退出。
CMD ["python", "-m", "cli.service.server", "--host", "0.0.0.0", "--port", "8080"]
