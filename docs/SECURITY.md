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
- **Scored by behaviour, flagged for intent.** Contained, that patch behaves like the gold fix,
  so it is scored correct, and it carries network, secrets and file-write flags plus behaviour
  flags (`socket.connect` to 1.1.1.1, a write to `/etc/cron.d`) in its review report.
- **Result forgery.** An `atexit` hook that rewrites `/out/results.json` is rejected before it
  runs. The same hook disguised (`__import__("ate" + "xit")`, a path built from fragments) is
  blocked at runtime and scored `tampered`, as is reaching the recorder through
  `gc.get_objects()`.

## Result integrity (Amendment 4)

The code under test runs in the same Python process as pytest and the harness plugin, so no
in-process check can be perfect: code that runs inside the referee can, with enough effort,
interfere with the referee. The aim is to make tampering need several independent tricks at
once, and to make each of them visible. Four layers:

1. **Static scanner** (`sandbox/scanner.py`, host side, before anything runs). Reads only the
   code the patch added. Blocks harness access (`atexit`, pytest internals, `sys.settrace`,
   writes into `sys.modules` or `builtins`, strings naming `/out` or `results.json`) as a scope
   violation. Flags network, processes, secrets, dynamic code, obfuscation, file writes and
   introspection. No third-party scanner: fewer dependencies, smaller supply chain.
2. **Audit hook** (in the harness plugin, installed before the library is imported; Python
   cannot remove it). Blocks every access to `/out` except by the two result writers (checked by
   the calling frame), and garbage-collector walks and `__code__` swaps from library code. Logs
   network, process, ctypes and sensitive-file events to a copy of stderr taken at startup, so
   pytest's capture cannot hide them and the Docker daemon keeps them.
3. **Integrity checks** (`evaluate/integrity.py`, host side, after the run). A canary test with
   a random name must fail with the bug's exception type; the container exit code, `junit.xml`
   and the console summary must all agree with the results file. Any blocked action, refused
   results file or disagreement → `tampered`.
4. **Behaviour comparison and review report** (`evaluate/patch_oracle.py`). The run's audit
   events are compared with the gold patch's run (cached per image); new behaviour is flagged.
   `review_report` gives the §7.5 reviewer a checklist of every flag and blocked action.

**Still possible, by design of in-process testing:**

- writing to a result file through a file descriptor number guessed or inherited, without
  opening it (`os.write` is not an audit event); the canary and the three-way check still have
  to be beaten as well;
- calling code with forged module globals (for example a function built with `types.FunctionType`
  to look like pytest's JUnit writer); the scanner flags `types.FunctionType`, `exec` and
  `__import__`;
- closing or redirecting stderr (`os.dup2`) before acting, so audit lines are lost; flagged
  statically;
- disguises that beat both the scanner's rules and the runtime checks.

Each of these needs deliberate, unusual code in a patch of at most 40 lines, which is exactly
what the manual audit of every correct patch (spec §7.5) reads. The audit stays the last line
of defence.
