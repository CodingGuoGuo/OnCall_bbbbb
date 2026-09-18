# ============================================================================
# SmartP 后端镜像（uvicorn 主应用 + 两个 MCP 服务，同容器）
#   - 前端已拆到 frontend/，由 nginx 提供，这里只跑接口
#   - 单进程运行：会话历史与通知广播都是进程内内存态，不能用多 worker
# ============================================================================
FROM python:3.13-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 运行期系统依赖：
#   curl   —— 容器内自检用
#   tzdata —— 时区
#   libpq5 —— psycopg（langgraph-checkpoint-postgres 的依赖）运行期需要
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl libpq5 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# ---- 依赖层（利用镜像层缓存：只要 pyproject.toml 没改，改代码不会重装依赖）----
# setuptools 的 packages.find 需要 app 包目录存在，先放一个 __init__.py 占位
COPY pyproject.toml README.md ./
COPY app/__init__.py ./app/
RUN pip install -e .

# ---- 业务代码 ----
COPY . .

# logs / uploads 必须先存在：loguru 的文件落盘不会自动创建父目录，缺了会在启动时直接报错
# static 是【空目录占位】：app/main.py 里的 StaticFiles(directory="static")
# 在目录不存在时会在 import 阶段抛 RuntimeError（Directory 'static' does not exist），
# 容器直接起不来。前端已由 nginx 提供，这个 mount 不会被访问，只需要目录存在。
RUN mkdir -p /app/logs /app/uploads /app/static

EXPOSE 9900

# 🔴 两个 MCP 服务在代码里写死了 127.0.0.1:8003/8004，必须和主应用同容器：
#    先把它们放到后台，等 3 秒再 exec 启动 uvicorn（exec 让 uvicorn 成为 1 号进程，
#    这样才能正确接收 docker stop 的信号）
CMD ["sh", "-c", "python mcp_servers/cls_server.py > logs/mcp_cls.log 2>&1 & python mcp_servers/monitor_server.py > logs/mcp_monitor.log 2>&1 & sleep 3 && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 9900"]