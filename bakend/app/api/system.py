"""系统配置展示接口

只读接口：把当前生效的配置暴露给前端「配置」面板。

安全约定（重要）：
1. 采用**白名单**逐字段取，绝不 `config.model_dump()` 全量返回 ——
   否则以后给 Settings 加了新的密钥字段会被自动泄露出去。
2. 所有密钥 / webhook 一律掩码，禁止返回原文。
"""

import platform
import re
import sys
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.services.vector_embedding_service import vector_embedding_service
from app.services.vector_store_manager import vector_store_manager

router = APIRouter()


def _mask_secret(secret: str) -> str:
    """密钥掩码：保留头 6 位 + 尾 4 位"""
    if not secret:
        return ""
    if len(secret) <= 12:
        return "****"
    return f"{secret[:6]}****{secret[-4:]}"


def _mask_url(url: str) -> str:
    """URL 掩码：保留协议/主机/路径，query 里的值只留前 4 位"""
    if not url:
        return ""

    def _repl(match: "re.Match[str]") -> str:
        # 分组 1 已经带了 ?/& 和 =，这里不要再补一个等号
        key, value = match.group(1), match.group(2)
        head = value[:4] if len(value) > 8 else ""
        return f"{key}{head}****"

    return re.sub(r"([?&][A-Za-z_]+=)([^&\s]+)", _repl, url)


def _channel(webhook_url: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """通知渠道：只汇报是否已配置 + 掩码后的地址"""
    data: Dict[str, Any] = {
        "configured": bool(webhook_url),
        "webhook_masked": _mask_url(webhook_url),
    }
    if extra:
        data.update(extra)
    return data


@router.get("/config")
async def get_config():
    """
    当前生效配置（只读、已脱敏）

    分五组返回：应用 / 向量库 / RAG / 模型 / 通知与集成，供前端分组卡片展示。
    """
    try:
        try:
            milvus_connected = milvus_manager.health_check()
        except Exception:  # noqa: BLE001
            milvus_connected = False

        data: Dict[str, Any] = {
            "app": {
                "name": config.app_name,
                "version": config.app_version,
                "debug": config.debug,
                "host": config.host,
                "port": config.port,
                "access_url": f"http://{config.host}:{config.port}",
                "app_url": config.app_url,
                "python": sys.version.split()[0],
                "platform": f"{platform.system()} {platform.release()}",
            },
            "vector_db": {
                "type": "Milvus",
                "host": config.milvus_host,
                "port": config.milvus_port,
                "timeout_ms": config.milvus_timeout,
                "knowledge_collection": vector_store_manager.collection_name,
                "primary_collection": milvus_manager.COLLECTION_NAME,
                "vector_dim": vector_embedding_service.dimensions,
                "connected": milvus_connected,
            },
            "rag": {
                "top_k": config.rag_top_k,
                "rewrite_count": config.rag_rewrite_count,
                "multi_query_enabled": config.rag_rewrite_count > 0,
                "retrieval_paths": 1 + max(config.rag_rewrite_count, 0),
                "chunk_max_size": config.chunk_max_size,
                "chunk_overlap": config.chunk_overlap,
                "dedup_key": "metadata['chunk_id']",
            },
            "model": {
                "provider": "阿里云 DashScope（OpenAI 兼容模式）",
                "chat_model": config.rag_model,
                "dashscope_model": config.dashscope_model,
                "embedding_model": config.dashscope_embedding_model,
                "vision_model": config.vision_model,
                "api_key_masked": _mask_secret(config.dashscope_api_key),
            },
            "notify": {
                "dingtalk": _channel(
                    config.dingtalk_webhook_url,
                    {"signed": bool(config.dingtalk_secret)},
                ),
                "feishu": _channel(config.feishu_webhook_url),
                "wecom": _channel(config.wecom_webhook_url),
                "webhook_dedup_window": config.webhook_dedup_window,
            },
            "integration": {
                "prometheus_base_url": config.prometheus_base_url,
                "prometheus_timeout": config.prometheus_request_timeout,
                "mcp_cls_url": config.mcp_cls_url,
                "mcp_cls_transport": config.mcp_cls_transport,
                "mcp_monitor_url": config.mcp_monitor_url,
                "mcp_monitor_transport": config.mcp_monitor_transport,
            },
        }

        return JSONResponse(
            status_code=200,
            content={"code": 200, "message": "success", "data": data},
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取配置失败: {e}")
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": f"获取配置失败: {e}", "data": None},
        )
