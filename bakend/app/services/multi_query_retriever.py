"""多路检索服务：查询改写 + 并行检索 + 按 chunk_id 去重

链路：

    用户问题 ──(LLM 改写)──> [原问题, 改写1, 改写2, 改写3]   语义不变
                                    │
                        ThreadPoolExecutor 并行检索
                        （向量化 + Milvus 检索都是 IO 等待，用线程就能真并发，
                          且不要求调用方是 async）
                                    │
                     合并 ──> 按 metadata["chunk_id"] 去重 ──> 交给大模型

chunk_id 由 document_splitter_service 在「切片时」写入，形如 `运维手册.md#0003`，
随 metadata 一起存进 Milvus，检索时原样返回。

历史数据还没有这个字段（metadata 是入库时写死的），此时去重会自动退化为
「来源 + 内容指纹」，避免过渡期把同一分片重复喂给大模型。
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Sequence

from langchain_core.documents import Document
from langgraph.constants import TAG_NOSTREAM
from loguru import logger

from app.config import config
from app.services.vector_store_manager import vector_store_manager


_REWRITE_PROMPT = """你是检索查询改写助手。请把用户的问题改写成 {n} 个不同表述的检索查询。

要求：
1. 保持原意完全不变，只换说法：同义词替换、补充领域术语、调整语序、口语化说法改成专业说法。
2. 不要新增原问题没有的条件、约束或假设，也不要把问题扩大或缩小范围。
3. 不要回答问题，不要解释，不要编号，不要输出任何多余文字。
4. 直接输出 JSON 数组，元素为字符串，例如：["改写后的查询一", "改写后的查询二"]

用户的问题：{question}"""


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 之类的包裹"""
    return re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip()).strip()


def _parse_rewrites(raw: str, n: int) -> List[str]:
    """从模型输出里解析改写结果（容忍 JSON / 引号 / 逐行 三种形态）"""
    text = _strip_code_fence(raw or "")
    if not text:
        return []

    # 形态 1：标准 JSON 数组（提示词要求的格式）
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()][:n]
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    return [str(item).strip() for item in value if str(item).strip()][:n]
    except Exception:  # noqa: BLE001 — 模型不按格式输出时走下面的兜底
        pass

    # 形态 2：整段里带引号的短语
    quoted = re.findall(r"[\"“'](.+?)[\"”']", text)
    if quoted:
        return [q.strip() for q in quoted if q.strip()][:n]

    # 形态 3：逐行输出，去掉 "1." / "-" / "•" 之类前缀
    lines = [
        re.sub(r"^\s*(?:[-*•]|\d+\s*[.、)])\s*", "", line).strip()
        for line in text.splitlines()
    ]
    return [line for line in lines if line][:n]


