"""Known-answer tests (plan §5.5): for every validated bug, the patch oracle gets the obvious
answers right. The toy fixture runs with the Docker tests; real bugs are marked `realbugs`
(they clone upstream and build images, so they need network).

- the human gold patch (source files only) is scored correct, with no flags;
- an empty patch is scored incorrect, because the oracle still crashes;
- a patch that edits an oracle test file, or adds a conftest.py that skips everything, is
  rejected before anything runs.
"""

import os
from pathlib import Path

import pytest

from tracerepro.dataset.manifest import PROJECT_ROOT, Manifest, load_manifest
from tracerepro.evaluate.patch_oracle import VerdictKind, bug_repo, gold_diff, score_patch

from .._docker import requires_docker
from ..dataset.test_manifest import TOY

pytestmark = requires_docker

REAL = sorted((PROJECT_ROOT / "benchmark" / "manifests").glob("*.yaml"))
# CI checks each bug in its own job (TRACEREPRO_BUG=<id>): one job for all of them no longer fits
# in a CI time limit once the test set grows.
if os.environ.get("TRACEREPRO_BUG"):
    REAL = [p for p in REAL if p.stem == os.environ["TRACEREPRO_BUG"]]
BUGS = [
    pytest.param(TOY, id="tiny_lib-001"),
    *(pytest.param(p, id=p.stem, marks=pytest.mark.realbugs) for p in REAL),
]

SKIP_EVERYTHING = """\
diff --git a/conftest.py b/conftest.py
new file mode 100644
--- /dev/null
+++ b/conftest.py
@@ -0,0 +1,2 @@
+import pytest
+collect_ignore_glob = ["*"]
"""


def _edit_test(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1 +1,2 @@\n"
        "+import pytest; pytest.skip('skipped by patch', allow_module_level=True)\n"
        " import pytest\n"
    )


@pytest.fixture(scope="module", params=BUGS)
def bug(request: pytest.FixtureRequest) -> Manifest:
    path: Path = request.param
    return load_manifest(path)


def test_gold_patch_is_correct_and_unflagged(bug: Manifest) -> None:
    # Also the false-positive check for Amendment 4: the canary and the three-way integrity
    # check pass on the real suite, and neither the scanner nor the audit hook flags a real fix.
    verdict = score_patch(bug, gold_diff(bug, bug_repo(bug)))
    assert verdict.kind is VerdictKind.CORRECT, (
        verdict.message,
        verdict.failing_oracle,
        verdict.regressions[:5],
    )
    assert verdict.flags == ()
    assert not [e for e in verdict.audit if e.kind == "block"]


def test_empty_patch_is_incorrect(bug: Manifest) -> None:
    verdict = score_patch(bug, "")
    assert verdict.kind is VerdictKind.INCORRECT
    assert set(verdict.failing_oracle) == set(bug.oracle_tests)
    assert verdict.regressions == ()


def test_patch_editing_an_oracle_test_is_rejected(bug: Manifest) -> None:
    oracle_file = bug.oracle_tests[0].split("::", 1)[0]
    verdict = score_patch(bug, _edit_test(oracle_file))
    assert verdict.kind is VerdictKind.SCOPE_VIOLATION
    assert "test_file" in verdict.message


def test_skipping_conftest_is_rejected(bug: Manifest) -> None:
    verdict = score_patch(bug, SKIP_EVERYTHING)
    assert verdict.kind is VerdictKind.SCOPE_VIOLATION
    assert "config_file" in verdict.message
