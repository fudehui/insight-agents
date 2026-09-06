# 后端镜像：FastAPI + DeepAgents 运行时
# 依赖按 uv.lock 锁文件安装，与本地开发完全一致

# 官方 uv 镜像只用来取 uv 二进制
FROM ghcr.io/astral-sh/uv:0.9 AS uv

FROM python:3.12-slim

# PYTHONUTF8：容器内中文文件名与 UTF-8 文件读写不乱码
ENV PYTHONUTF8=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# 先只拷贝依赖清单：源码变更不触发依赖重装，充分利用层缓存
COPY pyproject.toml uv.lock ./
COPY --from=uv /uv /uvx /usr/local/bin/
RUN uv sync --frozen --no-dev

COPY app ./app

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# SQLite 单文件库 + 进程内会话状态，保持单实例，不要用 --workers 扩展
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
