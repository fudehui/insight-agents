"""
Markdown 文件生成工具

供主智能体把最终整理后的内容写入当前会话工作目录。工具会把模型传入的
filename/path 交给 resolve_path 统一解析，避免模型直接操作真实绝对路径。

本工具同时是报告交付前的质量闸门：任务登记过网络/RAGFlow 来源但报告
缺少引用时拒绝写入，并把来源登记表的完整清单喂回模型要求补写；两次
拒绝后兜底自动附加参考来源章节，保证任务能交付且来源不缺失。
"""

from pathlib import Path

try:
    from typing import Annotated
except ImportError:
    from typing_extensions import Annotated
from langchain_core.tools import tool

from app.api import source_registry
from app.api.context import get_session_context
from app.api.monitor import monitor
from app.utils.path_utils import resolve_path

# 闸门最多退回模型的次数；达到次数后自动附注来源放行，避免消耗步数空转
_MAX_GATE_REJECTIONS = 2

# 报告中存在来源章节的关键词（RAGFlow 等无链接来源的引用检查依据）
_SOURCE_SECTION_KEYWORDS = ("参考来源", "引用来源", "数据来源", "来源清单")


def _has_web_citation(content: str) -> bool:
    """报告是否包含至少一个网络链接引用"""
    return "http://" in content or "https://" in content


def _has_source_section(content: str) -> bool:
    """报告是否包含来源章节或来源说明文字"""
    return any(keyword in content for keyword in _SOURCE_SECTION_KEYWORDS)


def _build_gate_rejection_message(missing: list[str]) -> str:
    """构造退回模型的拒绝信息，附上来源登记表的完整清单供直接使用"""
    manifest = source_registry.format_source_manifest()
    return (
        f"报告缺少来源引用（{'、'.join(missing)}），本次未写入文件。\n"
        "请按要求补充后重新调用 generate_markdown：\n"
        "1. 正文引用外部信息的位置标注来源编号（如 [1]）；\n"
        "2. 文末添加【参考来源】章节，网络来源写成'编号. [标题](URL)'。\n"
        "以下是本次任务收集到的全部来源，请直接使用：\n"
        f"{manifest}"
    )


@tool
def generate_markdown(
    content: Annotated[str, "要写入Markdown文档的文本内容"],
    filename: Annotated[str, "Markdown文档的文件名（不包含扩展名或包含.md）"],
    path: Annotated[str, "文件保存的绝对路径"] = "",
):
    """
    根据提供的文本内容生成 Markdown 文件

    注意：若本次任务收集过网络或知识库来源，报告中必须包含来源引用
    （正文标注来源编号，文末有【参考来源】章节），否则文件会被拒绝写入。
    :param content: 要写入 Markdown 文档的完整文本
    :param filename: 输出文件名，缺少 .md 后缀时会自动补全
    :param path: 可选保存路径；通常由运行时工作目录指令约束为相对路径
    :return: 文件生成结果说明；被质量闸门拦截时返回补充来源的具体要求
    """
    # 埋点只记录长度和预览：整份 content 会撑爆事件日志和前端展示
    monitor.report_tool(
        "Markdown文档生成工具",
        {"内容长度(字符)": len(content), "内容预览": content[:100]},
    )

    # 质量闸门：先于任何文件操作执行
    sources = source_registry.get_task_sources()
    missing: list[str] = []
    if sources["web"] and not _has_web_citation(content):
        missing.append("网络来源链接")
    if sources["docs"] and not _has_source_section(content):
        missing.append("RAGFlow 文档来源标注")

    if missing:
        rejections = source_registry.count_gate_rejection()
        if rejections < _MAX_GATE_REJECTIONS:
            return _build_gate_rejection_message(missing)

        # 多次退回仍缺引用：兜底自动附注来源清单后放行，保证任务可交付
        manifest = source_registry.format_source_manifest()
        content = content.rstrip() + "\n\n## 参考来源（系统自动附注）\n\n" + manifest

    if not filename.endswith(".md"):
        filename += ".md"

    # session_dir 由 run_deep_agent 写入 ContextVar，保证文件写入当前会话工作目录
    session_dir = get_session_context()

    # 先把模型传入的 path/filename 合成一个逻辑路径，再交给 resolve_path 做统一清洗
    if path and path != ".":
        full_input_path = str(Path(path) / filename)
    else:
        full_input_path = filename
    # resolve_path 会拒绝越出会话目录的路径（绝对路径、../ 穿越等）
    try:
        full_path_str = resolve_path(full_input_path, session_dir)
    except ValueError as e:
        return f"错误：{e}"
    file_path = Path(full_path_str)

    parent_dir = file_path.parent

    try:
        # 允许模型指定 session_dir 下的子目录；不存在时自动创建
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)

        file_path.write_text(content, encoding="utf-8")

        if missing:
            return (
                f"Markdown文件 '{file_path}' 已生成。注意：报告原始内容仍缺少来源引用，"
                "已在文末自动附注【参考来源】章节，请提示用户核对引用位置。"
            )
        return f"Markdown文件 '{file_path}' 已成功生成并保存。"
    except Exception as e:
        print(f"[MarkdownTool] 文件写入失败: {e}")
        return f"生成Markdown文件失败: {str(e)}"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证 Markdown 写入和路径解析效果
    def get_session_context():
        return "./examples/test_docs"

    test_content = "# 测试文档\n这是 Markdown 生成工具的本地测试内容"
    test_filename = "测试文件"
    test_path = "sub_dir"

    print("===== 开始测试：Markdown 文件生成 =====")
    result = generate_markdown.invoke(
        {"content": test_content, "filename": test_filename, "path": test_path}
    )

    print(f"\n调用结果：{result}")
    if "已成功生成" in result or "已生成" in result:
        file_path = Path(result.split("'")[1])
        print(
            f"验证结果：文件 {file_path} {'存在' if file_path.exists() else '不存在'}"
        )
