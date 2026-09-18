"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "SuperBizAgent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    # Windows 下如果进程是被后台/无窗口方式启动的，控制台窗口会处于「隐藏」状态，
    # 日志照写但看不见；打开这个开关后启动时会把该窗口显示出来，方便实时看日志。
    # 不想让它弹窗就把 .env 里设成 SHOW_CONSOLE_WINDOW=false
    show_console_window: bool = True

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）
    # 图片解析用的多模态模型（支持视觉的 OpenAI 兼容模型）
    vision_model: str = "qwen-vl-max"

    # PostgreSQL 检查点连接串（会话历史持久化；留空则回退内存 MemorySaver）
    pg_conn_string: str = ""

    # Milvus 配置
    milvus_host: str = "192.168.150.102"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_top_k: int = 3
    rag_model: str = "qwen-max"  # 使用快速响应模型，不带扩展思考

    # 多路检索：把用户问题额外改写成 rag_rewrite_count 条等价问法（不改变原意），
    # 并行检索后按 metadata["chunk_id"] 去重再交给大模型。
    # 总检索路数 = 1（原问题）+ rag_rewrite_count；设为 0 即退化为原来的单路检索
    rag_rewrite_count: int = 2

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    # MCP 服务配置（transport: stdio | sse | streamable-http）
    # 腾讯云托管 MCP 的 URL 通常含 /sse/，需使用 sse；本地 FastMCP 使用 streamable-http
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"

    # Prometheus
    prometheus_base_url: str = "http://127.0.0.1:9090"
    prometheus_request_timeout: float = 10.0

    # 通知配置（app/utils/notify.py 用：哪个渠道配了 URL 就推哪个）
    app_url: str = "http://192.168.150.1:9900"  # 通知卡片里「查看详细日志」按钮的地址
    feishu_webhook_url: str = ""
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""  # 钉钉机器人「加签」密钥（以 SEC 开头），启用加签时必须填
    wecom_webhook_url: str = ""

    # Alertmanager webhook 去重窗口（秒）：同 fingerprint 在该窗口内不重复诊断
    webhook_dedup_window: int = 60

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            }
        }


# 全局配置实例
config = Settings()
