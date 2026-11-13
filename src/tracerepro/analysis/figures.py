"""The analysis figures (spec §12), as deterministic SVG from `report.analyze`'s numbers.

- `failure_labels.svg`: failed trials per label, one bar per pipeline;
- `per_bug.svg`: each bug's 3 trials per pipeline (✓ / ✗ in the cell, so the grid never relies
  on colour alone), with the 2-of-3 outcome;
- `cost_accuracy.svg`: bugs solved (with the 95% interval) against mean tokens per run.

Determinism: text stays text (`svg.fonttype: none`), the bundled DejaVu Sans font, a fixed id
salt and no date in the metadata, so the same numbers always give the same bytes. Colours are
the validated categorical slots 1 and 2 (blue, orange) for the pipelines; pass/fail uses the
status palette with a mark in every cell.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("svg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from tracerepro.evaluate.taxonomy import PRECEDENCE, TAMPERED

SERIES = {"A": "#2a78d6", "B": "#eb6834"}
GOOD, BAD = "#0ca30c", "#d03b3b"
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
NAMES = {"A": "A: repro first", "B": "B: direct patch"}

STYLE: Any = {  # rc_context wants literal keys; these are checked by the tests
    "svg.fonttype": "none",
    "svg.hashsalt": "tracerepro",
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "font.size": 10,
    "axes.edgecolor": MUTED,
    "axes.labelcolor": INK,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "legend.frameon": False,
}


def _save(fig: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        path, format="svg", metadata={"Date": None, "Creator": "tracerepro"}, bbox_inches="tight"
    )
    plt.close(fig)
    # Same bytes as the repository's whitespace hooks would leave (no trailing spaces, one
    # final newline), so committed figures never differ from regenerated ones.
    text = "\n".join(line.rstrip() for line in path.read_text().splitlines())
    path.write_text(text + "\n")
    return path


def failure_labels(n: dict[str, Any], path: Path) -> Path:
    ps = list(n["pipelines"])
    labels = [x for x in (*PRECEDENCE, TAMPERED) if any(n["pipelines"][p]["labels"][x] for p in ps)]
    labels = labels or list(PRECEDENCE)
    fig, ax = plt.subplots(figsize=(7.5, 0.45 * len(labels) + 1.4))
    height = 0.8 / len(ps)
    for i, p in enumerate(ps):
        ys = [j + (i - (len(ps) - 1) / 2) * height for j in range(len(labels))]
        vals = [n["pipelines"][p]["labels"][x] for x in labels]
        ax.barh(ys, vals, height=height * 0.9, color=SERIES.get(p, MUTED), label=NAMES.get(p, p))
        for y, v in zip(ys, vals, strict=True):
            if v:  # a zero needs no label: the missing bar says it
                ax.text(v + 0.2, y, str(v), va="center", fontsize=9, color=INK)
    ax.set_yticks(range(len(labels)), [x.replace("_", " ") for x in labels])
    ax.invert_yaxis()
    ax.set_xlabel("failed trials")
    ax.xaxis.grid(True, color=GRID)
    ax.set_axisbelow(True)
    top = max(n["pipelines"][p]["labels"][x] for p in ps for x in labels)
    ax.set_xlim(0, max(1, top) * 1.12)
    ax.set_title("Why trials failed (one label per failed trial)", pad=28)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=len(ps))
    return _save(fig, path)


def per_bug(n: dict[str, Any], path: Path) -> Path:
    ps = list(n["pipelines"])
    bugs = list(n["per_bug"])
    width = max(len(n["per_bug"][b][p]["trials"]) for b in bugs for p in ps)
    fig, ax = plt.subplots(
        figsize=(1.6 + 0.55 * width * len(ps) + 1.4 * len(ps), 0.32 * len(bugs) + 1.4)
    )
    for row, bug in enumerate(bugs):
        for i, p in enumerate(ps):
            entry = n["per_bug"][bug][p]
            x0 = i * (width + 2)
            for t, ok in enumerate(entry["trials"]):
                ax.add_patch(
                    Rectangle(
                        (x0 + t + 0.05, row + 0.08),
                        0.9,
                        0.84,
                        color=GOOD if ok else BAD,
                        alpha=0.9 if ok else 0.75,
                        linewidth=0,
                    )
                )
                ax.text(
                    x0 + t + 0.5,
                    row + 0.52,
                    "✓" if ok else "✗",
                    ha="center",
                    va="center",
                    color="white",
                    fontsize=9,
                    fontweight="bold",
                )
            ax.text(
                x0 + width + 0.15,
                row + 0.52,
                "solved" if entry["solved"] else "",
                va="center",
                fontsize=8,
                color=INK,
            )
    ax.set_xlim(-0.1, len(ps) * (width + 2))
    ax.set_ylim(len(bugs), -0.6)
    ax.set_yticks([r + 0.5 for r in range(len(bugs))], bugs, fontsize=8)
    ax.set_xticks(
        [i * (width + 2) + width / 2 for i in range(len(ps))], [NAMES.get(p, p) for p in ps]
    )
    ax.xaxis.set_ticks_position("top")
    ax.tick_params(length=0)
    for side in ("left", "bottom", "top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_title("Per bug: 3 trials per pipeline (✓ correct, ✗ not); solved = 2 of 3", pad=24)
    return _save(fig, path)


def cost_accuracy(n: dict[str, Any], path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    for p, e in n["pipelines"].items():
        x, acc = e["tokens"]["mean"], e["accuracy"]
        y, (lo, hi) = acc["rate"], acc["ci"]
        ax.errorbar(
            [x],
            [y],
            yerr=[[y - lo], [hi - y]],
            fmt="o",
            markersize=9,
            capsize=5,
            color=SERIES.get(p, MUTED),
            elinewidth=2,
            label=NAMES.get(p, p),
        )
        ax.annotate(
            f"{NAMES.get(p, p)}\n{acc['k']}/{acc['n']} bugs, {x / 1000:,.0f}k tokens",
            (x, y),
            xytext=(12, -4),
            textcoords="offset points",
            fontsize=9,
            color=INK,
        )
    ax.set_xlim(0, max(e["tokens"]["mean"] for e in n["pipelines"].values()) * 1.6)
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:.0f}k"))
    ax.set_xlabel("mean tokens per run")
    ax.set_ylabel("bugs solved (2 of 3), 95% CI")
    ax.grid(True, color=GRID)
    ax.set_axisbelow(True)
    ax.set_title("Cost versus accuracy")
    ax.legend(loc="lower right")
    return _save(fig, path)


def draw_all(n: dict[str, Any], out: Path) -> list[Path]:
    with plt.rc_context(STYLE):
        return [
            failure_labels(n, out / "failure_labels.svg"),
            per_bug(n, out / "per_bug.svg"),
            cost_accuracy(n, out / "cost_accuracy.svg"),
        ]
