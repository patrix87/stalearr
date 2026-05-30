# Condition-matrix selection — design plan

> **Status:** proposal / WIP on branch `feature/condition-matrix-selection`.
> Supersedes parts of [optimizer-design.md](optimizer-design.md). Nothing here is wired up
> yet — this is the agreed direction before code.

## Why change

Today the swap decision is a **single objective**: TOPSIS collapses score, resolution and
size into one `closeness` number and we grab when the pick beats the current file by
`min_closeness_gain`. That has a known failure mode: because the three axes are traded off
against each other, the "best" pick can be a **bigger file with a lower score** (Efficient
optimizes size+score by weight, so it will sometimes accept a worse-scoring, larger file if
the weighted math says so). We never want that.

The real rules we care about are not "maximize a weighted blend". They are **hard
constraints about which transitions are allowed**, and those constraints **differ per
profile**. TOPSIS is still useful — but only *after* the illegal transitions are removed, and
only for profiles that genuinely want to balance two or more axes (Efficient, Balanced).

So the new shape is:

```
candidates
  -> hard rejections (blocklist/parse/dead)        [unchanged]
  -> gb/h floor (fake/low-bitrate)                 [unchanged]
  -> score gap-cut                                  [unchanged]
  -> TRANSITION GATE (new): drop any candidate whose move from the current
     file is forbidden for this profile             [NEW — the condition matrix]
  -> PICK among survivors:
       Remux               -> max_score (deterministic, size only as tie-break)
       Compact             -> min_size  (deterministic, gated by a viability score)
       Quality/Balanced/Efficient -> TOPSIS for best fit (score vs size by weight)
```

The condition matrix is the gate. TOPSIS becomes a tie-break/best-fit step, not the
arbiter of legality.

## The three deltas

Every decision compares a **candidate** against the **current file** on three axes. We
classify each axis into coarse buckets (thresholds are config, see below):

| Axis | Buckets |
| --- | --- |
| Resolution | `below_target` · `at_target` · `above_target`; and vs current: `lower` / `same` / `higher` |
| Score | `much_lower` · `slightly_lower` · `same` · `higher` (magnitude matters) |
| Size | `smaller` · `same` · `bigger` |

