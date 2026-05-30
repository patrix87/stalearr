"""Weight lab: visualize how the TOPSIS presets pick releases.

Drives the real engine and the shipped presets (defaults.toml). For each scenario it shows
each candidate's closeness under every preset (★ = winner; "drop" = excluded by that preset's
gb/h floor or the score gap-cut) and the ACT/HOLD decision vs the current file. Part 2 stresses
retention on a large and a small release pool, and checks that a much-smaller, lower-score
release still survives and ranks well.

Run:  uv run python tools/weight_lab.py
Writes a timestamped Markdown report under ./reports/ and prints a short summary.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from optimizarr.features.optimizer.config import Preset, default_topsis
from optimizarr.features.optimizer.topsis import GB, Topsis, eligible


def _release(title: str, score: int, res: int, size_gb: float) -> dict:
    return {
        "title": title,
        "customFormatScore": score,
        "quality": {"quality": {"resolution": res}},
        "size": int(size_gb * GB),
        "rejections": [],
    }


@dataclass
class Scenario:
    name: str
    target_res: int
    runtime_h: float
    current: dict
    candidates: list[dict]
    note: str = ""


SCENARIOS: list[Scenario] = [
    Scenario(
        name="4K: remux vs web vs lean x265",
        target_res=2160,
        runtime_h=2.0,
        current=_release("(current) mediocre 2160p", 700_000, 2160, 30.0),
        candidates=[
            _release("2160p Remux", 1_000_000, 2160, 60.0),  # 30 GB/h
            _release("2160p WEB-DL", 950_000, 2160, 24.0),  # 12 GB/h
            _release("2160p x265", 930_000, 2160, 8.0),  # 4 GB/h, lean
        ],
        note="Remux/Quality tolerate the big remux (high bloat); Efficient/Compact swing to the "
        "lean encodes. The remux is 'bloated' only under the size-conscious presets.",
    ),
    Scenario(
        name="Floor varies by preset: is the tiny encode even considered?",
        target_res=2160,
        runtime_h=2.0,
        current=_release("(current) 1080p", 800_000, 1080, 18.0),
        candidates=[
            _release("2160p WEB-DL", 960_000, 2160, 22.0),  # 11 GB/h
            _release("2160p micro x265", 900_000, 2160, 6.0),  # 3 GB/h
        ],
        note="The 3 GB/h encode is below Remux/Quality's 2160 floor (so dropped there) but above "
        "Balanced/Efficient/Compact's floor (kept) — the gb/h floor is per-preset.",
    ),
]

RETENTION_RUNTIME_H = 2.0
RETENTION_TARGET = 2160
RETENTION_PRESET = "Compact"  # size-leaning: where small gems matter most


def _included(t: Topsis, preset: Preset, releases: list[dict], runtime_h: float) -> set[int]:
    """ids of releases that survive a preset's gb/h floor + the score gap-cut."""
    after_gbh = t.filter_by_gbh_floor(eligible(releases), runtime_h, preset.size_by_resolution)
    return {id(r) for r in t.filter_by_score_gap(after_gbh)}


def _closeness(t: Topsis, preset: Preset, release: dict, runtime_h: float, target: int) -> float:
    attrs = t.attributes_for(release, runtime_h, preset.size_by_resolution, target)
    return t.closeness(attrs, preset.weights)


def render_scenario(t: Topsis, sc: Scenario) -> tuple[list[str], list[str]]:
    presets = t.cfg.presets
    cur_clo = {
        name: _closeness(t, p, sc.current, sc.runtime_h, sc.target_res)
        for name, p in presets.items()
    }
    included = {name: _included(t, p, sc.candidates, sc.runtime_h) for name, p in presets.items()}
    cand_clo = {
        id(r): {
            name: _closeness(t, p, r, sc.runtime_h, sc.target_res) for name, p in presets.items()
        }
        for r in sc.candidates
    }
    winner = {
        name: max(
            (r for r in sc.candidates if id(r) in included[name]),
            key=lambda r: cand_clo[id(r)][name],
            default=None,
        )
        for name in presets
    }

    md = [
        f"## {sc.name}",
        "",
        f"- target {sc.target_res}p · runtime {sc.runtime_h:g}h",
        f"- {sc.note}",
        "",
    ]
    header = ["release", "score", "res", "GB/h", *presets]
    md.append("| " + " | ".join(header) + " |")
    md.append("|" + "|".join(["---"] * len(header)) + "|")

    def row(r: dict, clo: dict[str, float], dropped: set[str] | None) -> str:
        gbh = (r["size"] / GB) / sc.runtime_h
        res = r["quality"]["quality"]["resolution"]
        cells = [r["title"], f"{r['customFormatScore']:,}", f"{res}p", f"{gbh:.1f}"]
        for name in presets:
            if dropped is not None and name in dropped:
                cells.append("drop")
            else:
                val = f"{clo[name]:.3f}"
                if winner[name] is not None and r is winner[name]:
                    val = f"**{val} ★**"
                cells.append(val)
        return "| " + " | ".join(cells) + " |"

    md.append(row(sc.current, cur_clo, None) + "  ← current")
    for r in sc.candidates:
        dropped = {name for name in presets if id(r) not in included[name]}
        md.append(row(r, cand_clo[id(r)], dropped))
    md.append("")

    md.append("**Decision per preset** (Δ vs current; ACT if ≥ min_closeness_gain):")
    md.append("")
    summary = [f"  {sc.name}"]
    for name in presets:
        win = winner[name]
        if win is None:
            md.append(f"- `{name}`: no viable candidate → **HOLD**")
            summary.append(f"    {name:<10} → HOLD (none)")
            continue
        gain = cand_clo[id(win)][name] - cur_clo[name]
        act = gain >= t.cfg.min_closeness_gain
        verb = "ACT" if act else "HOLD"
        md.append(f"- `{name}`: **{win['title']}** (Δ {gain:+.3f}) → **{verb}**")
        summary.append(f"    {name:<10} → {verb:<4} {win['title']} (Δ {gain:+.3f})")
    md.append("")
    return md, summary


