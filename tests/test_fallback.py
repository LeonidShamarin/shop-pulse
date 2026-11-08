"""Провайдерський фолбек для tool-calling консультанта: порядок, перемикання, форма.

Без мережі: OpenAI-сумісний клієнт — на httpx.MockTransport, ланцюжок — на стабах.
"""

from __future__ import annotations

import json

import httpx
import pytest

from shoppulse.assistant import (
    FallbackChat,
    GroqChat,
    OpenAICompatChat,
    TransientError,
    build_chat,
)


class _Stub:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        self.calls += 1
        if self.fail:
            raise TransientError(f"{self.name} down")
        return {"message": {"content": self.name}, "usage": {"prompt_tokens": 1}}


def test_first_working_provider_short_circuits() -> None:
    a, b = _Stub("a"), _Stub("b")
    out = FallbackChat([("a", a), ("b", b)]).chat([], [])
    assert out["message"]["content"] == "a" and (a.calls, b.calls) == (1, 0)


def test_falls_through_then_raises_naming_each() -> None:
    a, b = _Stub("groq:GROQ_API_KEY", fail=True), _Stub("gemini:GEMINI_API_KEY", fail=True)
    with pytest.raises(TransientError) as e:
        FallbackChat([("groq:GROQ_API_KEY", a), ("gemini:GEMINI_API_KEY", b)]).chat([], [])
    assert a.calls == b.calls == 1
    assert "groq:GROQ_API_KEY" in str(e.value) and "gemini:GEMINI_API_KEY" in str(e.value)


def test_build_chat_order_dedup_single_and_empty() -> None:
    env = {"GROQ_API_KEY": "gsk_p", "GEMINI_API_KEY": "AIza_a", "OPENAI_API_KEY": "sk-a",
           "XAI_API_KEY": "xai-a", "GEMINI_API_KEY_2": "AQ.1", "GEMINI_API_KEY_3": "AQ.2",
           "GROQ_API_KEY_2": "gsk_2"}
    names = [n for n, _ in build_chat("groq", env=env).members]
    assert names == ["groq:GROQ_API_KEY", "gemini:GEMINI_API_KEY", "openai:OPENAI_API_KEY",
                     "xai:XAI_API_KEY", "gemini:GEMINI_API_KEY_2", "gemini:GEMINI_API_KEY_3",
                     "groq:GROQ_API_KEY_2"]
    assert isinstance(build_chat("groq", env={"GROQ_API_KEY": "x"}), GroqChat)
    assert build_chat("groq", env={}) is None


def test_openai_compat_chat_shape_passes_tools_and_no_reasoning_effort() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi", "tool_calls": []}}],
                                         "usage": {"prompt_tokens": 9}})

    c = OpenAICompatChat("openai", "sk-key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = c.chat([{"role": "user", "content": "q"}], [{"type": "function", "function": {"name": "t"}}])
    assert out["message"]["content"] == "hi" and out["usage"]["prompt_tokens"] == 9
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["body"]["model"] == "gpt-4o-mini"
    assert seen["body"]["tools"] and seen["body"]["tool_choice"] == "auto"
    assert "reasoning_effort" not in seen["body"]


def test_openai_compat_chat_4xx_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad")

    c = OpenAICompatChat("xai", "bad", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPError):
        c.chat([{"role": "user", "content": "q"}], [])
