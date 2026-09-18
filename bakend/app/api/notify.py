"""通知推送的浏览器订阅端点

浏览器用 SSE 长连接订阅「刚推送到钉钉的那条消息」，在对话区以可折叠卡片展示。

这是一条**纯在线通道**：浏览器断开即注销订阅，不做任何持久化 ——
没有浏览器打开时，广播端自然是空操作。
"""

import asyncio
import json

from fastapi import APIRouter, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.utils import notify_stream

router = APIRouter()

# 空闲多久发一次心跳（秒）：防止中间层把长连接静默掐断
_HEARTBEAT_SECONDS = 20


def _sse(payload: dict) -> dict:
    """SSE 报文（与 chat.py / aiops.py 保持同一形态）"""
    return {"event": "message", "data": json.dumps(payload, ensure_ascii=False)}


@router.get("/notify/stream")
async def notify_stream_endpoint(request: Request):
    """订阅推送记录（SSE）

    事件类型：
    - `{"type": "ready"}`：建连确认
    - `{"type": "ping"}`：心跳（无消息时的保活）
    - `{"type": "dingtalk", "data": {title, text, alerts, pushed_at}}`：钉钉推送记录

    Returns:
        SSE 事件流
    """
    queue = notify_stream.subscribe()

    async def event_generator():
        try:
            yield _sse({"type": "ready"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield _sse({"type": "ping"})
                    continue
                yield _sse({"type": "dingtalk", "data": item})
        finally:
            # 断开（或生成器被关闭）时务必注销，避免订阅者集合泄漏
            notify_stream.unsubscribe(queue)
            logger.info("通知订阅连接已结束")

    return EventSourceResponse(event_generator())
