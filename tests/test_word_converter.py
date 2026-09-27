"""
Markdown 转 PDF 模块（app/utils/word_converter.py）的单元测试

覆盖：Markdown 元素解析（标题/列表/表格/代码块/行内标记/空文档）、
异常输入（源文件不存在、reportlab 缺失兜底、构建后未产出文件）、
中文字体注册的成功与异常分支，以及一次真实生成 PDF 的端到端链路
"""

import importlib
import sys
from pathlib import Path

from reportlab.platypus import Paragraph, Preformatted, Spacer, Table

from app.utils import word_converter as wc


def _story(md_content: str) -> list:
    """按真实转换流程构建 story：先注册中文字体，再解析 Markdown"""
    wc._register_fonts()
    styles = wc._build_styles()
    return wc._markdown_to_story(md_content, styles)


def _paragraphs(story: list) -> list[Paragraph]:
    return [el for el in story if isinstance(el, Paragraph)]


# ---------------------------------------------------------------------------
# 行内标记与样式
# ---------------------------------------------------------------------------


def test_format_inline_bold_and_code():
    assert wc._format_inline("**粗体**") == "<b>粗体</b>"
    assert wc._format_inline("`代码`") == '<font name="Courier">代码</font>'
    assert (
        wc._format_inline("**粗体**和`代码`")
        == '<b>粗体</b>和<font name="Courier">代码</font>'
    )


def test_format_inline_escapes_html_specials():
    assert wc._format_inline("a<b>c & d") == "a&lt;b&gt;c &amp; d"
    # 无任何标记的文本仅做转义、保持原样
    assert wc._format_inline("普通文本") == "普通文本"


def test_build_styles_contains_all_document_styles():
    styles = wc._build_styles()
    assert set(styles) == {"body", "h1", "h2", "h3", "code"}
    assert styles["body"].fontName == "STSong-Light"
    assert styles["code"].fontName == "Courier"
    assert styles["h1"].fontSize == 22
    assert styles["h3"].fontSize == 14


# ---------------------------------------------------------------------------
# 标题 / 列表解析
# ---------------------------------------------------------------------------


def test_parse_heading_levels():
    assert wc._parse_heading("# 一级") == (1, "一级")
    assert wc._parse_heading("## 二级") == (2, "二级")
    assert wc._parse_heading("###### 六级") == (6, "六级")


def test_parse_heading_rejects_non_heading_lines():
    assert wc._parse_heading("没有井号的文本") is None
    assert wc._parse_heading("#无空格不算标题") is None
    assert wc._parse_heading("#") is None
    assert wc._parse_heading("") is None


def test_parse_bullet_variants():
    assert wc._parse_bullet("- 项目") == "项目"
    assert wc._parse_bullet("* 项目") == "项目"
    assert wc._parse_bullet("-无空格") is None
    assert wc._parse_bullet("普通段落") is None


# ---------------------------------------------------------------------------
# 表格解析
# ---------------------------------------------------------------------------


def test_is_table_start_detection():
    lines = ["| 甲 | 乙 |", "| --- | --- |", "| 1 | 2 |"]
    assert wc._is_table_start(lines, 0) is True
    # 分隔符行本身不作为表格起点
    assert wc._is_table_start(lines, 1) is False
    # 当前行没有竖线
    assert wc._is_table_start(["普通文本", "--- | ---"], 0) is False
    # 已经是最后一行，没有下一行可作分隔符
    assert wc._is_table_start(["| 甲 |"], 0) is False


def test_is_table_start_accepts_separator_without_edge_pipes():
    assert wc._is_table_start(["甲 | 乙", "--- | ---"], 0) is True
    # 分隔符短横线数量不足（至少 3 个）时不识别为表格
    assert wc._is_table_start(["甲 | 乙", "-- | --"], 0) is False


