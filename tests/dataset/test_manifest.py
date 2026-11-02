from pathlib import Path
from typing import Any

import pytest

from tracerepro.dataset.manifest import PROJECT_ROOT, ManifestError, load_manifest, parse_manifest

TOY = PROJECT_ROOT / "tests" / "fixtures" / "tiny_lib" / "manifest.yaml"
MANIFESTS = sorted((PROJECT_ROOT / "benchmark" / "manifests").glob("*.yaml"))


def minimal(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "demo-1",
        "repo": "pallets/flask",
        "c_bug": "a" * 40,
        "c_fix": "b" * 40,
        "python_version": "3.11",
        "image_tag": "bug_env:demo-1",
    }
    data.update(overrides)
    return data


def test_toy_manifest_loads() -> None:
    m = load_manifest(TOY)
    assert m.id == "tiny_lib-001"
    assert m.base_image == "python:3.11-slim"
    assert m.target_frame is not None and m.target_frame.function == "_widest"
    assert m.fix_date == "2026-11-01"
    assert Path(m.repo_location()).is_file()


@pytest.mark.parametrize("path", MANIFESTS, ids=[p.stem for p in MANIFESTS])
def test_benchmark_manifests_load(path: Path) -> None:
    m = load_manifest(path)
    assert path.stem == m.id
    lock = m.deps_lock_path()
    assert lock is not None and lock.is_file()


def test_github_repo_location() -> None:
    assert parse_manifest(minimal()).repo_location() == "https://github.com/pallets/flask.git"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": "Bad ID"}, "invalid id"),
        ({"repo": "https://evil.example/x"}, "repo must be"),
        ({"c_bug": "abc123"}, "full 40-character"),
        ({"c_fix": "a" * 40}, "must differ"),
        ({"python_version": "3.11-slim"}, "invalid python_version"),
        ({"python_version": "2.7"}, "invalid python_version"),
        ({"image_tag": "bug_env:other"}, "image_tag must be"),
        ({"target_frame": {"file": "x.py"}}, "target_frame"),
        ({"excluded_fix_tests": [{"id": "t"}]}, "excluded_fix_tests"),
        ({"oracle_tests": "tests/test_x.py::t"}, "list of strings"),
        ({"deps_lock": "/etc/passwd"}, "relative"),
        ({"surprise": 1}, "unknown fields"),
    ],
)
def test_rejects_bad_manifests(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        parse_manifest(minimal(**overrides))


def test_rejects_missing_required() -> None:
    data = minimal()
    del data["c_bug"]
    with pytest.raises(ManifestError, match="missing required"):
        parse_manifest(data)
