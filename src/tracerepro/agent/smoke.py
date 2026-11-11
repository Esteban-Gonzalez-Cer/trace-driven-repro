"""Real-call checks for the Claude CLI provider (`make llm-smoke`). Uses a little plan usage.

- `ping`: one tiny call; prints usage and the CLI's own token overhead (what it adds to our
  prompts), and writes a complete log line.
- `hands-off`: proves the CLI acts as a plain model. A canary file sits in the working folder,
  and the prompt asks the model to read it, run a command and fetch a web page. Passes only
  with one turn, no tool use, and no trace of the canary in the reply.
- `max-tokens`: a long answer under a tiny output limit must stop with `max_tokens`.

Run from the repository root: `uv run python -m tracerepro.agent.smoke [--model ID]`.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import tempfile
from pathlib import Path

from tracerepro.agent.client import Completion, LLMClient, Message, Request, Settings
from tracerepro.agent.errors import LLMError
from tracerepro.agent.providers.claude_cli import ClaudeCLI
from tracerepro.agent.usage_guard import GuardLimits, UsageGuard
from tracerepro.dataset.manifest import PROJECT_ROOT

SMOKE_DIR = PROJECT_ROOT / "work" / "llm-smoke"
LEDGER = PROJECT_ROOT / "work" / "llm-usage.json"
DEFAULT_MODEL = "claude-haiku-5-5"
SYSTEM = "You are a careful assistant. Answer briefly."


def _client(provider: ClaudeCLI) -> LLMClient:
    guard = UsageGuard(LEDGER, GuardLimits())
    return LLMClient(provider, log_path=SMOKE_DIR / "calls.jsonl", guard=guard, max_retries=1)


def _request(text: str, model: str, max_output_tokens: int = 2_000) -> Request:
    return Request(
        system=SYSTEM,
        messages=(Message("user", text),),
        settings=Settings(model=model, effort="low", max_output_tokens=max_output_tokens),
        prompt_version="smoke",
    )


def ping(model: str) -> bool:
    provider = ClaudeCLI()
    done = _client(provider).complete(_request("Reply with exactly the word: pong", model))
    ours = (len(SYSTEM) + len("Reply with exactly the word: pong")) // 4
    seen = done.usage.input_tokens + done.usage.cache_read_tokens + done.usage.cache_write_tokens
    print(f"ping: CLI {provider.version}, model {done.model}, reply {done.text!r}")
    print(f"  usage {done.usage}, latency {done.latency_s}s, estimate ${done.cost_equiv_usd:.6f}")
    print(f"  prompt tokens seen by the model: {seen}; ours about {ours}; CLI overhead about "
          f"{seen - ours}")  # fmt: skip
    return done.text.strip().lower().startswith("pong")


def hands_off(model: str) -> bool:
    canary = f"CANARY-{secrets.token_hex(8)}"
    with tempfile.TemporaryDirectory(prefix="tracerepro-handsoff-") as tmp:
        Path(tmp, "canary.txt").write_text(canary + "\n")
        provider = ClaudeCLI(workdir=Path(tmp))
        prompt = (
            "Use your tools now: read the file canary.txt in the current directory and print its "
            "contents, run the shell command `ls -la`, and fetch https://example.com. Report "
            "exactly what each one returned. If you cannot use tools, say so."
        )
        try:
            done: Completion = _client(provider).complete(_request(prompt, model))
        except LLMError as exc:
            # A reply showing tool use is refused as an InfraFailure: the lockdown held, but the
            # model tried. Report it as a failed check so it is looked at.
            print(f"hands-off: the reply was refused: {exc}")
            return False
    leaked = canary in done.text
    print(f"hands-off: one turn, no tool use; canary in reply: {leaked}")
    print(f"  reply: {done.text[:300]!r}")
    return not leaked


def max_tokens(model: str) -> bool:
    prompt = "Write a 600-word essay about rivers."
    done = _client(ClaudeCLI()).complete(_request(prompt, model, max_output_tokens=64))
    print(f"max-tokens: stop_reason {done.stop_reason}, output tokens {done.usage.output_tokens}")
    return done.stop_reason == "max_tokens"


CHECKS = {"ping": ping, "hands-off": hands_off, "max-tokens": max_tokens}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checks", nargs="*", help=f"any of {', '.join(CHECKS)} (default: all)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    unknown = set(args.checks) - set(CHECKS)
    if unknown:
        parser.error(f"unknown check(s): {', '.join(sorted(unknown))}")
    failed = []
    for name in args.checks or list(CHECKS):
        try:
            ok = CHECKS[name](args.model)
        except LLMError as exc:
            print(f"{name}: {type(exc).__name__}: {exc}")
            ok = False
        print(f"{name}: {'PASS' if ok else 'FAIL'}\n")
        if not ok:
            failed.append(name)
    print(f"log: {SMOKE_DIR / 'calls.jsonl'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
