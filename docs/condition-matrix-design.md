# Condition-matrix selection — design rationale & validation roadmap

> **Status:** implemented on branch `feature/condition-matrix-selection`. This document is the
> *rationale* — why the model is shaped the way it is, the real-world bitrate research behind the
> size reference, and the remaining tuning/validation work. For the **as-built** algorithm,
> formulas, and config reference, see [ALGORITHM.md](../ALGORITHM.md); the worked HDR ladder and
> the validation sweep plan below are still the to-do list for tuning the shipped defaults on a
> real release pool.

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

## Size model: one shared reference, profiles expressed relative to it

This is a **space-saving tool** — nothing should ever push a file *bigger* except a genuine
score/resolution gain (and the [gate](#universal-forbidden-transitions-every-profile) already
governs those). So the size model is split into two layers:

**1. A single objective reference per resolution** — `{floor, target, ceiling}` in GiB/h,
shared by *all* profiles. This is reality, not taste:

- `floor`  — below this the encode is fake / too soft for the resolution → hard pre-filter drop.
- `target` — a **realistic GiB/h for a good (HDR) release** at this resolution. The honest
  "this is what a solid encode actually weighs" number.
- `ceiling` — above this is bloat: no real quality benefit, only wasted space.

**2. A per-profile size *aim*, relative to that target** — each profile says how far below the
realistic size it wants to sit, as a fraction of `target` (so config is always *relative to the
ideal*, never a hand-typed absolute):

| Profile | `size_aim` | meaning | score weight | size weight |
| --- | --- | --- | --- | --- |
| Remux | — (size ignored) | bitrate is the point; take the best score | very high | ~0 |
| Quality | `1.0` | at the realistic target | high | small |
| Balanced | `0.8` | slightly smaller | medium | medium |
| Efficient | `0.65` | much smaller | medium | high |
| Compact | `0.5` (→ toward floor) | as small as still viable | low | very high |

**The size-desirability curve is one-sided** (this is what protects your disk): `n_size = 1.0`
for anything **at or below the aim**, then ramps down to `0` at `ceiling`. *Smaller than the aim
is never penalized.* Consequences:

- A tiny, good-scoring current file is already `n_size = 1.0`, so **nothing ever inflates it**
  to climb toward a target. The much-smaller-current-file problem disappears by construction —
  for every profile, not just some.
- Among candidates at-or-below the aim, size ties at 1.0 and **score breaks the tie** → "small
  enough, then best score", which is exactly "good score *and* smaller".
- "slightly vs much smaller" is purely **where the aim sits** (`0.8` vs `0.65`); "how hard size
  competes with score" is the **size weight**. Two independent, legible knobs.
- Remux sets size weight ≈ 0, so being far above `ceiling` costs it nothing — it just takes the
  top score. No special-case curve needed.

The fake-floor is the *only* place size is allowed to veto a release; everywhere else **size is
a cost, never a quality proxy**. The optimizer trusts `customFormatScore` for quality — a tiny
file that scores high is kept; if it's actually garbage, that's a Profilarr scoring bug to fix
at the source, not something to paper over by demanding bigger files.

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
→ a deterministic sort is clearer and oscillation-proof. All profiles share one size
[reference](#size-model-one-shared-reference-profiles-expressed-relative-to-it) and differ only
by their relative `size_aim` + weights — "at target" above means each profile's own aim point.

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

Two layers, both relative to one objective reference (see
[Size model](#size-model-one-shared-reference-profiles-expressed-relative-to-it) and the
[reference ladder](#size-reference-ladder-proposed-new-defaults)).

**Shared reference — defined once, not per profile:**

```toml
[optimizer.topsis.reference]
# Realistic GiB/h per resolution (HDR-assumed). All profiles read these.
"2160" = { floor = 3.0, target = 6.5, ceiling = 18 }
"1080" = { floor = 1.0, target = 2.5, ceiling = 8 }
"720"  = { floor = 0.4, target = 1.0, ceiling = 4 }
"480"  = { floor = 0.2, target = 0.5, ceiling = 3 }   # SDR
```

**Per-profile — weights + a *relative* size aim + transition params:**

```toml
[optimizer.topsis.presets.Efficient]
score = 0.45                  # TOPSIS pick-step weights
resolution = 0.10
size = 0.45
size_aim = 0.65               # fraction of reference target: 1.0 = at target, <1 = smaller.
                              # n_size = 1.0 at/below aim, ramps to 0 at ceiling (one-sided).

[optimizer.topsis.presets.Efficient.transitions]
score_slack = 0.02            # |Δn_score| within this = "same" (noise band)
score_much = 0.10             # below -this = "much lower"  (MUST be > score_slack)
size_slack = 0.03             # |Δn_size| within this = "same"
allow_bigger_for_score = true # Compact sets this false (never a bigger file)
viability_score = 0           # Compact/Quality floor for accepting score drops
pick = "topsis"               # "topsis" | "max_score" | "min_size"
```

Per shipped preset:

| Preset | `size_aim` | `pick` | size weight |
| --- | --- | --- | --- |
| Remux | n/a (size ignored) | `max_score` | ~0 |
| Quality | `1.0` | `topsis` | small |
| Balanced | `0.8` | `topsis` | medium |
| Efficient | `0.65` | `topsis` | high |
| Compact | `0.5` | `min_size` (gated by `viability_score`) | very high |

This keeps **one config surface and one set of size numbers**: the reference is defined once;
each preset only carries a relative `size_aim` + weights + its `transitions` sub-table. Changing
"what a good 1080p file weighs" is a single edit in `[reference]`, not five preset edits.
Per-profile-name overrides may also set `size_aim` / weights; they inherit the shared reference.

## Code plan

1. **New module `transitions.py`** in `features/optimizer/`:
   - `classify(current, candidate, cfg) -> Deltas` (resolution/score/size buckets).
   - `is_forbidden(deltas, transition_cfg) -> (bool, reason)` implementing the universal rules
     + per-profile params. This is the single source the doc tables are generated from.
   - A `matrix(transition_cfg)` helper that renders the ACCEPT/❌ table (used by a tools/
     script to regenerate this doc and by tests to assert doc==code).
2. **Config** (`config.py`): parse the shared `[optimizer.topsis.reference]` table
   (`{floor, target, ceiling}` per resolution) and per-preset `size_aim` + `transitions`;
   default sensible values in `defaults.toml`; validate `floor < target ≤ ceiling`,
   `0 < size_aim ≤ 1`, `score_much > score_slack`, and `pick` ∈ {topsis, max_score, min_size}.
   Drop the old per-preset `size_by_resolution`.
3. **Decision** (`decision.py`): insert the transition gate between prefilters and the pick;
   replace the `min_closeness_gain` swap test with: *gate the candidates, then pick by the
   profile's `pick` method; ACT if a survivor exists, else HOLD (satisfied)*. Resolution
   fallback: run the gate on at-target candidates first, fall back to off-target only if empty.
4. **Topsis** (`topsis.py`): replace the tent in `normalize_size` with the **one-sided curve**
   — `n_size = 1.0` for `gbh ≤ aim` (where `aim = size_aim × reference.target[res]`), then
   linear ramp to `0` at `reference.ceiling[res]`; the `reference.floor[res]` stays a hard
   pre-filter drop. Resolution and score normalization unchanged. Keep `rank`/`closeness` for
   `pick = "topsis"`; add `max_score` / `min_size` deterministic pickers (stable tie-breaks so
   ranking is reproducible — important for oscillation).
5. **State**: unchanged (satisfied/HOLD model still applies — HOLD now means "no legal,
   improving transition exists").
6. **Tests**:
   - `test_transitions.py`: every universal rule, each profile matrix cell, and the
     **partial-order/no-oscillation property** (for each profile, for random A/B pairs, not
     both A→B and B→A accepted).
   - `test_decision.py`: resolution-fallback behavior; the four worked examples from the user.
   - Doc-sync test: `matrix()` output matches the tables in this file (or generate the tables).
7. **Validate the existing weights and limits** (see [Validation](#validation-of-current-weights-and-limits)).
8. **Docs**: keep the as-built matrices in `ALGORITHM.md`; update README's "How it works"
   step 3–4; keep this file as the rationale.
9. **Lint/format**: `ruff check` + `ruff format` before done; write validation results to a
   timestamped `.md` in `reports/`.

## Validation of current weights and limits

Before trusting the redesign, **re-validate every number** — the preset weights, the shared
`[reference]` `{floor, target, ceiling}`, each preset's `size_aim`, `score_gap`, and the new
`score_slack` / `score_much` / `size_slack` thresholds. The point is to confirm they produce the
right picks under the new gate + one-sided size curve (a floor/target tuned against the old
tent-everywhere model may be wrong now).

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
  - **much-smaller, lower-score current file**: confirm *no* profile inflates size — a file
    already at/below its aim is `n_size = 1.0` and is never swapped for a bigger one; Remux just
    holds unless a higher-scoring release exists.
  - **excellent release slightly above target must not be dropped for size**: a high-score
    release just above the aim takes only a small, linear `n_size` penalty (not a cliff), so for
    score-leaning profiles (Remux/Quality/Balanced) it still wins; for Efficient/Compact a
    *marginal* score lead may lose to a smaller file (intended), but a clearly-better release
    must win. Assert this; if a genuinely great release loses, the lever is `size_aim` above the
    realistic target (plateau extends past it) or a smaller size weight — not a hard exclusion.
- Assert the **no-oscillation property** on the real presets: for random A/B pairs there is no
  profile where both A→B and B→A are accepted (this also checks `score_much > score_slack`
  holds with the shipped numbers).
- Sweep each knob (`score_slack`, `score_much`, `size_slack`, the reference `floor/target/
  ceiling`, each `size_aim`) across a small range and record where picks flip, so the shipped
  defaults are chosen from data, not guessed. Land the values in `defaults.toml`, rationale in
  `reports/`.

Where possible, validate against a **real Radarr/Sonarr release pool** (a `dry_run = true` run,
or captured `GET /api/v3/release` payloads) rather than only synthetic scenarios, so the
reference floor/target/ceiling are checked against real-world bitrates.

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

The table above is the *old* per-preset `size_by_resolution` (kept for context). It's replaced
by a **single shared reference** with profiles aiming relative to it — see
[Size reference ladder](#size-reference-ladder-proposed-new-defaults). Takeaways folded in:

- **Don't inflate.** This is a space-saving tool, so the reference `target` is anchored to a
  *lean good HDR encode*, not a generous one, and no profile ever aims above it. The reference
  is the only set of absolute numbers; profiles pull downward from it.
- HDR is assumed, but it is **not** applied as a premium *on top of* generous SDR figures (that
  was the mistake — it pushed everything up). The reference targets are chosen directly as
  realistic lean HDR-content sizes and stay near the ~6 GiB/h-for-4K intuition.

Sources: [UHD/4K remux bitrates (Hacker News)](https://news.ycombinator.com/item?id=39337834),
[1080p Blu-ray/remux bitrates (Linus Tech Tips)](https://linustechtips.com/topic/1059408-dvdbluray-resolutions-and-bitrates/),
[x265 4K & 1080p encoding bitrates (arstech)](https://arstech.net/video-encoding-bitrates-for-4k-and-1080p-with-h-264-and-h-265/),
[HEVC bitrate guidance (Plex forum)](https://forums.plex.tv/t/is-there-a-standard-bitrate-that-is-recommended-for-hevc-h265-in-720p-1080p-and-4k/203849),
[4K HDR x265 encoding settings (Code Calamity)](https://codecalamity.com/encoding-settings-for-hdr-4k-videos-using-10-bit-x265/),
[1080p HDR vs SDR bitrate premium (arstech)](https://arstech.net/video-encoding-bitrates-for-4k-and-1080p-with-h-264-and-h-265/).

### Size reference ladder (proposed new defaults)

**One objective reference per resolution, shared by all profiles.** HDR-assumed, but anchored
to a *lean good HDR encode* — these numbers are deliberately modest because the whole point is
to reclaim disk, and every profile only ever aims **at or below** these. Unit GiB/h
(`GB = 1024³`); `1 Mbps ≈ 0.42 GiB/h`, Mbps shown for reference only.

| Res | floor | target | ceiling | target Mbps | what `target` represents |
| --- | --- | --- | --- | --- | --- |
| 2160 | 3.0 | 6.5 | 18 | ~15.5 | a good, lean 4K HDR x265 encode |
| 1080 | 1.0 | 2.5 | 8 | ~6 | a good 1080p HDR encode |
| 720 | 0.4 | 1.0 | 4 | ~2.4 | a good 720p encode |
| 480 | 0.2 | 0.5 | 3 | ~1.2 | SD (SDR; HDR n/a) |

Then each profile aims relative to `target` (`aim = size_aim × target`), with `n_size = 1.0`
at-or-below `aim` and ramping to 0 at `ceiling`. Worked aim points:

| Profile | `size_aim` | aim @2160 | aim @1080 | intent |
| --- | --- | --- | --- | --- |
| Quality | 1.0 | 6.5 | 2.5 | at the realistic target |
| Balanced | 0.8 | 5.2 | 2.0 | slightly smaller |
| Efficient | 0.65 | 4.2 | 1.6 | much smaller |
| Compact | 0.5 | 3.25 | 1.25 | toward the floor / smallest viable |
| Remux | — | — | — | ignores size (weight ~0), takes top score |

Why this is safe for disk and sensible:

- Nothing aims above `target`; the gate forbids bigger-at-no-gain; the curve never rewards
  *bigger*. So size only ever increases as a side effect of a real score/resolution upgrade.
- A current file already at/below a profile's aim scores `n_size = 1.0` → it is **never**
  swapped for a bigger one to "reach" a target. The much-smaller-file problem is gone for all
  profiles.
- Efficient @2160 aim ≈ 4.2 GiB/h (~10 Mbps) and Compact ≈ 3.25 (~8 Mbps) — close to the old
  shipped Efficient target (4) the user was comfortable with, *not* the inflated ~7 from the
  previous draft.
- `floor` (3.0 @2160 ≈ 7 Mbps) is the shared fake/too-soft cut; below it 4K HDR visibly bands.

These are the **sweep's starting points** — confirmed against a real HDR release pool, then
landed in `defaults.toml`.

Ready-to-paste:

```toml
[optimizer.topsis.reference]
"2160" = { floor = 3.0, target = 6.5, ceiling = 18 }
"1080" = { floor = 1.0, target = 2.5, ceiling = 8 }
"720"  = { floor = 0.4, target = 1.0, ceiling = 4 }
"480"  = { floor = 0.2, target = 0.5, ceiling = 3 }

# per preset (weights elsewhere): size_aim only
# Remux: size_aim unused (size weight ~0); Quality 1.0; Balanced 0.8; Efficient 0.65; Compact 0.5
```

## Resolved (this round)

- **slight/much measured on `Δn_score`** (fixed `[anti_ideal, ideal]` normalized scale), per
  axis — *not* on combined closeness, and *not* field-relative. Field-relativity stays in
  gap-cut. This is what preserves the no-oscillation invariant.
- **One shared size reference** `{floor, target, ceiling}` per resolution; profiles aim
  *relative* to it via `size_aim` (fraction of target). Config is always relative to the ideal,
  and the reference numbers are deliberately lean (space-saving tool).
- **One-sided size curve:** `n_size = 1.0` at/below the aim, ramping to 0 at ceiling. Smaller is
  never penalized → nothing ever inflates a file; the much-smaller-current-file problem is gone
  for every profile, not just some.
- **Bigger files only ever happen as a side effect of a real score/resolution gain** (gated).
  No profile targets a bigger file; the optimizer never infers quality from size beyond the
  fake-floor — it trusts the score.

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
