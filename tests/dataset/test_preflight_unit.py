"""Preflight pieces that need no Docker: crash targets, subset order, candidates, log, payload."""

import json
from pathlib import Path

import pytest
import yaml

from tracerepro.dataset.candidate import CandidateError, parse_candidate
from tracerepro.dataset.manifest import dump_manifest, load_manifest, parse_manifest
from tracerepro.dataset.preflight import (
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
