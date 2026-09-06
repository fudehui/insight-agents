"""
Token 用量回调处理器的单元测试

验证三种 provider 数据形态：langchain 归一化 usage_metadata、
OpenAI 原生 dict、OpenAI CompletionUsage 对象，以及无用量时的静默跳过
"""

from unittest import mock

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.agent.usage_callback import TokenUsageCallbackHandler


class FakeCompletionUsage:
    """模拟 openai SDK 的 CompletionUsage 对象（属性访问形态）"""

    def __init__(self, prompt, completion, total):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


def _result(message=None, llm_output=None):
    generations = [[ChatGeneration(message=message)]] if message else [[]]
    return LLMResult(generations=generations, llm_output=llm_output)


def test_reports_from_normalized_usage_metadata():
    message = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )
    handler = TokenUsageCallbackHandler()

    with mock.patch("app.agent.usage_callback.monitor") as monitor_mock:
        handler.on_llm_end(_result(message=message, llm_output={}))

    monitor_mock.report_token_usage.assert_called_once_with(10, 4, 14)


def test_falls_back_to_openai_dict_usage():
    # 无 usage_metadata 时兜底读 llm_output 的 OpenAI 命名字段
    message = AIMessage(content="x")
    llm_output = {
        "token_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    }
    handler = TokenUsageCallbackHandler()

    with mock.patch("app.agent.usage_callback.monitor") as monitor_mock:
        handler.on_llm_end(_result(message=message, llm_output=llm_output))

    monitor_mock.report_token_usage.assert_called_once_with(7, 3, 10)


def test_falls_back_to_completion_usage_object():
    # openai SDK 新版本 llm_output 里是 CompletionUsage 对象而非 dict
    message = AIMessage(content="x")
    llm_output = {"token_usage": FakeCompletionUsage(5, 2, 7)}
    handler = TokenUsageCallbackHandler()

    with mock.patch("app.agent.usage_callback.monitor") as monitor_mock:
        handler.on_llm_end(_result(message=message, llm_output=llm_output))

    monitor_mock.report_token_usage.assert_called_once_with(5, 2, 7)


def test_missing_usage_is_silently_skipped():
    handler = TokenUsageCallbackHandler()

    with mock.patch("app.agent.usage_callback.monitor") as monitor_mock:
        handler.on_llm_end(_result(message=AIMessage(content="x"), llm_output={}))
        handler.on_llm_end(_result(message=None, llm_output=None))

    monitor_mock.report_token_usage.assert_not_called()


def test_total_computed_when_absent():
    # usage_metadata 模型强制要求 total_tokens，"缺 total"只可能出现在
    # provider 原生数据里：走 llm_output 兜底路径验证自动补算
    message = AIMessage(content="x")
    llm_output = {"token_usage": {"prompt_tokens": 6, "completion_tokens": 2}}
    handler = TokenUsageCallbackHandler()

    with mock.patch("app.agent.usage_callback.monitor") as monitor_mock:
        handler.on_llm_end(_result(message=message, llm_output=llm_output))

    monitor_mock.report_token_usage.assert_called_once_with(6, 2, 8)
