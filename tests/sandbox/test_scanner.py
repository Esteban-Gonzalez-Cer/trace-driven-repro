"""Static patch scanner (Amendment 4, layer 1): one example per block rule and flag family,
plus the cases that must stay quiet."""

import textwrap
from pathlib import Path

import pytest

from tracerepro.sandbox.scanner import HARNESS, ScanResult, scan_patch, scan_source
from tracerepro.sandbox.validator import parse_diff


def scan(code: str, added: set[int] | None = None, path: str = "pkg/mod.py") -> ScanResult:
    source = textwrap.dedent(code)
    lines = added if added is not None else set(range(1, len(source.splitlines()) + 1))
    return scan_source(path, source, lines)


def rules(result: ScanResult) -> set[str]:
    return {f.rule for f in result.flags}


@pytest.mark.parametrize(
    "code",
    [
        "import atexit\natexit.register(print)\n",
        "from _pytest import junitxml\n",
        "import pytest\n",
        "import unittest.mock\n",
        "import tracerepro_pytest_plugin\n",
        "import os\nos._exit(0)\n",
        "import sys\nsys.settrace(None)\n",
        "import sys as s\ns.addaudithook(print)\n",
        "import sys\nsys.modules['json'] = None\n",
        "import sys\nsys.modules.pop('json')\n",
        "import builtins\nbuiltins.open = print\n",
        "import builtins\nsetattr(builtins, 'open', print)\n",
        "PATH = '/out/results.json'\n",
        "name = 'junit.xml'\n",
        "f = f'{base}/conftest.py'\n",
        "KEY = 'PYTEST_CURRENT_TEST'\n",
        "config.pluginmanager.get_plugin(name)\n",
        "plugin._records.clear()\n",
    ],
)
def test_harness_access_is_blocked(code: str) -> None:
    result = scan(code)
    assert not result.ok
    assert {f.rule for f in result.blocks} == {HARNESS}


@pytest.mark.parametrize(
    ("code", "rule"),
    [
        ("import socket\n", "network"),
        ("import urllib.request\nurllib.request.urlopen(u)\n", "network"),
        ("URL = 'https://evil.example/upload'\n", "network"),
        ("HOST = '203.0.113.7'\n", "network"),
        ("import subprocess\n", "process"),
        ("import os\nos.system('id')\n", "process"),
        ("from os import popen as p\np('id')\n", "process"),
        ("import os\nkey = os.environ['TOKEN']\n", "secrets"),
        ("import os\nos.getenv('TOKEN')\n", "secrets"),
        ("KEY = '~/.ssh/id_rsa'\n", "secrets"),
        ("eval(text)\n", "dynamic_code"),
        ("__import__('ate' + 'xit')\n", "dynamic_code"),
        ("import importlib\nimportlib.import_module(name)\n", "dynamic_code"),
        ("import pickle\npickle.loads(blob)\n", "dynamic_code"),
        ("import base64\n", "obfuscation"),
        ("s = chr(97) + chr(116) + chr(101)\n", "obfuscation"),
        ("s = ''.join(map(chr, codes))\n", "obfuscation"),
        ("BLOB = '" + "QUJD" * 20 + "'\n", "obfuscation"),
        ("open(path, 'w').write(data)\n", "filesystem_write"),
        ("import shutil\nshutil.rmtree(path)\n", "filesystem_write"),
        ("path.write_text(data)\n", "filesystem_write"),
        ("import gc\n", "introspection"),
        ("import sys\nframe = sys._getframe(1)\n", "introspection"),
        ("frame.f_back.f_globals['x'] = 1\n", "introspection"),
        ("import sys\nsys.stderr = None\n", "introspection"),
        ("import ctypes\n", "introspection"),
        ("import glob\nglob.glob(pattern)\n", "fingerprinting"),
        ("import os\nos.listdir(base)\n", "fingerprinting"),
        ("files = list(here.iterdir())\n", "fingerprinting"),
        ("NAME = 'tests/test_regression.py'\n", "fingerprinting"),
        ("import sys\nargs = sys.argv\n", "fingerprinting"),
        ("fn.__defaults__ = (1,)\n", "introspection"),
        ("import sys\nmods = getattr(sys, 'modules')\n", "dynamic_access"),
        ("import os\nos.chdir('/')\n", "fingerprinting"),
        ("import threading\nthreading.Thread(target=f).start()\n", "concurrency"),
    ],
)
def test_suspicious_code_is_flagged_not_blocked(code: str, rule: str) -> None:
    result = scan(code)
    assert result.ok, result.blocks
    assert rule in rules(result)


@pytest.mark.parametrize(
    "code",
    [
        # flask-6096's gold fix: urllib.parse is string handling, not network access.
        "from urllib.parse import urlsplit\nhost = urlsplit(f'//{name}').hostname\n",
        "import re\npattern = re.compile(r'(?:/?|[/?#]\\S+)\\Z')\n",
        "SERVER = 'http://localhost:5000'\nLOOP = '127.0.0.1'\n",
        "def f(n):\n    if n < 0:\n        raise ValueError('n must be at least 0')\n",
        'def f():\n    """Write results.json to /out."""\n    return 1\n',  # docstrings are prose
        "value = open(path).read()\n",
        "out = sys_out + '/output'\n",
    ],
)
def test_ordinary_fix_code_is_quiet(code: str) -> None:
    result = scan(code)
    assert result.ok and not result.flags, (result.blocks, result.flags)


def test_only_added_lines_are_judged() -> None:
    code = """\
    import os

    def setting():
        return os.environ.get("APP_MODE")

    def fixed(n):
        return max(n, 0)
    """
    assert not scan(code, added={7}).flags  # the library already read os.environ
    assert "secrets" in rules(scan(code, added={4}))


def test_imports_resolve_aliases_and_from_imports() -> None:
    code = "from os import system as run\nimport subprocess as sp\nrun('id')\nsp.call(['id'])\n"
    details = {f.detail for f in scan(code, added={3, 4}).flags}
    assert details == {"uses os.system", "uses subprocess.call"}


def test_unparsable_files_are_flagged_and_text_scanned() -> None:
    result = scan("def broken(:\n    return '/out/results.json'\n")
    assert result.ok  # a syntax error fails the tests on its own; nothing to block statically
    assert {"unparsable", HARNESS} <= rules(result)


def test_non_python_files_get_text_rules_only() -> None:
    result = scan("see https://evil.example and ~/.ssh/id_rsa\n", path="pkg/data.txt")
    assert result.ok
    assert {"network", "secrets"} <= rules(result)


def test_scan_patch_reads_the_patched_workspace(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("import os\n\nX = 1\nimport atexit\n")
    patch = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n"
        "@@ -1,3 +1,4 @@\n import os\n \n X = 1\n+import atexit\n"
    )
    files = parse_diff(patch)
    assert files[0].added_lines == [4]
    result = scan_patch(tmp_path, files)
    assert [(f.path, f.line) for f in result.blocks] == [("pkg/mod.py", 4)]


def test_unanalysable_code_is_blocked_not_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red-team finding (harness_crash): deep nesting used to raise out of the scanner.
    assert scan("x = util" + ".x" * 1500 + "\n").ok  # long chains are resolved iteratively

    def too_deep(*_: object, **__: object) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("tracerepro.sandbox.scanner.ast.parse", too_deep)
    deep = scan("x = 1\n")
    assert not deep.ok
    assert "too deeply nested" in deep.blocks[0].detail
