"""Context assembler (plan §6.2): file selection, ordering, the truncation rule and determinism,
on the toy fixture and small generated packages."""

import shutil
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tracerepro.context.assembler import (
    Context,
    ContextError,
    assemble,
    import_refs,
    imports_of,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_lib"


def payload(*frames: tuple[str, int], in_app: bool = True) -> dict[str, Any]:
    return {
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "stacktrace": {
                        "frames": [
                            {"filename": f, "function": "f", "lineno": n, "in_app": in_app}
                            for f, n in frames
                        ]
                    },
                }
            ]
        }
    }


@pytest.fixture
def tiny(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE / "buggy", root)
    shutil.copytree(FIXTURE / "tests", root / "tests")
    return root


TINY = payload(("tiny_lib/report.py", 21), ("tiny_lib/report.py", 11))


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(textwrap.dedent(text))


def test_trace_file_and_its_imports_are_selected(tiny: Path) -> None:
    ctx = assemble(TINY, tiny)
    assert ctx.file_list() == ("tiny_lib/report.py", "tiny_lib/util.py")
    assert [f.role for f in ctx.files] == ["trace", "import"]
    assert ctx.stage == "full" and ctx.dropped == ()
    text = ctx.render()
    assert "=== file: tiny_lib/report.py (trace)" in text
    assert "   11  " in text and "return max(len(util.label(r)) for r in rows)" in text


def test_output_is_deterministic(tiny: Path) -> None:
    assert assemble(TINY, tiny).render() == assemble(TINY, tiny).render()
    assert assemble(TINY, tiny, budget=400).render() == assemble(TINY, tiny, budget=400).render()


def test_test_files_never_enter_the_context(tiny: Path) -> None:
    write(tiny, {"tiny_lib/conftest_helpers.py": "X = 1\n"})
    (tiny / "tiny_lib" / "report.py").write_text(
        (tiny / "tiny_lib" / "report.py").read_text()
        + "\nfrom tests import test_regression\nfrom tiny_lib import test_util\n"
    )
    write(tiny, {"tiny_lib/test_util.py": "SECRET_ORACLE = 1\n"})
    framed = payload(
        ("tests/test_regression.py", 5), ("tiny_lib/test_util.py", 1), ("tiny_lib/report.py", 11)
    )
    ctx = assemble(framed, tiny)
    assert ctx.file_list() == ("tiny_lib/report.py", "tiny_lib/util.py")
    assert "SECRET_ORACLE" not in ctx.render() and "test_empty_report" not in ctx.render()


def test_unsafe_or_missing_frame_paths_are_ignored(tiny: Path) -> None:
    framed = payload(
        ("/etc/passwd", 1),
        ("../outside.py", 1),
        ("tiny_lib/../../outside.py", 1),
        ("<string>", 1),
        ("tiny_lib/missing.py", 1),
        ("tiny_lib/report.py", 11),
    )
    assert assemble(framed, tiny).file_list() == ("tiny_lib/report.py", "tiny_lib/util.py")
    with pytest.raises(ContextError):
        assemble(payload(("/etc/passwd", 1)), tiny)
    with pytest.raises(ContextError):
        assemble({"exception": {}}, tiny)
    with pytest.raises(ContextError):
        assemble(payload(("tiny_lib/report.py", 11), in_app=False), tiny)


def test_innermost_trace_file_comes_first(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/outer.py": "from pkg import inner\n\ndef run():\n    inner.boom()\n",
            "src/pkg/inner.py": "def boom():\n    raise ValueError\n",
        },
    )
    ctx = assemble(payload(("src/pkg/outer.py", 4), ("src/pkg/inner.py", 2)), tmp_path)
    assert ctx.file_list() == ("src/pkg/inner.py", "src/pkg/outer.py")
    assert {f.role for f in ctx.files} == {"trace"}


def test_relative_and_absolute_imports_resolve_inside_the_package(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/sub/__init__.py": "",
            "src/pkg/sub/mod.py": textwrap.dedent(
                """\
                import os
                import requests
                from . import sibling
                from .. import top
                from ..other import thing, helper_name
                from pkg.sub.deep import x as y
                import pkg.sub.sibling
                from ... import escape
                """
            ),
            "src/pkg/sub/sibling.py": "",
            "src/pkg/sub/deep.py": "x = 1\n",
            "src/pkg/top.py": "",
            "src/pkg/other/__init__.py": "helper_name = 1\n",
            "src/pkg/other/thing.py": "",
        },
    )
    lines = (tmp_path / "src/pkg/sub/mod.py").read_text().splitlines()
    assert imports_of(tmp_path, "src/pkg/sub/mod.py", lines) == {
        "src/pkg/sub/sibling.py",
        "src/pkg/top.py",
        "src/pkg/other/thing.py",
        "src/pkg/other/__init__.py",
        "src/pkg/sub/deep.py",
    }


