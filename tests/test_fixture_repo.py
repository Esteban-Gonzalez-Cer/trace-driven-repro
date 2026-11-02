"""The committed tiny_lib bundle, its manifest hashes, and buggy/ + fixed/ must agree."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

from tracerepro.dataset.manifest import load_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_lib"


def _make_repo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_repo", FIXTURE / "make_repo.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rebuilt_repo_matches_manifest_hashes(tmp_path: Path) -> None:
    c_bug, c_fix = _make_repo().build_repo(tmp_path / "repo")
    manifest = load_manifest(FIXTURE / "manifest.yaml")
    assert (c_bug, c_fix) == (manifest.c_bug, manifest.c_fix), (
        "fixture sources changed: run `uv run python tests/fixtures/tiny_lib/make_repo.py` "
        "and update c_bug/c_fix in manifest.yaml"
    )


def test_committed_bundle_contains_both_commits() -> None:
    manifest = load_manifest(FIXTURE / "manifest.yaml")
    heads = subprocess.run(
        ["git", "bundle", "list-heads", str(FIXTURE / "repo.bundle")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert heads.split()[0] == manifest.c_fix
