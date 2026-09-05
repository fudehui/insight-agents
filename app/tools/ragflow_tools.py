"""
RAGFlow 知识库工具模块

封装两个给 RAGFlow 子智能体使用的 LangChain 工具：
get_assistant_list 用于发现可用聊天助手及其绑定知识库，
create_ask_delete 用于创建临时会话、发起一次问题查询，并在查询后删除会话。

改造要点：
1. 所有外部请求经过 call_guard 防护层（超时 / 错误分类 / tool_end 埋点）；
   ragflow-sdk 的 post 不支持 timeout 参数，超时完全依赖 guard 的 wait_for。
2. create_ask_delete 属于不幂等操作（提问可能已到达服务端），不做重试。
3. 保留 RAGFlow 返回的 reference（文档名、页码、相似度、片段），
   与回答一起以结构化文本返回，构成可回溯的证据链。
4. 临时会话的删除放入 finally，修复流式解析中途异常时会话泄漏的问题。
"""

import json

from langchain_core.tools import tool
from ragflow_sdk import RAGFlow

from app.api import source_registry
from app.ragflow.rag_config import _load_ragflow_env
from app.tools.call_guard import ExternalCallError, guarded_call

# 模块级复用 RAGFlow 客户端，避免每次工具调用都重新初始化 SDK 对象
api_key, base_url = _load_ragflow_env()
ragflow_client = RAGFlow(api_key=api_key, base_url=base_url)

# 助手列表查询轻量快速；提问包含大模型生成，整体放宽到 90 秒
_LIST_TIMEOUT_S = 15.0
_ASK_TIMEOUT_S = 90.0
# 引用来源最多展示 5 个片段，避免证据链本身撑爆模型上下文
_MAX_REFERENCE_CHUNKS = 5
_REFERENCE_SNIPPET_LEN = 100


def _extract_page_number(positions) -> str:
    """
    从 chunk 的 positions 字段尽力提取页码

    RAGFlow 的 positions 形如 [[页码, x1, y1, x2, y2], ...]，
    不同版本格式有差异，任何无法解析的情况都返回空字符串
    """
    try:
        first = positions[0][0]
        if isinstance(first, int):
            return str(first)
    except (TypeError, IndexError):
        pass
    return ""


def format_answer_with_references(answer: str, reference: dict | None) -> str:
    """
    把 RAGFlow 回答和引用信息组装成带证据链的结构化文本

    :param answer: 流式拼接得到的完整回答
    :param reference: 流式响应中的 reference 字段（可能为 None 或缺少 chunks）
    :return: 回答正文 + 【引用来源】列表；无引用时明确标注待核实
    """
    if not isinstance(reference, dict):
        return f"{answer}\n\n【引用来源】RAGFlow 本次回答未返回引用信息，该结论请谨慎引用。"

    chunks = [c for c in (reference.get("chunks") or []) if isinstance(c, dict)]
    if not chunks:
        return f"{answer}\n\n【引用来源】RAGFlow 本次回答未返回引用信息，该结论请谨慎引用。"

    lines = []
    for index, chunk in enumerate(chunks[:_MAX_REFERENCE_CHUNKS], start=1):
        doc_name = chunk.get("document_name") or "未知文档"
        page = _extract_page_number(chunk.get("positions"))
        location = f"第{page}页，" if page else ""
        similarity = chunk.get("similarity")
        score = f"相似度 {float(similarity):.2f}，" if isinstance(similarity, (int, float)) else ""
        snippet = str(chunk.get("content") or "").replace("\n", " ").strip()
        if len(snippet) > _REFERENCE_SNIPPET_LEN:
            snippet = snippet[:_REFERENCE_SNIPPET_LEN] + "..."
        lines.append(f"{index}. 《{doc_name}》{location}{score}片段：{snippet}")

    hidden = len(chunks) - min(len(chunks), _MAX_REFERENCE_CHUNKS)
    if hidden > 0:
        lines.append(f"（另有 {hidden} 个引用片段未展示）")

    return f"{answer}\n\n【引用来源】\n" + "\n".join(lines)


