"""Repro oracle (spec §7.4) and full trial grading on the toy fixture, in the real sandbox."""

from pathlib import Path

import pytest

from tracerepro import experiment
from tracerepro.agent.pipeline import AgentResult
from tracerepro.dataset.image_builder import build_image
from tracerepro.dataset.manifest import Manifest, load_manifest
from tracerepro.evaluate.patch_oracle import bug_repo, gold_diff
from tracerepro.evaluate.repro_oracle import judge_repro

from .._docker import requires_docker

pytestmark = requires_docker

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_lib"
HEADER = "from tiny_lib import format_report\nfrom tiny_lib.util import amount\n\n\n"
VALID = HEADER + 'def test_empty():\n    assert "TOTAL" in format_report([])\n'
NO_REPRO = HEADER + 'def test_one_row():\n    assert "TOTAL" in format_report([{"name": "a"}])\n'
WRONG_FRAME = HEADER + 'def test_bad_amount():\n    assert amount({"amount": "x"}) == 0\n'
IN_TEST = HEADER + "def test_own_error():\n    raise ValueError('mine')\n"
FAILS_ON_FIX = HEADER + "def test_empty():\n    format_report([])\n    assert False\n"
BROKEN = "import tiny_lib.nowhere\n\n\ndef test_x():\n    pass\n"


@pytest.fixture(scope="module")
def toy(tmp_path_factory: pytest.TempPathFactory) -> tuple[Manifest, Path, str]:
    manifest = load_manifest(FIXTURE / "manifest.yaml")
    work = tmp_path_factory.mktemp("work")
    build_image(manifest, work_dir=work)
    return manifest, work, gold_diff(manifest, bug_repo(manifest, work))


@pytest.mark.parametrize(
    ("test", "kind"),
    [
        (VALID, "valid"),
        (NO_REPRO, "no_repro"),
        (WRONG_FRAME, "invalid_repro"),
        (IN_TEST, "invalid_repro"),
        (FAILS_ON_FIX, "invalid_repro"),
        (BROKEN, "broken"),
    ],
)
def test_repro_verdicts(toy: tuple[Manifest, Path, str], test: str, kind: str) -> None:
    manifest, work, gold = toy
    verdict = judge_repro(manifest, test, gold, work_dir=work)
    assert verdict.kind == kind, verdict.detail
    if kind == "valid":
        assert verdict.on_bug.startswith("ValueError from tiny_lib/report.py::_widest")
        assert verdict.on_fix == "passed"


GOLD_EDIT = "    return max((len(util.label(r)) for r in rows), default=0)\n"
BREAKS_LABEL_DIFF = """diff --git a/tiny_lib/util.py b/tiny_lib/util.py
--- a/tiny_lib/util.py
+++ b/tiny_lib/util.py
@@ -6,4 +6,4 @@ def label(row: Mapping[str, object]) -> str:
     \"\"\"Display label for a row: its name, or '?' when the name is missing.\"\"\"
     name = row.get("name")
-    return str(name) if name is not None else "?"
+    return str(name) if name is not None else ""

"""


def _result(pipeline: str, diff: str | None, *, repro: str | None = None,
            shown: dict | None = None) -> AgentResult:  # fmt: skip
    context = {"shown_lines": shown if shown is not None else {"tiny_lib/report.py": [[1, 25]]}}
    status = "submitted" if diff else "no_patch"
    return AgentResult(pipeline=pipeline, status=status, final_diff=diff, repro=repro,
                       context=context)  # fmt: skip


def test_trials_are_labelled_end_to_end(
    toy: tuple[Manifest, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, work, gold = toy
    monkeypatch.setattr(experiment, "WORK_DIR", work)
    cases = {
        "correct": (_result("B", gold), (None, True)),
        "regression": (_result("B", BREAKS_LABEL_DIFF), ("regression_failure", False)),
        "fl_miss": (_result("B", BREAKS_LABEL_DIFF, shown={"tiny_lib/util.py": [[1, 14]]}),
                    ("fault_localization_miss", False)),
        "repro_then_correct": (_result("A", gold, repro=VALID), (None, True)),
        "invalid_repro": (_result("A", BREAKS_LABEL_DIFF, repro=NO_REPRO),
                          ("no_repro", False)),
    }  # fmt: skip
    for name, (res, expected) in cases.items():
        data, _ = experiment.evaluate(manifest, res)
        assert (data["label"], data["solved"]) == expected, (name, data)
        assert "context_coverage" in data
        if res.repro is not None:
            assert data["repro_oracle"]["kind"] in ("valid", "no_repro"), name
