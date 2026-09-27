"""chart_tools.generate_chart 的校验与围栏产出测试"""

from unittest import mock

import pytest

from app.tools import chart_tools

# 一份最小可渲染的 option（前端 echarts 可直接 init）
_VALID_OPTION = (
    '{"title": {"text": "销售额TOP3"},'
    ' "xAxis": {"type": "category", "data": ["连花清瘟", "奥司他韦", "替诺福韦"]},'
    ' "yAxis": {"type": "value"},'
    ' "series": [{"type": "bar", "data": [1200000, 700000, 186000]}]}'
)


@pytest.fixture(autouse=True)
def _mute_monitor():
    with mock.patch.object(chart_tools.monitor, "report_tool"):
        yield


def _invoke(option_json: str) -> str:
    return chart_tools.generate_chart.invoke({"option_json": option_json})


def test_valid_option_returns_echarts_fence():
    result = _invoke(_VALID_OPTION)

    assert result.startswith("图表校验通过")
    fence = result.split("\n\n", 1)[1]
    assert fence.startswith("```echarts\n") and fence.endswith("\n```")
    # 围栏内是重新序列化的等价 JSON（ensure_ascii=False 保留中文）
    body = fence.removeprefix("```echarts\n").removesuffix("\n```")
    assert "连花清瘟" in body and "series" in body


def test_invalid_json_returns_correction_guidance():
    # JS 对象字面量写法（单引号 + 尾逗号）不是合法 JSON
    result = _invoke("{'title': {'text': 't'},}")

    assert result.startswith("错误：option 不是合法 JSON")
    assert "纯 JSON" in result


def test_non_object_top_level_rejected():
    result = _invoke('["not", "an", "object"]')

    assert result.startswith("错误：option 顶层必须是 JSON 对象")


def test_function_string_rejected_anywhere():
    # formatter 回调藏在深层嵌套里也要拦
    option = (
        '{"series": [{"type": "bar", "data": [1, 2],'
        ' "label": {"formatter": function(params) { return params.name; }}}]}'
    )
    result = _invoke(option)
    assert result.startswith("错误：option 不是合法 JSON")

    # 函数以字符串形式传入同样拒绝（等价于前端 eval 注入面）
    option_str = (
        '{"series": [{"type": "bar", "data": [1, 2],'
        ' "label": {"formatter": "function(params){return params.name;}"}}]}'
    )
    result = _invoke(option_str)
    assert result.startswith("错误：option 中包含不允许的可执行片段")
    assert "字符串模板" in result


def test_script_and_arrow_payloads_rejected():
    for payload in (
        '{"title": {"text": "<script>alert(1)</script>"}}',
        '{"tooltip": {"formatter": "(params) => params.name"}}',
        '{"title": {"url": "javascript:void(0)"}}',
    ):
        result = _invoke(payload)
        assert result.startswith("错误：option 中包含不允许的可执行片段"), payload


def test_oversized_option_rejected():
    big = '{"series": [{"type": "bar", "data": [' + ",".join(["1"] * 20000) + "]}]}"
    assert len(big.encode("utf-8")) > 32 * 1024

    result = _invoke(big)

    assert result.startswith("错误：option 大小")
    assert "聚合" in result


def test_chinese_option_passes_ascii_size_limit():
    # 中文在 ensure_ascii=False 下按 UTF-8 计入体积校验，正常中文 option 不误伤
    option = '{"title": {"text": "销售额占比"}, "series": [{"type": "pie", "data": [{"name": "中成药", "value": 1200000}]}]}'

    result = _invoke(option)

    assert result.startswith("图表校验通过")
