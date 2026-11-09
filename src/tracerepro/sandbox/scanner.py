"""Static patch scanner (Amendment 4, layer 1). Runs on the host, after the patch is applied to
the workspace copy and before anything executes.

It reads only what the patch *added*: each changed `.py` file is parsed whole (so imports and
aliases resolve), but only nodes on added lines are judged, so code the library already had is
never reported. Two kinds of finding:

- **block** (`harness_access`): code whose only use in a library fix is tampering with the test
  run or its results (`atexit`, pytest internals, `sys.settrace`, writes into `sys.modules`,
  strings naming `/out` or `results.json`...). A block makes the patch a scope violation.
- **flag**: code a bug fix rarely needs and an attacker often does (network, processes, secrets,
  dynamic code, obfuscation, file writes, introspection). Flags never change a verdict; they are
  listed for the manual audit (spec §7.5).

Static scanning can be evaded by a determined author (that is what the runtime audit hook and
the integrity checks are for), so the rules aim for no false blocks on real fixes rather than
for completeness.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tracerepro.sandbox.validator import FileDiff

HARNESS = "harness_access"

BLOCK_MODULES = ("atexit", "pytest", "_pytest", "pluggy", "unittest", "tracerepro")
BLOCK_NAMES = (
    "os._exit",
    "sys.settrace",
    "sys.setprofile",
    "threading.settrace",
    "threading.setprofile",
    "sys.addaudithook",
)
# Strings naming the harness or its result files. Docstrings are not checked.
BLOCK_STRING = re.compile(
    r"(?<![\w.])/out(?![\w])|results\.json|junit|tracerepro|conftest", re.IGNORECASE
)
MODULE_MUTATORS = ("__setitem__", "__delitem__", "update", "pop", "setdefault", "clear")

# Flag rules: (rule, dotted-name prefixes). A dotted name matches a prefix when it equals it or
# continues it with "." (so "urllib.request.urlopen" matches "urllib.request").
FLAG_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("network", (
        "socket", "ssl", "urllib.request", "urllib3", "http.client", "http.server", "requests",
        "httpx", "aiohttp", "ftplib", "smtplib", "telnetlib", "poplib", "imaplib", "xmlrpc",
        "socketserver", "websocket", "websockets", "asyncio.open_connection",
    )),
    ("process", (
        "subprocess", "pty", "multiprocessing", "os.system", "os.popen", "os.execl", "os.execle",
        "os.execlp", "os.execlpe", "os.execv", "os.execve", "os.execvp", "os.execvpe",
        "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe", "os.spawnv", "os.spawnve",
        "os.spawnvp", "os.spawnvpe", "os.fork", "os.forkpty", "os.posix_spawn",
        "os.posix_spawnp", "os.kill", "os.killpg",
    )),
    ("secrets", ("os.environ", "os.environb", "os.getenv", "os.getenvb", "os.putenv",
                 "os.unsetenv")),
    ("dynamic_code", (
        "eval", "exec", "compile", "__import__", "importlib.import_module", "importlib.__import__",
        "pickle.load", "pickle.loads", "marshal.load", "marshal.loads", "types.CodeType",
        "types.FunctionType",
    )),
    ("obfuscation", (
        "base64", "binascii", "codecs.decode", "bytes.fromhex", "zlib.decompress",
        "lzma.decompress", "bz2.decompress",
    )),
    ("filesystem_write", (
        "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename", "os.renames",
        "os.replace", "os.chmod", "os.chown", "os.symlink", "os.link", "os.truncate",
        "shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copyfile",
        "shutil.copytree", "shutil.chown",
    )),
    ("introspection", (
        "gc", "ctypes", "cffi", "sys._getframe", "sys._current_frames", "inspect.stack",
        "inspect.currentframe", "inspect.getouterframes", "inspect.trace", "sys.modules",
        "os.dup", "os.dup2", "__builtins__", "globals",
    )),
)  # fmt: skip
# Attribute names flagged on any object (pathlib writes; frame and function internals).
FLAG_ATTRS = {
    "filesystem_write": {"write_text", "write_bytes", "unlink", "rmdir", "symlink_to",
                         "hardlink_to", "chmod", "touch"},
    "introspection": {"f_back", "f_globals", "f_locals", "f_builtins", "tb_frame", "gi_frame",
                      "cr_frame", "__globals__", "__code__", "__closure__", "__subclasses__"},
}  # fmt: skip
BUILTINS = {"eval", "exec", "compile", "__import__", "open", "chr", "setattr", "globals",
            "__builtins__"}  # fmt: skip
WRITE_MODE = re.compile(r"[wax+]")
SECRET_STRING = re.compile(
    r"\.ssh\b|id_rsa|id_ed25519|id_ecdsa|\.aws\b|\.netrc|\.pypirc|\.git-credentials|"
    r"\.docker/config|/etc/passwd|/etc/shadow|/proc/self/environ|docker\.sock",
    re.IGNORECASE,
)
URL_STRING = re.compile(r"\b(?:https?|ftp|wss?)://([^/\s:'\"]+)", re.IGNORECASE)
IP_STRING = re.compile(r"(?<![\d.])(\d{1,3})\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")
BLOB_STRING = re.compile(r"^(?:[A-Za-z0-9+/]{60,}={0,2}|[0-9a-fA-F]{48,})$")
CHR_PER_LINE = 3


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.path}:{self.line} {self.detail}"


@dataclass(frozen=True)
class ScanResult:
    blocks: tuple[Finding, ...] = ()
    flags: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blocks


def _harness_module(dotted: str) -> bool:
    top = dotted.split(".")[0]
    return top in BLOCK_MODULES or top.startswith("tracerepro")


def _matches(name: str, prefixes: Iterable[str]) -> str | None:
    for prefix in prefixes:
        if name == prefix or name.startswith(prefix + "."):
            return prefix
    return None


class _Collector:
    def __init__(self, path: str) -> None:
        self.path = path
        self.blocks: dict[tuple[int, str], Finding] = {}
        self.flags: dict[tuple[str, int, str], Finding] = {}

    def block(self, line: int, detail: str) -> None:
        self.blocks.setdefault((line, detail), Finding(HARNESS, self.path, line, detail))

    def flag(self, rule: str, line: int, detail: str) -> None:
        self.flags.setdefault((rule, line, detail), Finding(rule, self.path, line, detail))

    def name(self, dotted: str, line: int) -> None:
        """Judge one resolved dotted name (an import, a call target, an attribute chain)."""
        if _matches(dotted, BLOCK_NAMES) or _harness_module(dotted):
            self.block(line, f"uses {dotted}")
            return
        for rule, prefixes in FLAG_NAMES:
            if _matches(dotted, prefixes):
                self.flag(rule, line, f"uses {dotted}")

    def string(self, value: str, line: int, *, harness_blocks: bool = True) -> None:
        if BLOCK_STRING.search(value):
            if harness_blocks:
                self.block(line, f"string names the harness: {value[:80]!r}")
            else:
                self.flag(HARNESS, line, f"names the harness: {value[:80]!r}")
        if SECRET_STRING.search(value):
            self.flag("secrets", line, f"string names a secret location: {value[:80]!r}")
        for host in URL_STRING.findall(value):
            if host.lower() not in LOCAL_HOSTS:
                self.flag("network", line, f"URL to {host}")
        for match in IP_STRING.finditer(value):
            if match[0] not in LOCAL_HOSTS and match[1] != "127":
                self.flag("network", line, f"IP address {match[0]}")
        if BLOB_STRING.match(value.strip()):
            self.flag("obfuscation", line, f"encoded-looking string ({len(value)} chars)")

    def result(self) -> ScanResult:
        return ScanResult(
            tuple(sorted(self.blocks.values(), key=lambda f: (f.line, f.detail))),
            tuple(sorted(self.flags.values(), key=lambda f: (f.line, f.rule, f.detail))),
        )


def _imports(tree: ast.AST) -> dict[str, str]:
    """Local name -> dotted origin for every import in the file (aliases included)."""
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    table[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                table[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return table


def _resolve(node: ast.expr, table: dict[str, str]) -> str | None:
    """The dotted name an expression refers to, or None if it is not rooted in an import or a
    builtin we care about."""
    if isinstance(node, ast.Name):
        if node.id in table:
            return table[node.id]
        return node.id if node.id in BUILTINS else None
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, table)
        return f"{base}.{node.attr}" if base else None
    return None


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found.add(id(node.value))
    return found


def _write_mode(call: ast.Call) -> bool:
    mode: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(WRITE_MODE.search(mode.value))
    return True  # a computed mode: cannot tell, so report it


def scan_source(path: str, source: str, added: Iterable[int]) -> ScanResult:
    """Scan one patched file. `added` holds the line numbers (in `source`) the patch added."""
    lines = set(added)
    out = _Collector(path)
    if not lines:
        return out.result()
    if not path.endswith(".py"):
        for number, text in enumerate(source.splitlines(), start=1):
            if number in lines:
                out.string(text, number, harness_blocks=False)
        return out.result()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        out.flag("unparsable", getattr(exc, "lineno", 0) or 0, f"cannot parse: {exc}")
        for number, text in enumerate(source.splitlines(), start=1):
            if number in lines:
                out.string(text, number, harness_blocks=False)
        return out.result()

    table = _imports(tree)
    docstrings = _docstrings(tree)
    # Only the outermost name of a chain is judged: `os.environ.get`, not also `os.environ`.
    inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    chr_uses: Counter[int] = Counter()

    def on_added(node: ast.AST) -> bool:
        return getattr(node, "lineno", None) in lines

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                line = getattr(alias, "lineno", node.lineno)
                if line not in lines:
                    continue
                if isinstance(node, ast.Import):
                    out.name(alias.name, line)
                elif node.module and not node.level:
                    out.name(f"{node.module}.{alias.name}", line)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            if id(node) not in docstrings and lines.intersection(span):
                out.string(node.value, node.lineno)
        elif isinstance(node, ast.Attribute) and on_added(node):
            dotted = _resolve(node, table) if id(node) not in inner else None
            if dotted:
                out.name(dotted, node.lineno)
            for rule, attrs in FLAG_ATTRS.items():
                if node.attr in attrs:
                    out.flag(rule, node.lineno, f"uses .{node.attr}")
        elif isinstance(node, ast.Name) and on_added(node):
            dotted = _resolve(node, table)
            if dotted == "chr":
                chr_uses[node.lineno] += 1
            elif dotted and id(node) not in inner:
                out.name(dotted, node.lineno)
        elif isinstance(node, ast.Call) and on_added(node):
            func = _resolve(node.func, table)
            if func in ("open", "io.open", "os.open", "codecs.open") and _write_mode(node):
                out.flag("filesystem_write", node.lineno, f"{func}() for writing")
            elif func == "setattr" and node.args:
                target = _resolve(node.args[0], table)
                if target and (target == "builtins" or _harness_module(target)):
                    out.block(node.lineno, f"setattr on {target}")
            elif func and isinstance(node.func, ast.Attribute):
                owner = _resolve(node.func.value, table)
                if owner == "sys.modules" and node.func.attr in MODULE_MUTATORS:
                    out.block(node.lineno, f"changes sys.modules ({node.func.attr})")
            is_map = isinstance(node.func, ast.Name) and node.func.id == "map"
            if is_map and any(_resolve(a, table) == "chr" for a in node.args):
                out.flag("obfuscation", node.lineno, "builds text with map(chr, ...)")
        elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Delete):
            if not on_added(node):
                continue
            targets = node.targets if isinstance(node, ast.Assign | ast.Delete) else [node.target]
            for assigned in targets:
                _judge_target(assigned, table, out)

    for line, count in chr_uses.items():
        if count >= CHR_PER_LINE:
            out.flag("obfuscation", line, f"chr() used {count} times on one line")
    return out.result()


def _judge_target(target: ast.expr, table: dict[str, str], out: _Collector) -> None:
    if isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            _judge_target(element, table, out)
        return
    line = target.lineno
    if isinstance(target, ast.Subscript):
        owner = _resolve(target.value, table)
        if owner == "sys.modules" or owner == "__builtins__":
            out.block(line, f"writes into {owner}")
    elif isinstance(target, ast.Attribute):
        owner = _resolve(target.value, table)
        if owner in ("builtins", "__builtins__") or (owner and _harness_module(owner)):
            out.block(line, f"replaces {owner}.{target.attr}")
        elif owner == "sys" and target.attr in ("stdout", "stderr", "__stdout__", "__stderr__"):
            out.flag("introspection", line, f"replaces sys.{target.attr}")
    elif isinstance(target, ast.Name) and target.id == "__builtins__":
        out.block(line, "replaces __builtins__")


def scan_patch(repo: Path, files: Sequence[FileDiff]) -> ScanResult:
    """Scan every file a validated patch added lines to, as it is now in `repo`."""
    blocks: list[Finding] = []
    flags: list[Finding] = []
    root = repo.resolve()
    for diff in files:
        if not diff.new_path or not diff.added_lines:
            continue
        target = (root / diff.new_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            continue  # the validator already rejects unsafe paths; nothing to read here
        source = target.read_bytes().decode("utf-8", errors="replace")
        result = scan_source(diff.new_path, source, diff.added_lines)
        blocks.extend(result.blocks)
        flags.extend(result.flags)
    return ScanResult(tuple(blocks), tuple(flags))