def test_collect_table_collects_consecutive_rows():
    lines = ["| 甲 | 乙 |", "| --- | --- |", "| 1 | 2 |", "后续文本"]
    rows, next_index = wc._collect_table(lines, 0)
    assert rows == [["甲", "乙"], ["1", "2"]]
    assert next_index == 3


def test_split_table_row_strips_cells():
    assert wc._split_table_row("| 甲 | 乙 |") == ["甲", "乙"]
    assert wc._split_table_row("a|b") == ["a", "b"]


# ---------------------------------------------------------------------------
# story 构建主流程
# ---------------------------------------------------------------------------


def test_markdown_to_story_headings_map_to_level_styles():
    story = _story("# 一级\n## 二级\n### 三级\n#### 四级")
    assert [p.text for p in _paragraphs(story)] == ["一级", "二级", "三级", "四级"]
    assert [p.style.name for p in _paragraphs(story)] == [
        "Heading1",
        "Heading2",
        "Heading3",
        "Heading3",
    ]


def test_markdown_to_story_joins_paragraph_lines_and_blank_lines():
    story = _story("第一段第一行\n第一段第二行\n\n第二段")
    assert isinstance(story[0], Paragraph)
    assert story[0].text == "第一段第一行 第一段第二行"
    assert isinstance(story[1], Spacer)
    assert isinstance(story[2], Paragraph)
    assert story[2].text == "第二段"


def test_markdown_to_story_empty_document():
    assert _story("") == []


def test_markdown_to_story_bullets():
    story = _story("- 项目甲\n* 项目乙")
    assert [p.text for p in _paragraphs(story)] == ["• 项目甲", "• 项目乙"]


def test_markdown_to_story_code_block_closed_and_unclosed():
    closed = _story("```python\nprint('a')\nprint('b')\n```")
    assert len(closed) == 1
    assert isinstance(closed[0], Preformatted)
    assert closed[0].lines == ["print('a')", "print('b')"]

    # 未闭合围栏一直读到文档结尾
    unclosed = _story("```\nline1\nline2")
    assert isinstance(unclosed[0], Preformatted)
    assert unclosed[0].lines == ["line1", "line2"]


def test_markdown_to_story_flushes_paragraph_before_code_block():
    story = _story("前置文字\n```python\nx=1\n```")
    assert isinstance(story[0], Paragraph)
    assert story[0].text == "前置文字"
    assert isinstance(story[1], Preformatted)


def test_markdown_to_story_table_with_short_row_padded():
    story = _story("| 甲 | 乙 |\n| --- | --- |\n| 短行 |")
    tables = [el for el in story if isinstance(el, Table)]
    assert len(tables) == 1
    cell_values = tables[0]._cellvalues
    assert len(cell_values) == 2
    assert [cell.text for cell in cell_values[0]] == ["甲", "乙"]
    # 缺列的数据行补空单元格，避免 ReportLab 建表报错
    assert [cell.text for cell in cell_values[1]] == ["短行", ""]


# ---------------------------------------------------------------------------
# 字体注册
# ---------------------------------------------------------------------------


def test_register_fonts_real_call_is_idempotent():
    """真实注册内置中文 CID 字体；重复注册不应报错"""
    wc._register_fonts()
    wc._register_fonts()


def test_register_fonts_swallows_registration_failure(monkeypatch):
    """字体注册失败被静默吞掉，不阻断转换流程"""
    calls = []

    def _raise(font):
        # pdfmetrics.registerFont 收到的是字体对象而非名称字符串
        calls.append(getattr(font, "name", str(font)))
        raise RuntimeError("字体注册失败")

    monkeypatch.setattr(wc.pdfmetrics, "registerFont", _raise)

    wc._register_fonts()

    assert calls == ["STSong-Light"]


# ---------------------------------------------------------------------------
# convert_md_to_pdf 入口
# ---------------------------------------------------------------------------