def test_imports_are_ranked_by_use(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a_rare.py": "def r(): ...\n",
            "pkg/z_common.py": "def c(): ...\n",
            "pkg/main.py": textwrap.dedent(
                """\
                from pkg import a_rare
                from pkg.z_common import c

                def run():
                    a_rare.r()
                    return c() + c() + c()
                """
            ),
        },
    )
    lines = (tmp_path / "pkg/main.py").read_text().splitlines()
    assert import_refs(tmp_path, "pkg/main.py", lines) == {"pkg/a_rare.py": 1, "pkg/z_common.py": 3}
    ctx = assemble(payload(("pkg/main.py", 6)), tmp_path)
    assert ctx.file_list() == ("pkg/main.py", "pkg/z_common.py", "pkg/a_rare.py")


def big_package(root: Path, helpers: int = 3, body: int = 60) -> dict[str, Any]:
    functions = "\n".join(
        f'def f{i}(x):\n    """Doc {i}."""\n'
        + "".join(f"    x += {j}\n" for j in range(body))
        + "    return x\n"
        for i in range(8)
    )
    files = {"pkg/__init__.py": ""}
    for h in range(helpers):
        files[f"pkg/helper{h}.py"] = f'"""Helper {h}."""\nimport os\nLIMIT = {h}\n\n' + functions
    uses = "\n".join(f"    helper{h}.f0(1)" for h in range(helpers) for _ in range(helpers - h))
    main_body = "".join(f"    y = {j}\n" for j in range(300))
    files["pkg/main.py"] = (
        f"from pkg import {', '.join(f'helper{h}' for h in range(helpers))}\n\n"
        f"def run():\n{uses}\n{main_body}    raise ValueError(y)\n"
    )
    write(root, files)
    crash = files["pkg/main.py"].count("\n")
    return payload(("pkg/main.py", crash))


def test_truncation_stages_apply_in_order(tmp_path: Path) -> None:
    framed = big_package(tmp_path)
    full = assemble(framed, tmp_path, budget=10**7)
    assert full.stage == "full"
    sizes = {f.path: len(f.render()) for f in full.files}
    trace = sizes["pkg/main.py"]

    outline = assemble(framed, tmp_path, budget=full.chars - 1)
    assert outline.stage == "outline" and outline.chars <= full.chars - 1
    helper = next(f for f in outline.files if f.path == "pkg/helper0.py")
    assert helper.shape == "outline"
    text = helper.render()
    assert "def f3(x):" in text and '"""Doc 3."""' in text and "x += 5" not in text
    assert "LIMIT = 0" in text and "import os" in text

    one_outline = len(helper.render())
    dropped = assemble(framed, tmp_path, budget=trace + one_outline + 10)
    assert dropped.stage == "drop" and dropped.chars <= trace + one_outline + 10
    assert dropped.file_list() == ("pkg/main.py", "pkg/helper0.py")  # the most used is kept
    assert dropped.dropped == ("pkg/helper1.py", "pkg/helper2.py")

    window = assemble(framed, tmp_path, budget=trace // 2)
    assert window.stage == "window" and window.chars <= trace // 2
    main = window.files[0]
    assert main.shape == "window"
    rendered = main.render()
    assert "raise ValueError(y)" in rendered and "def run():" in rendered
    assert "from pkg import" in rendered and "y = 10\n" not in rendered

    cut = assemble(framed, tmp_path, budget=300)
    assert cut.stage == "cut" and cut.chars <= 300 + 200  # header overhead is small


def test_summary_reports_what_was_shown(tiny: Path) -> None:
    summary = assemble(TINY, tiny).summary()
    assert summary["files"] == [
        "tiny_lib/report.py (trace, whole)",
        "tiny_lib/util.py (import, whole)",
    ]
    assert summary["stage"] == "full" and summary["chars"] == assemble(TINY, tiny).chars
    assert isinstance(assemble(TINY, tiny), Context)
