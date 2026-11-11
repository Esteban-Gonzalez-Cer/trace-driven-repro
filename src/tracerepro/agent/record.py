"""The trial folder (development plan §8.1): everything a trial produced, so analysis never needs
to call a model again.

`runs/{run_id}/{bug}/{pipeline}/{model}/trial_{n}/` holds:

- `calls.jsonl`: every model call (written by the client as it goes);
- `attempt_{k}.reply.md`, `attempt_{k}.diff`, `attempt_{k}.feedback.md`: each attempt's raw
  reply, the diff made from it, and what was sent back;
- `repro_test.py`: Pipeline A's accepted repro test, if any;
- `final.diff`: the submitted patch, if any;
- `record.json`: the agent result (status, attempts with outcomes and labels, tokens, leaks,
  context summary, prompt hashes);
- `verdict.json` and `review.md`: the evaluator's verdict, written afterwards by the runner.

`record.json` is written last, atomically: a trial folder without it is incomplete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tracerepro.agent.pipeline import AgentResult

RECORD = "record.json"
VERDICT = "verdict.json"


def trial_dir(runs: Path, run_id: str, bug: str, pipeline: str, model: str, n: int) -> Path:
    return runs / run_id / bug / pipeline / model / f"trial_{n}"


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_record(folder: Path, result: AgentResult, meta: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for a in result.attempts:
        stem = f"attempt_{a.n}" if a.stage == "patch" else f"repro_{a.n}"
        (folder / f"{stem}.reply.md").write_text(a.reply, encoding="utf-8")
        if a.diff is not None:
            (folder / f"{stem}.diff").write_text(a.diff, encoding="utf-8")
        if a.feedback is not None:
            (folder / f"{stem}.feedback.md").write_text(a.feedback, encoding="utf-8")
    if result.repro is not None:
        (folder / "repro_test.py").write_text(result.repro, encoding="utf-8")
    if result.final_diff is not None:
        (folder / "final.diff").write_text(result.final_diff, encoding="utf-8")
    body = {"meta": meta, "result": result.to_dict()}
    _atomic(folder / RECORD, json.dumps(body, indent=2, sort_keys=True) + "\n")


def write_verdict(folder: Path, verdict: dict[str, Any], review: str | None = None) -> None:
    if review is not None:
        (folder / "review.md").write_text(review, encoding="utf-8")
    _atomic(folder / VERDICT, json.dumps(verdict, indent=2, sort_keys=True) + "\n")


def is_complete(folder: Path, *, evaluated: bool) -> bool:
    return (folder / RECORD).is_file() and (not evaluated or (folder / VERDICT).is_file())
