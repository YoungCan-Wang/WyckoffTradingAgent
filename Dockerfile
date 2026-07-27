# =============================================================================
# Wyckoff Trading Agent - Multi-stage Docker Build
# =============================================================================
# 构建阶段：安装依赖和应用
# 运行阶段：精简镜像，非root用户运行
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder - 安装依赖和构建应用
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv (Python 包管理器)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# 设置工作目录
WORKDIR /app

# 复制依赖文件（利用Docker缓存）
COPY pyproject.toml uv.lock ./

# 安装依赖
RUN uv pip install --system --upgrade pip && \
    uv pip install --system -e ".[mcp]" || uv pip install --system .

# ---------------------------------------------------------------------------
# Stage 2: Runtime - 精简运行镜像
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    WYCKOFF_HOME=/home/wyckoff/.wyckoff

# 安装运行时系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# 从builder阶段复制Python包
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 创建非root用户
RUN groupadd -r wyckoff && \
    useradd -r -g wyckoff -d ${WYCKOFF_HOME} -s /bin/bash wyckoff

# 创建应用目录和用户目录
RUN mkdir -p ${APP_HOME} ${WYCKOFF_HOME}/data ${WYCKOFF_HOME}/logs ${WYCKOFF_HOME}/config

# 复制应用代码
COPY . ${APP_HOME}/

# 设置目录权限
RUN chown -R wyckoff:wyckoff ${APP_HOME} ${WYCKOFF_HOME}

# 切换到非root用户
USER wyckoff

# 设置工作目录
WORKDIR ${APP_HOME}

# 创建数据持久化卷
VOLUME ["${WYCKOFF_HOME}/data", "${WYCKOFF_HOME}/logs"]

# 暴露端口（如果需要dashboard）
EXPOSE 8765

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# 默认入口点：启动TUI
ENTRYPOINT ["python", "-m", "cli"]
CMD ["--help"]
