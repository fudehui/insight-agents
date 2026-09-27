"""
ECharts 图表生成工具

供主智能体把结构化数据转成 ECharts option，返回 ```echarts 代码围栏文本，
模型将其原样嵌入 generate_markdown 的报告内容；前端 MarkdownRenderer 拦截
该围栏用 echarts 渲染成交互图表，PDF 链路按纯文本降级（见 docs/upgrade-plan.md
W7：SSR 需引入 Node sidecar 重依赖，图表 PDF 导出列二期）。

安全与健壮性约定（升级计划复审补充）：
- 只接受纯 JSON option——禁止函数体/Script 字符串，防止注入前端执行的脚本；
- 限制 option 体积，防止模型把巨型数据塞进报告拖垮渲染；
- 解析失败时返回面向模型的修正指引而不是抛异常。
"""

import json
from typing import Optional

try:
    from typing import Annotated
except ImportError:
    from typing_extensions import Annotated
from langchain_core.tools import tool

from app.api.monitor import monitor

# option 源文本上限：交互图表以汇总统计为主，32KB 足够容纳
# 数百个类目的 series，超出说明模型在搬运原始数据而非聚合结果
_MAX_OPTION_BYTES = 32 * 1024

# 字符串值中出现即拒绝的关键字（大小写不敏感）：可执行代码与脚本注入面
_FORBIDDEN_PATTERNS = ("function", "=>", "<script", "javascript:", "eval(")

# 围栏语言标记：前端 MarkdownRenderer 按该标记拦截渲染
CHART_FENCE_LANG = "echarts"


def _iter_strings(node):
    """深度优先遍历 option 中的所有字符串（dict 键与值、list 元素）"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_strings(item)


def validate_chart_option(option_json: str) -> tuple[Optional[dict], Optional[str]]:
    """
    校验模型提交的 ECharts option 源文本

    :return: (解析后的 option dict, None) 或 (None, 面向模型的错误说明)
    """
    if len(option_json.encode("utf-8")) > _MAX_OPTION_BYTES:
        return None, (
            f"错误：option 大小 {len(option_json.encode('utf-8')) // 1024}KB 超过 "
            f"{_MAX_OPTION_BYTES // 1024}KB 上限。图表只承载聚合统计结果，"
            "请压缩数据点（如只保留 TOP N、按月聚合），不要搬运原始明细。"
        )
    try:
        option = json.loads(option_json)
    except json.JSONDecodeError as exc:
        return None, (
            f"错误：option 不是合法 JSON（{exc}）。"
            "请提交纯 JSON 对象：属性名用双引号，禁止 JS 对象字面量写法"
            "（单引号、尾逗号、注释、函数）。"
        )
    if not isinstance(option, dict):
        return None, (
            f"错误：option 顶层必须是 JSON 对象（当前为 {type(option).__name__}），"
            "例如 {\"title\": {...}, \"xAxis\": {...}, \"yAxis\": {...}, \"series\": [...]}"
        )
    lowered = (s.lower() for s in _iter_strings(option))
    for text in lowered:
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern in text:
                return None, (
                    f"错误：option 中包含不允许的可执行片段（{pattern!r}）。"
                    "只接受纯数据 JSON——formatter 等回调请改用 ECharts 内置"
                    "字符串模板（如 '{b}: {c}'），不得内联函数。"
                )
    return option, None


@tool
def generate_chart(
    option_json: Annotated[str, "ECharts option 的完整 JSON 文本"],
    description: Annotated[Optional[str], "图表的简短说明（用于执行记录展示）"] = None,
) -> str:
    """
    生成可嵌入报告的交互式 ECharts 图表（柱状图/折线图/饼图等）

    提交一份 ECharts option 的 JSON 文本，校验通过后返回 ```echarts 代码围栏，
    将围栏原样写进 generate_markdown 的 content 即可在报告中渲染交互图表。
    注意：一份报告建议 1-2 张图；数据先在沙箱或 SQL 中聚合好再作图。
    :param option_json: ECharts option JSON（纯数据，禁止函数/脚本字符串）
    :param description: 本次图表的简短说明（可选）
    :return: ```echarts 围栏文本（原样嵌入报告）；校验失败时返回修正指引
    """
    monitor.report_tool(
        "图表生成工具",
        {
            "option大小(字符)": len(option_json),
            "说明": (description or "")[:80],
        },
    )

    option, error = validate_chart_option(option_json)
    if error:
        return error

    fence = f"```{CHART_FENCE_LANG}\n{json.dumps(option, ensure_ascii=False)}\n```"
    return (
        "图表校验通过。请将下面代码围栏原样嵌入报告正文（不要改动内容、"
        "不要包进其他代码块），并在图表前用一句话说明图表要传达的信息：\n\n"
        f"{fence}"
    )
