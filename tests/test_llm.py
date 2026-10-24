from types import SimpleNamespace

import groq
import httpx
import pytest

from groww_pulse.llm import FakeLLM, GroqClient, LLMClient, LLMContractError

SCHEMA_X = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
    "additionalProperties": False,
}


def _message(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


def _rate_limit_error():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return groq.RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


class StubGroq:
    """Stands in for groq.Groq: serves queued results (or raises them) and records request kwargs."""

    def __init__(self, results):
        self.results = list(results)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def stub(monkeypatch):
    holder = {}

    def install(results):
        holder["stub"] = StubGroq(results)
        monkeypatch.setattr(groq, "Groq", lambda *a, **k: holder["stub"])
        return holder["stub"]

    return install


def _client(**kw):
    return GroqClient("openai/gpt-oss-120b", sleep=lambda s: None, **kw)


def test_fake_llm_returns_in_order():
    fake = FakeLLM([{"a": 1}, {"a": 2}])
    assert fake.complete_json("sys", "u1", {}) == {"a": 1}
    assert fake.complete_json("sys", "u2", {}) == {"a": 2}
    assert fake.calls == [("sys", "u1"), ("sys", "u2")]


def test_fake_llm_raises_queued_exception():
    fake = FakeLLM([LLMContractError("bad"), {"ok": True}])
    with pytest.raises(LLMContractError):
        fake.complete_json("s", "u", {})
    assert fake.complete_json("s", "u", {}) == {"ok": True}


def test_fake_llm_exhausted_is_loud():
    with pytest.raises(AssertionError, match="no queued"):
        FakeLLM([]).complete_json("s", "u", {})


def test_fake_llm_chat_with_tools_returns_queued_message():
    msg = {"role": "assistant", "content": None, "tool_calls": []}
    fake = FakeLLM([msg])
    assert fake.chat_with_tools([{"role": "user", "content": "go"}], tools=[]) == msg


def test_both_implementations_satisfy_protocol():
    assert isinstance(FakeLLM([]), LLMClient)
    assert isinstance(_client(), LLMClient)


def test_groq_client_parses_json_content(stub):
    s = stub([_message('{"x": 1}')])
    assert _client().complete_json("sys", "user", SCHEMA_X) == {"x": 1}
    req = s.requests[0]
    assert req["model"] == "openai/gpt-oss-120b"
    assert req["response_format"]["type"] == "json_schema"
    assert req["response_format"]["json_schema"]["strict"] is True
    assert req["response_format"]["json_schema"]["schema"] == SCHEMA_X
    assert req["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert req["reasoning_effort"] == "medium"
    assert req["temperature"] == 0.2


def test_groq_client_reasoning_effort_override(stub):
    s = stub([_message('{"x": 1}')])
    _client().complete_json("s", "u", SCHEMA_X, reasoning_effort="high")
    assert s.requests[0]["reasoning_effort"] == "high"


def test_groq_client_rejects_schema_mismatch(stub):
    stub([_message('{"y": 1}')])
    with pytest.raises(LLMContractError):
        _client().complete_json("s", "u", SCHEMA_X)


def test_groq_client_rejects_malformed_json(stub):
    stub([_message("not json {")])
    with pytest.raises(LLMContractError):
        _client().complete_json("s", "u", SCHEMA_X)


def test_groq_client_retries_then_succeeds(stub):
    s = stub([_rate_limit_error(), _message('{"x": 2}')])
    c = _client(fallback_model="openai/gpt-oss-20b")
    assert c.complete_json("s", "u", SCHEMA_X) == {"x": 2}
    assert [r["model"] for r in s.requests] == ["openai/gpt-oss-120b"] * 2
    assert c.last_model == "openai/gpt-oss-120b"


def test_groq_client_falls_back_after_retries(stub):
    s = stub([_rate_limit_error()] * 3 + [_message('{"x": 3}')])
    c = _client(fallback_model="openai/gpt-oss-20b")
    assert c.complete_json("s", "u", SCHEMA_X) == {"x": 3}
    assert [r["model"] for r in s.requests] == ["openai/gpt-oss-120b"] * 3 + ["openai/gpt-oss-20b"]
    assert c.last_model == "openai/gpt-oss-20b"


def test_groq_client_raises_without_fallback(stub):
    stub([_rate_limit_error()] * 3)
    with pytest.raises(groq.RateLimitError):
        _client().complete_json("s", "u", SCHEMA_X)


def test_groq_client_backs_off_exponentially(stub):
    stub([_rate_limit_error()] * 2 + [_message('{"x": 1}')])
    sleeps = []
    GroqClient("m", sleep=sleeps.append).complete_json("s", "u", SCHEMA_X)
    assert sleeps == [1, 2]


def test_chat_with_tools_returns_assistant_dict(stub):
    call = SimpleNamespace(
        id="call_1", type="function", function=SimpleNamespace(name="run_stage", arguments='{"stage": "ingest"}')
    )
    s = stub([_message(content=None, tool_calls=[call])])
    tools = [{"type": "function", "function": {"name": "run_stage", "parameters": {"type": "object"}}}]
    out = _client().chat_with_tools([{"role": "user", "content": "go"}], tools)
    assert out == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "run_stage", "arguments": '{"stage": "ingest"}'}}
        ],
    }
    assert s.requests[0]["tools"] == tools
    assert s.requests[0]["tool_choice"] == "auto"


def test_groq_client_counts_token_usage(stub):
    first, second = _message('{"x": 1}'), _message('{"x": 2}')
    first.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
    second.usage = SimpleNamespace(prompt_tokens=50, completion_tokens=5)
    stub([first, second])
    client = _client()
    client.complete_json("s", "u", SCHEMA_X)
    client.complete_json("s", "u", SCHEMA_X)
    assert client.usage == {"prompt_tokens": 150, "completion_tokens": 25}
    assert FakeLLM([]).usage == {"prompt_tokens": 0, "completion_tokens": 0}
