"""Versioned prompts and the messages built from them (development plan §6.3).

Prompts live in `prompts/{name}_{version}.md`; every call logs the version and the file's
sha256, so a result can always be traced to the exact text the model saw.

Untrusted material (the crash payload, source code, test output, a generated repro test) goes
into **data blocks**: a start tag and a matching end tag that carry a random suffix chosen per
message, such as `<data-source-9f2c41aa>`. The suffix is re-drawn until it appears nowhere in
the material, so the material cannot close its own block or open a fake one with the same tag.
The system prompt tells the model that block contents are data, never instructions.

Delimiters are not a security boundary (spec §10): injected instructions may still sway the
model. They reduce confusion; containment comes from the validator, the scanner and the sandbox.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tracerepro.dataset.manifest import PROJECT_ROOT

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SYSTEM = "system"
PATCH = "patch"
REPRO = "repro"
_NAME_RE = re.compile(r"^[a-z]+$")
_VERSION_RE = re.compile(r"^v[0-9]+$")
_LABEL_RE = re.compile(r"^[a-z][a-z-]{0,30}$")
TAG_RE = re.compile(r"</?data-[a-z][a-z-]{0,30}-[0-9a-f]{8}>")
CURRENT_VERSION = "v2"
LEAK_MIN_CHARS = 40  # prompt lines at least this long are distinctive enough to detect a leak


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def id(self) -> str:
        return f"{self.name}_{self.version}"


def load_prompt(name: str, version: str = CURRENT_VERSION, directory: Path = PROMPTS_DIR) -> Prompt:
    if not _NAME_RE.match(name) or not _VERSION_RE.match(version):
        raise ValueError(f"bad prompt name or version: {name!r} {version!r}")
    return Prompt(name, version, (directory / f"{name}_{version}.md").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class DataBlock:
    label: str
    content: str


@dataclass(frozen=True)
class Message:
    """A user message: our instructions, then the data blocks, each in its own tags."""

    text: str
    tags: tuple[str, ...]


def _tag(label: str, content: str, taken: set[str], token: Callable[[], str]) -> str:
    if not _LABEL_RE.match(label):
        raise ValueError(f"bad data block label: {label!r}")
    for _ in range(100):
        tag = f"data-{label}-{token()}"
        if tag not in content and tag not in taken:
            return tag
    raise RuntimeError("could not find a data block tag absent from the content")


def build_message(
    instructions: str,
    blocks: Sequence[DataBlock],
    *,
    token: Callable[[], str] = lambda: secrets.token_hex(4),
) -> Message:
    """Instructions first, then each block as `<tag>` ... `</tag>` with a fresh random tag."""
    everything = instructions + "".join(b.content for b in blocks)
    taken: set[str] = set()
    parts = [instructions.rstrip("\n")]
    for block in blocks:
        tag = _tag(block.label, everything, taken, token)
        taken.add(tag)
        parts.append(f"<{tag}>\n{block.content.rstrip(chr(10))}\n</{tag}>")
    return Message("\n\n".join(parts) + "\n", tuple(sorted(taken)))


def patch_message(
    prompt: Prompt,
    *,
    payload: str,
    context: str,
    repro: str | None = None,
    repro_result: str | None = None,
    feedback: str | None = None,
    token: Callable[[], str] = lambda: secrets.token_hex(4),
) -> Message:
    """The patch request: crash report, source, and optionally a repro test with its result
    (Pipeline A) and the test results of the previous attempt (feedback loop)."""
    blocks = [DataBlock("crash-report", payload), DataBlock("source", context)]
    if repro is not None:
        blocks.append(DataBlock("repro-test", repro))
    if repro_result is not None:
        blocks.append(DataBlock("repro-result", repro_result))
    if feedback is not None:
        blocks.append(DataBlock("test-results", feedback))
    return build_message(prompt.text, blocks, token=token)


def repro_message(
    prompt: Prompt,
    *,
    payload: str,
    context: str,
    feedback: str | None = None,
    token: Callable[[], str] = lambda: secrets.token_hex(4),
) -> Message:
    blocks = [DataBlock("crash-report", payload), DataBlock("source", context)]
    if feedback is not None:
        blocks.append(DataBlock("repro-result", feedback))
    return build_message(prompt.text, blocks, token=token)


def distinctive_lines(prompts: Iterable[Prompt]) -> list[str]:
    lines = {
        line.strip()
        for prompt in prompts
        for line in prompt.text.splitlines()
        if len(line.strip()) >= LEAK_MIN_CHARS
    }
    return sorted(lines)


def leak_check(reply: str, prompts: Iterable[Prompt], tags: Iterable[str] = ()) -> list[str]:
    """What of our own text a reply repeats: long prompt lines, or data block tags. Any hit sets
    the run's `prompt_leak` flag for the reviewer; it never changes a verdict."""
    hits = [f"prompt line: {line[:80]}" for line in distinctive_lines(prompts) if line in reply]
    hits += [f"tag: {tag}" for tag in tags if tag in reply]
    hits += [f"tag pattern: {m}" for m in sorted(set(TAG_RE.findall(reply)))]
    return hits