def test_convert_md_to_pdf_generates_real_pdf(tmp_path):
    """端到端：真实生成 PDF，文件存在、非空且为 PDF 头"""
    md_path = tmp_path / "行业报告.md"
    md_path.write_text(
        "# 行业分析报告\n\n"
        "## 核心结论\n\n"
        "**支付转化**持续提升，`实时风控`需要加强。\n\n"
        "- 建议一\n- 建议二\n\n"
        "| 指标 | 结论 |\n| --- | --- |\n| 转化率 | 提升 |\n\n"
        "```python\nprint('报告')\n```\n",
        encoding="utf-8",
    )
    # 输出目录不存在时应自动创建
    pdf_path = tmp_path / "nested" / "行业报告.pdf"

    result = wc.convert_md_to_pdf(md_path, pdf_path)

    assert result == f"成功转换: {pdf_path}"
    assert pdf_path.exists()
    content = pdf_path.read_bytes()
    assert len(content) > 0
    assert content.startswith(b"%PDF")


def test_convert_md_to_pdf_missing_source_file(tmp_path):
    """源 Markdown 不存在时不抛异常，统一返回转换失败说明"""
    pdf_path = tmp_path / "out.pdf"

    result = wc.convert_md_to_pdf(tmp_path / "no_such.md", pdf_path)

    assert result.startswith("转换失败")
    assert not pdf_path.exists()


def test_convert_md_to_pdf_returns_hint_when_file_not_written(tmp_path, monkeypatch):
    """doc.build 未产出文件时返回“转换完成但未生成文件”提示"""

    class _SilentDocTemplate:
        """模拟构建后没有真实写出 PDF 文件的场景"""

        def __init__(self, *_args, **_kwargs):
            pass

        def build(self, _story):
            pass

    monkeypatch.setattr(wc, "SimpleDocTemplate", _SilentDocTemplate)

    md_path = tmp_path / "doc.md"
    md_path.write_text("# 标题\n正文", encoding="utf-8")
    pdf_path = tmp_path / "doc.pdf"

    result = wc.convert_md_to_pdf(md_path, pdf_path)

    assert result == f"转换完成但未生成文件: {pdf_path}"
    assert not pdf_path.exists()


def test_convert_md_to_pdf_without_reportlab_dependency():
    """reportlab 缺失时走 ImportError 兜底：转换入口直接返回安装提示"""
    reportlab_modules = [
        name for name in list(sys.modules) if name.split(".")[0] == "reportlab"
    ]
    saved = {name: sys.modules[name] for name in reportlab_modules}
    try:
        # sys.modules 里置 None 会让 import 语句抛 ImportError，模拟未安装
        for name in reportlab_modules:
            sys.modules[name] = None
        importlib.reload(wc)
        assert wc.SimpleDocTemplate is None
        assert (
            wc.convert_md_to_pdf(Path("a.md"), Path("a.pdf"))
            == "缺少依赖库，请安装 reportlab"
        )
    finally:
        # 恢复真实依赖并重载，保证模块回到可正常转换的状态
        sys.modules.update(saved)
        importlib.reload(wc)

    assert wc.SimpleDocTemplate is not None


# ---------------------------------------------------------------------------
# echarts 围栏降级（W7：PDF 链路无浏览器内核，交互图表输出占位说明）
# ---------------------------------------------------------------------------


def test_markdown_to_story_echarts_fence_becomes_placeholder():
    story = _story('图表：\n\n```echarts\n{"series": [{"type": "bar"}]}\n```\n')

    texts = [p.text for p in _paragraphs(story)]
    assert "此处为交互式图表" in texts[1]
    # option JSON 不应原样进入 PDF
    assert not any('"series"' in t for t in texts)


def test_markdown_to_story_plain_code_fence_still_preformatted():
    story = _story("```\nprint('hello')\n```\n")

    pres = [el for el in story if isinstance(el, Preformatted)]
    assert len(pres) == 1
    assert "print('hello')" in "\n".join(pres[0].lines)
