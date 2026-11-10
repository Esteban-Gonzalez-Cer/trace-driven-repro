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
  (`create_workspace(root, image)`), mounted read-write at `/repo`. Patches are applied and oracle
  test files copied in (`copy_into`) on the host before the run. `out/` is mounted at `/out` for
  result files.
- **Lockdown (spec §7.2):** `--network none`, `--read-only` root, tmpfs `/tmp` (also `HOME`),
  `--cap-drop ALL`, `no-new-privileges`, PID/CPU/memory limits (no swap), host-enforced
  wall-clock timeout (kill, then remove), no Docker socket, explicit environment only,
  `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`. The container runs as the invoking host
  user when that user is not root (so Linux bind mounts stay writable), otherwise as uid 1000.
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
