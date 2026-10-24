"""LLM client abstraction. All reasoning goes to Groq; tests use FakeLLM."""

import json
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import groq
import jsonschema

# Transient failures worth backing off on. Other API errors (bad key, bad request) fail at once.
RETRYABLE = (groq.RateLimitError, groq.APITimeoutError, groq.APIConnectionError, groq.InternalServerError)


class LLMContractError(Exception):
    """The model's output was not valid JSON or did not match the requested schema."""


@runtime_checkable
class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema: dict, reasoning_effort: str | None = None) -> dict: ...


class GroqClient:
    """Groq chat completions with strict JSON-schema output, backoff, and a one-shot fallback model.

    Reads GROQ_API_KEY from the environment (via the groq SDK).
    """

    def __init__(
        self,
        model: str,
        fallback_model: str | None = None,
        reasoning_effort: str = "medium",
        temperature: float = 0.2,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        self.fallback_model = fallback_model
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self.max_retries = max_retries
        self.last_model: str | None = None
        self._sleep = sleep
        self._client = None

    def _groq(self):
        if self._client is None:
            self._client = groq.Groq()
        return self._client

    def _create(self, **kwargs):
        """Try the primary model max_retries times with exponential backoff, then the fallback once."""
        for attempt in range(self.max_retries):
            try:
                resp = self._groq().chat.completions.create(model=self.model, **kwargs)
                self.last_model = self.model
                return resp
            except RETRYABLE:
                if attempt == self.max_retries - 1:
                    if not self.fallback_model:
                        raise
                    break
                self._sleep(2**attempt)
        resp = self._groq().chat.completions.create(model=self.fallback_model, **kwargs)
        self.last_model = self.fallback_model
        return resp

    def complete_json(self, system: str, user: str, schema: dict, reasoning_effort: str | None = None) -> dict:
        resp = self._create(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "emit", "schema": schema, "strict": True}},
            reasoning_effort=reasoning_effort or self.reasoning_effort,
            temperature=self.temperature,
        )
        content = resp.choices[0].message.content
        try:
            data = json.loads(content or "")
            jsonschema.validate(data, schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            raise LLMContractError(f"{self.last_model}: {e}") from e
        return data

    def chat_with_tools(self, messages: list[dict], tools: list[dict], reasoning_effort: str | None = None) -> dict:
        resp = self._create(
            messages=messages,
            tools=tools,
            tool_choice="auto",
            reasoning_effort=reasoning_effort or self.reasoning_effort,
            temperature=self.temperature,
        )
        msg = resp.choices[0].message
        out: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        return out


class FakeLLM:
    """Serves queued responses (or raises queued exceptions) in order and records what it received.

    A queued callable is called with the request (system, user, schema) or (messages, tools) and its
    return value is the response, for tests whose prompt content isn't known in advance.
    """

    def __init__(self, responses: list[dict | Exception | Callable]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.efforts: list[str | None] = []
        self.chat_calls: list[list[dict]] = []
        self.last_model = "fake"

    def _next(self, *request):
        assert self.responses, "FakeLLM: no queued responses left"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(*request) if callable(item) else item

    def complete_json(self, system: str, user: str, schema: dict, reasoning_effort: str | None = None) -> dict:
        self.calls.append((system, user))
        self.efforts.append(reasoning_effort)
        return self._next(system, user, schema)

    def chat_with_tools(self, messages: list[dict], tools: list[dict], reasoning_effort: str | None = None) -> dict:
        self.chat_calls.append(list(messages))
        return self._next(messages, tools)
