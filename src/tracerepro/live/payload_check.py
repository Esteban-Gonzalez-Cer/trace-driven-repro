"""Strict checks on a crash payload that arrives from outside (the live demo's webhook).

The benchmark's payloads are built by the harness itself; a webhook's are not. Before anything
reads it, a received payload must have exactly the shape `payload/builder.py` writes (schema 2),
with every string, list and mapping under a size cap, no control characters, and in-app frames
that name library source files only (the same rule the context assembler applies). Anything else
is refused with a list of reasons, and nothing of a refused payload is used.

This is a shape check, not a security boundary for the model: a payload that passes can still
carry hostile text (an exception message with instructions). That text reaches the model inside
a data block, as every benchmark payload does (SECURITY.md, prompt injection).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from tracerepro.context.assembler import _safe_source
from tracerepro.payload.builder import SCHEMA_VERSION

MAX_BYTES = 256 * 1024  # the largest benchmark payload is about 36 KB
MAX_VALUES = 5  # chained exceptions
MAX_FRAMES = 64
MAX_CONTEXT_LINES = 10
MAX_VARS = 64
MAX_TAGS = 20
SHORT = 200  # names: types, modules, functions, tags
PATH = 300
LINE = 1_000  # a source line
TEXT = 4_000  # an exception message
VALUE = 4_096  # a variable's repr

TOP_KEYS = {"schema_version", "synthetic", "event_id", "platform", "level", "exception",
            "contexts", "tags", "timestamp", "release", "environment"}  # fmt: skip
REQUIRED_TOP = {"schema_version", "event_id", "platform", "exception"}
VALUE_KEYS = {"type", "module", "value", "mechanism", "stacktrace"}
FRAME_KEYS = {"filename", "function", "lineno", "in_app", "pre_context", "context_line",
              "post_context", "vars"}  # fmt: skip
LEVELS = {"fatal", "error", "warning", "info", "debug"}
_EVENT_ID = re.compile(r"[0-9a-f]{32}")
_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
# C0 controls other than tab, newline and carriage return, DEL, and the C1 range.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class PayloadRejected(ValueError):
    """A received payload that does not have the expected shape."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class _Checker:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def fail(self, where: str, why: str) -> None:
        if len(self.reasons) < 20:  # enough to explain; a hostile payload cannot flood the log
            self.reasons.append(f"{where}: {why}")

    def text(self, where: str, value: Any, limit: int, *, required: bool = True) -> bool:
        if value is None and not required:
            return True
        if not isinstance(value, str):
            self.fail(where, "must be a string")
        elif len(value) > limit:
            self.fail(where, f"longer than {limit} characters")
        elif _CONTROL.search(value):
            self.fail(where, "contains control characters")
        else:
            return True
        return False

    def keys(self, where: str, value: Any, allowed: set[str], required: set[str]) -> bool:
        if not isinstance(value, dict):
            self.fail(where, "must be an object")
            return False
        for key in sorted(set(value) - allowed, key=str)[:5]:
            self.fail(where, f"unexpected key {str(key)[:40]!r}")
        for key in sorted(required - set(value)):
            self.fail(where, f"missing {key!r}")
        return True

    def items(self, where: str, value: Any, limit: int, *, minimum: int = 0) -> list[Any]:
        if not isinstance(value, list):
            self.fail(where, "must be a list")
            return []
        if not minimum <= len(value) <= limit:
            self.fail(where, f"must have {minimum} to {limit} items")
            return []
        return value

    def mapping(self, where: str, value: Any, limit: int, key_limit: int, value_limit: int) -> None:
        if not isinstance(value, dict):
            self.fail(where, "must be an object")
            return
        if len(value) > limit:
            self.fail(where, f"more than {limit} entries")
            return
        for key, item in value.items():
            if self.text(f"{where} key", key, key_limit):
                self.text(f"{where}[{key[:40]!r}]", item, value_limit)

    def frame(self, where: str, frame: Any) -> bool:
        """Whether the frame is a usable in-app frame."""
        if not self.keys(where, frame, FRAME_KEYS, {"filename", "function", "lineno", "in_app"}):
            return False
        self.text(f"{where}.filename", frame.get("filename"), PATH)
        self.text(f"{where}.function", frame.get("function"), SHORT)
        line = frame.get("lineno")
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= 1_000_000:
            self.fail(f"{where}.lineno", "must be a line number")
        in_app = frame.get("in_app")
        if not isinstance(in_app, bool):
            self.fail(f"{where}.in_app", "must be true or false")
        for key in ("pre_context", "post_context"):
            if key in frame:
                for k, text in enumerate(
                    self.items(f"{where}.{key}", frame[key], MAX_CONTEXT_LINES)
                ):
                    self.text(f"{where}.{key}[{k}]", text, LINE)
        self.text(f"{where}.context_line", frame.get("context_line"), LINE, required=False)
        if "vars" in frame:
            self.mapping(f"{where}.vars", frame["vars"], MAX_VARS, SHORT, VALUE)
        if in_app is True:
            name = frame.get("filename")
            if isinstance(name, str) and not _safe_source(name):
                self.fail(f"{where}.filename", "an in-app frame must name a library source file")
                return False
            return True
        return False

    def exception(self, where: str, value: Any) -> int:
        """The number of usable in-app frames."""
        if not self.keys(where, value, VALUE_KEYS, {"type", "value", "stacktrace"}):
            return 0
        kind = value.get("type")
        if self.text(f"{where}.type", kind, SHORT) and not _TYPE.fullmatch(kind):
            self.fail(f"{where}.type", "must be an exception class name")
        self.text(f"{where}.module", value.get("module"), SHORT, required=False)
        self.text(f"{where}.value", value.get("value"), TEXT)
        if "mechanism" in value:
            self._mechanism(f"{where}.mechanism", value["mechanism"])
        trace = value.get("stacktrace")
        if not self.keys(f"{where}.stacktrace", trace, {"frames"}, {"frames"}):
            return 0
        frames = self.items(f"{where}.stacktrace.frames", trace["frames"], MAX_FRAMES, minimum=1)
        return sum(self.frame(f"{where}.frames[{i}]", f) for i, f in enumerate(frames))

    def _mechanism(self, where: str, value: Any) -> None:
        if self.keys(where, value, {"type", "handled"}, set()):
            self.text(f"{where}.type", value.get("type"), SHORT, required=False)
            if not isinstance(value.get("handled", False), bool):
                self.fail(f"{where}.handled", "must be true or false")


