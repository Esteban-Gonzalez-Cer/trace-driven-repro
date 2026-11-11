# Methodology

TODO: Phase 3, frozen with the analysis plan at M3.

## Context given to the agent (spec §6, plan §6.2)

Built by `tracerepro.context.assembler` from the synthetic payload and the `c_bug` source tree.
The agent gets no browsing tools; this is everything it sees besides the payload.

**Selection.**
1. *Trace files:* the files of the payload's in-app frames, innermost frame first.
2. *1-hop imports:* source files the trace files import (absolute and relative imports, found
   with Python's `ast`), resolved inside the same top-level package. Standard library and
   dependencies are not included.
3. *Never included:* test files (`tests/`, `test_*.py`, `*_test.py`), non-Python files, and
   anything outside the package. The held-out oracle tests therefore cannot reach the agent.

**Order.** Trace files first. Imported files follow, ordered by how often the trace files use
the names imported from them (most used first; ties by path). The ranking only looks at the
buggy code, never at the fix.

**Format.** Each file starts with `=== file: <path> (<role>[, <shape> of <N> lines])`
and shows its lines with their real line numbers; skipped lines are shown as `...`.

**Cap and truncation rule.** The rendered context is capped at 120,000 characters (about 30,000
tokens). The cap is in characters so every model gets byte-identical context whatever its
tokenizer; real token counts are logged from each reply. Over the cap, these stages apply in
order, stopping as soon as the context fits:

1. *outline:* imported files are cut to their top-level definitions: imports, the first lines
   of assignments, class and function signatures with their docstrings, and methods'
   signatures.
2. *drop:* imported files are dropped, least used first; then any dropped file that still
   fits is added back as an outline, most used first.
3. *window:* trace files keep the lines within 40 lines of each frame (then 15), plus their
   outline, their imports and the signatures of the definitions enclosing each frame; if that
   is still too big, the outline goes. Dropped imported files are then added back as above.
4. *cut:* the last lines are cut at the cap.

The stage reached and the files dropped are logged per bug. On the 5 dev bugs: marshmallow-3016
fits whole (35k characters); flask-6096, marshmallow-3004 and marshmallow-3082 reach *drop*
(115k to 120k, 1 to 5 imported files dropped); more-itertools-1342 reaches *window* (its trace
file has 5,654 lines). In every case the file the maintainers fixed is in the context (for
marshmallow-3082 as an outline).
