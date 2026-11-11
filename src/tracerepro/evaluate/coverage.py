"""Was the agent shown the code it needed? (Amendment 5, spec §8 category 5.)

Spec §8 labels a failure a **Fault Localization Miss** when the gold diff touches files outside
the context given to the agent. A file can be in the context only as an outline or a window
(the truncation rule), with the lines the maintainers changed hidden. Amendment 5 makes the
check line-level: the context *covers* the fix only if every line the gold diff changes on the
buggy side was shown.

- A removed or replaced line must have been shown.
- For an inserted line, the line just before the insertion point must have been shown (the
  first line for an insertion at the top of a file).

This runs after the agent has finished, outside the agent package: the agent never sees it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def changed_old_lines(diff: str) -> dict[str, set[int]]:
    """Per file (repository path), the buggy-side line numbers the diff depends on."""
    found: dict[str, set[int]] = {}
    path: str | None = None
    old = 0
    in_hunk = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            path, in_hunk = None, False
        elif line.startswith("--- ") and not in_hunk:
            name = line[4:].strip()
            path = name[2:] if name.startswith("a/") else None
        elif line.startswith("+++ ") and not in_hunk:
            if path is None:  # a new file: nothing on the buggy side to have shown
                name = line[4:].strip()
                path = name[2:] if name.startswith("b/") else None
                found.setdefault(path or "", set())
                path = None
        elif (match := _HUNK_RE.match(line)) and path is not None:
            old, in_hunk = int(match.group(1)), True
            # git reports start 0 for an insertion at the top of an empty side
            old = max(old, 1) if match.group(2) != "0" else old + 1
        elif in_hunk and path is not None:
            if line.startswith("-"):
                found.setdefault(path, set()).add(old)
                old += 1
            elif line.startswith("+"):
                found.setdefault(path, set()).add(max(old - 1, 1))
            elif line.startswith(" "):
                old += 1
    found.pop("", None)
    return found


def _covered(line: int, ranges: Sequence[Sequence[int]]) -> bool:
    return any(start <= line <= end for start, end in ranges)


def context_coverage(
    gold_diff: str, shown: Mapping[str, Sequence[Sequence[int]]]
) -> dict[str, Any]:
    """Compare the gold diff's buggy-side lines with the lines the agent was shown."""
    needed = changed_old_lines(gold_diff)
    missing = {
        path: sorted(n for n in lines if not _covered(n, shown.get(path, ())))
        for path, lines in needed.items()
    }
    missing = {path: lines for path, lines in missing.items() if lines}
    return {
        "gold_files": sorted(needed),
        "gold_files_in_context": sorted(p for p in needed if p in shown),
        "gold_lines": sum(len(v) for v in needed.values()),
        "gold_lines_missing": {p: len(v) for p, v in missing.items()},
        "fix_shown": not missing,
        "fault_localization_miss": bool(missing),
    }
