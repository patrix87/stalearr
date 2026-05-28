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

    PICK --> GA{"Path A — shrink<br/>closeness gain >= min<br/>AND size savings >= min?"}
    PICK --> GB{"Path B — upgrade<br/>closeness gain >= upgrade min?"}
    GA -->|pass| ACT["ACT — POST grab {guid, indexerId}"]
    GB -->|pass| ACT
    GA -->|fail| OR{"either path passed?"}
    GB -->|fail| OR
    OR -->|no| HOLD["HOLD — mark satisfied"]
    OR -->|yes| ACT
```

### Pipeline notes

- The three pre-filters run in order; each only narrows the set. Tiers 1 → 2 → 2.5 → 3
  are tried in sequence and the **first non-empty tier wins** — so a clean library lands
  in Tier 1, a sparse search degrades gracefully, and negative-scored (Profilarr-banned)
  releases are never considered.
- TOPSIS weights and the size envelope (target/bloat GB/h) are **per profile**: a
  `2160p Quality` item is scored differently than `2160p Efficient`. Score dominates on
  Quality; size matters more on Efficient.
- Two independent gates lead to ACT. **Path A** is "keep quality, save real disk space."
  **Path B** is "materially better quality, size increase tolerated" (e.g. 1080p → 2160p).
  Either passing is enough.

---

## 2. Worker loop

The optimizer is a continuous interval-driven worker (not a cron pass). The unmonitor
feature keeps its own cron; the optimizer's cadence is governed by its own timers.

```mermaid
flowchart TD
    Start(["Optimizer worker start"]) --> Refresh["Fetch item list from Radarr/Sonarr"]
    Refresh --> Pool["Build active pool:<br/>cached − satisfied(in window) − in-flight"]
    Pool --> Empty{"Active pool empty?"}
    Empty -->|yes| Idle["Sleep until next list refresh<br/>(list_refresh_minutes)"]
    Idle --> Refresh
    Empty -->|no| Q["GET /api/v3/queue<br/>(one call: global gate + in-flight set)"]
    Q --> Gate{"queue count <= queue_max?"}
    Gate -->|no| WaitQ["Sleep queue_recheck_seconds"]
    WaitQ --> Q
    Gate -->|yes| Pick["Pick item — random or ordered"]
    Pick --> InFlight{"Item id in queue?"}
    InFlight -->|yes| Drop["In-flight — skip,<br/>remove from this session's pool"]
    Drop --> Settle
    InFlight -->|no| Eval["Evaluate releases<br/>(see pipeline above)"]
    Eval --> Decide{"ACT or HOLD?"}
    Decide -->|HOLD| Sat["State: satisfied<br/>(record file id + timestamp)"]
    Decide -->|ACT| Grab["POST grab · State: in_flight<br/>(record guid + timestamp)"]
    Sat --> RemovePool["Remove item from active pool"]
    Grab --> RemovePool
    RemovePool --> Settle["Sleep process_interval_seconds<br/>settle: let the grab surface in the queue"]
    Settle --> Empty
```

### Loop notes

- The **queue fetch sits at the top of each iteration**, *after* the settle sleep, so it
  always reflects the previous iteration's grab. One fetch serves both the global gate
  (`queue_max`) and the per-item in-flight check.
- `process_interval_seconds` (default 10) is a **settle delay**, not just pacing: after a
  `POST /api/v3/release`, Radarr needs a moment to hand the release to the download client
  and register it in the queue. Reading the queue too soon would miss the just-grabbed item.
- The active pool shrinks as items are processed and only grows on list refresh — this is
  what stops the worker from re-querying every item forever. Random pick draws from the
  active pool only, so optimized items are never re-picked.

---

## 3. Per-item state lifecycle

State lives in `/data/state.json`, keyed by movie id / episode id. The lifecycle is what
makes failed downloads self-correcting — no cooldown timer required.

```mermaid
stateDiagram-v2
    [*] --> Unprocessed: not in state

    Unprocessed --> Satisfied: HOLD — nothing better than current
    Unprocessed --> InFlight: ACT — grab posted

    InFlight --> InFlight: still in queue (downloading)
    InFlight --> Satisfied: left queue, file id changed (grab succeeded)
    InFlight --> Unprocessed: left queue, file unchanged (grab failed, blocklisted)

    Satisfied --> Unprocessed: reevaluate_after_days elapsed
```

### Why this self-corrects on failure

- A grab that **succeeds** replaces the file; on the next evaluation the algorithm sees a
  good current file and returns HOLD → the item becomes **satisfied**.
- A grab that **fails** is blocklisted by Radarr's Failed Download Handling. On the next
  evaluation, pre-filter 1 drops that blocklisted release, so TOPSIS picks the **next-best**
  candidate. Repeated failures simply walk down the ranking, one blocklisted release at a
  time, until one sticks (→ satisfied) or no viable candidate remains (HOLD → satisfied).
- In-flight is detected purely from queue membership, so an item mid-download is never
  re-grabbed.

> **Dependency:** this relies on Radarr/Sonarr **Failed Download Handling** being enabled
> (default on) so dead releases get blocklisted. Without it, a failed grab would not be
> de-prioritised on the next pass.
