# Manual audit: the second study (B versus B2)

The grader (the maintainers' crash tests) decides every headline number. This audit checks it
by hand, as the first study's did ([`AUDIT.md`](AUDIT.md)), and adds what the plan calls **fix
quality** ([`ANALYSIS_PLAN_B2.md`](ANALYSIS_PLAN_B2.md)): does a correct patch remove the cause
of the bug, or only its symptom? The files are in `benchmark/audit/b2/`; `make analyze` checks
them against the results and builds the tables in `results/b2/tables.md`.

## Who reviewed, and how

- **First pass:** Claude Opus 5.5 (`claude-opus-5-5`), after the run. It read every patch next
  to the crash report, the maintainers' fix and the oracle tests. Not blinded: the reviewer had
  seen the results, and knew which arm wrote each patch.
- **Second reviewer:** the project owner, on a random sample drawn with seed 20261111
  (`audit second-review --audit benchmark/audit/b2`): 10 correct patches, 5 incorrect, 5 State
  Deficit decisions. **Not done yet**; the sample is listed at the end.

## 1. Patches graded correct

105 correct trials (B 50, B2 55), 52 distinct patches. **None is `not_a_fix`**, so the audit
changes no headline number. 30 patches are `ok`, 22 `ok_with_note`.

**Fix level** (the owner's four questions), by trial:

| fix level | Pipeline B | Pipeline B2 |
|---|---|---|
| root_cause | 38 | 42 |
| other_layer | 3 | 5 |
| symptom | 9 | 8 |

The symptom fixes come from three bugs, in both arms alike:

- **click-3482** (all 6 trials). The pager closes `sys.stdout`, and the crash shows up later
  in the test runner. Every patch makes the runner's buffer ignore `close()`. A real program
  still loses its standard output. The maintainers stop the pager from closing a stream it does
  not own. In B2, the crash check sent back the first patch in all 3 trials, and the retry
  then went to the runner. The fix rules did not steer it to the pager.
- **starlette-3622** (B 3, B2 2). Four patches convert the `memoryview` only in the test
  client. `Response.render`, which the maintainers fix, still hands it to real servers.
- **marshmallow-2894** (all 6). Each patch copies one of the parent class's rules (a `None`
  default allows `None`) after `__init__`, instead of passing the defaults to the parent as the
  maintainers do. Borderline: it holds for this rule, but the mechanism stays.

`other_layer` covers marshmallow-2861 (it skips validators on `missing`, which differs from the
maintainers on a `Length(min=1)` validator) and werkzeug-3110 (the header is set by the caller
of the method the maintainers change).

**Correct, but incomplete.** Four patches remove the cause on the path that crashed but leave
the same mechanism next to it:

- more-itertools-1125: `partial_product` is left as it was;
- starlette-3372: the text and bytes branches are left;
- starlette-3622 #5: typed `memoryview`s still pass through;
- more-itertools-1276 #5: float values drift.

The beyond-the-crash tests catch exactly these (section 4).

One B2 patch (jinja2-1702) is identical to the maintainers' fix, byte for byte. That bug was
fixed in 2024, before the model's training cutoff.

## 2. Sample of patches graded incorrect

14 of 56 incorrect patches, drawn at random. **All 14 rejections are right**: false-negative
rate 0% (95% CI 0% to 23%). Three are worth a note:

- **more-itertools-1260** (2 sampled): fixes the reported crash in `running_min` and its two
  siblings, but not `running_statistics`, which the tests also check. These are incomplete
  fixes, counted as `not_a_fix`. The grader's verdict stands.
- **starlette-3130** (3 sampled): the KeyError is gone, but the debug information is dropped.
  The maintainers' tests check a new `response.extensions` entry. **This bug reads as
  feature-like**, like starlette-3510 and -3520. It was noticed only after the run, so the
  frozen descriptive "without" view does not leave it out. The write-up says so.
- **jinja2-1852/B2/2** adds an unreachable copy of a line: no change at all. Its crash check
  did not reproduce on the buggy code, so B2 had nothing to check the patch against.

## 3. State Deficit

1 of 28 bugs: **jinja2-2026**. The report has one frame, and the object's repr reads
`Undefined` although it is a `ChainableUndefined`. The `deepcopy` call that set off the error
is not shown. This is borderline: a dict argument hints at a `deepcopy` memo. Every other bug's
needed values are in the frame variables. werkzeug-3148 turns on a policy choice the
maintainers made (an invalid host becomes empty), not on missing state.

Failed trials relabelled to `state_deficit`: B 3, B2 1.

## 4. Beyond-the-crash tests (automatic)

The maintainers' other tests from the fix commit, run on every correct patch where the
maintainers' own fix passes them (`benchmark/results/b2-extra.json`):

| | Pipeline B | Pipeline B2 |
|---|---|---|
| Correct trials that also pass them | 3/12 | 3/13 |

Most correct patches on these bugs fix the crash but not the wider behaviour the maintainers
tested. Both arms do the same here.

## 5. Second review (to do, by the owner)

Drawn with seed 20261111; the decisions are not shown to the reviewer:

- **correct (10):** jinja2-1762#1, jinja2-1984#1, jinja2-1984#2, marshmallow-2861#1,
  marshmallow-2894#2, marshmallow-2894#4, more-itertools-1125#1, more-itertools-1153#1,
  starlette-3510#1, starlette-3622#3;
- **incorrect (5):** jinja2-1852/B2/2, jinja2-2026/B2/2, marshmallow-2730/B2/3,
  more-itertools-1260/B2/1, tomlkit-698/B2/2;
- **State Deficit (5):** jinja2-1984, marshmallow-2800, marshmallow-2894, more-itertools-1028,
  more-itertools-1153.

The answers go to `benchmark/audit/b2/second_review.yaml`, in the first study's format.

## What the audit means for the results

The grader's verdicts hold: no correct patch is a non-fix, and no sampled rejection was a real
fix. But "correct" is generous. About 1 in 6 correct trials, in both arms, fixes the symptom.
Most correct patches on bugs with wider maintainer tests fail them. **B2's fix rules did not
measurably change fix quality** (symptom: B 9, B2 8).
