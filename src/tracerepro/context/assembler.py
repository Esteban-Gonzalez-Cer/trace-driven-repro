"""Context assembler (development plan §6.2, spec §6): which source files the agent sees.

Given a bug's synthetic payload and the `c_bug` source tree, it selects:

1. **Trace files:** the files of the payload's in-app frames, innermost frame first.
2. **1-hop imports:** source files those trace files import (found with `ast`, absolute and
   relative imports), resolved inside the same top-level package. Ordered by relevance: how
   often the trace files use the names they import from each file, most used first (ties by
   path). This never looks at the fix.

Test files (`sandbox.paths.is_test_path`), non-Python files and anything outside the package
are never included, so the held-out oracle tests can never reach the agent.

Each file is shown with its repository path and real line numbers. A fixed budget in characters
caps the rendered context (the same cap for every model, whatever its tokenizer). Over budget,
the truncation rule applies in order, stopping as soon as the context fits:

1. ``outline``: imported files are cut to their top-level definitions (imports, short
   assignments, class and function signatures with docstrings, and methods' signatures).
2. ``drop``: imported files are dropped, least used first; then any dropped file that still
   fits is added back, most used first.
3. ``window``: trace files keep the lines around each frame's line (40 lines on each side,
   then 15), plus their outline, their imports and the enclosing definitions' signatures; if
   that is still too big, the outline goes. Dropped imported files are then added back as
   outlines, most used first, while they fit.
4. ``cut``: the last files are cut at the budget; a note says so.

The output is deterministic: the same payload and tree give byte-identical text.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from tracerepro.sandbox.paths import is_library_frame, is_test_path

DEFAULT_BUDGET_CHARS = 250_000
# (lines on each side of a frame, keep the file outline too), tried in order
WINDOWS = ((40, True), (15, True), (15, False))
STAGES = ("full", "outline", "drop", "window", "cut")
GAP = "      ..."


class ContextError(ValueError):
    pass


@dataclass(frozen=True)
class ContextFile:
    path: str
    role: str  # "trace" or "import"
    lines: tuple[tuple[int, str], ...]  # (line number, text); gaps are rendered as "..."
    total_lines: int
    shape: str = "whole"  # whole, outline, window, cut

    def render(self) -> str:
        header = f"=== file: {self.path} ({self.role}"
        header += ")" if self.shape == "whole" else f", {self.shape} of {self.total_lines} lines)"
        out = [header]
        previous = 0
        for number, text in self.lines:
            if number != previous + 1:
                out.append(GAP)
            out.append(f"{number:>5}  {text}")
            previous = number
        if previous != self.total_lines:
            out.append(GAP)
        return "\n".join(out)


@dataclass(frozen=True)
class Context:
    files: tuple[ContextFile, ...]
    stage: str
    budget: int
    dropped: tuple[str, ...] = ()
    candidates: tuple[str, ...] = field(default=(), compare=False)

    def render(self) -> str:
        return "\n\n".join(f.render() for f in self.files) + "\n"

    @property
    def chars(self) -> int:
        return len(self.render())

    def file_list(self) -> tuple[str, ...]:
        """Files the agent was shown (for the Fault Localization Miss label)."""
        return tuple(f.path for f in self.files)

    def shown_lines(self) -> dict[str, list[list[int]]]:
        """Per file, the line ranges the agent was shown, as [first, last] pairs. Used after the
        run to check whether the lines the maintainers changed were visible."""
        shown: dict[str, list[list[int]]] = {}
        for f in self.files:
            ranges: list[list[int]] = []
            for number, _ in f.lines:
                if ranges and ranges[-1][1] == number - 1:
                    ranges[-1][1] = number
                else:
                    ranges.append([number, number])
            shown[f.path] = ranges
        return shown

    def summary(self) -> dict[str, Any]:
        return {
            "shown_lines": self.shown_lines(),
            "files": [f"{f.path} ({f.role}, {f.shape})" for f in self.files],
            "dropped": list(self.dropped),
            "stage": self.stage,
            "chars": self.chars,
            "budget": self.budget,
        }


# --- frames and files ---------------------------------------------------------------------


def trace_frames(payload: Mapping[str, Any]) -> list[tuple[str, int]]:
    """(file, line) of the payload's in-app frames, outermost first, as the payload lists them."""
    try:
        frames = payload["exception"]["values"][-1]["stacktrace"]["frames"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ContextError("payload has no stack trace") from exc
    if not isinstance(frames, list):
        raise ContextError("payload frames are not a list")
    out = []
    for frame in frames:
        if not isinstance(frame, dict) or not frame.get("in_app"):
            continue
        file, line = frame.get("filename"), frame.get("lineno")
        if isinstance(file, str) and isinstance(line, int) and _safe_source(file):
            out.append((file, line))
    return out


def _safe_source(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        is_library_frame(path)
        and not is_test_path(path)
        and p.suffix == ".py"
        and ".." not in p.parts
        and "\\" not in path
    )


def _read(root: Path, path: str) -> list[str] | None:
    full = (root / path).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        return None
    if not full.is_file() or full.is_symlink():
        return None
    try:
        return full.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _package_root(root: Path, path: str) -> tuple[str, ...] | None:
    """Parts of the import root (the folder holding the top-level package) for a source file:
    `src` for src/marshmallow/fields.py, () for tiny_lib/report.py."""
    parts = PurePosixPath(path).parts[:-1]
    top = None
    for i in range(len(parts), 0, -1):
        if (root.joinpath(*parts[:i]) / "__init__.py").is_file():
            top = i - 1
        else:
            break
    return None if top is None else parts[:top]


def _module_parts(path: str, import_root: tuple[str, ...]) -> tuple[str, ...]:
    parts = PurePosixPath(path).with_suffix("").parts[len(import_root) :]
    return parts[:-1] if parts[-1] == "__init__" else parts


def _module_file(root: Path, import_root: tuple[str, ...], module: Sequence[str]) -> str | None:
    if not module:
        return None
    base = PurePosixPath(*import_root, *module)
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if (root / candidate).is_file():
            return candidate.as_posix()
    return None


def import_refs(root: Path, path: str, lines: list[str]) -> dict[str, int]:
    """Source files `path` imports, resolved inside its own top-level package, each with how
    often `path` uses the names it binds from that file (its relevance, used for ordering)."""
    import_root = _package_root(root, path)
    if import_root is None:
        return {}
    try:
        tree = ast.parse("\n".join(lines), filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    here = _module_parts(path, import_root)
    is_package = PurePosixPath(path).name == "__init__.py"
    package = here if is_package else here[:-1]
    top = here[:1]
    bound: dict[str, set[str]] = {}  # local name -> files it comes from

    def resolve(module: Sequence[str]) -> str | None:
        if tuple(module[:1]) != top:
            return None  # another package: stdlib or a dependency
        file = _module_file(root, import_root, module)
        return file if file is not None and file != path and _safe_source(file) else None

    def bind(name: str, file: str | None) -> None:
        if file is not None:
            bound.setdefault(name, set()).add(file)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                bind(alias.asname or parts[0], resolve(parts))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(package):
                    continue
                base = list(package[: len(package) - (node.level - 1)])
            else:
                base = []
            base += node.module.split(".") if node.module else []
            for alias in node.names:
                if alias.name == "*":
                    bind("*", resolve(base))
                    continue
                # A submodule (`from pkg import fields`) or a name defined in the module. A
                # submodule that is filtered out (a test file) never falls back to the package.
                sub = [*base, alias.name]
                is_submodule = _module_file(root, import_root, sub) is not None
                bind(alias.asname or alias.name, resolve(sub) if is_submodule else resolve(base))
    counts = {file: 0 for files in bound.values() for file in files}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in bound:
            for file in bound[node.id]:
                counts[file] += 1
    return counts


def imports_of(root: Path, path: str, lines: list[str]) -> set[str]:
    """Source files `path` imports, resolved inside its own top-level package."""
    return set(import_refs(root, path, lines))


# --- shaping files ------------------------------------------------------------------------


def _numbered(lines: list[str], keep: Iterable[int] | None = None) -> tuple[tuple[int, str], ...]:
    if keep is None:
        return tuple((i, text) for i, text in enumerate(lines, 1))
    return tuple((i, lines[i - 1]) for i in sorted(set(keep)) if 1 <= i <= len(lines))


def _header_lines(node: ast.AST) -> set[int]:
    """Decorators, the signature, and the docstring of a class or function."""
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    body_start = node.body[0].lineno if node.body else node.lineno
    keep = set(range(first, max(node.lineno, body_start - 1) + 1))
    head = node.body[0] if node.body else None
    if (
        isinstance(head, ast.Expr)
        and isinstance(head.value, ast.Constant)
        and isinstance(head.value.value, str)
    ):
        keep |= set(range(head.lineno, (head.end_lineno or head.lineno) + 1))
    elif head is not None and head.lineno == node.lineno:
        keep.add(node.lineno)  # one-line body on the def line
    return keep


def _outline_lines(lines: list[str]) -> set[int] | None:
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError, RecursionError):
        return None
    keep: set[int] = set()

    def statement(node: ast.stmt, depth: int) -> None:
        end = node.end_lineno or node.lineno
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            keep.update(_header_lines(node))
        elif isinstance(node, ast.ClassDef):
            keep.update(_header_lines(node))
            if depth == 0:
                for child in node.body:
                    statement(child, depth + 1)
        elif isinstance(node, ast.Import | ast.ImportFrom | ast.Assign | ast.AnnAssign):
            keep.update(range(node.lineno, min(end, node.lineno + 2) + 1))
        elif (
            depth == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            keep.update(range(node.lineno, end + 1))  # module docstring

    for node in tree.body:
        statement(node, 0)
    return keep


def _window_lines(
    lines: list[str], frame_lines: Iterable[int], radius: int, outline: bool
) -> set[int]:
    keep: set[int] = set(_outline_lines(lines) or ()) if outline else set()
    for line in frame_lines:
        keep.update(range(line - radius, line + radius + 1))
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError, RecursionError):
        return keep
    targets = set(frame_lines)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and node.col_offset == 0:
            keep.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            end = node.end_lineno or node.lineno
            if any(node.lineno <= t <= end for t in targets):
                keep.update(_header_lines(node))
    return keep


# --- assembly -----------------------------------------------------------------------------


def assemble(
    payload: Mapping[str, Any], root: Path, *, budget: int = DEFAULT_BUDGET_CHARS
) -> Context:
    frames = trace_frames(payload)
    sources: dict[str, list[str]] = {}
    frame_lines: dict[str, list[int]] = {}
    trace_order: list[str] = []
    for file, line in reversed(frames):  # innermost first
        if file not in sources:
            text = _read(root, file)
            if text is None:
                continue
            sources[file] = text
            trace_order.append(file)
        frame_lines.setdefault(file, []).append(line)
    if not trace_order:
        raise ContextError("no in-app trace frame points to a readable source file")
    refs: dict[str, int] = {}
    for file in trace_order:
        for imported, count in import_refs(root, file, sources[file]).items():
            refs[imported] = refs.get(imported, 0) + count
    import_order = []
    # Most used first; ties by path. Over budget, the least used are dropped first.
    for file in sorted(set(refs) - set(trace_order), key=lambda f: (-refs[f], f)):
        text = _read(root, file)
        if text is not None:
            sources[file] = text
            import_order.append(file)

    def whole(path: str, role: str) -> ContextFile:
        return ContextFile(path, role, _numbered(sources[path]), len(sources[path]))

    traces = [whole(p, "trace") for p in trace_order]
    imports = [whole(p, "import") for p in import_order]
    candidates = tuple(trace_order + import_order)

    def build(stage: str, dropped: Sequence[str] = ()) -> Context:
        return Context(tuple(traces + imports), stage, budget, tuple(dropped), candidates)

    ctx = build("full")
    if ctx.chars <= budget:
        return ctx
    # 1. outline the imported files
    for i, f in enumerate(imports):
        keep = _outline_lines(sources[f.path])
        if keep is not None:
            imports[i] = ContextFile(
                f.path, f.role, _numbered(sources[f.path], keep), f.total_lines, "outline"
            )
    ctx = build("outline")
    if ctx.chars <= budget:
        return ctx
    # 2. drop imported files, last first
    outlines = {f.path: f for f in imports}
    dropped: list[str] = []
    while imports:
        dropped.insert(0, imports.pop().path)
        ctx = build("drop", dropped)
        if ctx.chars <= budget:
            return _refill(ctx, ctx.files, outlines, "drop")
    # 3. windows around the frames in trace files
    for radius, outline in WINDOWS:
        traces = [
            ContextFile(
                f.path,
                f.role,
                _numbered(src, _window_lines(src, frame_lines[f.path], radius, outline)),
                f.total_lines,
                "window",
            )
            for f in traces
            for src in (sources[f.path],)
        ]
        ctx = build("window", dropped)
        if ctx.chars <= budget:
            return _refill(ctx, traces, outlines, "window")
    # 4. hard cut: keep whole lines while they fit, innermost file first
    kept: list[ContextFile] = []
    used = 0
    for f in traces:
        shown: list[tuple[int, str]] = []
        size = len(f.render()) - sum(len(f"{n:>5}  {t}") + 1 for n, t in f.lines)
        for number, src_line in f.lines:
            cost = len(f"{number:>5}  {src_line}") + len(GAP) + 2
            if used + size + cost > budget:
                break
            shown.append((number, src_line))
            size += cost
        if shown:
            kept.append(ContextFile(f.path, f.role, tuple(shown), f.total_lines, "cut"))
            used += size + 2
        if used >= budget:
            break
    traces = kept
    return build("cut", dropped)


def _refill(
    ctx: Context, kept: Sequence[ContextFile], outlines: Mapping[str, ContextFile], stage: str
) -> Context:
    """Add dropped imported files back as outlines, most used first, while they still fit."""
    files = list(kept)
    dropped = list(ctx.dropped)
    for path in ctx.dropped:
        trial = Context((*files, outlines[path]), stage, ctx.budget, (), ctx.candidates)
        if trial.chars <= ctx.budget:
            files.append(outlines[path])
            dropped.remove(path)
    return Context(tuple(files), stage, ctx.budget, tuple(dropped), ctx.candidates)


# --- command line -------------------------------------------------------------------------


def _materialized(manifest_path: Path, tmp: Path) -> tuple[dict[str, Any], Path]:
    from tracerepro.dataset.manifest import PROJECT_ROOT, load_manifest
    from tracerepro.dataset.repo import clone_name, ensure_clone, materialize

    manifest = load_manifest(manifest_path)
    if manifest.payload_path is None:
        raise ContextError(f"{manifest.id}: manifest has no payload")
    payload = json.loads((PROJECT_ROOT / manifest.payload_path).read_text())
    git_dir = ensure_clone(
        manifest.repo_location(),
        PROJECT_ROOT / "work" / "repos",
        clone_name(manifest.repo),
        [manifest.c_bug],
    )
    tree = tmp / manifest.id
    materialize(git_dir, manifest.c_bug, tree)
    return payload, tree


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the context each bug's agent would see.")
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET_CHARS)
    parser.add_argument("--show", action="store_true", help="print the rendered context")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="tracerepro-context-") as tmp:
        for path in args.manifests:
            payload, tree = _materialized(path, Path(tmp))
            ctx = assemble(payload, tree, budget=args.budget)
            print(json.dumps({"manifest": str(path), **ctx.summary()}, indent=2))
            if args.show:
                print(ctx.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
