# optimizarr

Two complementary jobs for a Radarr/Sonarr library, in one container:

1. **Optimizer** — a continuous worker that re-evaluates the releases available for each
   library item and grabs a better one when it exists (smaller at equal quality, or a
   genuine quality upgrade), using a multi-objective [TOPSIS](docs/optimizer-design.md)
   ranking. It is built around the reality that grabs frequently fail to download, so
   *"optimized"* means *the algorithm can no longer find anything better than the current
   file* — never merely *"we triggered a grab."*
2. **Unmonitor** — a cron job that unmonitors movies/episodes a configurable number of
   days after release, so the \*arr apps stop chasing pointless upgrades.

Both features are optional and independent. See
[docs/optimizer-design.md](docs/optimizer-design.md) for the full algorithm and worker-loop
design (with diagrams).

## Designed for Profilarr quality profiles

optimizarr is built to pair with **[Profilarr](https://github.com/Dictionarry-Hub/profilarr)**.
The defaults assume Profilarr's custom-format scoring convention — a "perfect" release
scores around **1,000,000** — and the per-profile tuning (weights, size envelopes) is keyed
by **quality-profile names** like `2160p Quality` or `1080p Efficient`. If you run Profilarr,
the defaults work out of the box.

It is **not strictly required**, though. The score scale and the profile-name-keyed tuning
are all configurable: set `score_ideal` to match your own scoring target, and add entries
under `weights_by_profile` / `size_envelope_by_profile` keyed by *your* profile names. With
those adjusted, optimizarr works against any custom-format scoring scheme — Profilarr just
saves you from tuning it yourself.

## How the optimizer works (short version)

The worker refreshes the library list on a slow interval and, on each short tick, if the
download queue is at or under `queue_max`, picks one not-yet-satisfied item that isn't
already downloading and evaluates it:

1. Fetch candidate releases (`GET /api/v3/release`).
2. Pre-filter: drop hard rejections (blocklisted/unparseable/dead), then a per-resolution
   GB/h sanity floor, then an adaptive 4-tier score floor (negatives always dropped).
3. Rank survivors with TOPSIS on three axes — score, resolution, size — using per-profile
   weights and size envelopes.
4. If the top pick's overall closeness beats the current file's by at least
   `min_closeness_gain` → **grab it**. Otherwise → **HOLD** and mark the item *satisfied* so
   it drops out of the pool until `reevaluate_after_days` elapses. Closeness already weighs
   score, resolution, and size together, so that single threshold covers both shrinking a
   bloated file and upgrading quality — there's no separate size/upgrade gate to balance.

A grab is never recorded as "done". Success simply shows up as a HOLD on the next
evaluation (→ satisfied); a failed grab was never satisfied, so the item stays in the pool
and is retried later — by then the dead release has been blocklisted by Radarr/Sonarr's
Failed Download Handling, so the next-best release is picked. **Failed Download Handling
must be enabled** (it is by default).

The download queue does double duty: it's the global pace gate (`queue_max`) *and* the
"skip anything already downloading" check — so there's no in-flight state to track and a
restart needs no reconciliation.

## Configuration

Two-part configuration:

- **Environment** holds only secrets, URLs, and paths.
- **`config.toml`** holds all behavior and tuning. Copy
  [`config.example.toml`](config.example.toml) and edit.

### Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `RADARR_URL` / `RADARR_API_KEY` | unset | Set both to enable Radarr. |
| `SONARR_URL` / `SONARR_API_KEY` | unset | Set both to enable Sonarr. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `TZ` | container default | Affects cron evaluation and log timestamps. |

If neither Radarr nor Sonarr is configured, the container exits 1.

### `config.toml`

All behavior lives in `config.toml`. The full set of keys — `dry_run`, the `[unmonitor]`
schedule/per-app options, the `[optimizer]` worker settings, the per-app `min_age_days`
release-age gate, and the `[optimizer.topsis]` tuning (weights, size envelopes, score
floor, swap gates — all per-profile) — is documented inline in
[`config.example.toml`](config.example.toml). Copy it to `config.toml` and edit.

The TOPSIS defaults match the tuning validated during development; you should only need to
touch them to add new profile names or adjust targets.

> The optimizer selects items by **`hasFile`**, regardless of monitored state — it improves
> the existing library, and the unmonitor job deliberately strips monitoring once a file
> exists.

## Docker Compose

```yaml
services:
  optimizarr:
    image: ghcr.io/patrix87/optimizarr:latest
    container_name: optimizarr
    restart: unless-stopped
    environment:
      TZ: America/Toronto
      LOG_LEVEL: INFO
      RADARR_URL: http://radarr:7878
      RADARR_API_KEY: ${RADARR_API_KEY}
      SONARR_URL: http://sonarr:8989
      SONARR_API_KEY: ${SONARR_API_KEY}
    volumes:
      - ./config:/config   # put config.toml here
      - ./data:/data       # state.json lives here
```

Drop a `.env` next to the compose file with `RADARR_API_KEY=...` / `SONARR_API_KEY=...`,
place your `config.toml` in `./config/`, then `docker compose up -d`. Pin to a specific tag
(e.g. `:v0.2.0`) instead of `:latest` for reproducible deploys. To build locally from a
clone, replace the `image:` line with `build: .`.

## Local development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .

# Run against a real Radarr/Sonarr without Docker:
cp .env.example .env             # fill in URLs + API keys
cp config.example.toml config.toml
# optimizarr reads /config/config.toml and writes /data/state.json by default;
# to run outside Docker, edit CONFIG_PATH / STATE_PATH at the top of optimizarr/config.py.
uv run --env-file .env python -m optimizarr
```

Set `dry_run = true` in `config.toml` first to log would-be actions without writing
anything (no grabs, no unmonitors, no state changes).

## Releasing

Two workflows live in `.github/workflows/`:

- **ci.yml** — runs `ruff check`, `ruff format --check`, and `pytest` on every push to
  `main` and every pull request.
- **release.yml** — builds a `linux/amd64` image and pushes it to
  `ghcr.io/patrix87/optimizarr` (image name follows the repo via `${{ github.repository }}`).

| Trigger | Tags pushed |
| --- | --- |
| Push tag `v1.2.3` | `1.2.3`, `1.2`, `1`, `latest` |
| Manual `workflow_dispatch` | (none — image built but no semver tag applies) |

```sh
git tag v0.2.0
git push --tags
```

One-time repo setup before the first release run:

- **Settings -> Actions -> General -> Workflow permissions**: *Read and write permissions*.
- After the first push, open the package at `https://github.com/patrix87?tab=packages`, set
  visibility to **Public**, and confirm it linked to the repo.

## Maintenance

```sh
uv lock --upgrade
```
