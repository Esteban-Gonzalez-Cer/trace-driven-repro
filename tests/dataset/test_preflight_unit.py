"""Preflight pieces that need no Docker: crash targets, subset order, candidates, log, payload."""

import json
from pathlib import Path

import docker.errors
import pytest
import yaml

from tracerepro.dataset.candidate import CandidateError, parse_candidate
from tracerepro.dataset.manifest import dump_manifest, load_manifest, parse_manifest
from tracerepro.dataset.preflight import (
    PREFLIGHT_LIMITS,
    Outputs,
    _exclude,
    _Preflight,
    changed_lines,
    changed_tests,
    crash_target,
    subsets_smallest_first,
)
from tracerepro.dataset.screening import read_log, upsert
from tracerepro.payload.builder import PayloadError, build_payload, dump_payload
from tracerepro.sandbox.runner import FailureInfo

from .test_manifest import TOY

TEST = ("tests/test_basic.py", "test_run", 10)
SITE = ("/usr/local/lib/python3.11/site-packages/_pytest/python_api.py", "raises", 99)
STDLIB = ("/usr/local/lib/python3.11/enum.py", "__call__", 700)
LIB_OUTER = ("src/pkg/app.py", "run", 40)
LIB_INNER = ("src/pkg/util.py", "parse", 7)


def failure(*frames: tuple[str, str, int], type_: str = "ValueError") -> FailureInfo:
    return FailureInfo(type_, "boom", frames)


def test_crash_target_is_innermost_library_frame() -> None:
    target, _ = crash_target(failure(TEST, LIB_OUTER, LIB_INNER, STDLIB))
    assert target == ("ValueError", "src/pkg/util.py", "parse")


def test_assertion_in_test_is_not_a_crash() -> None:
    target, reason = crash_target(failure(TEST, type_="AssertionError"))
    assert target is None and "not a crash" in reason


def test_pytest_raises_did_not_raise_is_not_a_crash() -> None:
    target, _ = crash_target(failure(TEST, SITE, type_="Failed"))
    assert target is None


def test_unittest_assertion_is_not_a_crash() -> None:
    case = ("/usr/local/lib/python3.11/unittest/case.py", "assertRaises", 778)
    target, reason = crash_target(failure(TEST, case, type_="AssertionError"))
    assert target is None and "not a crash" in reason


def test_crash_with_no_library_frame_is_excluded() -> None:
    target, reason = crash_target(failure(TEST, STDLIB))
    assert target is None and "Amendment 2" in reason


def test_subsets_smallest_first() -> None:
    assert list(subsets_smallest_first(["b.py", "a.py"])) == [
        ("a.py",),
        ("b.py",),
        ("a.py", "b.py"),
    ]
    assert len(list(subsets_smallest_first(["a", "b", "c"]))) == 7


CANDIDATE = {
    "id": "pkg-1",
    "repo": "owner/pkg",
    "c_fix": "a" * 40,
    "python_version": "3.11",
    "deps": {"strategy": "uv-lock", "groups": ["tests"]},
}


def test_parse_candidate_defaults() -> None:
    cand = parse_candidate(CANDIDATE)
    assert cand.test_paths == ("tests",)
    assert cand.split == "dev"
    assert cand.repo_location() == "https://github.com/owner/pkg.git"


@pytest.mark.parametrize(
    "change",
    [
        {"c_fix": "abc"},
        {"deps": {"strategy": "pip"}},
        {"deps": {"strategy": "uv-lock"}},
        {"deps": {"strategy": "compile", "inputs": ["requirements.txt"]}},  # no exclude_newer
        {"deps": {"strategy": "file", "path": "/abs/lock.txt"}},
        {"split": "final"},
        {"surprise": 1},
    ],
)
def test_parse_candidate_rejects(change: dict[str, object]) -> None:
    with pytest.raises(CandidateError):
        parse_candidate(CANDIDATE | change)


