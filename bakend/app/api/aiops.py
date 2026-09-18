"""
AIOps 智能运维接口
"""

import asyncio
import json
import time
from typing import List

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse
from loguru import logger

from app.config import config
from app.models.aiops import AIOpsRequest, AlertmanagerAlert, AlertmanagerWebhook
from app.services.aiops_service import aiops_service

router = APIRouter()


@router.post("/aiops")
async def diagnose_stream(request: AIOpsRequest):
    """
    AIOps 故障诊断接口（流式 SSE）

    **功能说明：**
    - 自动获取当前系统的活动告警
    - 使用 Plan-Execute-Replan 模式进行智能诊断
    - 流式返回诊断过程和结果

    **SSE 事件类型：**

    1. `status` - 状态更新
       ```json
       {
         "type": "status",
         "stage": "fetching_alerts",
         "message": "正在获取系统告警信息..."
       }
       ```

    2. `plan` - 诊断计划制定完成
       ```json
       {
         "type": "plan",
         "stage": "plan_created",
         "message": "诊断计划已制定，共 6 个步骤",
         "target_alert": {...},
         "plan": ["步骤1: ...", "步骤2: ..."]
       }
       ```

    3. `step_complete` - 步骤执行完成
       ```json
       {
         "type": "step_complete",
         "stage": "step_executed",
         "message": "步骤执行完成 (2/6)",
         "current_step": "查询系统日志",
         "result_preview": "...",
         "remaining_steps": 4
       }
       ```

    4. `report` - 最终诊断报告
       ```json
       {
         "type": "report",
         "stage": "final_report",
         "message": "最终诊断报告已生成",
         "report": "# 故障诊断报告\\n...",
         "evidence": {...}
       }
       ```

    5. `complete` - 诊断完成
       ```json
       {
         "type": "complete",
         "stage": "diagnosis_complete",
         "message": "诊断流程完成",
         "diagnosis": {...}
       }
       ```

    6. `error` - 错误信息
       ```json
       {
         "type": "error",
         "stage": "error",
         "message": "诊断过程发生错误: ..."
       }
       ```

    **使用示例：**
    ```bash
    curl -X POST "http://localhost:9900/api/aiops" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123"}' \\
      --no-buffer
    ```

    **前端使用示例：**
    ```javascript
    const eventSource = new EventSource('/api/aiops');

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'plan') {
        console.log('诊断计划:', data.plan);
      } else if (data.type === 'step_complete') {
        console.log('步骤完成:', data.current_step);
      } else if (data.type === 'report') {
        console.log('最终报告:', data.report);
      } else if (data.type === 'complete') {
        console.log('诊断完成');
        eventSource.close();
      }
    };
    ```

    Args:
        request: AIOps 诊断请求

    Returns:
        SSE 事件流
    """
    session_id = request.session_id or "default"
    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")

    async def event_generator():
        try:
            async for event in aiops_service.diagnose(session_id=session_id):
                # 发送事件
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False)
                }

                # 如果是完成或错误事件，结束流
                if event.get("type") in ["complete", "error"]:
                    break

            logger.info(f"[会话 {session_id}] AIOps 诊断流式响应完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] AIOps 诊断流式响应异常: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "stage": "exception",
                    "message": f"诊断异常: {str(e)}"
                }, ensure_ascii=False)
            }

    return EventSourceResponse(event_generator())


# ──────────────────────────────────────────────
# Alertmanager 告警自动触发：接收 → 去重 → 异步诊断 → 推送通知
# ──────────────────────────────────────────────

# 去重表：告警标识 -> 最近一次触发诊断的时间戳
_recent_diagnosis: dict[str, float] = {}


def _alert_key(alert: AlertmanagerAlert) -> str:
    """去重用的告警标识：优先用 Alertmanager 的 fingerprint，缺失时用 labels 拼兜底"""
    if alert.fingerprint:
        return alert.fingerprint
    labels = alert.labels or {}
    return "|".join(
        f"{k}={labels.get(k, '')}"
        for k in ("alertname", "severity", "instance", "namespace", "pod")
    )


