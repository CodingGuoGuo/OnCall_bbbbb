"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

# Windows 下 psycopg 异步模式要求 SelectorEventLoop（默认的 ProactorEventLoop 不支持）。
# 必须在事件循环创建之前设置；本项目无 asyncio 子进程需求（MCP 走 streamable-http），切换安全。
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os

from app.config import config
from loguru import logger
from app.api import chat, health, file, aiops, system, notify
from app.core.milvus_client import milvus_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")
    
    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    milvus_manager.connect()
    logger.info("✅ Milvus 连接成功")
    
    logger.info("=" * 60)
    
    yield
    
    # 关闭时执行
    logger.info("🔌 正在关闭 Milvus 连接...")
    milvus_manager.close()
    logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def no_store_frontend_assets(request, call_next):
    """前端资源禁用浏览器缓存

    index.html / styles.css / app.js 文件名固定不变，浏览器会用启发式缓存把它们
    存下来，导致「新 HTML + 旧 CSS/JS」的错乱渲染（页面改了但用户看到的还是旧样式，
    抽屉和遮罩会直接铺在正文里）。这里统一声明不缓存，前端每次读磁盘上的最新版本。
    只作用于页面与静态资源，/api/* 不受影响。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])
app.include_router(system.router, prefix="/api", tags=["系统配置"])
app.include_router(notify.router, prefix="/api", tags=["通知"])

# 挂载静态文件
static_dir = "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
        log_level="info"
    )
