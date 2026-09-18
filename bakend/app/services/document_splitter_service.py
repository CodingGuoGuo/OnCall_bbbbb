"""文档分割服务模块 - 基于 LangChain 的智能文档分割"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import (
    CharacterTextSplitter,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from loguru import logger

from app.config import config

# ---------------------------------------------------------------- 切片策略 ---

STRATEGY_GENERAL = "general"            # 通用切片：递归切 + 用户链之后自动追加默认兜底链
STRATEGY_CHAIN = "chain"                # 递归分隔符链：严格只用用户给的链，不追加兜底
STRATEGY_LOOP = "loop"                  # 固定窗口：固定长度滑动窗口，不看语义边界
STRATEGY_PARENT_CHILD = "parent_child"  # 父子切片：小子块参与检索，命中后把父块全文给大模型

ALL_STRATEGIES = (STRATEGY_GENERAL, STRATEGY_CHAIN, STRATEGY_LOOP, STRATEGY_PARENT_CHILD)

# ---------------------------------------------------------------- 规则类型 ---

RULE_SEPARATOR = "separator"   # 自定义分隔符
RULE_HEADING = "heading"       # 按 Markdown 标题层级
RULE_LENGTH = "length"         # 按固定长度硬切

ALL_RULE_TYPES = (RULE_SEPARATOR, RULE_HEADING, RULE_LENGTH)
HEADING_LEVELS = ("#", "##", "###")

# 参数边界（避免非法参数把服务打爆）
MIN_CHUNK_SIZE = 50
MAX_CHUNK_SIZE = 4000
MAX_PARENT_SIZE = 8000
MAX_SEPARATOR_LEN = 32
# 规则条数上限：级联是 O(规则数 × 片段数)，防止塞超长链把切分放大
MAX_RULES = 8
# 固定窗口 / 定长硬切走的是「逐字符切分再合并」，超大文本会撑出上百万个字符串对象，这里设个上限
MAX_LOOP_CHARS = 500_000

# 自定义分隔符之后追加的兜底分隔符链。
# 实测：RecursiveCharacterTextSplitter 只给单个自定义分隔符时，超出 chunk_size 的片段不会
# 被再拆（给 separators=['@@@'] + chunk_size=10 会返回 13 字符的块），必须留递归兜底。
_FALLBACK_SEPARATORS = ["\n\n", "\n", "。", "！", "？", ". ", " ", ""]

# 前端传来的分隔符是字面量（`\n\n` 是 4 个字符），这里做转义解释
_ESCAPE_MAP = (("\\n", "\n"), ("\\r", "\r"), ("\\t", "\t"))


def decode_separator(raw: str | None) -> str | None:
    """把前端传来的分隔符字面量解释成真实字符

    支持 `\\n` / `\\r` / `\\t` / `\\\\`。空串、纯空白返回 None，表示走 LangChain 默认分隔符链。

    Raises:
        ValueError: 分隔符超过 MAX_SEPARATOR_LEN
    """
    if raw is None:
        return None

    text = raw.replace("\\\\", "\x00")
    for literal, real in _ESCAPE_MAP:
        text = text.replace(literal, real)
    text = text.replace("\x00", "\\")

    # 只判「解码后为空」。`\n\n` 解码后是两个换行、strip() 之后为空，
    # 但它是完全合法的「按段落分片」分隔符，不能被当成「用户没填」。
    if text == "":
        return None
    if len(text) > MAX_SEPARATOR_LEN:
        raise ValueError(f"分隔符过长，最多 {MAX_SEPARATOR_LEN} 个字符")
    return text


@dataclass
class SplitRule:
    """一条分片依据

    按列表顺序级联生效：先按第 1 条切，仍超过 chunk_size 的片段再交给第 2 条，依次往下。
    """

    type: str    # separator | heading | length
    value: str   # separator = 真实分隔符；heading = "#"/"##"/"###"；length = 正整数

    def describe(self) -> str:
        return f"{self.type}={self.value!r}"


def _rule_error(index: int, message: str) -> ValueError:
    return ValueError(f"第 {index} 条分片依据有误：{message}")


def parse_rules(raw: str | None, label: str = "分片依据") -> list[SplitRule]:
    """解析前端传来的 JSON 规则列表

    形如 `[{"type":"separator","value":"\\\\n\\\\n"},{"type":"heading","value":"##"}]`。
    None / 空白返回空列表（表示调用方没配，由策略决定默认行为）。

    Raises:
        ValueError: JSON 非法、类型不支持、取值非法（提示里带第几条）
    """
    if raw is None or not str(raw).strip():
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label}不是合法的 JSON：{e}") from e

    if not isinstance(payload, list):
        raise ValueError(f"{label}必须是数组")
    if len(payload) > MAX_RULES:
        raise ValueError(f"{label}最多 {MAX_RULES} 条")

    rules: list[SplitRule] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise _rule_error(index, '格式应为 {"type": ..., "value": ...}')

        rule_type = str(item.get("type") or "").strip()
        raw_value = item.get("value")
        value = "" if raw_value is None else str(raw_value)

        if rule_type not in ALL_RULE_TYPES:
            raise _rule_error(index, f"类型不支持（可选: {', '.join(ALL_RULE_TYPES)}）")

        if rule_type == RULE_SEPARATOR:
            decoded = decode_separator(value)
            if decoded is None:
                raise _rule_error(index, "分隔符不能为空")
            rules.append(SplitRule(RULE_SEPARATOR, decoded))
        elif rule_type == RULE_HEADING:
            level = value.strip()
            if level not in HEADING_LEVELS:
                raise _rule_error(index, f"标题层级只能是 {', '.join(HEADING_LEVELS)}")
            rules.append(SplitRule(RULE_HEADING, level))
        else:
            try:
                number = int(value.strip())
            except (TypeError, ValueError) as e:
                raise _rule_error(index, "固定长度必须是整数") from e
            if not MIN_CHUNK_SIZE <= number <= MAX_CHUNK_SIZE:
                raise _rule_error(index, f"固定长度需在 {MIN_CHUNK_SIZE}~{MAX_CHUNK_SIZE} 字符之间")
            rules.append(SplitRule(RULE_LENGTH, str(number)))

    return rules


@dataclass
class SplitOptions:
    """一次切分使用的参数

    按次传入而不是改全局单例：`document_splitter_service` 是启动时构造的单例，
    把策略塞进单例会让并发上传互相污染，也会改变 /api/index_directory 的既有行为。
    """

    strategy: str = STRATEGY_GENERAL
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    separator: str | None = None                 # 单条分隔符（向后兼容），等价于一条 separator 规则
    parent_size: int | None = None               # 仅父子切片使用
    rules: list[SplitRule] | None = None         # 分片依据（通用 / 递归链 / 父子子块）
    parent_rules: list[SplitRule] | None = None  # 父子切片专用的父块依据

    @property
    def size(self) -> int:
        return self.chunk_size or config.chunk_max_size

    @property
    def overlap(self) -> int:
        """重复大小：父子切片恒为 0

        前端在父子策略下已经不渲染该输入框，这里从数据源头再兜一层，
        避免旧脚本或手工请求传进来的值悄悄生效。
        """
        if self.strategy == STRATEGY_PARENT_CHILD:
            return 0
        return config.chunk_overlap if self.chunk_overlap is None else self.chunk_overlap

    @property
    def parent(self) -> int:
        return self.parent_size or self.size * 2

    def effective_rules(self) -> list[SplitRule]:
        """本次切分实际使用的规则链

        - 显式传了 rules → 用它
        - 只传了旧的 separator 参数 → 视为单条 separator 规则（向后兼容）
        - 都没传 → 空列表，由策略决定默认链
        """
        if self.rules:
            return list(self.rules)
        if self.separator:
            return [SplitRule(RULE_SEPARATOR, self.separator)]
        return []

    def validate(self) -> None:
        """参数校验：非法直接抛 ValueError，由接口层转 400

        前置校验的意义是避免「生成完向量、写到一半才发现参数不对」。
        """
        if self.strategy not in ALL_STRATEGIES:
            raise ValueError(
                f"不支持的切片方式: {self.strategy}，可选: {', '.join(ALL_STRATEGIES)}"
            )
        if not MIN_CHUNK_SIZE <= self.size <= MAX_CHUNK_SIZE:
            raise ValueError(f"分片大小需在 {MIN_CHUNK_SIZE}~{MAX_CHUNK_SIZE} 字符之间")
        if self.overlap < 0 or self.overlap >= self.size:
            raise ValueError("重复大小必须大于等于 0 且小于分片大小")
        if self.strategy == STRATEGY_PARENT_CHILD:
            if not self.size < self.parent <= MAX_PARENT_SIZE:
                raise ValueError(f"父块大小需大于分片大小且不超过 {MAX_PARENT_SIZE} 字符")
        elif len(self.effective_rules()) > MAX_RULES:
            raise ValueError(f"分片依据最多 {MAX_RULES} 条")


def _make_chunk_id(file_name: str, index: int) -> str:
    """切片标识（写入 metadata["chunk_id"]），供多路检索去重使用

    形如 `运维手册.md#0003`。同一文件重新索引时序号会重新计算，
    但索引前会先按 _source 删除旧数据，所以不会产生脏数据。
    """
    return f"{file_name}#{index:04d}"


class DocumentSplitterService:
    """文档分割服务 - 使用 LangChain 的分割器"""

    def __init__(self):
        """初始化文档分割服务"""
        self.chunk_size = config.chunk_max_size
        self.chunk_overlap = config.chunk_overlap

        # Markdown 标题分割器 (只按一级和二级标题分割，减少分片数)
        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "h1"),
                ("##", "h2"),
                # 不再按三级标题分割，避免过度碎片化
            ],
            strip_headers=False,  # 保留标题在内容中
        )

        # 递归字符分割器 (用于二次分割，使用更大的chunk_size)
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size * 2,  # 加倍chunk_size，减少分片数
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            is_separator_regex=False,
        )

        logger.info(
            f"文档分割服务初始化完成, chunk_size={self.chunk_size}, "
            f"secondary_chunk_size={self.chunk_size * 2}, "
            f"overlap={self.chunk_overlap}"
        )

    def split_markdown(self, content: str, file_path: str = "") -> List[Document]:
        """
        分割 Markdown 文档 (两阶段分割 + 合并小片段)

        Args:
            content: Markdown 内容
            file_path: 文件路径 (用于元数据)

        Returns:
            List[Document]: 文档分片列表
        """
        if not content or not content.strip():
            logger.warning(f"Markdown 文档内容为空: {file_path}")
            return []

        try:
            # 第一阶段: 按标题分割
            md_docs = self.markdown_splitter.split_text(content)

            # 第二阶段: 按大小进一步分割
            docs_after_split = self.text_splitter.split_documents(md_docs)

            # 第三阶段: 合并太小的分片 (< 300字符)
            final_docs = self._merge_small_chunks(docs_after_split, min_size=300)

            # 添加文件路径元数据 + 切片标识
            file_name = Path(file_path).name
            for index, doc in enumerate(final_docs):
                doc.metadata["_source"] = file_path
                doc.metadata["_extension"] = ".md"
                doc.metadata["_file_name"] = file_name
                doc.metadata["chunk_id"] = _make_chunk_id(file_name, index)

            logger.info(f"Markdown 分割完成: {file_path} -> {len(final_docs)} 个分片")
            return final_docs

        except Exception as e:
            logger.error(f"Markdown 分割失败: {file_path}, 错误: {e}")
            raise

    def split_text(self, content: str, file_path: str = "") -> List[Document]:
        """
        分割普通文本文档

        Args:
            content: 文本内容
            file_path: 文件路径 (用于元数据)

        Returns:
            List[Document]: 文档分片列表
        """
        if not content or not content.strip():
            logger.warning(f"文本文档内容为空: {file_path}")
            return []

        try:
            # 直接使用递归字符分割器
            docs = self.text_splitter.create_documents(
                texts=[content],
                metadatas=[
                    {
                        "_source": file_path,
                        "_extension": Path(file_path).suffix,
                        "_file_name": Path(file_path).name,
                    }
                ],
            )

            # 写入切片标识 metadata["chunk_id"]，供多路检索去重使用
            file_name = Path(file_path).name
            for index, doc in enumerate(docs):
                doc.metadata["chunk_id"] = _make_chunk_id(file_name, index)

            logger.info(f"文本分割完成: {file_path} -> {len(docs)} 个分片")
            return docs

        except Exception as e:
            logger.error(f"文本分割失败: {file_path}, 错误: {e}")
            raise

    def split_document(self, content: str, file_path: str = "") -> List[Document]:
        """
        智能分割文档 (根据文件类型选择分割器)

        Args:
            content: 文档内容
            file_path: 文件路径

        Returns:
            List[Document]: 文档分片列表
        """
        if file_path.endswith(".md"):
            return self.split_markdown(content, file_path)
        else:
            return self.split_text(content, file_path)

    def _merge_small_chunks(
        self, documents: List[Document], min_size: int = 300
    ) -> List[Document]:
        """
        合并太小的分片

        Args:
            documents: 文档列表
            min_size: 最小分片大小 (字符数)

        Returns:
            List[Document]: 合并后的文档列表
        """
        if not documents:
            return []

        merged_docs = []
        current_doc = None

        for doc in documents:
            doc_size = len(doc.page_content)

            if current_doc is None:
                # 第一个文档
                current_doc = doc
            elif doc_size < min_size and len(current_doc.page_content) < self.chunk_size * 2:
                # 当前文档太小且合并后不会太大，则合并
                current_doc.page_content += "\n\n" + doc.page_content
                # 保留主文档的元数据
            else:
                # 保存当前文档，开始新文档
                merged_docs.append(current_doc)
                current_doc = doc

        # 添加最后一个文档
        if current_doc is not None:
            merged_docs.append(current_doc)

        return merged_docs

    # --------------------------------------------- 按次传参的切分入口（新增） ---

    def split_with_options(
        self,
        content: str,
        file_path: str = "",
        options: SplitOptions | None = None,
    ) -> tuple[List[Document], dict]:
        """按指定策略与参数切分文档（供上传接口调用，不影响全局默认行为）

        Args:
            content: 文档纯文本
            file_path: 文件路径（用于元数据）
            options: 切片参数，None 时用默认（通用切片 + config 里的默认大小）

        Returns:
            (分片列表, 统计信息 { chunk_count, parent_count, total_chars, avg_chars })

        Raises:
            ValueError: 参数非法（由接口层转 400）
        """
        opts = options or SplitOptions()
        opts.validate()

        if not content or not content.strip():
            logger.warning(f"文档内容为空: {file_path}")
            return [], {"chunk_count": 0, "parent_count": 0, "total_chars": 0, "avg_chars": 0}

        file_name = Path(file_path).name or "未命名文件"
        extension = Path(file_path).suffix or ".txt"

        if opts.strategy == STRATEGY_PARENT_CHILD:
            docs, parent_count = self._split_parent_child(content, opts)
        elif opts.strategy == STRATEGY_LOOP:
            docs = self._split_loop(content, opts)
            parent_count = 0
        elif opts.strategy == STRATEGY_CHAIN:
            docs = self._split_chain(content, opts)
            parent_count = 0
        else:
            docs = self._split_general(content, opts)
            parent_count = 0

        # 统一写元数据。chunk_id 规则必须与既有实现保持一致 —— multi_query_retriever
        # 的多路检索去重依赖它，改规则会让去重失效。
        for index, doc in enumerate(docs):
            doc.metadata["_source"] = file_path
            doc.metadata["_extension"] = extension
            doc.metadata["_file_name"] = file_name
            doc.metadata["chunk_id"] = _make_chunk_id(file_name, index)
            doc.metadata["strategy"] = opts.strategy

        total_chars = sum(len(d.page_content) for d in docs)
        stats = {
            "chunk_count": len(docs),
            "parent_count": parent_count,
            "total_chars": total_chars,
            "avg_chars": round(total_chars / len(docs)) if docs else 0,
        }
        logger.info(
            f"切分完成({opts.strategy}): {file_path} -> {len(docs)} 块"
            + (f"（父块 {parent_count} 个）" if parent_count else "")
            + f", size={opts.size}, overlap={opts.overlap}"
            + f", rules={[r.describe() for r in opts.effective_rules()]}"
        )
        return docs, stats

    def _build_chain_splitter(
        self,
        chunk_size: int,
        chunk_overlap: int,
        separators: list[str],
    ) -> RecursiveCharacterTextSplitter:
        """构造递归分隔符分割器（separators 为空列表时用 LangChain 默认链）"""
        return RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len,
            is_separator_regex=False,
            separators=separators or None,
            add_start_index=True,
        )

    def _rule_splitter(self, rule: SplitRule, chunk_size: int, chunk_overlap: int):
        """把一条分片依据映射到 LangChain 分割器

        - separator → 递归分隔符分割器，**只给这一个**分隔符：切不完的片段保持原样，
          正好交给链上的下一条规则继续切
        - heading   → Markdown 标题分割（纯文本没有标题时等价于不切，安全）
        - length    → 空分隔符的定长硬切（等价固定窗口）
        """
        if rule.type == RULE_SEPARATOR:
            return self._build_chain_splitter(chunk_size, chunk_overlap, [rule.value])
        if rule.type == RULE_HEADING:
            return MarkdownHeaderTextSplitter(
                headers_to_split_on=[(rule.value, f"h{len(rule.value)}")],
                strip_headers=False,
            )
        return CharacterTextSplitter(
            separator="",
            chunk_size=int(rule.value),
            chunk_overlap=0,
            length_function=len,
            strip_whitespace=False,
        )

    @staticmethod
    def _split_one(splitter, doc: Document) -> List[Document]:
        """用一条规则的切分器切开单个片段

        兼容两类分割器：`TextSplitter` 子类有 `split_documents`；
        而 `MarkdownHeaderTextSplitter` 只继承 `BaseDocumentTransformer`，只有
        `split_text()`（注意它返回的是 Document 列表，不是字符串列表）。
        """
        if hasattr(splitter, "split_documents"):
            return list(splitter.split_documents([doc]))
        return list(splitter.split_text(doc.page_content))

    def _apply_rules(
        self,
        content: str,
        rules: list[SplitRule],
        chunk_size: int,
        chunk_overlap: int,
        append_fallback: bool = False,
    ) -> List[Document]:
        """级联切分：按规则顺序逐层切，仍超过 chunk_size 的片段再交给下一条规则

        Args:
            content: 待切文本
            rules: 规则链（按顺序生效）
            chunk_size: 目标分片大小
            chunk_overlap: 相邻片段重复字符数
            append_fallback: 链走完后是否追加默认兜底链
                —— 「通用切片」为 True（保证超大块一定被切开），
                —— 「递归分隔符链」为 False（完全由用户掌控，允许超长块存在）

        Returns:
            List[Document]: 分片列表（metadata 由调用方统一补写）

        Raises:
            ValueError: 片段过大无法继续切分
        """
        if not rules:
            return [Document(page_content=content, metadata={})]

        # 链上全是 separator 时直接交给 LangChain 的递归分隔符链，语义等价且一步到位
        if all(rule.type == RULE_SEPARATOR for rule in rules):
            separators = [rule.value for rule in rules]
            if append_fallback:
                separators += [s for s in _FALLBACK_SEPARATORS if s not in separators]
            splitter = self._build_chain_splitter(chunk_size, chunk_overlap, separators)
            return [d for d in splitter.create_documents([content]) if d.page_content.strip()]

        # 混用了 heading / length：逐条级联
        current: List[Document] = [Document(page_content=content, metadata={})]
        for rule in rules:
            splitter = self._rule_splitter(rule, chunk_size, chunk_overlap)
            nxt: List[Document] = []
            for doc in current:
                text = doc.page_content
                if len(text) <= chunk_size:
                    nxt.append(doc)
                    continue
                if rule.type == RULE_LENGTH and len(text) > MAX_LOOP_CHARS:
                    raise ValueError(
                        f"片段过长（{len(text)} 字符），按固定长度硬切超出上限"
                        f"（{MAX_LOOP_CHARS}），请改用分隔符类依据"
                    )
                for piece in self._split_one(splitter, doc):
                    if piece.page_content.strip():
                        nxt.append(
                            Document(page_content=piece.page_content, metadata=dict(doc.metadata))
                        )
            current = nxt

        if append_fallback:
            fallback = self._build_chain_splitter(
                chunk_size, chunk_overlap, list(_FALLBACK_SEPARATORS)
            )
            settled: List[Document] = []
            for doc in current:
                if len(doc.page_content) <= chunk_size:
                    settled.append(doc)
                else:
                    settled.extend(
                        Document(page_content=p.page_content, metadata=dict(doc.metadata))
                        for p in self._split_one(fallback, doc)
                        if p.page_content.strip()
                    )
            current = settled

        return current

    def _strict_apply(
        self,
        content: str,
        rules: list[SplitRule],
        chunk_size: int,
        chunk_overlap: int = 0,
    ) -> List[Document]:
        """严格按分隔符切开：片段之间不做任何合并，chunk_size 只作为上限

        与 _apply_rules() 的差别只有一点：_apply_rules 走 LangChain 的
        RecursiveCharacterTextSplitter，会把相邻片段**累加到 chunk_size 才断开**
        （所以「按 \\n\\n 分段落」实际得到的是「攒够 N 字就断」）；
        本方法的规则链会对每个片段**无条件执行**（分隔符该切就切，与片段大小无关），
        切完原样保留，只有单个片段自己超过 chunk_size 时，才由兜底链继续切。

        因此在本路径下「父块大小 / 子块大小」的语义是**上限**，不是打包目标。

        仅父子切片使用：通用 / 递归链 / 固定窗口的策略语义就是「按大小打包」，
        不能被这里改掉。

        Args:
            content: 待切文本
            rules: 规则链（按顺序生效）
            chunk_size: 单个片段的上限（不是打包目标）
            chunk_overlap: 只有 length / 兜底链会用到的重复字符数

        Returns:
            List[Document]: 分片列表（metadata 由调用方统一补写）

        Raises:
            ValueError: 按固定长度硬切时片段过大
        """
        texts = [content.strip()] if content and content.strip() else []
        if not texts:
            return []

        for rule in rules:
            nxt: List[str] = []
            for text in texts:
                # 规则无条件执行：分隔符要真的把片段切开，跟片段大小无关。
                # 大小只决定「这个片段是否还需要交给后面的兜底链继续切」（见下）。
                if rule.type == RULE_LENGTH and len(text) > MAX_LOOP_CHARS:
                    raise ValueError(
                        f"片段过长（{len(text)} 字符），按固定长度硬切超出上限"
                        f"（{MAX_LOOP_CHARS}），请改用分隔符类依据"
                    )
                if rule.type == RULE_SEPARATOR:
                    nxt.extend(piece for piece in text.split(rule.value) if piece.strip())
                else:
                    splitter = self._rule_splitter(rule, chunk_size, chunk_overlap)
                    nxt.extend(
                        piece.page_content
                        for piece in self._split_one(
                            splitter, Document(page_content=text, metadata={})
                        )
                        if piece.page_content.strip()
                    )
            texts = nxt

        # 链走完后仍超限的，交给兜底链 —— 保证 chunk_size 始终是有效上限
        fallback = self._build_chain_splitter(chunk_size, 0, list(_FALLBACK_SEPARATORS))
        settled: List[str] = []
        for text in texts:
            if len(text) <= chunk_size:
                settled.append(text)
                continue
            settled.extend(
                piece.page_content
                for piece in self._split_one(fallback, Document(page_content=text, metadata={}))
                if piece.page_content.strip()
            )

        return [Document(page_content=t.strip(), metadata={}) for t in settled if t.strip()]

    def _split_general(self, content: str, opts: SplitOptions) -> List[Document]:
        """通用切片：用户规则链 + 自动追加默认兜底链"""
        rules = opts.effective_rules()
        if not rules:
            splitter = self._build_chain_splitter(opts.size, opts.overlap, [])
            return [d for d in splitter.create_documents([content]) if d.page_content.strip()]
        return self._apply_rules(
            content, rules, opts.size, opts.overlap, append_fallback=True
        )

    def _split_chain(self, content: str, opts: SplitOptions) -> List[Document]:
        """递归分隔符链：严格只用用户给的链，不追加任何兜底"""
        rules = opts.effective_rules()
        if not rules:
            raise ValueError("递归分隔符链至少要配置一条分片依据")
        return self._apply_rules(
            content, rules, opts.size, opts.overlap, append_fallback=False
        )

    def _split_loop(self, content: str, opts: SplitOptions) -> List[Document]:
        """循环切片：固定长度滑动窗口，步长 = 分片大小 − 重复大小，不看语义边界

        空分隔符会让 LangChain 先退化为逐字符切分、再按 chunk_size 合并，
        实测结果正好是固定窗口滑动（chunk_size=10/overlap=3 → 10,10,10,10,8 字符的相邻块）。
        """
        if len(content) > MAX_LOOP_CHARS:
            raise ValueError(
                f"循环切片单文件不超过 {MAX_LOOP_CHARS} 字符（当前 {len(content)}），请改用通用切片"
            )

        splitter = CharacterTextSplitter(
            separator="",
            chunk_size=opts.size,
            chunk_overlap=opts.overlap,
            length_function=len,
            strip_whitespace=False,
        )
        return splitter.create_documents([content])

    def _split_parent_child(
        self, content: str, opts: SplitOptions
    ) -> tuple[List[Document], int]:
        """父子切片：先按父块依据切大块，再按子块依据把每个父块切成小子块

        子块带 `parent_id` / `parent_content`，检索命中子块后可直接用父块全文补上下文，
        不依赖外部 docstore，进程重启也不丢（代价是父块正文在子块间重复存储）。
        重复大小在该策略下恒为 0（见 SplitOptions.overlap）。

        两级都走 `_strict_apply`（严格切分）：**在分隔符处断开、不跨片合并**，
        「父块大小 / 子块大小」只作为单个片段的上限。这样「按 \\n\\n 分段」拿到的
        就真的是段落边界，而不是「攒够 N 字就断」——后者会把某条正文和它后面的
        相似问句拆到两个块里。
        """
        # 父块依据：显式传了就用；只传了旧的单条 separator 也认（向后兼容）；都没有才给默认
        parent_rules = (
            opts.parent_rules or opts.effective_rules() or [SplitRule(RULE_SEPARATOR, "\n\n")]
        )
        child_rules = opts.effective_rules() or [SplitRule(RULE_SEPARATOR, "\n")]

        parents = self._strict_apply(content, parent_rules, opts.parent)
        if not parents:
            parents = [Document(page_content=content, metadata={})]

        children: List[Document] = []
        for p_index, parent in enumerate(parents):
            pieces = self._strict_apply(
                parent.page_content, child_rules, opts.size, opts.overlap
            )
            for child in pieces:
                child.metadata["parent_id"] = f"P{p_index:04d}"
                child.metadata["parent_index"] = p_index
                child.metadata["parent_content"] = parent.page_content
                children.append(child)
        return children, len(parents)


# 全局单例
document_splitter_service = DocumentSplitterService()
