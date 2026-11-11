"""A scripted provider for tests: no network, no plan usage, fully deterministic.

Each step of the script is a reply text, an exception to raise, or a function that receives the
request and returns either. Every request is recorded, so tests can check exactly what the
model would have been shown.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from tracerepro.agent.client import Completion, Request, Usage

Step = str | BaseException | Callable[[Request], "str | BaseException"]


@dataclass
class FakeReply:
    """A reply with a chosen stop reason (default `end_turn`)."""

    text: str
    stop_reason: str = "end_turn"


@dataclass
class FakeProvider:
    script: list[Step | FakeReply] = field(default_factory=list)
    name: str = "fake"
    requests: list[Request] = field(default_factory=list)

    @classmethod
    def replying(cls, replies: Iterable[Step | FakeReply]) -> FakeProvider:
        return cls(script=list(replies))

    @property
    def version(self) -> str:
        return "0"

    def complete(self, request: Request) -> Completion:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("the fake provider's script ran out")
        step = self.script.pop(0)
        if callable(step) and not isinstance(step, BaseException):
            step = step(request)
        if isinstance(step, BaseException):
            raise step
        reply = step if isinstance(step, FakeReply) else FakeReply(step)
        prompt_chars = len(request.system) + sum(len(m.content) for m in request.messages)
        return Completion(
            text=reply.text,
            stop_reason=reply.stop_reason,
            usage=Usage(input_tokens=prompt_chars // 4, output_tokens=len(reply.text) // 4),
            model=request.settings.model,
            latency_s=0.0,
            cost_equiv_usd=0.0,
            provider=self.name,
            provider_version=self.version,
        )