def _make_popular(rng: random.Random) -> list[dict]:
    rel: list[dict] = []

    def add(kind: str, count: int, lo: int, hi: int, size_lo: float, size_hi: float) -> None:
        for _ in range(count):
            score = min(1_000_000, rng.randint(lo, hi))
            size = round(rng.uniform(size_lo, size_hi), 1)
            rel.append(_release(f"{kind} {size}GB s={score // 1000}k", score, 2160, size))

    add("remux", 12, 880_000, 1_000_000, 45.0, 70.0)
    add("bluray", 22, 860_000, 1_000_000, 26.0, 44.0)
    add("web", 26, 850_000, 990_000, 15.0, 26.0)
    add("x265", 14, 880_000, 970_000, 6.0, 12.0)  # high score, small — the "gems"
    add("web-mid", 12, 550_000, 840_000, 14.0, 24.0)
    add("low", 4, 60_000, 480_000, 8.0, 30.0)
    add("FAKE", 4, 900_000, 1_000_000, 1.5, 2.6)  # tiny -> below gb/h floor
    rng.shuffle(rel)
    return rel


def _make_obscure() -> list[dict]:
    return [
        _release("remux 55.0GB s=700k", 700_000, 2160, 55.0),
        _release("web 18.0GB s=720k", 720_000, 2160, 18.0),
        _release("x265 7.0GB s=690k", 690_000, 2160, 7.0),  # small gem
        _release("low 9.0GB s=380k", 380_000, 2160, 9.0),
        _release("banned 10.0GB s=-30k", -30_000, 2160, 10.0),
    ]


def render_retention(t: Topsis, name: str, releases: list[dict]) -> list[str]:
    preset = t.cfg.presets[RETENTION_PRESET]
    rt = RETENTION_RUNTIME_H
    after_hard = eligible(releases)
    after_gbh = t.filter_by_gbh_floor(after_hard, rt, preset.size_by_resolution)
    kept = t.filter_by_score_gap(after_gbh)

    md = [f"### {name}  (preset {RETENTION_PRESET})", ""]
    md.append("| stage | releases |")
    md.append("|---|---|")
    md.append(f"| input | {len(releases)} |")
    md.append(f"| after gb/h floor (fakes out) | {len(after_gbh)} |")
    md.append(f"| after score gap-cut | {len(kept)} |")
    md.append("")

    ranked = sorted(kept, key=lambda r: -_closeness(t, preset, r, rt, RETENTION_TARGET))
    md.append("Top 8 by closeness:")
    md.append("")
    md.append("| # | release | score | size GB (GB/h) | closeness |")
    md.append("|---|---|---|---|---|")
    for i, r in enumerate(ranked[:8], 1):
        gbh = (r["size"] / GB) / rt
        clo = _closeness(t, preset, r, rt, RETENTION_TARGET)
        md.append(
            f"| {i} | {r['title']} | {r['customFormatScore']:,} | "
            f"{r['size'] / GB:.1f} ({gbh:.1f}) | {clo:.3f} |"
        )
    md.append("")
    if kept:
        smallest = min(kept, key=lambda r: r["size"])
        by_score = sorted(kept, key=lambda r: -r["customFormatScore"])
        s_rank = by_score.index(smallest) + 1
        c_rank = ranked.index(smallest) + 1
        verb = "wins" if c_rank == 1 else "ranks"
        md.append(
            f"Smallest survivor **{smallest['title']}**: #{s_rank} of {len(kept)} by score, "
            f"#{c_rank} by closeness — survived the filter and {verb} on size."
        )
        md.append("")
    return md


def main() -> None:
    t = Topsis(default_topsis())
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    md: list[str] = [
        "# TOPSIS preset lab",
        "",
        f"Generated {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Closeness is the only decision factor. Each preset bundles weights + a per-resolution",
        "size tent {floor, target, bloat} — n_size peaks at the *target* size for that release",
        "kind. ★ = winner per preset; 'drop' = excluded by its gb/h floor or gap-cut.",
        "",
        "## Presets",
        "",
        "| preset | score | resolution | size | 2160 size {floor, target, bloat} |",
        "|---|---|---|---|---|",
    ]
    for name, p in t.cfg.presets.items():
        w = p.weights
        floor, target, bloat = p.size_by_resolution.get(2160, (0.0, 0.0, 0.0))
        md.append(
            f"| {name} | {w['score']:.2f} | {w['resolution']:.2f} | {w['size']:.2f} | "
            f"{{{floor:g}, {target:g}, {bloat:g}}} |"
        )
    md.append("")
    md.append("# Part 1 — preset comparison on curated scenarios")
    md.append("")

    summaries: list[str] = []
    for sc in SCENARIOS:
        lines, summary = render_scenario(t, sc)
        md.extend(lines)
        summaries.extend(summary)

    md.append("# Part 2 — retention at scale")
    md.append("")
    summaries.append("  --- Part 2: retention ---")
    rng = random.Random(7)
    for name, releases in {
        "popular movie (~94 releases)": _make_popular(rng),
        "obscure movie (5 releases)": _make_obscure(),
    }.items():
        md.extend(render_retention(t, name, releases))
        summaries.append(f"    {name}: {len(releases)} in")

    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"weight-lab-{ts}.md"
    out_path.write_text("\n".join(md) + "\n")

    print("\n".join(summaries))
    print(f"\nFull report: {out_path}")


if __name__ == "__main__":
    main()