# @tool 会把函数签名和 docstring 暴露给 DeepAgents，模型据此决定是否调用以及如何填参
@tool
async def get_assistant_list() -> str:
    """
    查询 RAGFlow 中有哪些聊天助手，以及每个助手关联了哪些知识库

    作用：让模型先了解“哪个助手能回答哪类内部文档问题”，再决定后续要向哪个助手提问。
    调用 create_ask_delete 之前，应先调用本工具确认助手名称。
    :return: 有助手时返回助手名称、功能介绍、关联知识库；无助手或异常时返回中文提示
    """

    def _do_list():
        # list_chats 查询的是 RAGFlow 的 Chat 层，不是 Dataset 层
        # Chat 负责对外问答，Dataset 只负责承载文档
        chat_list = ragflow_client.list_chats()
        if not chat_list:
            return "没有任何可用助手"

        # 把每个助手的名称、描述和绑定知识库拼成模型容易阅读的路由信息
        chat_info = ""
        for chat in chat_list:
            # 不同版本 SDK 字段可能为空，这里用 getattr 兼容没有绑定知识库的助手
            dataset_names = getattr(chat, "kb_names", []) or []
            chat_info += (
                f"助手名称:{chat.name};功能介绍：{chat.description}; "
                f"关联的知识库：{'、'.join(dataset_names)} \n"
            )
        return chat_info

    try:
        return await guarded_call(
            "get_assistant_list",
            {},
            _do_list,
            display_name="ragflow聊天助手列表查询工具：get_assistant_list",
            timeout_s=_LIST_TIMEOUT_S,
            max_retries=1,
            budgeted=True,
        )
    except ExternalCallError as e:
        return f"查询助手信息异常，无可用助手。异常信息：{e}"


def _ask_once(chat_name: str, question: str) -> str:
    """
    同步执行一次完整提问：定位助手 -> 建临时会话 -> 流式提问 -> 清理会话

    整个函数在 guard 的线程池中执行；无论流式解析是否抛异常，
    finally 都会删除临时会话，避免在 RAGFlow 侧堆积泄漏
    """
    # 先按名称找到 Chat 对象；真正提问时还需要在 Chat 下创建 Session
    chats = ragflow_client.list_chats(name=chat_name)
    use_chat = chats[0]

    # 每次工具调用只创建一个临时会话，避免多轮上下文污染当前问题
    session = use_chat.create_session(name="temp_session_ask")

    try:
        # SDK 暂未直接封装当前流式接口，这里通过底层 post 调用 Chat completions API
        response = ragflow_client.post(
            f"/chats/{use_chat.id}/completions",
            {
                "messages": [{"role": "user", "content": question}],
                "stream": True,
                "session_id": session.id,
            },
            stream=True,
        )
        result = ""
        reference = None
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue

            # RAGFlow 流式返回遵循 SSE 风格：每行以 data: 开头，[DONE] 表示结束
            line = line.removeprefix("data:").strip()
            if line == "[DONE]":
                break
            data = json.loads(line)
            chunk_data = data.get("data")
            if not isinstance(chunk_data, dict):
                continue

            # reference 会随流式片段重复下发，保留最后一份非空引用作为证据链
            ref = chunk_data.get("reference")
            if isinstance(ref, dict) and ref:
                reference = ref

            answer = chunk_data.get("answer")
            if answer:
                # 部分流式片段会返回“截至当前的完整答案”，部分会返回增量内容
                # 这里兼容两种情况，尽量避免重复拼接
                if answer.startswith(result):
                    result = answer
                elif not result.startswith(answer):
                    result += answer
    finally:
        # 清理失败只记录日志，绝不能掩盖提问本身的结果或异常
        try:
            use_chat.delete_sessions(ids=[session.id])
        except Exception as cleanup_error:
            print(f"[RAGFlow] 临时会话清理失败（可能已泄漏）: {cleanup_error}")

    # 登记证据链中的文档来源（不经过模型转述），供报告质量闸门检查引用
    if isinstance(reference, dict):
        for chunk in reference.get("chunks") or []:
            if isinstance(chunk, dict) and chunk.get("document_name"):
                source_registry.register_doc_source(
                    str(chunk["document_name"]),
                    _extract_page_number(chunk.get("positions")),
                )

    return format_answer_with_references(result, reference)


@tool
async def create_ask_delete(chat_name, question) -> str:
    """
    向某个 RAGFlow 聊天助手创建临时会话并完成一次提问

    注意：调用此工具之前，必须先调用 get_assistant_list，明确可用助手名称和助手能力边界。
    :param chat_name: 助手名称，必须来自 get_assistant_list 返回结果
    :param question: 本次提问的问题
    :return: 回答正文 + 【引用来源】列表（文档名、页码、相似度、片段）；异常时返回中文错误提示
    """
    try:
        return await guarded_call(
            "create_ask_delete",
            {"chat_name": chat_name, "question": question},
            lambda: _ask_once(chat_name, question),
            display_name="ragflow提问助手工具：create_ask_delete",
            timeout_s=_ASK_TIMEOUT_S,
            max_retries=0,
            budgeted=True,
        )
    except ExternalCallError as e:
        return f"提问失败，错误原因：{e}"


# if __name__ == "__main__":
#     # 本地调试入口：直接运行本文件可验证 RAGFlow API Key、服务地址和助手名称是否可用
#     # print(get_assistant_list.invoke({}))
#     print(
#         create_ask_delete.invoke(
#             {
#                 "chat_name": "电商行业助手",
#                 "question": "如果我是一个电商平台运营负责人，应该怎样制定 2026 年 AI 应用路线图？",
#             }
#         )
#     )