"Slightly lower" vs "much lower" score is the crux of every size-leaning profile. See
**[Defining slight and much](#defining-slight-and-much-thresholds)** below for how the
boundary is measured and why it must be a *fixed* threshold.

Resolution rule shared by all profiles: the **target resolution is always what the profile
wants**. A different resolution is only acceptable when nothing at target survives the gate —
i.e. resolution is a **fallback dimension**, not something to trade away for size/score.
Implementation: gate at-target candidates first; only if that set is empty, reconsider
off-target ones.

## Defining slight and much (thresholds)

Each axis delta is classified on **its own normalized scale**, not on combined closeness.
Closeness blends score+resolution+size; if "slightly lower score" were defined on closeness, a
size change would leak into the score bucket and the matrix would stop separating the axes it
exists to separate. So:

- **Score delta** = `Δn_score` = candidate − current on the fixed `[anti_ideal, ideal]` → 0–1
  scale (the same normalization closeness is built from, isolated to the score axis).
  - `|Δn_score| ≤ score_slack`        → **same** (noise band).
  - `−score_much < Δn_score < −score_slack` → **slightly lower**.
  - `Δn_score ≤ −score_much`          → **much lower**. (mirror for higher)
- **Size delta** = `Δn_size` on the size axis, with a `size_slack` noise band so identical-size
  re-encodes don't churn (`smaller` / `same` / `bigger`).

**These boundaries are fixed (absolute on the normalized scale), not field-relative — on
purpose.** There are two different questions and only one of them is field-relative:

| Question | Relative to | Where it lives |
| --- | --- | --- |
| "Is this candidate in the same league as the other releases?" | the available field | **gap-cut prefilter** (already done) |
| "Is it meaningfully different from *my current file*?" | the current file, fixed scale | the **transition gate** (here) |

If the slight/much boundary floated with whatever releases happen to be available this hour,
the A↔B classification could flip between evaluations and the **no-oscillation guarantee
breaks** (it rests on `score_much > score_slack` being a stable inequality — see
[Anti-oscillation](#anti-oscillation-the-invariant-that-must-hold)). So field-relativity stays
confined to inclusion (gap-cut); the gate's thresholds are fixed config.

> Mapping to the earlier draft's names: `score_slack` is the same/slightly boundary;
> `score_much` (was `bigger_score_gate` in the first draft) is the slightly/much boundary.
> The required inequality is simply `score_much > score_slack`.

**"Tend to" / tilt** is *not* a gate threshold — it's the **TOPSIS weight** in the pick step.
Balanced and Efficient share an identical legality matrix; "leans score" vs "leans size" is
purely their score/size weights. The gate decides what's *legal*; the weights decide *taste*.

Default starting points (validate on the real library before trusting them — Profilarr's ~1M
scale means meaningful release-to-release diffs are often tens of thousands of points):
`score_slack ≈ 0.02`, `score_much ≈ 0.10` on `n_score`; `size_slack ≈ 0.03` relative.

## Size model: monotonic for size-leaners, tent only for quality-leaners

The original tent `{floor, target, bloat}` penalizes files *below* target (n_size rises from
floor up to target). But "too small is bad" is actually two separate claims, and only one of
them belongs in the size curve:

1. **"Too small = probably a fake/garbage encode."** Already handled by the **gb/h floor
   prefilter**. Once a release clears the floor this concern is fully spent.
2. **"Too small = below the bitrate I actually want."** Only true for **Remux/Quality**, where
   bitrate is genuinely part of the quality bar.

So the curve shape is **per profile intent**:

| Profile | Size curve | `ideal_gbh` (peak) | Pick |
| --- | --- | --- | --- |
| Remux | **tent** | in remux territory | `max_score` (size = tie-break only) |
| Quality | **tent** | a touch below Remux | `topsis`, heavy score weight, *small* size weight |
| Balanced | **monotonic** (smaller wins above floor) | = floor | `topsis`, score-leaning weights |
| Efficient | **monotonic** | = floor | `topsis`, size-leaning weights |
| Compact | **monotonic** | = floor | `min_size`, gated by `viability_score` |

This is your "ideal gb/h per profile, aim a few % below to different degrees": for tent
profiles the peak sits at `ideal_gbh`; for monotonic profiles smaller is strictly better and
the "how aggressively smaller" tilt is the TOPSIS **size weight** (Efficient > Balanced),
*not* a moved peak. Quality "optimizes size to a much lower degree than Efficient" via a small
size weight on top of its tent.

Quality therefore moves from the first draft's `pick = max_score` to **`pick = topsis`** (so
its small size preference actually acts). Only Remux stays `max_score`.

## When is a bigger file ever right? (the much-smaller-current-file case)

The hard case: the current file is *much smaller* than the profile's `ideal_gbh` (a superb
x265 encode — or garbage that happens to score high). With a tent, the current file sits on the
rising side (n_size < 1), a candidate at target scores n_size = 1, so TOPSIS sees the bigger
file as "better on size" and may inflate size at equal/lower score. The monotonic split above
**resolves this structurally**:

- **Efficient / Balanced / Compact** (monotonic): a much-smaller current file is *never* worse
  on size, so TOPSIS never pushes toward a bigger file to climb a tent. Size increases only
  happen as a side-effect of a real score/resolution gain the gate already permits.
- **Remux / Quality** (tent): bumping a tiny file up toward `ideal_gbh` *is* the intended
  behavior — for these profiles bitrate is part of the quality bar, so "increasing size is
  good" here. **The curve shape is the answer to "when is a bigger file right".**

The "garbage that scored high but is tiny" worry gets a hard boundary: **the optimizer trusts
`customFormatScore` and never infers quality from size beyond the fake-floor.** Distinguishing
"superb tiny encode" from "tiny garbage that scored high" from size alone is unsolvable; if a
garbage file scores high, that's a Profilarr scoring bug to fix at the source — not something
to paper over by inflating size (which would re-introduce exactly the unwanted size increases
this redesign removes). Size is a *cost*, never a quality proxy except via the floor.

## Universal forbidden transitions (every profile)

These are never acceptable regardless of profile:

1. **Bigger file, same resolution, no score increase** — pure bloat. ❌
2. **Smaller file but *much* lower score** (with or without lower resolution) — throwing away
   quality to save space. *Slightly* lower score is OK. ❌
3. **Lower score, bigger file, same resolution** — strictly worse on two axes. ❌
4. (derived) **Same score, same resolution, bigger file** — covered by #1. ❌

Everything not forbidden is a *candidate*; the per-profile matrix then narrows further and the
pick step orders what remains.

## Per-profile intent (targets)

| Profile | Score | Size | Resolution | One-liner |
| --- | --- | --- | --- | --- |
| **Remux** | maximize | don't care | at target | Highest score wins, size irrelevant. |
| **Quality** | maximize | mild preference smaller | at target | Highest score; break near-ties toward smaller. |
| **Balanced** | good, leans score | smaller, secondary | at target | Good score *and* smaller; tilt to score. |
| **Efficient** | good, leans size | smaller, primary-ish | at target | Good score *and* smaller; tilt to size. |
| **Compact** | viable floor | minimize | at target | Smallest file that still clears a viability score. |

Quality/Balanced/Efficient are the genuine multi-axis cases → TOPSIS picks among the legal
survivors (Quality with a heavy score weight, Balanced score-leaning, Efficient size-leaning).
Remux (`max_score`) and Compact (`min_size`) are effectively single-axis once the gate has run
→ a deterministic sort is clearer and oscillation-proof. See
[Size model](#size-model-monotonic-for-size-leaners-tent-only-for-quality-leaners) for which
profiles use a tent vs a monotonic size curve.

## Per-profile condition matrices

Each profile gets a matrix of `(score delta) × (size delta)` at **target resolution**, marking
each cell `ACCEPT` (legal transition, eligible to be picked) or `❌` (forbidden). Off-target
resolution is the fallback layer described above. `same`/`same`/`same` is the current file
(never a "grab", it's the HOLD baseline).

Legend: rows = candidate score vs current; cols = candidate size vs current.

### Remux — score is everything

| score ↓ / size → | smaller | same | bigger |
| --- | --- | --- | --- |
| higher | ✅ | ✅ | ✅ |
| same | ✅ | — | ❌ |
| slightly lower | ❌ | ❌ | ❌ |
| much lower | ❌ | ❌ | ❌ |

Remux never trades score for size: only same-or-higher score moves are legal (smaller at equal
score is a free win; bigger at higher score is fine).

### Quality — max score, mild size care

| score ↓ / size → | smaller | same | bigger |
| --- | --- | --- | --- |
| higher | ✅ | ✅ | ✅ (if gain clears `score_much`) |
| same | ✅ | — | ❌ |
| slightly lower | ✅ (only if *much* smaller) | ❌ | ❌ |
| much lower | ❌ | ❌ | ❌ |

Quality will accept a slightly-lower score only to bank a large size saving; a bigger file
needs a real score gain to justify it.

### Balanced — good score + smaller, leans score

| score ↓ / size → | smaller | same | bigger |
| --- | --- | --- | --- |
| higher | ✅ | ✅ | ✅ (if gain clears `score_much`) |
| same | ✅ | — | ❌ |
| slightly lower | ✅ | ❌ | ❌ |
| much lower | ❌ | ❌ | ❌ |

### Efficient — good score + smaller, leans size

| score ↓ / size → | smaller | same | bigger |
| --- | --- | --- | --- |
| higher | ✅ | ✅ | ✅ (only if gain is large) |
| same | ✅ | — | ❌ |
| slightly lower | ✅ | ❌ | ❌ |
| much lower | ❌ | ❌ | ❌ |

Balanced and Efficient share the same legality matrix; they differ **only in the pick step**
(TOPSIS weights tilt score vs size). The matrix keeps both honest; the weights set the taste.

### Compact — smallest viable

| score ↓ / size → | smaller | same | bigger |
| --- | --- | --- | --- |
| higher | ✅ | ✅ | ❌ |
| same | ✅ | — | ❌ |
| slightly lower | ✅ (if still ≥ `viability_score`) | ❌ | ❌ |
| much lower | ✅ only if ≥ `viability_score` **and** much smaller | ❌ | ❌ |

Compact will accept a real score drop to shrink the file, but never below a `viability_score`
floor, and never a bigger file (a higher score in a bigger file is *rejected* — that's the one
profile where bigger-at-higher-score is off limits).

> These tables are the human-readable spec. The machine form is a small set of per-profile
> predicate parameters (below), not 15 hand-typed cells — the tables are generated/validated
> from the params so config and docs can't drift.

## Anti-oscillation (the invariant that must hold)

Oscillation = A→B grabbed, then on re-eval B→A grabbed, forever. To make it **impossible by
construction**, the accept relation must be a **strict partial order** (irreflexive +
acyclic): if `B` is an accepted improvement over `A` for a profile, then `A` must be a
forbidden transition from `B` for that same profile.

Check the matrices against this:
- *smaller, slightly-lower score* accepted (A→B) ⇒ reverse is *bigger, slightly-higher score*.
  For all size-leaning profiles "bigger + slightly higher score" must be ❌ — and it is
  (bigger needs to clear `score_much`, a *large* gain, not slight). ✅
- *smaller, same score* accepted ⇒ reverse *bigger, same score* is ❌ (universal rule #1). ✅
- *bigger, higher score* accepted (clears gate) ⇒ reverse *smaller, lower score* — lower by the
  same gate amount = "much lower" = ❌. ✅ (as long as `score_much` > `score_slack`,
  the slightly/much boundary — this is the **one numeric constraint we must enforce in
  config validation**: `score_much > score_slack`.)

So the implementation must assert `score_much > score_slack` at config-load time. That
single inequality is what guarantees no two-file cycle. Document it loudly.

(The current `min_closeness_gain` provided weak oscillation protection by requiring strict
improvement; the partial-order argument replaces it with a structural guarantee.)

## Config shape (proposed)

The matrix is parameterized, not enumerated. Per profile (preset), add a `[…transitions]`
block alongside the existing weights/size curve:

```toml
[optimizer.topsis.presets.Efficient]
score = 0.55          # TOPSIS pick-step weights (multi-axis profiles)
resolution = 0.10
size = 0.35
size_curve = "monotonic"      # "tent" (Remux/Quality) | "monotonic" (Balanced/Efficient/Compact)
size_by_resolution = { ... }  # {floor, target, bloat}: floor feeds the gb/h prefilter always;
                              # target = ideal_gbh = tent peak (tent only); monotonic ignores
                              # target and just falls from floor->bloat (smaller wins).

[optimizer.topsis.presets.Efficient.transitions]
score_slack = 0.02            # |Δn_score| within this = "same" (noise band)
score_much = 0.10             # below -this = "much lower"  (MUST be > score_slack)
size_slack = 0.03             # |Δn_size| within this = "same"
allow_bigger_for_score = true # Compact sets this false (never a bigger file)
viability_score = 0           # Compact/Quality floor for accepting score drops
pick = "topsis"               # "topsis" | "max_score" | "min_size"
```

Pick + curve by shipped preset:

| Preset | `size_curve` | `pick` |
| --- | --- | --- |
| Remux | tent | `max_score` |
| Quality | tent | `topsis` (heavy score weight, small size weight) |
| Balanced | monotonic | `topsis` |
| Efficient | monotonic | `topsis` |
| Compact | monotonic | `min_size` (gated by `viability_score`) |

This keeps **one config surface**: presets gain `size_curve` + a `transitions` sub-table; the
existing `size_by_resolution` still supplies the gb/h floor (and the tent peak where used).
Per-profile-name overrides inherit the same fields.

## Code plan

1. **New module `transitions.py`** in `features/optimizer/`:
   - `classify(current, candidate, cfg) -> Deltas` (resolution/score/size buckets).
   - `is_forbidden(deltas, transition_cfg) -> (bool, reason)` implementing the universal rules
     + per-profile params. This is the single source the doc tables are generated from.
   - A `matrix(transition_cfg)` helper that renders the ACCEPT/❌ table (used by a tools/
     script to regenerate this doc and by tests to assert doc==code).
2. **Config** (`config.py`): parse `presets.<n>.size_curve` + `presets.<n>.transitions`,
   default sensible values per shipped preset in `defaults.toml`, validate
   `score_much > score_slack`, `size_curve` ∈ {tent, monotonic}, and `pick` ∈
   {topsis, max_score, min_size}.
3. **Decision** (`decision.py`): insert the transition gate between prefilters and the pick;
   replace the `min_closeness_gain` swap test with: *gate the candidates, then pick by the
   profile's `pick` method; ACT if a survivor exists, else HOLD (satisfied)*. Resolution
   fallback: run the gate on at-target candidates first, fall back to off-target only if empty.
4. **Topsis** (`topsis.py`): make `normalize_size` honor `size_curve` — `tent` is today's
   behavior; `monotonic` ignores `target` and falls from floor→bloat (already what
   `target == floor` does, just made explicit and the default for size-leaners). Keep
   `rank`/`closeness` for `pick = "topsis"`; add `max_score` / `min_size` deterministic pickers
   (stable tie-breaks so ranking is reproducible — important for oscillation).
5. **State**: unchanged (satisfied/HOLD model still applies — HOLD now means "no legal,
   improving transition exists").
6. **Tests**:
   - `test_transitions.py`: every universal rule, each profile matrix cell, and the
     **partial-order/no-oscillation property** (for each profile, for random A/B pairs, not
     both A→B and B→A accepted).
   - `test_decision.py`: resolution-fallback behavior; the four worked examples from the user.
   - Doc-sync test: `matrix()` output matches the tables in this file (or generate the tables).
7. **Validate the existing weights and limits** (see [Validation](#validation-of-current-weights-and-limits)).
8. **Docs**: fold the final matrices into `optimizer-design.md`; update README's "How it works"
   step 3–4; keep this file as the rationale.
9. **Lint/format**: `ruff check` + `ruff format` before done; write validation results to a
   timestamped `.md` in `reports/`.

## Validation of current weights and limits

Before trusting the redesign, **re-validate the numbers we already ship** — the preset weights,
the per-resolution `size_by_resolution` floors/targets/bloats, `score_gap`, and the new
`score_slack` / `score_much` / `size_slack` thresholds. The point is to confirm the values we
*have* still produce the right picks once the gate + monotonic curves change the behavior around
them (a floor/target tuned against the old tent-everywhere model may be wrong under the new one).

Use and extend the existing harness — `tools/weight_lab.py` already drives the real engine and
shipped presets, prints each candidate's closeness per preset (★ winner / "drop" = floored or
gap-cut), and writes a timestamped report to `reports/`. Extend it to:

- Run every scenario through the **new pipeline** (transition gate → per-profile pick), showing
  for each preset: which candidates the gate drops (and the rule that dropped them), the slight/
  much/same classification of each, and the final ACT/HOLD vs the current file.
- Cover, per preset, the cases the user called out:
  - bigger file, same resolution, no score increase → must be ❌ (all profiles);
  - smaller file, *much* lower score → ❌; smaller file, *slightly* lower score → ✅ for
    size-leaners, ❌ for Remux;
  - lower score, bigger file, same resolution → ❌;
  - **much-smaller, lower-score current file**: confirm Efficient/Balanced/Compact do *not*
    inflate size, and Remux/Quality *do* move up toward `ideal_gbh`.
- Assert the **no-oscillation property** on the real presets: for random A/B pairs there is no
  profile where both A→B and B→A are accepted (this also checks `score_much > score_slack`
  holds with the shipped numbers).
- Sweep each threshold (`score_slack`, `score_much`, `size_slack`, the floors/targets) across a
  small range and record where picks flip, so the shipped defaults are chosen from data, not
  guessed. Land the chosen values in `defaults.toml` and the rationale in the `reports/` file.

Where possible, validate against a **real Radarr/Sonarr release pool** (a `dry_run = true` run,
or captured `GET /api/v3/release` payloads) rather than only synthetic scenarios, so the floors
and bloats are checked against real-world bitrates.

### Real-world bitrate reference (sanity-check the size curves)

The size curve unit is GB/h where the code's `GB = 1024³`, i.e. **GiB/h**. Conversion:
**1 Mbps ≈ 0.42 GiB/h** (`Mbps × 3600 / 8 / 1024`). Published bitrate ranges, converted, vs the
shipped `size_by_resolution` targets:

| Tier | Res | Typical bitrate | ≈ GiB/h | Shipped target | Read |
| --- | --- | --- | --- | --- | --- |
| Remux | 2160 | 60–100 Mbps (UHD BD remux; 50–80 GiB/movie) | ~25–42 | 20 (floor 12, bloat 100) | target sits *below* the real low end — deliberate "leanest remux wins"; floor 12 (~28 Mbps) safely under real remuxes. OK. |
| Remux | 1080 | 20–40 Mbps avg ~25–30 (25–30 GiB/movie) | ~8–17 | 10 (floor 6, bloat 40) | target right at the real low edge; floor 6 (~14 Mbps) cleanly separates remux from encodes (2–6 Mbps). OK. |
| Quality | 2160 | high-bitrate HEVC encode ~15–25 Mbps | ~6–10 | 10 (floor 4) | target at the top of the encode band — matches "full BluRay-grade encode". OK. |
| Quality | 1080 | ~6–10 Mbps | ~2.5–4 | 4 (floor 2) | OK. |
| Efficient | 2160 | good x265 10–18 Mbps (HDR +10–20%) | ~4.2–7.5 | 4 (floor 1.5, bloat 16) | **target ≈ low edge / slightly below** the "good 4K HEVC" band → may over-reward sub-spec encodes. Check real 4K encodes aren't pushed too small; consider target ~5–6. |
| Efficient | 1080 | good x265 2–6 Mbps | ~1–2.7 | 2 (floor 0.8) | lands in the sweet spot. OK. |
| Balanced | 1080/2160 | between Efficient and Compact | — | 2.5 / 5.0 | plausible; validate via sweep. |
| Compact | any | smallest above the fake floor | — | = floor | by design. |
| — | 720 | typical encode 1–2 Mbps | ~0.45–0.9 | Eff 0.8 / Bal 1.2 | OK. |

Takeaways to fold into the validation sweep:

- The **2160p Efficient target (4 GiB/h ≈ 9.4 Mbps)** is the main suspect — it's at/under the
  low end of real "good" 4K HEVC; with the new *monotonic* curve (smaller always wins) this is
  less harmful than under a tent, but confirm it doesn't make Efficient grab visibly-soft 4K
  encodes. Candidate bump: target ~5–6.
- **HDR runs ~10–20% higher bitrate** at the same quality; the curves don't distinguish HDR, so
  HDR 4K encodes sit higher on the cost curve. Note as a known limitation; a per-HDR curve is
  out of scope for now.
- Remux floors/targets and the 1080p encode tiers are **well-supported by the data** — no change
  indicated beyond what the sweep confirms.

Sources: [UHD/4K remux bitrates (Hacker News)](https://news.ycombinator.com/item?id=39337834),
[1080p Blu-ray/remux bitrates (Linus Tech Tips)](https://linustechtips.com/topic/1059408-dvdbluray-resolutions-and-bitrates/),
[x265 4K & 1080p encoding bitrates (arstech)](https://arstech.net/video-encoding-bitrates-for-4k-and-1080p-with-h-264-and-h-265/),
[HEVC bitrate guidance (Plex forum)](https://forums.plex.tv/t/is-there-a-standard-bitrate-that-is-recommended-for-hevc-h265-in-720p-1080p-and-4k/203849).

## Resolved (this round)

- **slight/much measured on `Δn_score`** (fixed `[anti_ideal, ideal]` normalized scale), per
  axis — *not* on combined closeness, and *not* field-relative. Field-relativity stays in
  gap-cut. This is what preserves the no-oscillation invariant.
- **Size curve is per-profile shape:** tent for Remux/Quality (bitrate is a quality bar),
  monotonic-smaller-wins for Balanced/Efficient/Compact (the gb/h floor already catches fakes).
- **Quality → `pick = "topsis"`** with a heavy score weight + small size weight (not
  `max_score`), so it optimizes size *to a lower degree than Efficient*.
- **Bigger files only for Remux/Quality**, via their tent — answers "when is increasing size
  good". The optimizer never infers quality from size beyond the floor; it trusts the score.

## Open questions still to settle during implementation

- Exact default values for `score_slack` / `score_much` / `size_slack` — need validation on the
  real library (start `0.02 / 0.10 / 0.03`).
- Resolution-fallback mechanics: gate at-target first, fall back off-target only if empty —
  confirm it can't itself oscillate when the at-target set flickers in/out across evaluations.
- Should the universal rules be overridable per profile, or truly hard? Keep them hard for now.

## What stays the same

- Worker loop, queue gating, satisfied-state, unmonitor, auto-import of downgrades.
- Hard rejections, gb/h floor, score gap-cut prefilters.
- The grab-is-never-"done" philosophy and re-evaluation cadence.
