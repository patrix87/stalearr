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
       single-axis profiles (Remux, Quality, Compact) -> deterministic sort
       multi-axis profiles (Efficient, Balanced)       -> TOPSIS for best fit
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

"Slightly lower" vs "much lower" score is the crux of every size-leaning profile and needs a
real threshold (e.g. a relative drop ≤ `score_slack` is "slightly lower", more is "much
lower"). `score_slack` is a fraction of the score scale or of the current score — TBD during
implementation, lean toward *relative to current score*.

Resolution rule shared by all profiles: the **target resolution is always what the profile
wants**. A different resolution is only acceptable when nothing at target survives the gate —
i.e. resolution is a **fallback dimension**, not something to trade away for size/score.
Implementation: gate at-target candidates first; only if that set is empty, reconsider
off-target ones.

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

Balanced and Efficient are the same shape (good score + smaller file); they differ only in
tilt. Those two are the genuine multi-axis cases → keep TOPSIS for them. Remux/Quality/Compact
are effectively single-axis once the gate has run → a deterministic sort is clearer and
oscillation-proof.

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
| higher | ✅ | ✅ | ✅ (if gain clears `bigger_score_gate`) |
| same | ✅ | — | ❌ |
| slightly lower | ✅ (only if *much* smaller) | ❌ | ❌ |
| much lower | ❌ | ❌ | ❌ |

Quality will accept a slightly-lower score only to bank a large size saving; a bigger file
needs a real score gain to justify it.

### Balanced — good score + smaller, leans score

| score ↓ / size → | smaller | same | bigger |
| --- | --- | --- | --- |
| higher | ✅ | ✅ | ✅ (if gain clears `bigger_score_gate`) |
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
  (bigger needs to clear `bigger_score_gate`, a *large* gain, not slight). ✅
- *smaller, same score* accepted ⇒ reverse *bigger, same score* is ❌ (universal rule #1). ✅
- *bigger, higher score* accepted (clears gate) ⇒ reverse *smaller, lower score* — lower by the
  same gate amount = "much lower" = ❌. ✅ (as long as `bigger_score_gate` > `score_slack`,
  the slightly/much boundary — this is the **one numeric constraint we must enforce in
  config validation**: `bigger_score_gate > score_slack`.)

So the implementation must assert `bigger_score_gate > score_slack` at config-load time. That
single inequality is what guarantees no two-file cycle. Document it loudly.

(The current `min_closeness_gain` provided weak oscillation protection by requiring strict
improvement; the partial-order argument replaces it with a structural guarantee.)

## Config shape (proposed)

The matrix is parameterized, not enumerated. Per profile (preset), add a `[…transitions]`
block alongside the existing weights/size curve:

```toml
[optimizer.topsis.presets.Efficient]
score = 0.55          # still used by the TOPSIS pick step (multi-axis profiles)
resolution = 0.10
size = 0.35
size_by_resolution = { ... }              # unchanged, still feeds gb/h floor + size tent

[optimizer.topsis.presets.Efficient.transitions]
score_slack = 0.05            # relative drop still counted as "slightly lower"
bigger_score_gate = 0.15      # min relative score gain to justify a bigger file (> score_slack!)
allow_bigger_for_score = true # Compact sets this false
viability_score = 0           # Compact/Quality floor for accepting score drops
pick = "topsis"               # "topsis" | "max_score" | "min_size"
```

- `pick = "max_score"` → Remux/Quality (deterministic, size only as tie-break).
- `pick = "min_size"`  → Compact.
- `pick = "topsis"`    → Balanced/Efficient (existing weights/size tent decide best fit).

This keeps **one config surface**: presets gain a `transitions` sub-table; the size tent stays
for the gb/h floor and for TOPSIS. Per-profile-name overrides inherit the same sub-table.

## Code plan

1. **New module `transitions.py`** in `features/optimizer/`:
   - `classify(current, candidate, cfg) -> Deltas` (resolution/score/size buckets).
   - `is_forbidden(deltas, transition_cfg) -> (bool, reason)` implementing the universal rules
     + per-profile params. This is the single source the doc tables are generated from.
   - A `matrix(transition_cfg)` helper that renders the ACCEPT/❌ table (used by a tools/
     script to regenerate this doc and by tests to assert doc==code).
2. **Config** (`config.py`): parse `presets.<n>.transitions`, default sensible values per
   shipped preset in `defaults.toml`, validate `bigger_score_gate > score_slack` and that
   `pick` ∈ {topsis, max_score, min_size}.
3. **Decision** (`decision.py`): insert the transition gate between prefilters and the pick;
   replace the `min_closeness_gain` swap test with: *gate the candidates, then pick by the
   profile's `pick` method; ACT if a survivor exists, else HOLD (satisfied)*. Resolution
   fallback: run the gate on at-target candidates first, fall back to off-target only if empty.
4. **Topsis** (`topsis.py`): keep `rank`/`closeness` for the `pick = "topsis"` path; add
   `max_score` / `min_size` deterministic pickers (with stable tie-breaks so ranking is
   reproducible — important for oscillation).
5. **State**: unchanged (satisfied/HOLD model still applies — HOLD now means "no legal,
   improving transition exists").
6. **Tests**:
   - `test_transitions.py`: every universal rule, each profile matrix cell, and the
     **partial-order/no-oscillation property** (for each profile, for random A/B pairs, not
     both A→B and B→A accepted).
   - `test_decision.py`: resolution-fallback behavior; the four worked examples from the user.
   - Doc-sync test: `matrix()` output matches the tables in this file (or generate the tables).
7. **Docs**: fold the final matrices into `optimizer-design.md`; update README's "How it works"
   step 3–4; keep this file as the rationale.
8. **Lint/format**: `ruff check` + `ruff format` before done; write validation results to a
   timestamped `.md` in `reports/`.

## Open questions to settle during implementation

- `score_slack` / `bigger_score_gate` units: relative to current score, or to the
  `[anti_ideal, ideal]` scale? Lean **relative to current** so it behaves sensibly across the
  whole library.
- Size "same" tolerance: a few % band so re-encodes of identical size don't churn.
- Does Quality really want `pick = "max_score"`, or TOPSIS with a heavy score weight? Start
  with `max_score` + size tie-break; revisit if it grabs marginal upgrades too eagerly.
- Should the universal rules be overridable per profile, or truly hard? Keep them hard for now.

## What stays the same

- Worker loop, queue gating, satisfied-state, unmonitor, auto-import of downgrades.
- Hard rejections, gb/h floor, score gap-cut prefilters.
- The grab-is-never-"done" philosophy and re-evaluation cadence.