def test_screening_upsert_updates_in_place(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    upsert(log, {"id": "a", "repo": "o/a", "decision": "candidate", "reason": "looks good"})
    upsert(log, {"id": "b", "repo": "o/b", "decision": "excluded", "reason": "not a crash"})
    upsert(log, {"id": "a", "repo": None, "decision": "preflight_pass", "reason": "checks pass"})
    rows = read_log(log)
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[0]["repo"] == "o/a" and rows[0]["decision"] == "preflight_pass"
    assert list(rows[0])[:2] == ["id", "repo"]
    with pytest.raises(ValueError):
        upsert(log, {"id": "c", "decision": "maybe"})


def test_manifest_dump_round_trips_and_is_stable() -> None:
    m = load_manifest(TOY)
    text = dump_manifest(m, "header line")
    assert text.startswith("# header line\n")
    assert parse_manifest(yaml.safe_load(text)) == m
    assert dump_manifest(parse_manifest(yaml.safe_load(text)), "header line") == text


def test_payload_drops_test_frames_and_masks_addresses(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "util.py").write_text("".join(f"line {i}\n" for i in range(1, 15)))
    info = FailureInfo(
        "pkg.errors.ParseError",
        "bad value <Thing at 0x7f3a2b1c9d40>",
        (TEST, LIB_OUTER, LIB_INNER, STDLIB),
        ({"secret_test_var": "1"}, {}, {"value": "<Thing object at 0x7f3a2b1c9d40>"}, {}),
    )
    payload = build_payload("pkg-1", "c" * 40, "3.11", info, tmp_path)
    exc = payload["exception"]["values"][0]
    assert (exc["type"], exc["module"]) == ("ParseError", "pkg.errors")
    assert "0x7f3a" not in json.dumps(payload)
    frames = exc["stacktrace"]["frames"]
    assert [f["filename"] for f in frames] == ["src/pkg/app.py", "src/pkg/util.py", STDLIB[0]]
    assert "tests/" not in dump_payload(payload) and "secret_test_var" not in dump_payload(payload)
    inner = frames[1]
    assert inner["context_line"] == "line 7"
    assert inner["pre_context"] == [f"line {i}" for i in range(2, 7)]
    assert inner["vars"] == {"value": "<Thing object at 0x…>"}
    assert frames[2]["in_app"] is False and "vars" not in frames[2]
    assert payload["synthetic"] is True


def test_payload_needs_a_library_frame(tmp_path: Path) -> None:
    with pytest.raises(PayloadError):
        build_payload("pkg-1", "c" * 40, "3.11", failure(TEST, STDLIB), tmp_path)


TEST_MODULE = """\
import pytest


def helper():
    return 1


def test_untouched():
    assert helper()


class TestThing:
    @pytest.mark.parametrize("x", [1, 2, 3])
    def test_param(self, x):
        assert x

    def test_body_changed(self):
        assert helper() == 1


async def test_new_async():
    pass
"""


def test_changed_lines_from_zero_context_diff() -> None:
    diff = "@@ -10,0 +11,2 @@\n+a\n+b\n@@ -20 +22 @@\n-x\n+y\n@@ -30,2 +31,0 @@\n-z\n-w\n"
    assert changed_lines(diff) == {11, 12, 22, 31}


def test_changed_tests_maps_lines_to_test_functions() -> None:
    assert changed_tests(TEST_MODULE, {13}) == {"TestThing::test_param"}  # decorator line
    assert changed_tests(TEST_MODULE, {18}) == {"TestThing::test_body_changed"}
    assert changed_tests(TEST_MODULE, {22}) == {"test_new_async"}
    assert changed_tests(TEST_MODULE, {5}) == set()  # helper changed, no test body touched


class FakeImages:
    def __init__(self, error: Exception | None = None) -> None:
        self.removed: list[str] = []
        self.error = error

    def remove(self, image: str) -> None:
        if self.error:
            raise self.error
        self.removed.append(image)


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.images = FakeImages(error)


def run_stopped_after_build(
    tmp_path: Path, client: FakeClient, *, built: bool = True, keep_image: bool = False
) -> tuple[str, dict[str, object]]:
    """Run a preflight whose image 'builds', then is excluded at the crash search."""
    flight = _Preflight(
        parse_candidate(CANDIDATE),
        work_dir=tmp_path,
        outputs=Outputs(tmp_path, tmp_path, tmp_path, None),
        client=client,  # type: ignore[arg-type]
        runs=1,
        limits=PREFLIGHT_LIMITS,
        keep_image=keep_image,
    )

    def build() -> None:
        flight.image = "bug_env:pkg-1"

    def find_crash() -> None:
        raise _exclude("Criterion 1: no crash")

    def classify_and_stop() -> None:
        raise _exclude("Criterion 3: too big")

    flight.classify = (lambda: None) if built else classify_and_stop  # type: ignore[method-assign]
    flight.build = build  # type: ignore[method-assign]
    flight.find_crash = find_crash  # type: ignore[method-assign]
    result = flight.run()
    report = json.loads((tmp_path / "preflight" / "pkg-1" / "report.json").read_text())
    return result.decision, report


def test_image_of_an_excluded_candidate_is_removed(tmp_path: Path) -> None:
    client = FakeClient()
    decision, report = run_stopped_after_build(tmp_path, client)
    assert decision == "excluded"
    assert client.images.removed == ["bug_env:pkg-1"]
    assert report["image_removed"] == "bug_env:pkg-1"


def test_no_removal_before_the_image_exists_or_when_kept(tmp_path: Path) -> None:
    client = FakeClient()
    run_stopped_after_build(tmp_path, client, built=False)
    run_stopped_after_build(tmp_path, client, keep_image=True)
    assert client.images.removed == []


def test_image_removal_problems_are_reported_not_raised(tmp_path: Path) -> None:
    _, report = run_stopped_after_build(tmp_path, FakeClient(docker.errors.ImageNotFound("gone")))
    assert "image_removed" not in report
    _, report = run_stopped_after_build(tmp_path, FakeClient(docker.errors.APIError("in use")))
    assert report["image_removal_error"] == "in use"


# --- crash kind (DECISIONS.md, 2026-11-11) -----------------------------------------------------

KIND_SOURCE = """\
def parse(text):
    return int(text)


def check(value):
    if value is None:
        raise ValueError(
            "value is required"
        )
    return value


def reraise(exc):
    raise exc
"""


def _kind(tmp_path: Path, frames: tuple[tuple[str, str, int], ...]) -> str:
    from tracerepro.dataset.preflight import crash_kind

    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text(KIND_SOURCE)
    return crash_kind(FailureInfo("ValueError", "boom", frames), tmp_path)


def test_a_failing_call_is_a_python_failure(tmp_path: Path) -> None:
    frames = (("tests/test_x.py", "test", 3), ("pkg/mod.py", "parse", 2))
    assert _kind(tmp_path, frames) == "python_failure"


def test_a_raise_statement_is_a_library_raise(tmp_path: Path) -> None:
    frames = (("tests/test_x.py", "test", 3), ("pkg/mod.py", "check", 7))
    assert _kind(tmp_path, frames) == "library_raise"
    multi_line = (("pkg/mod.py", "check", 8),)  # a line inside the multi-line raise
    assert _kind(tmp_path, multi_line) == "library_raise"
    assert _kind(tmp_path, (("pkg/mod.py", "reraise", 14),)) == "library_raise"


def test_an_exception_from_deeper_code_is_a_python_failure(tmp_path: Path) -> None:
    frames = (("pkg/mod.py", "parse", 2), ("/usr/lib/python3.11/enum.py", "__call__", 700))
    assert _kind(tmp_path, frames) == "python_failure"
    assert _kind(tmp_path, ()) == "python_failure"


def test_manifest_crash_kind_is_validated() -> None:
    data = yaml.safe_load(TOY.read_text())
    assert parse_manifest({**data, "crash_kind": "library_raise"}).crash_kind == "library_raise"
    with pytest.raises(Exception, match="crash_kind"):
        parse_manifest({**data, "crash_kind": "crash"})


def test_lock_fallback_inputs_follow_the_projects_own_scheme(tmp_path: Path) -> None:
    from tracerepro.dataset.candidate import CandidateError, _fallback_inputs

    project = '[project]\nname = "p"\nversion = "1"\n'
    (tmp_path / "pyproject.toml").write_text(project + '[dependency-groups]\ntests = ["pytest"]\n')
    assert _fallback_inputs(tmp_path, ("tests",)) == [
        "pyproject.toml", "--group", "pyproject.toml:tests"
    ]  # fmt: skip
    extras = '[project.optional-dependencies]\ntests = ["pytest"]\n'
    (tmp_path / "pyproject.toml").write_text(project + extras)
    assert _fallback_inputs(tmp_path, ("tests",)) == ["pyproject.toml", "--extra", "tests"]
    (tmp_path / "pyproject.toml").write_text(project)
    (tmp_path / "requirements-tests.txt").write_text("-e .\npytest\n-r requirements.txt\n")
    inputs = _fallback_inputs(tmp_path, ("tests",))
    assert inputs[0] == "pyproject.toml"
    assert (tmp_path / inputs[1]).read_text() == "pytest\n"
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "p"\n')
    with pytest.raises(CandidateError, match="no \\[project\\] table"):
        _fallback_inputs(tmp_path, ("dev",))


def test_an_error_while_building_the_exception_is_a_python_failure(tmp_path: Path) -> None:
    # starlette-3783: `raise ValueError(f"... {cast.__name__}")` fails with AttributeError.
    from tracerepro.dataset.preflight import crash_kind

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "msg.py").write_text(
        'def message(cast):\n    raise ValueError(f"not a valid {cast.__name__}")\n'
    )
    frames = (("pkg/msg.py", "message", 2),)
    assert crash_kind(FailureInfo("AttributeError", "x", frames), tmp_path) == "python_failure"
    assert crash_kind(FailureInfo("ValueError", "x", frames), tmp_path) == "library_raise"
    builtin = FailureInfo("builtins.ValueError", "x", frames)  # module-qualified names too
    assert crash_kind(builtin, tmp_path) == "library_raise"
