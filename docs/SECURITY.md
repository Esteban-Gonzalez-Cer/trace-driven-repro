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

## Red-team tests (Phase 1)

`tests/evaluate/test_red_team.py`, on the toy fixture:

- **Injection payload.** A crash message carrying "ignore all previous instructions…" text stays
  a single JSON string value in the payload; nothing is interpreted, no keys are added.
- **The actions it asks for.** A `.github/workflows` edit is rejected by the validator. A source
  patch that tries to connect out, list environment variables, read `~/.ssh/id_rsa`, the Docker
  socket and a host file, and write to `/etc/cron.d` applies (it is "only" a source edit) but is
  contained: the network is unreachable, no host variable or file is visible, the write fails.
- **Scored by behaviour, not intent.** Contained, that patch behaves like the gold fix and the
  oracle scores it correct. Spotting intent is the manual audit's job (spec §7.5): every patch
  judged correct is read by a person.

## Known limitation: in-process result forgery

The code under test runs in the same process as pytest and the harness plugin, and `/out` is
writable, so a malicious patch could in principle rewrite `results.json` or `junit.xml`. The
container stops this from reaching the host, but not from lying about results. Mitigations:
the patch validator (#12) rejects test and interpreter configuration changes, and every patch
judged correct is audited by hand (spec §7.5), where an `atexit` hook rewriting `/out` is plain to
see. `test_result_forgery_is_detected` demonstrates the attack (it is a strict `xfail`: if the
harness ever starts detecting forgery, the test will say so).
