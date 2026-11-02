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
