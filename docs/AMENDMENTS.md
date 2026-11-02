# Spec amendments

The spec changes only through a logged amendment, and only for (a) a bug in the harness or
(b) infeasibility discovered by running something (spec §15). "It would be nicer" is not a
valid reason.

| # | Date | Spec section | Reason | Impact |
|---|---|---|---|---|
| 1 | 2026-11-01 | §5.4 check 1, §5.5 (`oracle_tests`, `gold_files`), §8 category 5 | (b) Infeasibility found by running the flask-6096 preflight: some human fixes bundle the crash fix with an independent non-crash change | Oracle limited to tests that reproduce the crash; `gold_files` limited to the gold hunks those tests need. Details below |
| 2 | 2026-11-01 | §5.2 (new criterion 6), §5.5 (`target_frame`), §7.4 condition 2 | (b) Infeasibility found by running click-3805: the crash traceback contains no frame inside the library, so `target_frame` is undefined and the agent's context would contain no source files | Such bugs are excluded at curation; "library frame" is defined precisely. Details below |

## Amendment 1: Mixed fixes (crash-only oracle)

**Found by:** manual preflight of flask-6096 ([write-up](preflight/flask-6096.md), issue #15).
PR pallets/flask#6096 fixes a crash in `app.py::run` and, independently, a non-crash cookie-domain
bug in `testing.py`.

**The problem:** spec §5.5 defines `oracle_tests` as "test ids from the fix commit" and
`gold_files` as "files touched by the human fix". Applied literally to a mixed fix:

- The non-crash test fails on `c_bug` with `AssertionError`, not the target exception, so
  preflight check 1 fails and a valid crash bug is excluded.
- If the bug were kept anyway, `gold_files` would include a file that is not in the crash trace.
  Fault Localization Miss (taxonomy #5) takes precedence over Regression and Incorrect Patch, so
  every failed run would be mislabeled because of a change the crash report says nothing about.

**The rule** (applies to every bug, dev and test set):

1. **Fix tests.** Let *T* be the test ids added or changed in `c_fix` that fail on `c_bug` (with
   the `c_fix` test files copied in) and pass on `c_fix`.
2. **`oracle_tests`** is the subset of *T* whose failure on `c_bug` is the target exception
   raised from the target frame (same matching as the repro oracle, spec §7.4 condition 2). If
   this subset is empty, the bug is a dataset exclusion.
3. **Unused fix tests.** Tests in *T* but not in `oracle_tests` are recorded in the manifest as
   `excluded_fix_tests` with a reason. They are not run at evaluation time. They fail on `c_bug`,
   so they are never in the baseline either.
4. **`gold_files`** is the smallest set of source files *F* from the gold diff such that applying
   only the gold hunks for *F* to `c_bug` (a) makes every `oracle_tests` test pass and (b) causes
   no test in `baseline_pass_set` to fail. With at most 3 gold files (§5.2), check every subset in
   order of size. If several minimal subsets tie, keep all of their files and record the tie in
   `notes`. When nothing is bundled, *F* is simply every gold file.
5. **`gold_files_full`** (new manifest field) records every source file the human fix touched,
   for provenance and the write-up.
6. **Unchanged:** the full human gold patch must still score correct in the known-answer tests
   (§11). The patch oracle is still "`oracle_tests` pass and no baseline test fails". The repro
   oracle and the diff size limits (measured on the full gold diff at screening) are unchanged.

**Impact:**

- Preflight check 1 is evaluated on `oracle_tests` as defined above.
- Fault Localization Miss compares the agent's context against the reduced `gold_files`.
- The manifest schema gains `excluded_fix_tests` and `gold_files_full`.
- Automated preflight (#13) derives `oracle_tests` and `gold_files` mechanically, never by hand.
- The write-up and LIMITATIONS.md report how many bugs had a reduced oracle.
- flask-6096 stays validated: `oracle_tests` is the crash test only and `gold_files` is
  `[src/flask/app.py]`. Applying only that hunk passes the crash oracle and all 491 baseline tests.

## Amendment 2: Crashes with no library frame

**Found by:** screening and a manual run of click-3805 (issue #16). On Python 3.11, pickling
`click._utils.Sentinel` members fails with `ValueError: <object ...> is not a valid Sentinel`. The
traceback has only two kinds of frames: the test file and `/usr/local/lib/python3.11/enum.py`.
No frame inside `src/click/` is ever on the stack, because the bug is a missing `__reduce_ex__`
that stdlib code trips over.

**The problem:** spec §5.5 defines `target_frame` as the "innermost library frame", and §6 builds
the agent's context from "the source files containing trace frames". With no library frame:

- `target_frame` is undefined, and so is repro oracle condition 2 (§7.4).
- The context would contain no source files, so the run is close to a guaranteed Fault
  Localization Miss. That says nothing about the agent and would bias H2.

**The rule:**

1. **Library frame:** a traceback frame whose file, resolved relative to the repository root, is
   inside the project's importable package source (for example `src/click/`, `src/flask/`). These
   are never library frames:
   - test files, matched by the §7.1 test patterns (`tests/*`, `test_*.py`, `*_test.py`,
     `conftest.py`)
   - the standard library
   - `site-packages` (installed dependencies)
   - synthetic files such as `<string>` or `<frozen ...>`
2. **`target_frame`** is the innermost library frame of the failure, recorded as `file` (relative to
   the repo root) and `function`. Frames from the standard library or dependencies *inside* it,
   such as `int()` raising for flask-6096, don't change the target.
3. **New selection criterion 6 (§5.2):** the crash traceback must contain at least one library
   frame. Under Amendment 1, a fix test whose failure has no library frame cannot be an oracle test
   and goes in `excluded_fix_tests`. If no fix test has a library frame, the bug is a dataset
   exclusion.
4. **One target:** every test in `oracle_tests` must share the same `target_exception` and
   `target_frame`. If the reproducing fix tests disagree, the bug is excluded with that reason,
   because a single synthetic payload cannot describe two different crashes.

**Impact:**

- click-3805 is excluded.
- Preflight automation (#13) and the payload builder apply rules 1–4 mechanically from the
  harness pytest plugin's frame records (#11).
- LIMITATIONS.md notes that the benchmark leaves out crashes raised entirely outside the library's
  own code. That is a scope limit, not a model result.
