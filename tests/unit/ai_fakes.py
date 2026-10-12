"""Stand-ins shared by the AI unit tests: a model that answers from a script."""
from __future__ import annotations

from types import SimpleNamespace

from superfarm.ai.models import ChatReply, ToolCall


def call(tool: str, /, _id: str = "c1", **arguments) -> ChatReply:
    """A reply that is one tool call."""
    return ChatReply(tool_calls=[ToolCall(_id, tool, arguments)])


def calls(*pairs: tuple[str, dict]) -> ChatReply:
    return ChatReply(tool_calls=[ToolCall(f"c{n}", name, arguments) for n, (name, arguments) in enumerate(pairs, 1)])


def say(text: str) -> ChatReply:
    return ChatReply(content=text)


class Scripted:
    """A model client that gives its replies in order. A reply may be a ChatReply, an
    exception to raise, or a callable taking the messages. When the script runs out the
    last reply repeats, so a test of limits does not have to count."""

    def __init__(self, *replies, tools: bool = True, vision: bool = False, provider: str = "openai"):
        self.profile = SimpleNamespace(tools=tools, vision=vision, provider=provider)
        self.replies = list(replies)
        self.seen: list[dict] = []

    def chat(self, messages, *, tools=None, json_schema=None, max_tokens=None, timeout=None) -> ChatReply:
        self.seen.append({"messages": messages, "tools": tools, "json_schema": json_schema})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(reply):
            reply = reply(messages)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def prompts(self) -> list[str]:
        """The user message of every request, i.e. what the model was shown each step."""
        return [next(m["content"] for m in reversed(s["messages"]) if m["role"] == "user") for s in self.seen]
