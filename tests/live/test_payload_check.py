"""The live demo's shape check on received payloads: every benchmark payload passes it, and
malformed, oversized or hostile shapes are refused with reasons (no Docker, no plan usage)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tracerepro.live.payload_check import (
    MAX_FRAMES,
    PayloadRejected,
    check_payload,
    without_tags,
)

PAYLOADS = sorted((Path(__file__).resolve().parents[2] / "benchmark" / "payloads").glob("*.json"))
DEMO = next(p for p in PAYLOADS if p.stem == "werkzeug-3110")


def demo() -> dict[str, Any]:
    return json.loads(DEMO.read_text())


def frame0(p: dict[str, Any]) -> dict[str, Any]:
    return p["exception"]["values"][0]["stacktrace"]["frames"][0]  # type: ignore[no-any-return]


@pytest.mark.parametrize("path", PAYLOADS, ids=lambda p: p.stem)
def test_every_benchmark_payload_passes(path: Path) -> None:
    data = json.loads(path.read_text())
    clean = check_payload(data)
    assert clean == data and clean is not data


def broken(change: Any) -> list[str]:
    data = demo()
    change(data)
    with pytest.raises(PayloadRejected) as info:
        check_payload(data)
    return info.value.reasons


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda p: p.update(schema_version=1), "schema_version"),
        (lambda p: p.update(schema_version=True), "schema_version"),
        (lambda p: p.update(platform="javascript"), "platform"),
        (lambda p: p.update(event_id="../../etc"), "event_id"),
        (lambda p: p.update(extra={"a": 1}), "unexpected key"),
        (lambda p: p.pop("exception"), "missing 'exception'"),
        (lambda p: p["exception"].update(values=[]), "1 to 5 items"),
        (lambda p: p["exception"]["values"][0].update(type="os.system('x')"), "class name"),
        (lambda p: p["exception"]["values"][0].update(value="x" * 5000), "longer than"),
        (lambda p: p["exception"]["values"][0].update(value="a\x00b"), "control characters"),
        (lambda p: frame0(p).update(filename="../../etc/passwd"), "library source file"),
        (lambda p: frame0(p).update(filename="tests/test_x.py"), "library source file"),
        (lambda p: frame0(p).update(filename="src/pkg/mod.so"), "library source file"),
        (lambda p: frame0(p).update(lineno="12"), "line number"),
        (lambda p: frame0(p).update(lineno=0), "line number"),
        (lambda p: frame0(p).update(in_app="yes"), "true or false"),
        (lambda p: frame0(p).update(vars={f"v{i}": "1" for i in range(65)}), "more than 64"),
        (lambda p: frame0(p).update(vars={"v": {"nested": "x"}}), "must be a string"),
        (lambda p: frame0(p).update(pre_context=["x"] * 11), "0 to 10 items"),
        (lambda p: frame0(p).update(code="exec()"), "unexpected key"),
        (lambda p: p["tags"].update({"k": "v" * 201}), "longer than"),
        (lambda p: p.update(contexts={"os": {"name": "x"}}), "unexpected key"),
    ],
)
def test_malformed_payloads_are_refused_with_a_reason(change: Any, reason: str) -> None:
    reasons = broken(change)
    assert any(reason in r for r in reasons), reasons


def test_a_payload_must_have_an_in_app_library_frame() -> None:
    def no_app(p: dict[str, Any]) -> None:
        for f in p["exception"]["values"][0]["stacktrace"]["frames"]:
            f["in_app"] = False

    assert broken(no_app) == ["exception: no in-app frame in a library source file"]


def test_sizes_are_capped_and_the_reasons_are_few() -> None:
    def many(p: dict[str, Any]) -> None:
        frames = p["exception"]["values"][0]["stacktrace"]["frames"]
        frames[:] = [dict(frames[0], lineno="x") for _ in range(MAX_FRAMES)]

    assert len(broken(many)) == 20
    for bad in ([], "x", 3, None):
        with pytest.raises(PayloadRejected):
            check_payload(bad)


def test_the_routing_tag_is_removed_and_the_rest_kept() -> None:
    data = demo()
    tagged = copy.deepcopy(data)
    tagged["tags"]["tracerepro_bug"] = "werkzeug-3110"
    assert without_tags(check_payload(tagged), {"tracerepro_bug"}) == data
    data.pop("tags")
    data["tags"] = {"tracerepro_bug": "x"}
    assert "tags" not in without_tags(data, {"tracerepro_bug"})
