# Spec amendments

The spec changes only through a logged amendment, and only for (a) a bug in the harness or
(b) infeasibility discovered by running something (spec §15). "It would be nicer" is not a
valid reason.

| # | Date | Spec section | Reason | Impact |
|---|---|---|---|---|
| 1 | 2026-11-01 | §5.4 check 1, §5.5 (`oracle_tests`, `gold_files`), §8 category 5 | (b) Infeasibility found by running the flask-6096 preflight: some human fixes bundle the crash fix with an independent non-crash change | Oracle limited to tests that reproduce the crash; `gold_files` limited to the gold hunks those tests need. Details below |

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
