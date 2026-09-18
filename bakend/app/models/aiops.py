"""
AIOps 请求和响应模型
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class AIOpsRequest(BaseModel):
    """AIOps 诊断请求"""
    
    session_id: Optional[str] = Field(
        default="default",
        description="会话ID，用于追踪诊断历史"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "session-123"
            }
        }


class AlertmanagerAlert(BaseModel):
    """单条告警（Alertmanager webhook 的 alerts 数组元素）

    除 status/labels/annotations/startsAt 外，Alertmanager 真实 payload 还带
    fingerprint。这里把它和 webhook 的 version/groupKey/receiver 一并设为可选：
    Alertmanager 实际发送时会带上，而手工 curl 联调时可以不填（缺失时用
    labels 生成兜底指纹，见 api/aiops.py 的 _alert_key）。
    """
    status: str                      # "firing" | "resolved"
    labels: Dict[str, str] = Field(default_factory=dict)      # alertname, severity, instance...
    annotations: Dict[str, str] = Field(default_factory=dict)  # summary, description
    startsAt: Optional[str] = None   # ISO 时间
    endsAt: Optional[str] = None
    fingerprint: Optional[str] = None  # Alertmanager 生成的去重指纹


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook payload（简化版）"""
    status: str                                    # "firing" | "resolved"
    alerts: List[AlertmanagerAlert] = Field(default_factory=list)
    version: str = "4"
    groupKey: str = ""
    receiver: str = ""


class AlertInfo(BaseModel):
    """告警信息"""
    alertname: str
    severity: str
    instance: str
    duration: str
    description: Optional[str] = None


class DiagnosisResponse(BaseModel):
    """诊断响应（非流式）"""
    
    code: int = 200
    message: str = "success"
    data: Dict[str, Any]
    
    class Config:
        json_schema_extra = {
            "example": {
                "code": 200,
                "message": "success",
                "data": {
                    "status": "completed",
                    "target_alert": {
                        "alertname": "HighCPUUsage",
                        "severity": "critical"
                    },
                    "diagnosis": {
                        "root_cause": "数据库连接池耗尽",
                        "recommendations": ["扩容数据库连接池", "优化SQL查询"]
                    }
                }
            }
        }
