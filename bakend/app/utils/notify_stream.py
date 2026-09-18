"""推送给浏览器的通知广播（纯内存，不落库）

用途：钉钉推送成功的那一刻，把「刚发出去的那条消息」同时推给所有在线的浏览器，
让它在对话区以可折叠卡片的形式出现。

设计取舍：
- **纯内存**：浏览器不在线时没有任何订阅者，广播自然是空操作，也不写文件/数据库。
- **非阻塞**：每个订阅者一个上限 20 的队列，用 put_nowait；慢客户端只丢消息，
  绝不拖住诊断与推送主流程。
- **单进程**：当前部署是 `uvicorn app.main:app` 单进程，广播可达全部浏览器；
  若将来改为 `--workers N`，只有同进程的订阅者能收到（已知限制）。
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from loguru import logger

# 每个订阅者（一个浏览器页签）一个专属队列
_subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = set()

# 单连接积压上限：慢客户端只丢消息，不占内存
_MAX_QUEUE = 20


def subscribe() -> "asyncio.Queue[Dict[str, Any]]":
    """注册一个订阅者，返回它的专属队列（连接断开时必须 unsubscribe）"""
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=_MAX_QUEUE)
    _subscribers.add(queue)
    logger.info(f"通知订阅已建立，当前在线订阅数: {len(_subscribers)}")
    return queue


def unsubscribe(queue: "asyncio.Queue[Dict[str, Any]]") -> None:
    """注销订阅者（幂等，重复调用无副作用）"""
    _subscribers.discard(queue)
    logger.info(f"通知订阅已断开，当前在线订阅数: {len(_subscribers)}")


def has_subscribers() -> bool:
    """当前是否有浏览器在线

    预留给「没有任何浏览器在线时就跳过推送」这类策略；本轮未使用
    （钉钉推送本身不受浏览器在线与否影响，否则没人看页面时告警就没人收到了）。
    """
    return bool(_subscribers)


async def broadcast_dingtalk(
    title: str,
    text: str,
    alert_names: Optional[List[str]] = None,
) -> int:
    """把一条「已推送到钉钉」的记录广播给所有在线浏览器，返回成功入队的订阅者数

    Args:
        title: 推送标题（与发给钉钉的一致）
        text: 推送给钉钉的 markdown 正文（与发给钉钉的逐字一致）
        alert_names: 涉及的告警名，供卡片展示

    Returns:
        int: 成功入队的订阅者数；无人在线时为 0

    注意：任何异常都在内部吞掉并记日志 —— 通知展示失败绝不能影响推送主流程。
    """
    if not _subscribers:
        logger.info("没有在线浏览器，钉钉推送记录不展示")
        return 0

    payload: Dict[str, Any] = {
        "title": title,
        "text": text,
        "alerts": list(alert_names or []),
        "pushed_at": datetime.now().strftime("%H:%M:%S"),
    }

    delivered = 0
    for queue in list(_subscribers):
        try:
            queue.put_nowait(payload)
            delivered += 1
        except asyncio.QueueFull:
            logger.warning("通知队列已满，丢弃这条推送记录（浏览器处理不过来）")
        except Exception as e:  # noqa: BLE001 — 广播不能影响推送主流程
            logger.warning(f"通知广播失败（已忽略）: {e}")

    logger.info(f"钉钉推送记录已推送给 {delivered} 个在线浏览器")
    return delivered
