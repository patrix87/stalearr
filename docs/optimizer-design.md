# Optimizarr — Optimizer Design

The optimizer evaluates the releases available for each library item, decides whether a
better one exists (smaller at equal quality, or a genuine quality upgrade), and grabs it
through Radarr/Sonarr. It is built around the reality that **grabbed releases frequently
fail to download** — so "optimized" means *the algorithm can no longer find anything
better than the current file*, never merely *we triggered a grab*.

This document describes two things:

1. **Release evaluation** — how a single item's candidate releases are filtered, scored,
   and turned into an ACT/HOLD decision.
2. **Worker loop** — the continuous, queue-gated process that walks the library, and the
   per-item state lifecycle that makes failure handling self-correcting.

---

## 1. Release evaluation pipeline

For one movie (or episode), this is how candidates become a decision.

```mermaid
flowchart TD
    A["Item selected for evaluation"] --> B["GET /api/v3/release for the item"]
    B --> C["Pre-filter 1: drop hard rejections<br/>blocklisted · unparseable · wrong item · dead torrent"]
    C --> D["Pre-filter 2: drop below GB/h sanity floor<br/>per resolution — catches fake/low-bitrate encodes"]
    D --> E["Pre-filter 3: adaptive score floor"]

    E --> T1{"Tier 1<br/>any release >= 900k?"}
    T1 -->|yes| P["Candidate pool"]
    T1 -->|no| T2{"Tier 2<br/>any >= current file's score?"}
    T2 -->|yes| P
    T2 -->|no| T3{"Tier 2.5<br/>any >= top score − 250k?"}
    T3 -->|yes| P
    T3 -->|no| T4["Tier 3: all releases >= 0<br/>(negative scores always dropped)"]
    T4 --> P

    P --> S["TOPSIS: score each candidate on 3 axes<br/>score · resolution · size (per-profile envelope + weights)"]
    S --> R["Rank by closeness to ideal"]
    R --> PICK["Pick = highest closeness"]

    PICK --> G{"closeness(pick) − closeness(current)<br/>>= min_closeness_gain?"}
    G -->|yes| ACT["ACT — POST grab {guid, indexerId}"]
    G -->|no| HOLD["HOLD — mark satisfied"]
```

### Pipeline notes

- The three pre-filters run in order; each only narrows the set. Tiers 1 → 2 → 2.5 → 3
  are tried in sequence and the **first non-empty tier wins** — so a clean library lands
  in Tier 1, a sparse search degrades gracefully, and negative-scored (Profilarr-banned)
  releases are never considered.
- TOPSIS weights and the size envelope (target/bloat GB/h) are **per profile**: a
  `2160p Quality` item is scored differently than `2160p Efficient`. Score dominates on
  Quality; size matters more on Efficient.
- The swap decision is a **single threshold**: grab iff the pick's closeness beats the
  current file's by at least `min_closeness_gain`. Because closeness already folds in score,
  resolution, and size (via the per-profile envelope + weights), that one check naturally
  covers both shrinking a bloated file (smaller → higher `n_size` → higher closeness) and a
  genuine quality upgrade (e.g. 1080p → 2160p). The policy lives in the **weights**, not in
  separate size/upgrade gates — so tuning behavior means tuning the weights.

---

## 2. Worker loop

The optimizer is a continuous interval-driven worker (not a cron pass). The unmonitor
feature keeps its own cron; the optimizer's cadence is governed by its own timers.

```mermaid
flowchart TD
    Start(["Optimizer worker start"]) --> Refresh["Fetch item list from Radarr/Sonarr<br/>(every list_refresh_minutes)"]
    Refresh --> Pool["Build active pool:<br/>items with a file − satisfied(in window) − already evaluated this pass"]
    Pool --> Empty{"Active pool empty?"}
    Empty -->|yes| Idle["Idle sleep, then re-check / refresh"]
    Idle --> Refresh
    Empty -->|no| Q["GET /api/v3/queue<br/>(one call: pace gate + 'already downloading' set)"]
    Q --> Gate{"queue count <= queue_max?"}
    Gate -->|no| WaitQ["Sleep process_interval_seconds"]
    WaitQ --> Q
    Gate -->|yes| Pick["Pick item — random or ordered"]
    Pick --> Dl{"Item id already in queue?"}
    Dl -->|yes| Skip["Skip — already downloading"]
    Skip --> Settle
    Dl -->|no| Eval["Evaluate releases<br/>(see pipeline above)"]
    Eval --> Decide{"ACT or HOLD?"}
    Decide -->|HOLD| Sat["Mark satisfied<br/>(drops out of the pool)"]
    Decide -->|ACT| Grab["POST grab — record nothing"]
    Sat --> Settle["Sleep process_interval_seconds<br/>settle: let a grab surface in the queue"]
    Grab --> Settle
    Settle --> Empty
```

### Loop notes

- One **queue fetch per iteration** serves both the pace gate (`queue_max`) and the
  "is this item already downloading?" skip — so there is **no in-flight state** to track,
  and a restart needs no reconciliation.
- `process_interval_seconds` (default 10) is a **settle delay**: after a
  `POST /api/v3/release`, Radarr needs a moment to register the release in the queue.
  Reading too soon would make the next `queue_max` check miss the just-grabbed item.
- A grab **records nothing**. Each picked item is remembered for the current **pass** so
  it isn't re-picked; one pass covers every not-yet-satisfied item, however long that takes.
  A **list refresh does not restart the pass** — it only updates the candidate set (new
  items become pickable, removed ones drop). When the pass is fully covered it resets and a
  new one begins. Satisfied items stay excluded until their reevaluate window elapses, so
  over successive passes the active set keeps shrinking.

---

## 3. Per-item state lifecycle

State lives in `/data/state.json`, keyed by movie id / episode id. It records exactly one
thing — whether an item is **satisfied** — and that minimalism is what makes failure
handling self-correcting, with no in-flight tracking or cooldown timer.

```mermaid
stateDiagram-v2
    [*] --> Unprocessed: not in state

    Unprocessed --> Unprocessed: ACT — grab posted (state unchanged)
    Unprocessed --> Satisfied: HOLD — nothing better than current

    Satisfied --> Unprocessed: reevaluate_after_days elapsed
```

### Why this self-corrects on failure

- A grab is **never recorded**. The only persisted states are *unprocessed* and *satisfied*.
- A grab that **succeeds** replaces the file; on the next evaluation the algorithm sees a
  good current file and returns HOLD → the item becomes **satisfied** and leaves the pool.
- A grab that **fails** was never marked satisfied, so the item stays in the pool. When it's
  picked again, pre-filter 1 drops the now-blocklisted release and TOPSIS picks the
  **next-best** candidate. Repeated failures walk down the ranking, one blocklisted release
  at a time, until one sticks (→ satisfied) or nothing viable remains (HOLD → satisfied).
- A download **in progress** is skipped via live queue membership, never re-grabbed — so the
  "did the grab work?" question is answered implicitly by re-evaluation, not by bookkeeping.

> **Dependency:** this relies on Radarr/Sonarr **Failed Download Handling** being enabled
> (default on) so dead releases get blocklisted. Without it, a failed grab would not be
> de-prioritised on the next pass.
