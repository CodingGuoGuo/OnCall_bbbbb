"""Milvus 客户端工厂模块"""

from loguru import logger
from pymilvus import (
    DataType,
    MilvusClient,
    MilvusException,
)

from app.config import config


class MilvusClientManager:
    """Milvus 客户端管理器（纯 MilvusClient 实现，无过时 API）"""

    # 常量定义
    COLLECTION_NAME: str = "OnCall"
    VECTOR_DIM: int = 1024  # 统一使用 1024 维
    ID_MAX_LENGTH: int = 100
    CONTENT_MAX_LENGTH: int = 8000
    DEFAULT_SHARD_NUMBER: int = 2

    def __init__(self) -> None:
        """初始化 Milvus 客户端管理器"""
        self._client: MilvusClient | None = None

    # ──────────────────────────────────────────────
    # 连接 & 初始化
    # ──────────────────────────────────────────────

    def connect(self) -> MilvusClient:
        """
        连接到 Milvus 服务器并初始化 collection

        Returns:
            MilvusClient: Milvus 客户端实例

        Raises:
            RuntimeError: 连接或初始化失败时抛出
        """
        # 幂等：避免重复初始化
        if self._client is not None:
            logger.debug("Milvus 已连接，跳过重复 connect")
            return self._client

        try:
            uri = f"http://{config.milvus_host}:{config.milvus_port}"
            logger.info(f"正在连接到 Milvusasd: {config.milvus_host}:{config.milvus_port}")

            self._client = MilvusClient(uri=uri)
            logger.info("成功连接到 Milvus")

            # 检查并创建 collection
            if not self._client.has_collection(self.COLLECTION_NAME):
                logger.info(f"collection '{self.COLLECTION_NAME}' 不存在，正在创建...")
                self._create_collection()
                logger.info(f"成功创建 collection '{self.COLLECTION_NAME}'")
            else:
                logger.info(f"collection '{self.COLLECTION_NAME}' 已存在")
                self._check_and_fix_dimension()

            return self._client

        except MilvusException as e:
            logger.error(f"Milvus 操作失败: {e}")
            self.close()
            raise RuntimeError(f"Milvus 操作失败: {e}") from e
        except ConnectionError as e:
            logger.error(f"连接 Milvus 失败: {e}")
            self.close()
            raise RuntimeError(f"连接 Milvus 失败: {e}") from e
        except Exception as e:
            logger.error(f"连接 Milvus 失败: {e}")
            self.close()
            raise RuntimeError(f"连接 Milvus 失败: {e}") from e

    # ──────────────────────────────────────────────
    # Collection 管理
    # ──────────────────────────────────────────────

    def _create_collection(self) -> None:
        """创建 biz collection（使用 MilvusClient 原生 API）"""
        schema = self._client.create_schema(
            description="Business knowledge collection",
            auto_id=False,
            enable_dynamic_field=False,
        )
        schema.add_field(
            field_name="id",
            datatype=DataType.VARCHAR,
            max_length=self.ID_MAX_LENGTH,
            is_primary=True,
        )
        schema.add_field(
            field_name="vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=self.VECTOR_DIM,
        )
        schema.add_field(
            field_name="content",
            datatype=DataType.VARCHAR,
            max_length=self.CONTENT_MAX_LENGTH,
        )
        schema.add_field(
            field_name="metadata",
            datatype=DataType.JSON,
        )

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            metric_type="COSINE",
            index_type="IVF_FLAT",
            params={"nlist": 128},
        )

        self._client.create_collection(
            collection_name=self.COLLECTION_NAME,
            schema=schema,
            index_params=index_params,
            shards_num=self.DEFAULT_SHARD_NUMBER,
            # 传了 index_params 时 pymilvus 会紧接着隐式 load_collection，默认 timeout=None
            # —— 服务端加载卡住就会把调用方永久挂死，这里给个上界。
            # （langchain_milvus 的 timeout 单位是秒，config 里配的是毫秒）
            timeout=config.milvus_timeout / 1000,
        )
        logger.info(f"成功创建 collection '{self.COLLECTION_NAME}' 并建立索引")

    def _check_and_fix_dimension(self) -> None:
        """检查向量维度是否匹配，不匹配则重建 collection"""
        info = self._client.describe_collection(self.COLLECTION_NAME)
        existing_dim = None
        for field in info.get("fields", []):
            if field.get("name") == "vector":
                existing_dim = field.get("params", {}).get("dim")
                break

        if existing_dim is not None and existing_dim != self.VECTOR_DIM:
            logger.warning(
                f"检测到向量维度不匹配！当前 collection 维度: {existing_dim}, 配置维度: {self.VECTOR_DIM}"
            )
            logger.info(f"正在删除旧 collection '{self.COLLECTION_NAME}'...")
            self._client.drop_collection(self.COLLECTION_NAME)
            logger.info(f"正在重新创建 collection '{self.COLLECTION_NAME}'...")
            self._create_collection()
            logger.info(f"成功重新创建 collection，维度: {self.VECTOR_DIM}")
        elif existing_dim is not None:
            logger.info(f"向量维度匹配: {self.VECTOR_DIM}")
        else:
            logger.warning("未找到 vector 字段的维度信息，跳过维度校验")

    # ──────────────────────────────────────────────
    # 数据操作
    # ──────────────────────────────────────────────

    def insert(self, data: list[dict]) -> list:
        """
        插入数据

        Args:
            data: 数据列表，每条包含 id / vector / content / metadata

        Returns:
            插入结果
        """
        if self._client is None:
            raise RuntimeError("客户端未初始化，请先调用 connect()")
        result = self._client.insert(
            collection_name=self.COLLECTION_NAME,
            data=data)
        return result

    def search(
        self,
        query_vectors: list[list[float]],
        top_k: int = 10,
        output_fields: list[str] | None = None,
        filter_expr: str | None = None,
    ) -> list:
        """
        向量检索

        Args:
            query_vectors: 查询向量列表
            top_k: 返回 top-k 结果
            output_fields: 需要返回的字段
            filter_expr: 标量过滤表达式

        Returns:
            检索结果
        """
        if self._client is None:
            raise RuntimeError("客户端未初始化，请先调用 connect()")

        search_params = {"metric_type": "COSINE", "params": {"nprobe": 10}}
        results = self._client.search(
            collection_name=self.COLLECTION_NAME,
            data=query_vectors,
            limit=top_k,
            output_fields=output_fields or ["id", "content", "metadata"],
            search_params=search_params,
            filter=filter_expr,  # pymilvus 2.6.x 参数名为 filter（expr 为旧名，传入会报重复关键字错误）
        )
        return results

    def query(
        self,
        filter_expr: str,
        output_fields: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        collection_name: str | None = None,
        timeout: float | None = None,
    ) -> list:
        """
        标量查询

        Args:
            filter_expr: 过滤表达式
            output_fields: 需要返回的字段
            limit: 返回条数上限
            offset: 偏移量（分页用，pymilvus 2.6 支持）
            collection_name: 目标 collection，默认本类约定的 COLLECTION_NAME。
                知识库文档存在 biz 里（vector_store_manager.COLLECTION_NAME），
                不指定的话会查错库。
            timeout: 超时（秒），默认取 config.milvus_timeout（毫秒换算）——
                Milvus 调用的默认超时是 None，服务端一卡就会把调用方吊死

        Returns:
            检索结果
        """
        if self._client is None:
            raise RuntimeError("客户端未初始化，请先调用 connect()")
        return self._client.query(
            collection_name=collection_name or self.COLLECTION_NAME,
            filter=filter_expr,  # pymilvus 2.6.x 参数名为 filter（expr 为旧名）
            output_fields=output_fields,
            limit=limit,
            offset=offset,
            timeout=timeout if timeout is not None else config.milvus_timeout / 1000,
        )

    def delete_by_ids(self, ids: list[str]) -> None:
        """按主键删除"""
        if self._client is None:
            raise RuntimeError("客户端未初始化，请先调用 connect()")
        self._client.delete(collection_name=self.COLLECTION_NAME, ids=ids)

    def flush(self) -> None:
        """手动落盘"""
        if self._client is None:
            raise RuntimeError("客户端未初始化，请先调用 connect()")
        self._client.flush(collection_name=self.COLLECTION_NAME)

    # ──────────────────────────────────────────────
    # 统计 & 健康检查
    # ──────────────────────────────────────────────

    def get_collection_stats(self) -> dict:
        """获取 collection 统计信息"""
        if self._client is None:
            raise RuntimeError("客户端未初始化，请先调用 connect()")
        info = self._client.describe_collection(self.COLLECTION_NAME)
        row_count = self._client.get_collection_stats(self.COLLECTION_NAME)
        return {"info": info, "stats": row_count}

    def health_check(self) -> bool:
        """
        健康检查

        Returns:
            bool: True 表示健康，False 表示异常
        """
        try:
            if self._client is None:
                return False
            # MilvusClient 能列出 collection 即说明连接正常
            _ = self._client.list_collections()
            return True
        except (MilvusException, ConnectionError) as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            return False
        except Exception as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            return False

    # ──────────────────────────────────────────────
    # 连接管理
    # ──────────────────────────────────────────────

    def close(self) -> None:
        """关闭客户端"""
        errors = []
        try:
            if self._client is not None:
                self._client.close()
        except Exception as e:
            errors.append(f"关闭 MilvusClient 失败: {e}")
        self._client = None

        if errors:
            error_msg = "; ".join(errors)
            logger.error(f"关闭 Milvus 连接时出现错误: {error_msg}")
        else:
            logger.info("已关闭 Milvus 连接")

    def get_client(self) -> MilvusClient:
        """
        获取 MilvusClient 实例

        Returns:
            MilvusClient: 客户端实例

        Raises:
            RuntimeError: 未初始化时抛出
        """
        if self._client is None:
            raise RuntimeError("客户端未初始化，请先调用 connect()")
        return self._client

    # ──────────────────────────────────────────────
    # 上下文管理器
    # ──────────────────────────────────────────────

    def __enter__(self) -> "MilvusClientManager":
        """上下文管理器入口"""
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """上下文管理器退出"""
        self.close()


# 全局单例
milvus_manager = MilvusClientManager()
