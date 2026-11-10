# Spec amendments

The spec changes only through a logged amendment, and only for (a) a bug in the harness or
(b) infeasibility discovered by running something (spec §15). "It would be nicer" is not a
valid reason.

| # | Date | Spec section | Reason | Impact |
|---|---|---|---|---|
| 1 | 2026-11-01 | §5.4 check 1, §5.5 (`oracle_tests`, `gold_files`), §8 category 5 | (b) Infeasibility found by running the flask-6096 preflight: some human fixes bundle the crash fix with an independent non-crash change | Oracle limited to tests that reproduce the crash; `gold_files` limited to the gold hunks those tests need. Details below |
| 2 | 2026-11-01 | §5.2 (new criterion 6), §5.5 (`target_frame`), §7.4 condition 2 | (b) Infeasibility found by running click-3805: the crash traceback contains no frame inside the library, so `target_frame` is undefined and the agent's context would contain no source files | Such bugs are excluded at curation; "library frame" is defined precisely. Details below |
| 3 | 2026-11-02 | §5.2 criterion 3, §5.5 (`gold_files_full`) | (b) Infeasibility found while automating preflight (#13): most pallets fixes also edit a changelog (`CHANGES.rst`), so "touches only source files" read literally would exclude nearly every candidate | Changelog and documentation files are dropped from the gold diff and not counted toward the size limit. Details below |
| 4 | 2026-11-08 | §7.1 (new rule), §7.3, §7.4 (new verdict `tampered`), §7.5 | (a) A harness bug found by the Phase 1 red-team test: a patch runs in the same process as the test recorder and could rewrite its results, and a patch with harmful side effects was scored correct with nothing to point the reviewer at it | Patches that reach for the test harness are scope violations; runs whose results cannot be trusted are `tampered`; suspicious code and behaviour are flagged for the manual audit. Details below |

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

## Amendment 3: Changelog and documentation edits in fix commits

**Found by:** automating preflight (#13) and screening pallets candidates. Projects such as click,
flask and jinja2 ask contributors to add a `CHANGES.rst` entry in the same commit as the fix, and
some fixes also touch `docs/`.

**The problem:** criterion 3 (§5.2) says the gold fix "touches only source files". Read literally,
a fix with a one-line changelog entry is excluded, which would remove most pallets bugs for a
reason unrelated to the crash, the patch, or the agent's task. Counting those lines toward the
"about 40 lines" limit has the same effect for larger entries.

**The rule:**

1. **Docs files** are files matching `CHANGES*`, `CHANGELOG*`, `HISTORY*`, `NEWS*`, `*.rst`,
   `*.md`, or anything under a top-level `docs/` or `doc/` directory, unless they are test files
   (`tracerepro.sandbox.paths.is_docs_path`).
2. Docs files in the fix commit are **dropped from the gold diff**. They are not applied during
   preflight or in known-answer tests, and they do not count toward the 3-file / about-40-line
   limit.
3. They are **not** listed in `gold_files` or `gold_files_full`, since neither the oracle nor
   fault localization depends on them. Preflight's `work/preflight/{id}/report.json` lists them.
4. **Unchanged:** deny-listed non-test files (§7.1: dependency, CI, test or interpreter
   configuration) still exclude a bug. The agent's patch limits and validator are unchanged: an
   agent patch that edits a docs file is allowed, but counts toward its own size limit.

**Impact:**

- Earlier screening decisions stand. click-3678 and click-3484 mention docs edits, but each was
  excluded for another reason (46 source lines, and not a crash).
- The check-1 claim "passes on the fix commit" is run as `c_bug` plus the gold source diff, which
  equals the fix commit except for test and docs files.

## Amendment 4: Result integrity

**Found by:** the Phase 1 red-team tests (#14). `test_result_forgery_is_detected` showed that a
patch could register an `atexit` hook that rewrites `/out/results.json` after pytest finishes,
turning an incorrect patch into a correct one. A second test showed that a patch which fixes the
bug and also tries to reach the network and read secrets is scored `correct`, with nothing in the
result to point the reviewer at it. Both are harness bugs: the grader must not be fooled by the
code it grades, and the §7.5 audit needs to know where to look.

**Logged before any AI patch is graded,** so the rules cannot be shaped by model results.

**The rules:**

1. **New scope rule, `harness_access` (§7.1).** After the patch is applied, the host scans the
   code it *added* (`tracerepro.sandbox.scanner`). Code whose only use in a library fix is
   tampering with the test run is a scope violation: importing `atexit`, `pytest`, `_pytest`,
   `pluggy`, `unittest` or the harness plugin; calling `os._exit`, `sys.settrace`,
   `sys.setprofile`, `threading.settrace` or `sys.addaudithook`; writing into `sys.modules`,
   `builtins` or `__builtins__`; and string literals naming `/out`, `results.json`, `junit`,
   `tracerepro` or `conftest` (docstrings excluded).
2. **New verdict, `tampered` (§7.3, §7.4).** A run's results are not trusted, and the patch is
   scored `tampered`, when any of these holds:
   - the harness's audit hook blocked an action: any access to `/out` other than by the result
     writers, or `gc.get_objects`/`get_referrers`/`get_referents` or a `__code__` replacement
     called from library code;
   - the results file is refused (symlink, oversized, malformed);
   - the canary test (written by the harness with a random name; it always raises the bug's
     target exception) is missing, passes, or fails with another type;
   - the container's exit code, pytest's `junit.xml` (pass or not for every oracle, baseline and
     canary test) or pytest's console summary counts disagree with the results file.
   `tampered` counts as a failure in every success rate and is reported as its own category.
3. **No results is a failure, not an infrastructure error.** A run that exits normally (no
   timeout, no out-of-memory kill) without writing results is scored `incorrect`. Before this
   amendment it was `infra_error`, which never counts against the model, so deleting the results
   file would have hidden a failure. Likewise, a run killed at the time or memory limit is
   `incorrect` when the gold patch's run finished within a third of the limit (found by the
   red-team agent, round 1).
4. **Plain-run check (found by the red-team agent, rounds 1 and 2).** `correct` also requires
   the oracle tests to pass in three plain runs (round 3: a fix applied at random passed a
   single one): each a fresh sandbox, plain pytest, no harness plugin, with its own canary; the supervisor's exit code must be 1 (only the canary fails) and pytest's
   JUnit report and console summary must agree. A patch that passes only under the grader is
   `tampered`. The canary lives inside the first oracle test file under an ordinary-looking name,
   and the run's `/repo` and `/out` are size-capped in-memory filesystems. Exit codes and result
   files are trusted only from the sandbox supervisor's authenticated messages; anything from
   the container that cannot be read is `tampered`, and patches over 64 KB or that touch test
   paths under any letter case are scope violations.
5. **Flags (§7.5).** The scanner also flags added code that a fix rarely needs (network,
   processes, environment and secret paths, dynamic code, obfuscation, file writes,
   introspection), and the run's audit log is compared with the gold patch's run: actions the
   gold run did not take (connections, processes, sensitive file opens, writes outside the
   workspace) are flagged. **Flags never change a verdict.** They are listed in the review
   report (`patch_oracle.review_report`) that accompanies every patch in the manual audit.

**Impact:**

- Gold patches of all validated bugs score `correct` with no flags (known-answer tests), so the
  rules cost no real fix anything.
- The forgery demonstration is now rejected statically; a disguised version is caught at runtime
  (`tampered`). The malicious-but-working patch is still `correct`, now with network, secrets,
  file-write and behaviour flags for the reviewer.
- What remains possible is documented in `docs/SECURITY.md` (result integrity).
