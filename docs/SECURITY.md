# Security

TODO: full write-up in Phase 6. Must state that delimiters are not a security boundary.

## Containment (Phase 1, tested)

Generated tests and patches are untrusted code. They run only inside the locked-down container
described in `ARCHITECTURE.md` (container runner). `tests/sandbox/test_sandbox_docker.py` checks,
on every CI run:

- network access fails (public IP, private IP, Docker host name);
- writes outside `/repo`, `/tmp` and `/out` fail, including site-packages and the plugin mount;
- a fork bomb stops at the PID limit;
- an infinite loop is killed at the timeout;
- no host environment variable is visible (a canary and a fake API key are set on the host);
- the process is non-root, has no capabilities, has `NoNewPrivs` set, and sees no Docker socket;
- no bytecode is written to the workspace.

## Known limitation: in-process result forgery

The code under test runs in the same process as pytest and the harness plugin, and `/out` is
writable, so a malicious patch could in principle rewrite `results.json` or `junit.xml`. The
container stops this from reaching the host, but not from lying about results. Mitigations:
the patch validator (#12) rejects test and interpreter configuration changes, red-team tests
(#14) try forging results, and every patch judged correct is audited by hand (spec §7.5).
