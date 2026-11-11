"""Output contracts (development plan §6.3): strict parsing of the model's replies.

- **Repro:** exactly one fenced Python code block, the whole test file. The harness saves it as
  its own file outside the patch, so the validator's ban on test edits still applies to patches.
- **Patch:** one or more search-and-replace edit blocks::

      FILE: path/to/file.py
      <<<<<<< SEARCH
      exact lines from the current file
      =======
      replacement lines
      >>>>>>> REPLACE

  Prose around the blocks is ignored, as are code fences wrapped around them. Inside a block
  the text is taken verbatim.

A reply that breaks the contract is a failed attempt, not an error: parsing returns a
`ContractFailure` whose `label` is the failure taxonomy category (spec §8).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

FILE_PREFIX = "FILE:"
SEARCH = "<<<<<<< SEARCH"
DIVIDER = "======="
REPLACE = ">>>>>>> REPLACE"
_FENCE_RE = re.compile(r"^```[ \t]*([A-Za-z0-9_+-]*)[ \t]*$")
PYTHON_TAGS = frozenset({"python", "py", "python3", ""})
MAX_REPLY_CHARS = 400_000


class Failure(enum.StrEnum):
    NO_CODE_BLOCK = "no_code_block"
    SEVERAL_CODE_BLOCKS = "several_code_blocks"
    UNCLOSED_CODE_BLOCK = "unclosed_code_block"
    NO_EDIT_BLOCK = "no_edit_block"
    MALFORMED_EDIT_BLOCK = "malformed_edit_block"
    NO_MATCH = "no_match"
    SEVERAL_MATCHES = "several_matches"
    BAD_PATH = "bad_path"
    NO_CHANGE = "no_change"
    TOO_LONG = "too_long"


# Taxonomy (spec §8): malformed output is a Syntax or Import Error, except a path that tries to
# leave the repository, which is a Scope Violation (as the validator would rule).
SYNTAX_OR_IMPORT_ERROR = "syntax_or_import_error"
SCOPE_VIOLATION = "scope_violation"


@dataclass(frozen=True)
class ContractFailure:
    kind: Failure
    detail: str

    @property
    def label(self) -> str:
        return SCOPE_VIOLATION if self.kind is Failure.BAD_PATH else SYNTAX_OR_IMPORT_ERROR

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass(frozen=True)
class Edit:
    path: str
    search: str
    replace: str


def _lines(reply: str) -> list[str]:
    return reply.replace("\r\n", "\n").split("\n")


def parse_repro(reply: str) -> str | ContractFailure:
    """The single Python code block's content (the test file), or why there is none."""
    if len(reply) > MAX_REPLY_CHARS:
        return ContractFailure(Failure.TOO_LONG, f"reply has {len(reply)} characters")
    blocks: list[str] = []
    current: list[str] | None = None
    python = False
    for line in _lines(reply):
        match = _FENCE_RE.match(line.strip())
        if current is None:
            if match:
                current, python = [], match.group(1).lower() in PYTHON_TAGS
            continue
        if match and not match.group(1):
            if python:
                blocks.append("\n".join(current) + "\n")
            current = None
            continue
        current.append(line)
    if current is not None:
        return ContractFailure(Failure.UNCLOSED_CODE_BLOCK, "a code block is never closed")
    if not blocks:
        return ContractFailure(Failure.NO_CODE_BLOCK, "no Python code block in the reply")
    if len(blocks) > 1:
        return ContractFailure(
            Failure.SEVERAL_CODE_BLOCKS, f"{len(blocks)} Python code blocks; exactly one expected"
        )
    if not blocks[0].strip():
        return ContractFailure(Failure.NO_CODE_BLOCK, "the code block is empty")
    return blocks[0]


def parse_edits(reply: str) -> list[Edit] | ContractFailure:
    """The reply's edit blocks, in order, or why they cannot be read."""
    if len(reply) > MAX_REPLY_CHARS:
        return ContractFailure(Failure.TOO_LONG, f"reply has {len(reply)} characters")
    lines = _lines(reply)
    edits: list[Edit] = []
    path: str | None = None
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith(FILE_PREFIX):
            path = stripped[len(FILE_PREFIX) :].strip().strip("`")
            i += 1
            continue
        if stripped != SEARCH:
            if stripped in (DIVIDER, REPLACE):
                return ContractFailure(
                    Failure.MALFORMED_EDIT_BLOCK, f"line {i + 1}: {stripped!r} outside a block"
                )
            i += 1
            continue
        if not path:
            return ContractFailure(
                Failure.MALFORMED_EDIT_BLOCK, f"line {i + 1}: SEARCH without a FILE: line before"
            )
        start = i + 1
        try:
            divider = next(j for j in range(start, len(lines)) if lines[j].strip() == DIVIDER)
            end = next(j for j in range(divider + 1, len(lines)) if lines[j].strip() == REPLACE)
        except StopIteration:
            return ContractFailure(
                Failure.MALFORMED_EDIT_BLOCK, f"line {i + 1}: block is missing ======= or REPLACE"
            )
        inner = lines[start:end]
        if any(line.strip() in (SEARCH, FILE_PREFIX) for line in inner) or any(
            line.strip().startswith(FILE_PREFIX) for line in inner
        ):
            return ContractFailure(
                Failure.MALFORMED_EDIT_BLOCK, f"line {i + 1}: a block starts inside another"
            )
        search = "\n".join(lines[start:divider])
        replace = "\n".join(lines[divider + 1 : end])
        edits.append(Edit(path, search + "\n" if search else "", replace + "\n" if replace else ""))
        i = end + 1
    if not edits:
        return ContractFailure(Failure.NO_EDIT_BLOCK, "no SEARCH/REPLACE edit block in the reply")
    return edits
