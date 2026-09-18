"""向量检索服务模块"""

from typing import Any, Dict, List

from loguru import logger

from app.core.milvus_client import milvus_manager
from app.services.vector_embedding_service import vector_embedding_service


class SearchResult:
    """搜索结果类"""

    def __init__(
        self,
        id: str,
        content: str,
        score: float,
        metadata: Dict[str, Any],
    ):
        self.id = id
        self.content = content
        self.score = score
        self.metadata = metadata

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "content": self.content,
            "score": self.score,
            "metadata": self.metadata,
        }


class VectorSearchService:
    """向量检索服务 - 负责从 Milvus 中搜索相似向量"""

    def __init__(self):
        """初始化向量检索服务"""
        logger.info("向量检索服务初始化完成")

    def search_similar_documents(self, query: str, top_k: int = 3) -> List[SearchResult]:
        """
        搜索相似文档

        Args:
            query: 查询文本
            top_k: 返回最相似的K个结果

        Returns:
            List[SearchResult]: 搜索结果列表

        Raises:
            RuntimeError: 搜索失败时抛出
        """
        try:
            logger.info(f"开始搜索相似文档, 查询: {query}, topK: {top_k}")

            # 1. 将查询文本向量化
            query_vector = vector_embedding_service.embed_query(query)
            logger.debug(f"查询向量生成成功, 维度: {len(query_vector)}")

            # 2. 通过 MilvusClient 执行向量检索（COSINE 度量，分数越大越相似）
            hits = milvus_manager.search(
                query_vectors=[query_vector],
                top_k=top_k,
                output_fields=["id", "content", "metadata"],
            )

            # 3. 解析搜索结果（MilvusClient 返回格式: [{id, distance, entity: {...}}]）
            search_results = []
            for hit in hits[0]:
                entity = hit.get("entity", {})
                search_results.append(
                    SearchResult(
                        id=entity.get("id"),
                        content=entity.get("content"),
                        score=hit.get("distance", 0.0),
                        metadata=entity.get("metadata", {}),
                    )
                )

            logger.info(f"搜索完成, 找到 {len(search_results)} 个相似文档")
            return search_results

        except Exception as e:
            logger.error(f"搜索相似文档失败: {e}")
            raise RuntimeError(f"搜索失败: {e}") from e


# 全局单例
vector_search_service = VectorSearchService()