@router.post("/aiops/webhook")
async def webhook_handler(payload: AlertmanagerWebhook):
    """
    Alertmanager webhook 接收端点（告警自动触发诊断 → 推送通知）

    **流程：**
    1. resolved 直接跳过（先只处理 firing）
    2. 同一告警在 `webhook_dedup_window` 秒内不重复诊断
    3. 按实际告警内容构造动态 prompt
    4. 异步跑诊断（Alertmanager 要求 10s 内响应，不能阻塞）
    5. 诊断完成后推送飞书/钉钉/企业微信

    **返回：**
    - `{"status": "accepted", "alerts_count": N, ...}` 已受理，后台正在诊断
    - `{"status": "skipped", "reason": "..."}` 全是 resolved 或在去重窗口内
    """
    if payload.status == "resolved":
        logger.info("[webhook] 收到 resolved 状态，跳过")
        return {"status": "skipped", "reason": "resolved alerts ignored"}

    window = config.webhook_dedup_window
    now = time.time()
    tasks: List[AlertmanagerAlert] = []

    for alert in payload.alerts:
        if alert.status != "firing":
            continue

        key = _alert_key(alert)
        if now - _recent_diagnosis.get(key, 0) < window:
            logger.info(f"[webhook] 告警 {key} 在 {window}s 去重窗口内，跳过")
            continue

        _recent_diagnosis[key] = now
        tasks.append(alert)

    if not tasks:
        return {"status": "skipped", "reason": "all alerts deduplicated or resolved"}

    logger.info(f"[webhook] 收到 {len(tasks)} 条 firing 告警，转后台异步诊断")

    # 必须异步：Alertmanager 要求 webhook 10 秒内返回
    asyncio.create_task(_run_diagnosis_and_notify(tasks))

    return {
        "status": "accepted",
        "alerts_count": len(tasks),
        "message": f"已接收 {len(tasks)} 条告警，正在异步诊断",
    }


async def _run_diagnosis_and_notify(alerts: List[AlertmanagerAlert]):
    """异步执行诊断，完成后推送通知"""
    prompt = _build_diagnosis_prompt(alerts)
    session_id = f"webhook-{int(time.time())}"
    logger.info(f"[webhook] 构造动态 prompt，涉及 {len(alerts)} 条告警，会话 {session_id}")

    report = ""
    try:
        async for event in aiops_service.execute(
            user_input=prompt,
            session_id=session_id
        ):
            if event.get("type") == "complete":
                report = event.get("response", "") or ""

        if not report:
            logger.warning(f"[会话 {session_id}] 诊断结束但没有产出报告内容")

        # 延迟导入：通知模块出问题也不影响手动诊断端点
        from app.utils.notify import notify_diagnosis
        await notify_diagnosis(report or "(诊断未产出报告内容)", alerts)

    except Exception as e:
        logger.error(f"[会话 {session_id}] 自动诊断流程异常: {e}", exc_info=True)
        from app.utils.notify import notify_error
        await notify_error(str(e), alerts)


def _build_diagnosis_prompt(alerts: List[AlertmanagerAlert]) -> str:
    """从告警 payload 构造动态 prompt"""
    alert_summaries = []
    for a in alerts:
        alert_summaries.append(
            f"- 告警名: {a.labels.get('alertname', 'unknown')}\n"
            f"  严重级别: {a.labels.get('severity', 'unknown')}\n"
            f"  实例: {a.labels.get('instance', 'unknown')}\n"
            f"  命名空间: {a.labels.get('namespace', '-')}\n"
            f"  Pod: {a.labels.get('pod', '-')}\n"
            f"  触发时间: {a.startsAt or '-'}\n"
            f"  描述: {a.annotations.get('description', a.annotations.get('summary', ''))}"
        )

    return f"""你是一名 SRE 工程师，系统刚触发了以下告警，请完成根因分析并生成诊断报告。

告警列表:
{chr(10).join(alert_summaries)}

诊断要求（按顺序执行）:
1. 调用 query_prometheus_alerts() 查看全局告警状态
2. 根据告警涉及的 service/instance 调用 MCP Monitor 工具：
   - list_all_services() 查看服务列表
   - query_cpu_metrics() 查看 CPU 趋势
   - query_memory_metrics() 查看内存趋势
   - query_process_list() 查看进程状态
3. 根据告警服务名调用 CLS MCP 工具：
   - search_topic_by_service_name() 找到对应 topic
   - search_log() 查询相关错误日志（过滤 level=ERROR）
   - analyze_log_pattern() 分析日志模式
4. 调用 retrieve_knowledge() 查询知识库里相关的运维文档
5. 综合所有证据生成诊断报告，包含:
   - 告警现象描述
   - 根因分析（带数据证据）
   - 处理建议（具体可操作）
   - 风险评估

注意：所有内容必须基于工具查询的真实数据，严禁编造。当前环境若某个工具不可用（如本地没有 MCP 服务），
请如实说明"该工具不可用"，不要编造查询结果。
"""
