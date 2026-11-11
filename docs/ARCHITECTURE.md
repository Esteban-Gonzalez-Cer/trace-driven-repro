# Architecture

TODO: full write-up in Phase 6. Components land here as they are built.

## Image builder (`tracerepro.dataset.image_builder`, Phase 1)

`make build-images [MANIFESTS=...] [BUILD_FLAGS=--no-cache]` builds one `bug_env:{id}` image per
manifest:

1. **Repo:** a bare clone cache under `work/repos/` (GitHub `owner/name`, or `local:` path such as
   the toy fixture's `repo.bundle`). Both `c_bug` and `c_fix` must exist.
2. **Snapshot:** the `c_bug` tree is written with a throwaway index and `checkout-index`, with no
   `.git` and ignoring `export-ignore`. Fix-commit tests never enter the image.
3. **Dockerfile:**
   - `FROM python:{python_version}-slim`
   - provenance labels
   - non-root user 1000
   - `pip install --no-deps -r` the manifest's pinned `deps_lock`
   - an editable install of the project, then `pip check`, which proves the lock is complete
     (with `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0`, since the snapshot has no `.git` for
     VCS-versioned projects)
4. **Build:** with network on (Docker SDK).
5. **Record:** `pip freeze --all` runs in an offline container, is checked against the lock, and
   is written with image and base-image ids to `work/builds/{id}/build.json`. Rebuilding with
   `--no-cache` must give the same freeze.

Locks (`benchmark/locks/*.txt`) accept only exact `==` pins. Options such as `--index-url` and
`-e`, and direct URLs, are rejected.

## Container runner (`tracerepro.sandbox.runner`, Phase 1)

`run_sandboxed(image, argv, workspace, limits=...)` runs one command and returns exit code,
output (last 200k chars), duration, and whether it timed out or was OOM-killed.
`run_pytest(image, test_ids, workspace)` adds the pytest flags and reads results back.

- **Workspace:** a throwaway host directory per run. `repo/` is a copy of the image's `/repo`
  (`create_workspace(root, image)`). Patches are applied and oracle test files copied in
  (`copy_into`) on the host before the run. `repo/` is mounted **read-only** at `/src` and copied
  at start into `/repo`, which like `/out` is a size-capped in-memory tmpfs (1 GB and 64 MB by
  default): nothing the code under test writes reaches the host disk.
- **Supervisor** (`sandbox/plugin/tracerepro_supervisor.py`): process 1 of every container,
  started with `python -I -S` and a per-run secret in its environment. It makes itself
  non-dumpable (code running as the same user cannot read its memory or environment), removes
  the secret from its children's environment, copies `/src` to `/repo`, runs the command with a
  pipe for the plugin's audit events, kills every leftover process when the command ends, then
  sends the allow-listed result files (`results.json`, `junit.xml`; regular, size-capped) and
  the exit code as log lines carrying the secret. The host trusts only those lines, so code
  under test cannot forge an exit code, a result file or an audit event by printing; the exit
  code is also read from `docker wait`, since process 1 cannot be killed from inside. The log is
  read as a capped stream: past 64 MB the run is killed as a flood.
- **Lockdown (spec §7.2):** `--network none`, `--read-only` root, tmpfs `/tmp` (also `HOME`),
  `--cap-drop ALL`, `no-new-privileges`, PID/CPU/memory limits (no swap), host-enforced
  wall-clock timeout (kill, then remove), no Docker socket, explicit environment only,
  `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, `-p no:unraisableexception` (a warning
  fired at garbage collection must not fail whichever test is running). The container runs as the invoking host
  user when that user is not root, otherwise as uid 1000; the tmpfs mounts are owned by it.
- **Results:** pytest writes `--junitxml` and the harness plugin
  (`sandbox/plugin/tracerepro_pytest_plugin.py`, stdlib-only, mounted read-only) writes
  `results.json`: per test the pytest node id, phase and outcome, and per failure the exception
  type, message and traceback frames (innermost last, paths relative to `/repo`). Collection
  errors are recorded with their report text. The repro gate and oracle checks read this file;
  node ids match manifest ids directly, so no JUnit name mapping is needed.
- **Trust:** result files are written inside the sandbox, so `parse_results` caps their size,
  refuses symlinks, and checks the schema.

## Patch validator (`tracerepro.sandbox.validator`, Phase 1)

`validate(diff, limits)` runs on the host before any patch executes (spec §7.1). It parses the
git-style unified diff itself, following hunk line counts, and returns every violation with a
rule name: `malformed`, `binary`, `unsafe_path` (absolute, `..`, backslash, `.git/`), `symlink`,
`file_mode`, `test_file`, `config_file` (`conftest.py` at any depth, `pytest.ini`, `tox.ini`,
`setup.cfg`, `sitecustomize.py`, `usercustomize.py`, `*.pth`), `dependency_or_ci`
(`pyproject.toml`, `setup.py`, `requirements*.txt`, `.github/`), `too_many_files`,
`too_many_lines` (defaults 3 files, 40 lines). Paths must carry `a/` and `b/` prefixes, so the
checked paths are exactly what `git apply -p1` writes.

`apply_patch(workspace, diff)` validates, then runs `git apply --check` and `git apply` in the
workspace copy, with `GIT_CEILING_DIRECTORIES` set so an enclosing repository is never used.
Results: `applied`, `rejected` (a Scope Violation) or `failed` (valid but does not apply).

Path rules live in `tracerepro.sandbox.paths` and are shared with preflight and the evaluator
(`is_test_path`, `is_docs_path`, `is_library_frame`).

## Preflight automation (`tracerepro.dataset.preflight`, Phase 1)

`make validate-gold [CANDIDATES=...]` turns each `benchmark/candidates/{id}.yaml` (fix commit,
Python version, dependency strategy) into a validated manifest, or an exclusion with a reason.
It is a mechanical version of `docs/preflight/flask-6096.md`:

1. `c_bug` = first parent of `c_fix`. Changed files are split by `sandbox.paths` into tests, docs
   (dropped, Amendment 3), deny-listed (exclusion) and source; criterion 3 is checked on source.
2. The lock (`uv-lock`, `compile` capped at a date, or an existing `file`) is written and
   `bug_env:{id}` is built with the image builder.
3. The fix commit's test files run on `c_bug` and with the gold source diff. A failing fix test is
   an oracle test if its exception was raised outside the test, its traceback has a library frame,
   and it passes with gold. All oracle tests must share one target (Amendment 2). Other failing fix
   tests become `excluded_fix_tests` with the reason (Amendment 1).
4. The whole suite runs 3 times on `c_bug` in evaluation layout: oracle tests must crash the same
   way each time, varying tests become `flaky_set`, always-passing tests the baseline.
5. Subsets of the source files are tried smallest first; the first that makes the oracle pass with
   no baseline regression is `gold_files` (Amendment 1).
6. The oracle is re-run with `--tracerepro-locals` and `tracerepro.payload.builder` writes the
   synthetic Sentry-style payload: frames from the outermost library frame inward, source context
   and masked local reprs for library frames, no test frames.

7. The oracle failure's kind is recorded as `crash_kind` (`crash_kind()`): `library_raise` if
   the innermost frame is a library `raise` statement, otherwise `python_failure`.

Outputs: `benchmark/manifests/{id}.yaml` (deterministic YAML), `benchmark/locks/{id}.txt`,
`benchmark/payloads/{id}.json`, an upserted `benchmark/screening_log.jsonl` row, and
`work/preflight/{id}/report.json` with every run's outcome counts and durations. Re-running gives
byte-identical manifests and payloads.

A candidate that is excluded or fails has its `bug_env:{id}` image removed (with its untagged
layers) so screening does not pile up images; `--keep-images` keeps them for debugging. Images
rebuild from the candidate on demand.

## Patch oracle (`tracerepro.evaluate.patch_oracle`, Phase 1)

`score_patch(manifest, diff)` implements spec §7.3 and §7.4 for patches: validate on the host
(violation → `scope_violation`), create a fresh workspace from `bug_env:{id}`, copy the fix
commit's test files in from `c_fix`, re-apply the diff from the host (`apply_failed` if it does
not apply), then run `oracle_tests` plus `baseline_pass_set`. `correct` needs every oracle test to
pass and no baseline test to fail; otherwise `incorrect`. A run that does not finish is
`infra_error`, never a model failure. `gold_diff(manifest, repo)` gives the human fix restricted to
source files, for known-answer tests.

Amendment 4 adds result-integrity layers to `score_patch`: after applying the patch it runs the
static scanner (`sandbox/scanner.py`; harness access → `scope_violation`), adds a canary test,
and after the run checks the plugin's audit log and the consistency of the results file, the
container exit code, `junit.xml` and the console summary (`evaluate/integrity.py`; any problem →
`tampered`). A normal exit with no results is `incorrect`. Suspicious code and behaviour the gold
patch's run did not show become `Verdict.flags`, rendered by `review_report` for the manual audit.
See `SECURITY.md` (result integrity).

Known-answer tests (`tests/evaluate/test_known_answer.py`) check, for the toy fixture and every
`benchmark/manifests/*.yaml`, that the gold patch is correct, an empty patch is incorrect, and
patches that edit an oracle test or add a skipping `conftest.py` are rejected. Real bugs carry the
`realbugs` marker and run in the CI `known-answer` job; they also require the gold patch to carry
no flags. Red-team tests (`tests/evaluate/test_red_team.py`) cover an injection payload,
malicious patches, and plain and disguised result forgery.

## Prompts and output contracts (`tracerepro.agent.prompts`, `parse`, `edits`, Phase 2)

- `load_prompt(name, version)` reads `prompts/{name}_{version}.md` and gives its sha256.
  `patch_message` and `repro_message` build the user message: the prompt text, then data blocks
  whose tags are hashed from the message content (`build_message`, `content_tokens`). `leak_check(reply, prompts, tags)` lists what
  of our own text a reply repeats.
- `parse_repro(reply)` returns the test file from the single Python code block;
  `parse_edits(reply)` returns the `Edit(path, search, replace)` blocks. Both return a
  `ContractFailure(kind, detail)` instead when the reply breaks the contract; its `label` is the
  taxonomy category.
- `apply_edits(tree, edits)` applies the edits to a scratch copy (the tree is not touched) and
  returns `EditResult(diff, files)`, the diff written by git (`git add`, then `git diff`, so new
  files and missing final newlines are handled the way `git apply` expects), or a
  `ContractFailure`. Contracts and labels are in `METHODOLOGY.md`.
## Context assembler (`tracerepro.context.assembler`, Phase 2)

`assemble(payload, tree, budget=250_000)` returns a `Context`: the selected files
(`ContextFile`: path, role `trace` or `import`, the numbered lines shown, and shape `whole`,
`outline`, `window` or `cut`), the truncation stage reached, and the files dropped. `render()`
gives the text shown to the agent; `file_list()` the files it was shown, and `shown_lines()` the
exact line ranges, kept in the trial record for the Fault Localization Miss check
(`tracerepro.evaluate.coverage.context_coverage`, Amendment 5, run at evaluation time). Selection, ordering and the truncation rule are in `METHODOLOGY.md`.

- Frame paths come from the payload and are treated as untrusted: only relative paths to `.py`
  files that are library frames, not tests, with no `..`, that resolve inside the tree and are
  not symlinks are read.
- `import_refs` resolves imports with `ast` against the package layout (`src/` or flat) and
  counts how often each imported file's names are used; files filtered out (tests) never fall
  back to their package's `__init__.py`.
- `python -m tracerepro.context.assembler benchmark/manifests/*.yaml [--show]` prints, per bug,
  the files chosen, the stage and the size (it materializes `c_bug` from the cached clone).
## LLM client (`tracerepro.agent`, Phase 2)

`LLMClient(provider).complete(request, budget)` returns a `Completion` (text, stop reason, token
usage, latency, the provider's list-price estimate). A `Request` is a system prompt, the messages,
`Settings(model, effort, max_output_tokens)` and the prompt version and hash.

- **Providers** (`agent/providers/`) only turn a request into a completion and classify failures:
  `ClaudeCLI` (the user's Claude plan, see below) and `FakeProvider` (scripted, for tests).
- **Failures** (`agent/errors.py`): `TransientError` (network, overload, timeout) is retried with
  exponential backoff and jitter and never counts as an attempt; when retries run out the call
  is an `InfraFailure`, rerun later and never a model failure. `UsageLimitReached` (the plan's
  limit) and `PacingLimitReached` (our guard) stop the run cleanly for a later resume.
  `TokenCapReached` ends a run, which then submits its best patch. A model's own failure (a
  refusal, output cut off) is a `Completion` with `model_failed` true: a failed attempt.
- **Log:** one JSON line per call, failed or not: time, provider and version, model, effort,
  prompt version and hash, request hash (sha256 of the canonical request), the full request and
  response, usage, latency, cost estimate, retries, cache hit.
- **Cache:** with `cache_dir`, an identical request returns the stored completion and uses no
  plan usage. Measured and final runs turn it off (trials must be independent).
- **Limits** (`agent/usage_guard.py`): `TokenBudget` is the per-run token cap shared by both
  pipelines; `UsageGuard` keeps a ledger (`work/llm-usage.json`) and enforces calls per hour,
  calls per day and a pause between calls. Cached answers never touch it.

**Claude CLI provider** (`agent/providers/claude_cli.py`). Runs `claude -p --model … --effort …
--system-prompt … --tools "" --restricted --strict-mcp-config --disable-slash-commands
--permission-prompts none --no-session-persistence --output-format json` as an argument list,
with the prompt on stdin, in a fresh empty temporary folder, with an allow-listed environment
(home, user, locale, temp dir, a fixed `PATH`, the output limit). API-key variables and the
parent Claude Code session's variables are never passed, and there is no `--fallback-model`.
Earlier turns are rendered into the one prompt, so each call is stateless. The JSON reply is
checked before it is trusted: one turn (more means the answer was continued past the output
limit, counted as cut off), no tool or permission requests, no server tools, no subagents, and
exactly the requested model; anything else is an `InfraFailure`.

`make llm-smoke [MODEL=…]` makes three real calls: `ping` (usage and the CLI's own token
overhead), `hands-off` (a canary file in the working folder and a prompt asking to read it, run
a command and fetch a page: passes only with no tool use and no canary in the reply) and
`max-tokens` (a long answer under a tiny limit must come back as `max_tokens`).

## Pipelines and the benchmark runner (`tracerepro.agent.pipeline`, `tracerepro.experiment`, Phase 2)

- **Workbench** (`agent/workbench.py`): `DockerWorkbench(manifest, root)` copies the image's
  `/repo` once (the `c_bug` tree, used for the context and the edits), then for each attempt
  copies it again, applies the diff with `apply_patch`, adds any extra files (Pipeline A's
  repro test) and runs the visible baseline with `run_pytest` in the usual locked-down sandbox.
  It returns a `TrialRun`: applied or not, regressions, the last 60 lines of output. Everything
  is removed on exit.
- **Pipeline B** (`run_pipeline_b(bug, client, bench, config)`): the attempt loop described in
  `METHODOLOGY.md`. It returns an `AgentResult`: status, the submitted diff, every `Attempt`
  (outcome, taxonomy label, reply, diff, feedback, tokens), tokens used, prompt-leak hits, the
  context summary and the prompt hashes. `InfraFailure` and the usage limits propagate.
- **Pipeline A** (`run_pipeline_a`): repro replies judged by `agent/repro_gate.py` (`judge`,
  with the target from `payload_target`), the test placed at `repro/test_repro.py` through the
  workbench's `extra_files` and run alone (`baseline=False`); then `patch_loop` with the repro as
  an extra file and `repro_passes` as the acceptance condition.
- **Trial folder** (`agent/record.py`): `runs/{run_id}/{bug}/{pipeline}/{model}/trial_{n}/` with
  `calls.jsonl`, each attempt's reply, diff and feedback, `final.diff`, `record.json` (written
  last), then `verdict.json` and `review.md` from the evaluator.
- **Runner** (`python -m tracerepro.experiment CONFIG`, `make run-benchmark`): reads a run
  config (`benchmark/configs/*.yaml`: model, effort, prompt version, pipelines, trials, bugs,
  token cap, context budget, cache, usage guard), builds the run id
  (`{name}-{commit}-{prompt version}-{model}-cli{version}`), runs each trial on a fresh
  workbench, then scores the submitted diff with `score_patch`. Finished trials are skipped; a
  trial interrupted by an infrastructure failure or a usage limit is moved to
  `trial_{n}.aborted-{time}` and rerun next time; a usage limit stops the run (exit code 3).

## Full evaluation (`tracerepro.evaluate.repro_oracle`, `taxonomy`, Phase 3)

`experiment.evaluate(manifest, result)` grades one trial: context coverage (Amendment 5), the
repro oracle for Pipeline A (`judge_repro(manifest, test, gold_diff, work_dir=...)`: the test on
`c_bug`, then on `c_bug` plus the gold diff, each in a fresh sandbox with a canary and
`check_integrity`), the patch oracle (`score_patch`), and finally `label_run(pipeline, result,
verdict)`, which returns `(label, solved)`. Rules in `METHODOLOGY.md`.

## Screening tools (`tracerepro.dataset.find_candidates`, preflight batches, Phase 3)

- `find_candidates LIB... [--since DATE] [--limit N] [--per-library K] [--write]` lists merged
  pull requests with `gh pr list` (read-only), filters them with `keep` (criterion 3's split
  comes from `preflight.split_changes`), orders them by `crash_hint`, and with `--write` writes
  `benchmark/candidates/{lib}-{number}.yaml` (`split: test`) from the library's environment
  profile in `benchmark/screening_profiles.yaml`.
- `python -m tracerepro.dataset.preflight` keeps a `Batch`: test candidates of a library that
  already has `--cap-per-library` (6) test bugs are skipped, the batch stops at `--stop-at` (30)
  test bugs, and a summary is printed at the end.
