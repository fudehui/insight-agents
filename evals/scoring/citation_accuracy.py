"""
引用准确率评分模块（P0-1 评分三件套之二；DeepEval 属 W3，不在本模块）

输入契约
--------
- report_md：run_eval 产出的单 case JSON（``evals/results/{run_id}/case_{case_id}.json``）
  中的 ``report_md`` 字段，即 generate_markdown 写出的报告原文。
- sources：同一 case JSON 中的 ``sources`` 字段，元素结构
  ``{"type", "title", "url_or_ref"}``，由 run_eval 从
  ``source_registry.get_task_sources()``（app/api/source_registry.py）扁平化而来：
  web=[{"title","url"}] → type="web"、url_or_ref=URL；
  docs=[{"doc","page"}] → type="docs"、url_or_ref=文档名；
  sql=[str] → type="sql"、url_or_ref=SQL 原文。

引用格式取证（2026-09-13，以代码与真实报告为准）
------------------------------------------------
1. 正文引用标记是方括号数字，如 ``[1]``、``[2][4]``：app/prompt/prompts.yml
   第 39-44 行要求"标注对应的来源编号（如 [1]）"；app/tools/markdown_tools.py
   的引用闸门拒绝信息同样以 ``[1]`` 示例。
2. 文末有【参考来源】章节，逐条 ``编号. ...``：
   - 网络来源：``1. [标题](URL)``（真实报告 app/output/session_2037c502/药品行业分析报告.md
     即为 "## 【参考来源】" + 全来源统一编号 1..17，末条为无链接的数据库来源文字；
     session_1c6af65d/2026年新能源汽车行业趋势报告.md 正文中出现 [1][3]、[2][4] 等标记）；
   - RAGFlow：``1. 《文档名》第N页``；数据库：``1. 查询说明文字``（无链接）。
3. 闸门两次拒绝后的兜底路径会自动追加 "## 参考来源（系统自动附注）" +
   format_source_manifest() 渲染的分类型清单（网络来源：/RAGFlow 文档来源：/
   数据库查询记录：，各类型从 1 重新编号）。
4. 据此确定的抽取规则：正文抽 ``[^N]``、``[N]``、``【N】`` 三种标记（N 为 1-3 位
   数字，避免把 "[2026]" 这类方括号年份误判为引用；跳过 "[1]: url" 链接定义行）；
   参考来源章节按"标题式行首"关键词（参考来源/引用来源/数据来源/来源清单，
   同 markdown_tools._SOURCE_SECTION_KEYWORDS，但要求行首为标题写法）定位，
   标题行之后只解析 ``N. ...`` 条目，不再抽正文标记。

评分口径
--------
- total_citations：正文引用标记总数（含重复）；unique_cited：去重后的编号数。
- matched：能对应到登记来源的唯一编号数。对应规则：
  a) 优先用参考来源章节的 "编号 → 条目(标题/URL)" 解析后与 sources 比对：
     URL 型来源按 URL 归一化（去首尾空白与末尾 "/"，大小写敏感）全等，
     无链接时退化按标题包含；docs/sql 等文本型来源按归一化文本包含（≥4 字）；
  b) 编号在参考章节无条目（或报告没有参考章节）时，退化为按位置匹配
     sources[N-1]——真实报告采用全来源统一编号，与扁平 sources 的
     web→docs→sql 顺序一致；该假设是兜底口径，已在测试中固定。
- accuracy = matched / unique_cited（0-1 浮点数）；unique_cited=0 时取 0.0。
- 边界行为：
  - report_md 为 None 或全空白 → 抛 ValueError（completed case 不应产出空报告，
    空报告视为上游数据缺陷，宁可显式暴露也不静默给 0 分）；
  - sources 为空 → matched=0、accuracy=0.0，unmatched_citations 收录全部唯一
    引用编号，uncited_sources 为空列表（没有登记来源，谈不上"未引用"）；
  - 报告无引用 → accuracy=0.0 且 matched=0（引用覆盖率 0 是真实结果）。
- v2 口径（基线重算迭代，2026-09-27）：v1 的两个已知局限已修复——
  1) 参考条目"标题 - URL"纯文本写法（非 Markdown 链接）此前 url 字段为空、
     URL 从未参与比对，现从条目文本抽取裸 URL 参与；
  2) URL 归一化升级：百分号解码统一（模型抄中文原文、登记侧 percent-encoded
     属同一资源）、fragment/跟踪参数（utm_* 等）剔除、scheme/host 小写、
     查询参数排序；单侧无查询串时退化 host+path 比对（抄写丢参数视为同一
     资源），两侧都带查询串则必须归一化全等（防 ?id=123 vs ?id=456 误合）。
  3) SQL 来源由"条目包含 SQL 原文"扩展为双路径：SQL 涉及的表名命中条目
     文本即通过（模型写报告必然转述 SQL，原文包含几乎不可能命中）；表名
     未出现时保留原有文本包含路径（条目直接引用 SQL 原文的兜底清单场景）。
  同一批基线报告重算：42/93（45.2%）→ 74/93（79.6%）；救回的均为形式差异，
  真失配（模型张冠李戴的引用）按 v1 同样扣分。
- 已知局限（如实记录）：中文路径 URL 的 percent-decode 后比对依赖双方编码
  形态可互推，IDN 域名不做 punycode 归一；条目内 URL 被模型截断成短链时
  host+path 不等仍判未命中（短链与原链无法可靠互推）。

与 docs/evaluation-metric-template.md 的对应
--------------------------------------------
- 指标 3"引用覆盖率"（报告实际引用的来源数 ÷ 登记来源数）可用本模块结果计算：
  matched ÷ len(sources)，uncited_sources 即差集明细；
- 本模块另产出 P0-1 自建的"引用准确率"：报告引用中指向登记来源的比例，对应
  手工基线备注"报告 36 个引用 URL 中 35 个在登记来源内"的口径。
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

# 与 app/tools/markdown_tools.py 的 _SOURCE_SECTION_KEYWORDS 同款关键词，但
# 限定"标题式行首"：可选 Markdown 修饰符（#/ */-/>）后紧跟关键词（可带【】包裹）。
# 不能用裸子串匹配——正文散文提及"参考来源"三个字（如"此处编号在参考来源中
# 并不存在 [5]"）会把参考章节起点误判到正文中间。真实标题形如
# "## 【参考来源】"、"*【参考来源】*"、"## 参考来源（系统自动附注）"
_SECTION_HEADING_RE = re.compile(r"^\s*[#>*\-\s]*[【\[]?\s*(?:参考来源|引用来源|数据来源|来源清单)")

# 正文引用标记：[^1]（脚注式）、[1]（主格式，见 prompts.yml 与真实报告）、【1】（全角）
# 数字限 1-3 位，避免把 "[2026]" 等方括号年份误判为引用
_CITATION_RE = re.compile(r"\[\^?(\d{1,3})\]|【(\d{1,3})】")

# Markdown 链接定义行 "[1]: https://..." 不是正文引用
_LINK_DEF_RE = re.compile(r"^\s*\[\^?\d{1,3}\]:")

# 参考来源条目行："1. [标题](URL)" / "3. 《文档名》第3页" / "17. 说明文字"
_REF_ENTRY_RE = re.compile(r"^\s*(\d{1,3})\s*[.、)）]\s*(.+?)\s*$")

# 条目内的 Markdown 链接 "[标题](URL)"（URL 内含括号的场景不支持，如实记录）
_MD_LINK_RE = re.compile(r"^\[([^\]]*)\]\((\S+?)\)$")

# 文本包含匹配的最短长度下限，避免 "AI" 这类超短标题造成误命中
_MIN_CONTAIN_LEN = 4


def _split_report(report_md: str) -> tuple[list[str], str]:
    """
    把报告切分为（正文行列表, 参考来源章节文本）

    以第一处命中标题式关键词行（_SECTION_HEADING_RE，如 "## 【参考来源】"、
    "**【参考来源】**"）为分界：该行及之前属于正文，之后全部视为参考来源
    章节。真实报告的参考来源固定在文末；正文散文提及"参考来源"但不在行首
    标题位的行不会误触发分界。
    """
    lines = report_md.splitlines()
    for idx, line in enumerate(lines):
        if _SECTION_HEADING_RE.match(line):
            return lines[:idx], "\n".join(lines[idx + 1 :])
    return lines, ""


def extract_citations(report_md: str) -> list[str]:
    """
    从报告正文抽取引用标识（编号字符串列表）

    规则（详见模块 docstring"引用格式取证"）：
    - 匹配 [1] / [^1] / 【1】，编号取 1-3 位数字；
    - 只扫描参考来源章节之前的正文（章节里的 "1. [标题](URL)" 是来源清单，
      不是引用标记）；
    - 跳过 "[1]: url" 链接定义行；
    - 按出现顺序返回，保留重复（如 "[1][1]" → ["1", "1"]），由调用方按需去重：
      total_citations=len(result)，unique_cited=len(set(result))。
    - report_md 为空/None 时返回 []（纯函数，宽松处理；严格校验在
      score_citations 中做）。
    """
    if not report_md:
        return []
    body_lines, _section = _split_report(str(report_md))
    markers: list[str] = []
    for line in body_lines:
        if _LINK_DEF_RE.match(line):
            continue
        for match in _CITATION_RE.finditer(line):
            markers.append(match.group(1) or match.group(2))
    return markers


def _parse_reference_entries(section_text: str) -> dict[str, list[dict]]:
    """
    解析参考来源章节为 {编号: [{"title","url","text"}, ...]}

    条目行 "N. ..."：Markdown 链接拆出 title/url，其余整行作为 text
    （覆盖《文档名》第N页、SQL 说明等无链接条目）。
    同一编号可能出现多条（闸门兜底清单按类型分别从 1 编号），因此值为列表。
    """
    entries: dict[str, list[dict]] = {}
    for line in section_text.splitlines():
        entry_match = _REF_ENTRY_RE.match(line)
        if not entry_match:
            continue
        number, rest = entry_match.group(1), entry_match.group(2)
        link_match = _MD_LINK_RE.match(rest)
        if link_match:
            entry = {"title": link_match.group(1).strip(), "url": link_match.group(2).strip(), "text": link_match.group(1).strip()}
        else:
            entry = {"title": "", "url": "", "text": rest}
        entries.setdefault(number, []).append(entry)
    return entries


def _extract_bare_url(text: str) -> str:
    """
    从参考条目纯文本中抽取裸 URL

    真实报告大量条目是"标题 - URL"的纯文本写法（非 Markdown 链接），
    _parse_reference_entries 解析后 url 字段为空，导致 URL 明明相同
    却从未参与比对（基线批次 45.2% 的主要失配来源）。此处补抽取，
    抽不到返回空串
    """
    match = _BARE_URL_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,;、。;.")


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "spm", "fr",
}


def _norm_url(url: str) -> str:
    """
    URL 归一化（v2 口径）：百分号解码统一、fragment 与跟踪参数剔除、
    scheme/host 小写、默认端口去除、查询参数排序、末尾 "/" 去除

    真实失配样本：模型抄写中文原文 URL、登记侧为 percent-encoded 形态
    （同一资源）；以及携带 utm/fr 等跟踪参数的变体。这些是形式差异，
    不应判为引用失配。路径大小写仍敏感
    """
    decoded = unquote(str(url).strip().rstrip("/"))
    try:
        parts = urlsplit(decoded)
    except ValueError:
        return decoded
    host = (parts.netloc or "").lower()
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit(
        (parts.scheme.lower(), host, parts.path or "", urlencode(query), "")
    )


def _norm_text(text: str) -> str:
    """文本归一化：去掉全部空白与书名号/引号包裹，便于《文档名》类条目比对"""
    return re.sub(r"\s+", "", str(text)).strip("《》「」\"'“”")


def _text_match(a: str, b: str) -> bool:
    """归一化后的双向包含匹配；短侧需 ≥4 字且去掉截断省略号，避免误命中"""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    short = short.rstrip(".").rstrip("…").rstrip(".")
    return len(short) >= _MIN_CONTAIN_LEN and short in long


# 条目文本中的裸 URL（"标题 - URL"纯文本写法；结尾标点剥到 URL 之外）
_BARE_URL_RE = re.compile(r'https?://[^\s)）\]】"\'<>]+')


def _sql_table_names(sql_text: str) -> set[str]:
    """从 SQL 原文提取表名（FROM/JOIN/UPDATE/INSERT INTO 后的标识符，小写）"""
    return {
        t.lower()
        for t in re.findall(
            r"(?:FROM|JOIN|UPDATE|INTO)\s+([A-Za-z_][\w]*)", sql_text or "", re.I
        )
    }


def _candidate_matches_source(entry: dict, source: dict) -> bool:
    """单个参考条目与单条登记来源的比对（规则见模块 docstring"评分口径"）"""
    source_ref = str(source.get("url_or_ref") or "").strip()
    source_type = str(source.get("type") or "").strip().lower()
    url_like = source_ref.lower().startswith(("http://", "https://")) or source_type == "web"
    if url_like:
        # URL 候选两路：条目自带的 Markdown 链接 URL + 纯文本条目里抽取的裸 URL
        url_candidates = [
            u for u in (entry["url"], _extract_bare_url(entry["text"])) if u
        ]
        if url_candidates and source_ref:
            for candidate in url_candidates:
                norm_candidate = _norm_url(candidate)
                norm_source = _norm_url(source_ref)
                if norm_candidate == norm_source:
                    return True
                # 单侧无查询串时退化 host+path 比对：模型抄写时常丢查询参数，
                # 视为同一资源；两侧都带查询串则必须归一化后全等
                cand_parts, src_parts = urlsplit(norm_candidate), urlsplit(norm_source)
                if (cand_parts.netloc, cand_parts.path) == (
                    src_parts.netloc,
                    src_parts.path,
                ) and (not cand_parts.query or not src_parts.query):
                    return True
        # 报告条目没写链接（或来源缺 URL）：退化按标题比对
        return _text_match(entry["title"] or entry["text"], str(source.get("title") or ""))
    if source_type == "sql":
        # SQL 来源的语义匹配（v2 口径）：模型写报告时把 SQL 转述为描述文字，
        # 与原文无字面包含关系——按 SQL 涉及的表名匹配条目文本。表名命中
        # 直接通过；未命中则继续走下方文本包含比对，保留"条目直接引用
        # SQL 原文"（闸门兜底附注清单）场景的既有命中路径
        tables = _sql_table_names(source_ref)
        if tables:
            entry_text = re.sub(r"\s+", "", (entry["text"] or "")).lower()
            if any(
                t.replace("_", "") in entry_text.replace("_", "") for t in tables
            ):
                return True
    # docs / sql 等文本型来源：条目文本与登记内容双向包含
    return _text_match(entry["text"], source_ref) or _text_match(entry["text"], str(source.get("title") or ""))


def _resolve_cited_number(
    number: str, entries: dict[str, list[dict]], sources: list[dict], matched_indices: set
) -> bool:
    """
    判断一个被引用编号是否指向登记来源，命中时把来源下标记入 matched_indices

    优先查参考章节条目；编号无条目时退化为按位置匹配 sources[N-1]
    （真实报告为全来源统一编号，见模块 docstring"评分口径"b 项）。
    """
    for entry in entries.get(number) or []:
        for idx, source in enumerate(sources):
            if _candidate_matches_source(entry, source):
                matched_indices.add(idx)
                return True
    # 同一编号可能有多条条目（闸门兜底清单按类型从 1 重新编号），必须遍历完
    # 所有条目才能判定失配；有条目但全部不匹配时不再走位置兜底，
    # 避免把错引误判成命中
    if entries.get(number):
        return False
    position = int(number)
    if 1 <= position <= len(sources):
        matched_indices.add(position - 1)
        return True
    return False


def score_citations(report_md: str, sources: list[dict] | None) -> dict:
    """
    报告引用与登记来源比对，返回引用准确率评分

    :param report_md: 报告原文；为 None 或全空白时抛 ValueError
    :param sources: 登记来源列表 [{"type","title","url_or_ref"}]，None 按空列表处理
    :return: {"total_citations", "unique_cited", "matched", "accuracy",
              "unmatched_citations", "uncited_sources"}
    """
    if report_md is None or not str(report_md).strip():
        raise ValueError(
            "report_md 为空（None 或全空白），无法做引用评分；"
            "completed case 不应产出空报告，请检查 run_eval 的报告收集链路"
        )
    sources = [s for s in (sources or []) if isinstance(s, dict)]

    body_lines, section_text = _split_report(str(report_md))
    markers: list[str] = []
    for line in body_lines:
        if _LINK_DEF_RE.match(line):
            continue
        for match in _CITATION_RE.finditer(line):
            markers.append(match.group(1) or match.group(2))

    entries = _parse_reference_entries(section_text)
    matched_indices: set = set()
    unmatched: list[str] = []
    for number in dict.fromkeys(markers):  # 去重保序
        if not _resolve_cited_number(number, entries, sources, matched_indices):
            unmatched.append(number)

    unique_cited = len(set(markers))
    matched = unique_cited - len(unmatched)
    uncited_sources = [
        "{type}: {ref}".format(
            type=str(s.get("type") or "unknown"),
            ref=str(s.get("title") or s.get("url_or_ref") or "").strip() or "（无标识）",
        )
        for idx, s in enumerate(sources)
        if idx not in matched_indices
    ]
    return {
        "total_citations": len(markers),
        "unique_cited": unique_cited,
        "matched": matched,
        "accuracy": matched / unique_cited if unique_cited else 0.0,
        "unmatched_citations": unmatched,
        "uncited_sources": uncited_sources,
    }