def _dedup_keep_order(items: Sequence[str]) -> List[str]:
    """字符串去重（保持首次出现顺序）"""
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        key = (item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


class MultiQueryRetriever:
    """查询改写 + 并行检索 + 按 chunk_id 去重"""

    def __init__(self) -> None:
        self._llm = None

    # ──────────────────────────────────────────────
    # 1. 查询改写
    # ──────────────────────────────────────────────

    def _get_llm(self):
        """惰性创建改写用的 LLM（低温度、非流式，只要一次完整输出）"""
        if self._llm is None:
            from app.core.llm_factory import llm_factory

            self._llm = llm_factory.create_chat_model(
                model=config.rag_model,
                temperature=0.2,
                streaming=False,
            )
        return self._llm

    def rewrite_queries(self, question: str, n: int | None = None) -> List[str]:
        """
        把用户问题改写成 n 条等价问法，连同原问题一起返回

        Args:
            question: 用户原始问题
            n: 额外改写的条数，默认取 config.rag_rewrite_count

        Returns:
            List[str]: [原问题, 改写1, ...]，共 1+n 条；失败时只返回 [原问题]
        """
        n = config.rag_rewrite_count if n is None else n
        original = (question or "").strip()
        if not original:
            return []

        if n <= 0:
            logger.debug("rag_rewrite_count <= 0，使用单路检索")
            return [original]

        rewrites: List[str] = []
        try:
            # TAG_NOSTREAM：改写给用户看不着，是检索的内部步骤。
            # LangGraph 在 stream_mode="messages" 下会把图内**任何** LLM 调用的结果推进
            # 消息流（连非流式的也会在 on_llm_end 里整条推出，见 langgraph/pregel/_messages.py），
            # 不打这个标签，改写出来的问题列表就会被当成回答内容吐给前端。
            response = self._get_llm().invoke(
                _REWRITE_PROMPT.format(n=n, question=original),
                config={"tags": [TAG_NOSTREAM]},
            )
            raw = response.content if hasattr(response, "content") else str(response)
            rewrites = _parse_rewrites(str(raw), n)
        except Exception as e:  # noqa: BLE001 — 改写只是增强手段，不能拖垮检索
            logger.warning(f"查询改写失败，退化为单路检索: {e}")

        queries = _dedup_keep_order([original, *rewrites])
        if original not in queries:
            queries.insert(0, original)

        logger.info(f"查询改写完成，共 {len(queries)} 路: {queries}")
        return queries

    # ──────────────────────────────────────────────
    # 2. 并行检索
    # ──────────────────────────────────────────────

    @staticmethod
    def _retrieve_one(retriever, query: str) -> List[Document]:
        """单路检索；单路失败只丢这一路，不影响其他路"""
        try:
            docs = retriever.invoke(query)
            logger.debug(f"检索 '{query}' -> {len(docs)} 条")
            return docs
        except Exception as e:  # noqa: BLE001
            logger.warning(f"检索失败（query='{query}'）: {e}")
            return []

    def parallel_retrieve(
        self, queries: Sequence[str], top_k: int
    ) -> List[List[Document]]:
        """
        多路并行检索（返回顺序与 queries 一一对应）

        Args:
            queries: 多路查询
            top_k: 每路各自返回多少条

        Returns:
            List[List[Document]]: 每路的检索结果
        """
        if not queries:
            return []

        # retriever 只建一次；向量库不可用时在这里抛出，由调用方决定怎么提示
        retriever = vector_store_manager.get_vector_store().as_retriever(
            search_kwargs={"k": top_k}
        )

        if len(queries) == 1:
            return [self._retrieve_one(retriever, queries[0])]

        with ThreadPoolExecutor(
            max_workers=len(queries), thread_name_prefix="rag-multi"
        ) as pool:
            futures = [pool.submit(self._retrieve_one, retriever, q) for q in queries]
            groups: List[List[Document]] = []
            for query, future in zip(queries, futures):
                try:
                    groups.append(future.result())
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"检索任务异常（query='{query}'）: {e}")
                    groups.append([])
        return groups

    # ──────────────────────────────────────────────
    # 3. 按 chunk_id 去重
    # ──────────────────────────────────────────────

    @staticmethod
    def chunk_key(doc: Document) -> str:
        """
        分片标识：优先 metadata["chunk_id"]（切片时写入），
        老数据没有该字段时退化为「来源 + 内容指纹」，保证同一分片只出现一次
        """
        metadata: Dict[str, Any] = doc.metadata or {}

        chunk_id = metadata.get("chunk_id") or metadata.get("_chunk_id")
        if chunk_id not in (None, ""):
            return f"chunk::{chunk_id}"

        source = metadata.get("_source") or metadata.get("source") or ""
        digest = hashlib.md5(doc.page_content.encode("utf-8")).hexdigest()
        return f"raw::{source}::{digest}"

    def dedup_by_chunk_id(self, docs: Sequence[Document]) -> List[Document]:
        """按 chunk_id 去重，保持首次出现的顺序"""
        seen: set[str] = set()
        result: List[Document] = []
        for doc in docs:
            key = self.chunk_key(doc)
            if key in seen:
                continue
            seen.add(key)
            result.append(doc)
        return result

    # ──────────────────────────────────────────────
    # 对外入口
    # ──────────────────────────────────────────────

    def retrieve(self, question: str, top_k: int | None = None) -> List[Document]:
        """
        多路检索完整流程：改写 -> 并行检索 -> 按 chunk_id 去重

        Args:
            question: 用户问题
            top_k: 每路检索条数，默认 config.rag_top_k

        Returns:
            List[Document]: 去重后的文档列表
        """
        k = top_k or config.rag_top_k
        queries = self.rewrite_queries(question)
        if not queries:
            return []

        groups = self.parallel_retrieve(queries, k)
        merged = [doc for group in groups for doc in group]
        deduped = self.dedup_by_chunk_id(merged)

        logger.info(
            f"多路检索完成: {len(queries)} 路 × top{k} 共 {len(merged)} 条 "
            f"-> 按 chunk_id 去重后 {len(deduped)} 条"
        )
        return deduped


# 全局单例
multi_query_retriever = MultiQueryRetriever()
