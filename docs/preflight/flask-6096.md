# Manual preflight: flask-6096

Phase 0, task 9. I ran the spec §5.4 gold-patch preflight by hand with plain `git` and `docker`, to learn what the
Phase 1 harness has to do. Run on 2026-11-01 (macOS, Docker 29.2.1, `python:3.11-slim`).

**Verdict: VALIDATED** as a dev-set bug under
[Amendment 1](../AMENDMENTS.md#amendment-1-mixed-fixes-crash-only-oracle): `oracle_tests` is the
crash test only and `gold_files` is `[src/flask/app.py]`. The fix bundles a second, non-crash
change (see *Mixed fix*).

Manifest values under Amendment 1:

```yaml
oracle_tests:
  - tests/test_basic.py::test_run_from_config[None-None-[::1]:8080-::1-8080]
excluded_fix_tests:
  - id: tests/test_testing.py::test_session_transaction_ipv6
    reason: fails on c_bug with AssertionError, not the target ValueError (non-crash cookie-domain fix)
gold_files: [src/flask/app.py]
gold_files_full: [src/flask/app.py, src/flask/testing.py]
```

## Bug

| Field | Value |
|---|---|
| repo | `pallets/flask` |
| issue / fix PR | [#6093](https://github.com/pallets/flask/issues/6093) / [#6096](https://github.com/pallets/flask/pull/6096) |
| `c_bug` | `514fc6b3e8402e4c646d5284e97a4f0ab50a7c4b` (first parent of the merge, "Merge branch 'stable'") |
| `c_fix` | `05e9c6bd630ecf4ec0ec884b1fc7901663737bc7` (merge commit of #6096) |
| `fix_date` | 2026-08-11 |
| `target_exception` | `ValueError: invalid literal for int() with base 10: ':1]:8080'` |
| `target_frame` | `src/flask/app.py` :: `run` (innermost library frame; the raise is in builtin `int()`) |
| crash oracle | `tests/test_basic.py::test_run_from_config[None-None-[::1]:8080-::1-8080]` |
| gold diff | `src/flask/app.py` +7/-4, `src/flask/testing.py` +1/-1, tests +15 |
| `python_version` | 3.11 (`requires-python >=3.10`) |

Root cause: `Flask.run()` splits `SERVER_NAME` with `.partition(":")`, so `"[::1]:8080"` yields the
port string `":1]:8080"` and `int()` raises.

## Results

| §5.4 check | Result |
|---|---|
| 1. Gold test fails on `c_bug` with the target exception, passes on `c_fix` | **Pass** for the crash test: `ValueError` raised from `src/flask/app.py:735 in run` on `c_bug`; passes on `c_fix` |
| 2. No gold regressions | **Pass**: all 491 tests that pass on `c_bug` also pass on `c_fix` (493 pass there, including the 2 new ones) |
| 3. Stability (3 runs on `c_bug`) | **Pass**: same 491/491 pass set in all 3 runs; `flaky_set = []` |
| 4. Offline isolation | **Pass**: image built with network on; every pytest run used `--net=none` |

Cost: the image builds in about 10 s and the full suite runs in about 0.7 s. Flask is cheap to host.

## Mixed fix (resolved by Amendment 1, option 1)

PR #6096 fixes two `.partition(":")` uses:

- `app.py::run`: the **crash** above.
- `testing.py::session_transaction`: a **non-crash** bug. The cookie domain becomes `"["` for IPv6
  hosts. Its new test, `test_session_transaction_ipv6`, fails on `c_bug` with `AssertionError`
  (`'None' == '42'`), not with the target exception.

Applying only the `app.py` hunk to `c_bug` makes the crash oracle pass, while
`test_session_transaction_ipv6` still fails. The two changes are independent. With only that hunk
applied, the full `c_bug` suite still passes (491/491), so `{src/flask/app.py}` is the minimal
`gold_files` set required by Amendment 1, rule 4.

If both new tests were oracle tests, check 1 would fail and the bug would be excluded. With only
the crash test as the oracle, every check passes. That choice raises a second question about
`gold_files`. Spec §5.5 defines it as "files touched by the human fix", which here includes
`testing.py`. That file is not in the crash trace, and the Fault Localization Miss label
(taxonomy #5) takes precedence over Regression and Incorrect Patch. So every failed run on this
bug would be labeled a localization miss because of a change the oracle doesn't test.

Options:

1. **Restrict the oracle (chosen):** `oracle_tests` = the tests that fail on `c_bug` with the
   target exception. `gold_files` = the files touched by the gold hunks those tests need, after
   checking that this subset of the gold patch makes the oracle pass.
2. **Exclude mixed fixes:** stricter and simpler, but lowers yield.

**Decision (2026-11-01):** option 1, logged as
[Amendment 1](../AMENDMENTS.md#amendment-1-mixed-fixes-crash-only-oracle). It adds a no-regression
check on the gold subset and two manifest fields: `excluded_fix_tests` and `gold_files_full`.

## Problems hit (what the harness needs)

1. **Pallets merges with merge commits.** `c_fix` has two parents, and `c_bug` must be the
   first parent. I checked that `git diff --stat c_bug c_fix` matches the PR's file list.
   *Harness:* take the first parent and assert that the diff matches the PR files.
2. **Mixed fixes exist.** One case in the first validated bug. *Harness:* derive `oracle_tests`
   from the fix-commit tests that newly fail with the target exception, not from all new tests.
3. **Parametrized test ids** contain `[`, `]`, `:` and `-`, as in
   `test_run_from_config[None-None-[::1]:8080-::1-8080]`. *Harness:* pass ids as argv list items,
   never through a shell.
4. **JUnit ids differ from pytest node ids.** JUnit gives `tests.test_basic` and
   `test_run_from_config[...]`; manifests use `tests/test_basic.py::...`. *Harness:* one tested
   mapping function, or have the harness pytest plugin (plan §5.2) record node ids directly.
5. **Quoting bit me.** A zsh variable holding several `-v` flags became a single docker argument
   ("invalid spec: too many colons"). *Harness:* build docker argv as lists and never shell
   strings, which also matters for injection safety.
6. **Each repo pins dependencies differently.** Flask uses `uv.lock` with dependency groups. I used
   `uv export --frozen --no-emit-project --no-default-groups --group tests --python 3.11` to get
   pinned `requirements.lock.txt`. Click and requests will differ. *Harness:* a per-repo dependency
   strategy, with the exported lock stored as `deps_lock`.
7. **Patches need an editable install.** `pip install --no-deps -e /repo` makes edits under
   `src/` take effect without reinstalling. The repo is `chown`ed to the non-root user.
8. **A read-only root filesystem works for testing.** It needed `-p no:cacheprovider`,
   `PYTHONDONTWRITEBYTECODE=1`, `HOME=/tmp` and a tmpfs on `/tmp`. Applying a patch needs a
   writable copy of `/repo` (spec §7.2). Here I bind-mounted single files read-only over the tree.
   *Harness:* a throwaway writable copy per run.
9. **Bringing in oracle tests:** bind-mounting the `c_fix` test files over `/repo/tests/...`
   worked for the gold check. This is the same mechanism the evaluator needs ("copy in from
   `C_fix`").
10. **Screening:** for click-3805 the exception is raised inside stdlib `enum` during unpickling,
    so the trace may contain **no library frame** at all. The spec needs a rule for
    `target_frame` in that case (probably: exclude, since the agent's context would be empty).

## Commands (reproducible)

```bash
git clone https://github.com/pallets/flask.git work/flask
C_BUG=514fc6b3e8402e4c646d5284e97a4f0ab50a7c4b; C_FIX=05e9c6bd630ecf4ec0ec884b1fc7901663737bc7
git -C work/flask diff --stat $C_BUG $C_FIX
mkdir -p work/flask-6096/{bug,fix,out}
git -C work/flask archive $C_BUG | tar -x -C work/flask-6096/bug   # no .git
git -C work/flask archive $C_FIX | tar -x -C work/flask-6096/fix
(cd work/flask-6096/bug && uv export --frozen --no-emit-project --no-default-groups \
   --group tests --no-hashes --python 3.11 -o ../requirements.lock.txt)
```

Dockerfile (build context is one exported tree with `requirements.lock.txt` copied in):

```dockerfile
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --create-home --uid 1000 runner
WORKDIR /repo
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install -r /tmp/requirements.lock.txt
COPY . /repo
RUN pip install --no-deps -e /repo && chown -R runner:runner /repo
USER runner
```

Locked-down run, an approximation of spec §7.2:

```bash
docker run --rm --net=none --read-only --tmpfs /tmp:rw,size=256m \
  --user 1000:1000 --cap-drop=ALL --security-opt no-new-privileges \
  --pids-limit 256 --cpus 2 --memory 2g -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
  -v "$PWD/out:/out:rw" \
  -v "$PWD/fix/tests/test_basic.py:/repo/tests/test_basic.py:ro" \
  -v "$PWD/fix/tests/test_testing.py:/repo/tests/test_testing.py:ro" \
  bug_env:flask-6096-bug \
  timeout 600 python -m pytest -p no:cacheprovider -q --junitxml=/out/gold_bug.xml \
  "tests/test_basic.py::test_run_from_config" tests/test_testing.py::test_session_transaction_ipv6
```

I compared stability and regressions from the JUnit XML files (`suite_bug_{1,2,3}.xml`,
`suite_fix.xml`) by per-test status, not from console output.