def _top(c: _Checker, data: dict[str, Any]) -> None:
    version = data.get("schema_version")
    if version != SCHEMA_VERSION or isinstance(version, bool):
        c.fail("schema_version", f"must be {SCHEMA_VERSION}")
    event = data.get("event_id")
    if not isinstance(event, str) or not _EVENT_ID.fullmatch(event):
        c.fail("event_id", "must be 32 lowercase hex digits")
    if data.get("platform") != "python":
        c.fail("platform", "must be 'python'")
    if "level" in data and data["level"] not in LEVELS:
        c.fail("level", f"must be one of {sorted(LEVELS)}")
    if "synthetic" in data and not isinstance(data["synthetic"], bool):
        c.fail("synthetic", "must be true or false")
    for key in ("timestamp", "release", "environment"):
        if key in data:
            c.text(key, data[key], SHORT)
    if "tags" in data:
        c.mapping("tags", data["tags"], MAX_TAGS, SHORT, SHORT)
    if "contexts" in data:
        _contexts(c, data["contexts"])


def _contexts(c: _Checker, value: Any) -> None:
    if not c.keys("contexts", value, {"runtime"}, set()):
        return
    runtime = value.get("runtime")
    if runtime is not None and c.keys("contexts.runtime", runtime, {"name", "version"}, set()):
        c.text("contexts.runtime.name", runtime.get("name"), SHORT, required=False)
        c.text("contexts.runtime.version", runtime.get("version"), SHORT, required=False)


def check_payload(data: Any) -> dict[str, Any]:
    """A deep copy of `data` if it is a well-formed crash payload; `PayloadRejected` otherwise."""
    c = _Checker()
    if not c.keys("payload", data, TOP_KEYS, REQUIRED_TOP):
        raise PayloadRejected(c.reasons)
    _top(c, data)
    exception = data.get("exception")
    usable = 0
    if exception is not None and c.keys("exception", exception, {"values"}, {"values"}):
        values = c.items("exception.values", exception["values"], MAX_VALUES, minimum=1)
        usable = sum(c.exception(f"exception.values[{i}]", v) for i, v in enumerate(values))
    if not c.reasons and usable == 0:
        c.fail("exception", "no in-app frame in a library source file")
    if c.reasons:
        raise PayloadRejected(c.reasons)
    return copy.deepcopy(data)


def without_tags(payload: Mapping[str, Any], keys: set[str]) -> dict[str, Any]:
    """The payload without some routing tags, so the model sees what the benchmark measured."""
    out = copy.deepcopy(dict(payload))
    tags = {k: v for k, v in out.get("tags", {}).items() if k not in keys}
    if tags:
        out["tags"] = tags
    else:
        out.pop("tags", None)
    return out
