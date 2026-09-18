"""文件上传接口模块"""

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.services.document_loader_service import SUPPORTED_EXTENSIONS
from app.services.document_splitter_service import (
    STRATEGY_GENERAL,
    SplitOptions,
    decode_separator,
    parse_rules,
)
from app.services.vector_index_service import vector_index_service
from loguru import logger

router = APIRouter()

# 文件上传后存储的路径
UPLOAD_DIR = Path("./uploads")
# 支持的文件类型（与文档加载服务保持一致）
ALLOWED_EXTENSIONS = SUPPORTED_EXTENSIONS
# 单个文件支持最大大小
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# 回传给前端的 chunk 预览上限：防止大文件把响应体和浏览器渲染打爆
MAX_PREVIEW_CHUNKS = 500
# 单个 chunk 正文回传上限（前端默认只展示前 200 字，展开时需要更多）
MAX_PREVIEW_CHARS = 2000


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    strategy: str = Form(STRATEGY_GENERAL),
    chunk_size: str | None = Form(None),
    chunk_overlap: str | None = Form(None),
    separator: str | None = Form(None),
    parent_size: str | None = Form(None),
    rules: str | None = Form(None),
    parent_rules: str | None = Form(None),
):
    """
    上传文件 → 建立向量索引 → 回传本次切出的 chunk 列表（仅供前端临时预览）

    切片参数全部可选，不传时行为与改造前完全一致（通用切片 + config 默认大小），
    因此既有的 `curl -F "file=@..."` 调用方式仍然可用。

    Args:
        file: 上传的文件
        strategy: general(通用) | chain(递归分隔符链) | loop(固定窗口) | parent_child(父子)
        chunk_size: 分片大小（字符），留空用默认
        chunk_overlap: 重复大小（字符），留空用默认；父子切片下恒为 0
        separator: 单条分片依据（向后兼容旧调用），支持 `\\n\\n` / `@@@` / `\\n` 字面量
        parent_size: 父子切片的父块大小（字符），留空用子块大小的 2 倍
        rules: 分片依据规则列表（JSON 字符串），形如
               `[{"type":"separator","value":"\\\\n\\\\n"},{"type":"heading","value":"##"}]`；
               类型可选 separator / heading / length。传了它就以它为准，`separator` 被忽略
        parent_rules: 父子切片专用的父块依据列表（JSON 字符串），格式同 rules

    Returns:
        JSONResponse: { code, message, data: { filename, file_path, size, indexed,
                        strategy, stats, chunks, total, truncated } }

    Raises:
        HTTPException: 400 参数非法 / 500 其它失败
    """
    try:
        # 1. 验证文件
        if not file.filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")

        # 2. 规范化文件名（去除空格，处理 Windows 上传的文件）
        safe_filename = _sanitize_filename(file.filename)

        # 3. 验证文件扩展名
        file_extension = _get_file_extension(safe_filename)
        if file_extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
            )

        # 4. 组装并校验切片参数（前置校验，避免进了嵌入阶段才发现参数不对）
        try:
            options = SplitOptions(
                strategy=(strategy or STRATEGY_GENERAL).strip(),
                chunk_size=_to_int(chunk_size, "分片大小"),
                chunk_overlap=_to_int(chunk_overlap, "重复大小"),
                separator=decode_separator(separator),
                parent_size=_to_int(parent_size, "父块大小"),
                rules=parse_rules(rules, "分片依据"),
                parent_rules=parse_rules(parent_rules, "父块依据"),
            )
            options.validate()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # 5. 创建上传目录
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        # 6. 保存文件
        file_path = UPLOAD_DIR / safe_filename

        # 如果文件已存在，先删除旧文件（实现覆盖更新）
        if file_path.exists():
            logger.info(f"文件已存在，将覆盖: {file_path}")
            file_path.unlink()

        # 读取并保存文件内容
        content = await file.read()

        # 验证文件大小
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件大小超过限制（最大 {MAX_FILE_SIZE} 字节）")

        file_path.write_bytes(content)

        logger.info(f"文件上传成功: {file_path}")

        # 7. 建立向量索引（索引失败不回滚已保存的文件，只把 indexed 标为 False）
        chunks: list[dict] = []
        stats: dict = {}
        indexed = True
        try:
            logger.info(f"开始为上传文件创建向量索引: {file_path}")
            documents, stats = vector_index_service.index_single_file(str(file_path), options)
            chunks, total, truncated = _to_preview_chunks(documents)
            logger.info(f"向量索引创建成功: {file_path}, 切出 {total} 块")
        except ValueError as e:
            # 切分阶段才暴露的参数问题（例如循环切片超长），按参数错误返回
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            indexed = False
            total = 0
            truncated = False
            logger.error(f"向量索引创建失败: {file_path}, 错误: {e}")

        # 8. 返回响应
        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success",
                "data": {
                    "filename": safe_filename,
                    "file_path": str(file_path),
                    "size": len(content),
                    "indexed": indexed,
                    "strategy": options.strategy,
                    "stats": stats,
                    "chunks": chunks,
                    "total": total,
                    "truncated": truncated,
                },
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}")


@router.post("/index_directory")
async def index_directory(directory_path: str = None):
    """
    索引指定目录下的所有文件

    Args:
        directory_path: 目录路径（可选，默认使用 uploads 目录）

    Returns:
        JSONResponse: 索引结果
    """
    try:
        logger.info(f"开始索引目录: {directory_path or 'uploads'}")

        # 执行索引
        result = vector_index_service.index_directory(directory_path)

        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success" if result.success else "partial_success",
                "data": result.to_dict(),
            },
        )

    except Exception as e:
        logger.error(f"索引目录失败: {e}")
        raise HTTPException(status_code=500, detail=f"索引目录失败: {e}")


def _to_preview_chunks(
    documents: list, limit: int = MAX_PREVIEW_CHUNKS
) -> tuple[list[dict], int, bool]:
    """把分片列表转成前端预览用的结构（只取前 limit 条，正文截断）

    Returns:
        (预览列表, 总块数, 是否被截断)
    """
    preview: list[dict] = []
    for index, doc in enumerate(documents):
        if index >= limit:
            break
        text = doc.page_content or ""
        preview.append(
            {
                "index": index,
                "chunk_id": doc.metadata.get("chunk_id", ""),
                "parent_id": doc.metadata.get("parent_id"),
                "chars": len(text),
                "content": text[:MAX_PREVIEW_CHARS],
                "cut": len(text) > MAX_PREVIEW_CHARS,
            }
        )
    return preview, len(documents), len(documents) > limit


def _to_int(value: str | None, label: str) -> int | None:
    """把表单里的字符串数字转成 int；空串/空白视为未填（返回 None）"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as e:
        raise ValueError(f"{label}必须是整数") from e


def _get_file_extension(filename: str) -> str:
    """
    获取文件扩展名

    Args:
        filename: 文件名

    Returns:
        str: 扩展名（小写，不含点）
    """
    parts = filename.rsplit(".", 1)
    if len(parts) == 2:
        return parts[1].lower()
    return ""


def _sanitize_filename(filename: str) -> str:
    """
    规范化文件名，去除空格和特殊字符

    Args:
        filename: 原始文件名

    Returns:
        str: 规范化后的文件名
    """
    # 去除空格
    sanitized = filename.replace(" ", "_")
    # 去除其他可能导致问题的字符
    for char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        sanitized = sanitized.replace(char, "_")
    return sanitized
