"""
诊断结果通知推送工具

支持飞书、钉钉、企业微信
"""

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import List

import httpx
from loguru import logger

from app.config import config
from app.models.aiops import AlertmanagerAlert
from app.utils.notify_stream import broadcast_dingtalk


def _build_alert_title(alerts: List[AlertmanagerAlert]) -> str:
    """构造告警标题"""
    names = set(a.labels.get("alertname", "unknown") for a in alerts)
    return f"🔴 AIOps 自动诊断 ({len(alerts)} 条告警: {', '.join(list(names)[:3])})"


def _alert_names(alerts: List[AlertmanagerAlert]) -> List[str]:
    """告警名去重列表（给浏览器卡片展示用）"""
    return sorted({a.labels.get("alertname", "unknown") for a in alerts})


def _build_dingtalk_text(title: str, report: str) -> str:
    """钉钉 markdown 正文

    广播给浏览器时复用同一份文本，保证「对话框里显示的就是真发出去的那条」。
    """
    return f"# {title}\n\n{report[:6000]}"


def _build_dingtalk_url() -> str:
    """构造钉钉 webhook URL：配置了加签密钥时追加 timestamp 与 sign。

    钉钉机器人安全设置若启用「加签」，请求必须带 timestamp + sign，否则会被拒
    （errcode 310000 / sign not match）。密钥以 SEC 开头，存于 ``config.dingtalk_secret``。
    """
    url = config.dingtalk_webhook_url
    secret = config.dingtalk_secret
    if not secret:
        return url
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={timestamp}&sign={sign}"


async def notify_feishu(report: str, alerts: List[AlertmanagerAlert]) -> bool:
    """推送诊断报告到飞书机器人"""
    if not config.feishu_webhook_url:
        logger.warning("飞书 webhook 未配置，跳过通知")
        return False

    title = _build_alert_title(alerts)
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red"
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": report[:4000]  # 飞书有长度限制
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看详细日志"},
                            "url": f"{config.app_url}/api/aiops",
                            "type": "primary"
                        }
                    ]
                }
            ]
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(config.feishu_webhook_url, json=payload)
            resp.raise_for_status()
            logger.info("飞书通知推送成功")
            return True
    except Exception as e:
        logger.error(f"飞书通知推送失败: {e}")
        return False


async def notify_dingtalk(report: str, alerts: List[AlertmanagerAlert]) -> bool:
    """推送诊断报告到钉钉机器人"""
    if not config.dingtalk_webhook_url:
        logger.warning("钉钉 webhook 未配置，跳过通知")
        return False

    title = _build_alert_title(alerts)
    text = _build_dingtalk_text(title, report)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text
        }
    }

    target_url = _build_dingtalk_url()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(target_url, json=payload)
            resp.raise_for_status()
            # 钉钉接口即使被拒（加签不匹配/关键词不匹配/限流）也返回 HTTP 200，
            # 真实结果在响应体 errcode 里，必须检查，否则"推送成功"是假象
            try:
                body = resp.json()
            except Exception:
                body = {}
            errcode = body.get("errcode", 0)
            if errcode != 0:
                logger.error(f"钉钉通知被拒: errcode={errcode} errmsg={body.get('errmsg')}")
                return False
            logger.info("钉钉通知推送成功")
            # 同一条内容也推给在线的浏览器：对话区里以可折叠卡片展示
            # （无人在线时是空操作，不影响上面的推送结果）
            await broadcast_dingtalk(title, text, _alert_names(alerts))
            return True
    except Exception as e:
        logger.error(f"钉钉通知推送失败: {e}")
        return False


async def notify_wecom(report: str, alerts: List[AlertmanagerAlert]) -> bool:
    """推送诊断报告到企业微信机器人"""
    if not config.wecom_webhook_url:
        logger.warning("企业微信 webhook 未配置，跳过通知")
        return False

    title = _build_alert_title(alerts)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"## {title}\n\n{report[:4000]}"
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(config.wecom_webhook_url, json=payload)
            resp.raise_for_status()
            logger.info("企业微信通知推送成功")
            return True
    except Exception as e:
        logger.error(f"企业微信通知推送失败: {e}")
        return False


async def notify_diagnosis(report: str, alerts: List[AlertmanagerAlert]) -> None:
    """统一通知入口：按配置推送到各渠道"""
    tasks = []
    if config.feishu_webhook_url:
        tasks.append(notify_feishu(report, alerts))
    if config.dingtalk_webhook_url:
        tasks.append(notify_dingtalk(report, alerts))
    if config.wecom_webhook_url:
        tasks.append(notify_wecom(report, alerts))

    if not tasks:
        logger.warning("未配置任何通知渠道，诊断报告未推送")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    success_count = sum(1 for r in results if r is True)
    logger.info(f"通知推送完成: {success_count}/{len(tasks)} 个渠道成功")


async def notify_error(error_msg: str, alerts: List[AlertmanagerAlert]) -> None:
    """推送错误信息（诊断流程本身出错时）"""
    title = _build_alert_title(alerts)
    report = f"## {title}\n\n❌ 自动诊断流程异常: {error_msg}"
    logger.error(f"诊断流程异常: {error_msg}")

    tasks = []
    if config.feishu_webhook_url:
        tasks.append(notify_feishu(report, alerts))
    if config.dingtalk_webhook_url:
        tasks.append(notify_dingtalk(report, alerts))
    if config.wecom_webhook_url:
        tasks.append(notify_wecom(report, alerts))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
