"""
上传文件读取工具

供主智能体读取用户在当前会话中上传的临时附件。工具会先通过 ContextVar
拿到本次 session_dir，再把模型传入的文件名解析到真实路径，支持文本、
Word、PDF 和 Excel 等常见格式。
"""

from pathlib import Path
from typing import Annotated
import os

from langchain_core.tools import tool

from app.api.context import get_session_context
from app.api.monitor import monitor
from app.utils.path_utils import resolve_path

# 文本类内容单次返回的字符上限：整份大文件进入上下文会挤占对话窗口，
# 超限时截断并提示模型用 offset 分段续读；Excel 已有 head+describe 收敛，不走此逻辑
_MAX_TEXT_CHARS = int(os.getenv("FILE_READ_MAX_CHARS", "30000"))

# 文档解析依赖按需导入：缺少某类依赖时，只影响对应文件格式，不影响工具整体注册
try:
    import docx
except ImportError:
    docx = None

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import pandas as pd
except ImportError:
    pd = None


def _limit_text(text: str, filename: str, offset: int) -> str:
    """
    按 offset 截取文本片段，超限时附上续读指引

    :param text: 待截断的全文
    :param filename: 模型传入的文件名，用于续读提示
    :param offset: 起始字符偏移
    :return: [offset, offset+上限) 区间的文本；仍有剩余时结尾追加续读提示
    """
    start = max(0, offset)
    total = len(text)
    if start >= total:
        return (
            f"offset {start} 已超出文件末尾（'{filename}' 共 {total} 个字符），"
            "请检查后续读位置。"
        )
    segment = text[start : start + _MAX_TEXT_CHARS]
    if start + _MAX_TEXT_CHARS >= total:
        if start > 0:
            segment = f"（从第 {start} 个字符继续）\n" + segment
        return segment
    return (
        segment + f"\n\n[文件内容未完] '{filename}' 共 {total} 个字符，本次返回第 "
        f"{start} 到 {start + len(segment)} 个字符。继续读取请再次调用本工具，"
        f"并传入 offset={start + len(segment)}。"
    )


@tool
def read_file_content(
    filename: Annotated[
        str, "要读取的文件名或路径（支持 .md, .docx, .pdf, .xlsx, .xls）"
    ],
    instruction: Annotated[
        str, "对提取内容的具体指令（例如：'提取摘要', '统计数据'）"
    ] = "提取全部内容",
    offset: Annotated[
        int, "字符偏移量：上一次返回提示'文件内容未完'时，传入提示中的 offset 值续读"
    ] = 0,
) -> str:
    """
    读取当前会话目录中的指定文件内容

    对于 Excel 文件，会自动提供数据统计信息（head 和 describe）。
    大文本文件单次最多返回约 3 万字符，若返回末尾提示"文件内容未完"，
    请使用提示中的 offset 值再次调用以读取后续内容。
    :param filename: 文件名或相对路径，通常由主智能体从上传文件列表中选择
    :param instruction: 模型传入的读取意图，用于监控展示，不改变底层解析逻辑
    :param offset: 起始字符偏移，用于分段读取超长文件
    :return: 文件文本内容、表格摘要，或中文错误提示
    """
    monitor.report_tool(
        "文件内容读取工具",
        {"filename": filename, "instruction": instruction, "offset": offset},
    )

    # 解析路径时优先约束在当前 session_dir 内；越界路径由 resolve_path 抛错拒绝
    session_dir = get_session_context()
    try:
        file_path = Path(resolve_path(filename, session_dir))
    except ValueError as e:
        return f"错误：{e}"

    if not file_path.exists():
        return f"错误：文件 '{filename}' 不存在 (解析路径: {file_path})。"

    # 根据文件后缀选择解析方式；未知后缀会先按 UTF-8 文本兜底读取
    ext = file_path.suffix.lower()

    try:
        if ext in [".md", ".txt"]:
            return _limit_text(file_path.read_text(encoding="utf-8"), filename, offset)

        elif ext == ".docx":
            if docx is None:
                return "错误：未安装 'python-docx' 库，无法读取 Word 文件。"
            # python-docx 读取段落文本，适合课程中的普通 Word 附件
            doc = docx.Document(str(file_path))
            full_text = [para.text for para in doc.paragraphs]
            return _limit_text("\n".join(full_text), filename, offset)

        elif ext == ".pdf":
            if pypdf is None:
                return "错误：未安装 'pypdf' 库，无法读取 PDF 文件。"
            # pypdf 按页提取文本，扫描件或图片型 PDF 可能无法提取有效文字
            reader = pypdf.PdfReader(str(file_path))
            text = "\n".join([page.extract_text() or "" for page in reader.pages])
            return _limit_text(text, filename, offset)

        elif ext in [".xlsx", ".xls"]:
            if pd is None:
                return "错误：未安装 'pandas' 库，无法读取 Excel 文件。"

            try:
                df = pd.read_excel(str(file_path))
            except Exception as e:
                return f"读取 Excel 失败: {str(e)}"

            # Excel 不直接返回全量数据，先给模型列名、预览和统计摘要，避免上下文过长
            result = [
                f"文件: {filename}",
                f"行数: {len(df)}, 列数: {len(df.columns)}",
                f"列名: {', '.join(df.columns.astype(str))}",
                "\n[前5行数据预览]:",
                df.head().to_string(index=False),
                "\n[统计描述]:",
                df.describe().to_string(),
            ]
            return "\n".join(result)

        else:
            try:
                return _limit_text(
                    file_path.read_text(encoding="utf-8"), filename, offset
                )
            except UnicodeDecodeError:
                return f"错误：不支持的文件格式 '{ext}'，且无法作为文本读取。"

    except Exception as e:
        return f"读取文件出错: {str(e)}"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证 Markdown、PDF 等上传文件读取效果
    def get_session_context():
        return "./examples/test_docs"

    md_path = "sub_dir/测试文件.md"
    pdf_path = "sub_dir/测试文件.pdf"

    result = read_file_content.invoke({"filename": md_path})
    print("===== 读取MD文件结果 =====")
    print(result)

    result_pdf = read_file_content.invoke(
        {"filename": pdf_path, "instruction": "提取PDF文字"}
    )
    print("\n===== 读取PDF文件结果 =====")
    print(result_pdf)
