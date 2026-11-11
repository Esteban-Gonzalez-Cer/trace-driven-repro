"""Context coverage (Amendment 5): was the agent shown the lines the gold fix changes?"""

import textwrap
from pathlib import Path

from tracerepro.context.assembler import assemble
from tracerepro.evaluate.coverage import changed_old_lines, context_coverage

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_lib"

REPLACE = textwrap.dedent(
    """\
    diff --git a/pkg/a.py b/pkg/a.py
    index 1111111..2222222 100644
    --- a/pkg/a.py
    +++ b/pkg/a.py
    @@ -8,7 +8,8 @@ def f():
     x = 1
     y = 2
     z = 3
    -    return max(rows)
    +    if not rows:
    +        return 0
    +    return max(rows)
     a = 1
     b = 2
     c = 3
    """
)
INSERT = textwrap.dedent(
    """\
    diff --git a/pkg/b.py b/pkg/b.py
    index 1111111..2222222 100644
    --- a/pkg/b.py
    +++ b/pkg/b.py
    @@ -3,0 +4,2 @@ import os
    +import sys
    +import re
    diff --git a/pkg/new.py b/pkg/new.py
    new file mode 100644
    index 0000000..3333333
    --- /dev/null
    +++ b/pkg/new.py
    @@ -0,0 +1 @@
    +X = 1
    """
)


def test_replaced_lines_and_insertion_points_are_found() -> None:
    assert changed_old_lines(REPLACE) == {"pkg/a.py": {11}}
    # Inserting after line 3 (no context) depends on line 3; a new file depends on nothing.
    assert changed_old_lines(INSERT) == {"pkg/b.py": {3}, "pkg/new.py": set()}


def test_coverage_needs_every_changed_line_shown() -> None:
    shown = {"pkg/a.py": [[1, 5], [9, 12]]}
    covered = context_coverage(REPLACE, shown)
    assert covered["fix_shown"] and not covered["fault_localization_miss"]
    outline = {"pkg/a.py": [[1, 5], [12, 20]]}
    missed = context_coverage(REPLACE, outline)
    assert missed["fault_localization_miss"] and missed["gold_lines_missing"] == {"pkg/a.py": 1}
    assert missed["gold_files_in_context"] == ["pkg/a.py"]  # in context, yet the line was hidden
    absent = context_coverage(REPLACE, {})
    assert absent["fault_localization_miss"] and absent["gold_files_in_context"] == []


def test_the_toy_fix_is_covered_whole_but_not_when_cut_away(tmp_path: Path) -> None:
    import shutil

    tree = tmp_path / "repo"
    shutil.copytree(FIXTURE / "buggy", tree)
    gold = (FIXTURE / "gold.patch").read_text()
    frame = {"filename": "tiny_lib/report.py", "function": "format_report", "lineno": 21}
    payload = {"exception": {"values": [{"stacktrace": {"frames": [{**frame, "in_app": True}]}}]}}
    whole = assemble(payload, tree)
    assert context_coverage(gold, whole.shown_lines())["fix_shown"]
    tiny = assemble(payload, tree, budget=250)  # cut: only the first lines survive
    assert tiny.stage == "cut"
    assert context_coverage(gold, tiny.shown_lines())["fault_localization_miss"]
