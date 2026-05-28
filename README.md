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

## How the optimizer works (short version)

For each item with a file, on its own queue-gated interval:

1. Fetch candidate releases (`GET /api/v3/release`).
2. Pre-filter: drop hard rejections (blocklisted/unparseable/dead), then a per-resolution
   GB/h sanity floor, then an adaptive 4-tier score floor (negatives always dropped).
3. Rank survivors with TOPSIS on three axes — score, resolution, size — using per-profile
   weights and size envelopes.
4. Grab the top pick only if it clears a swap gate (real size savings at equal quality, or
   a material quality upgrade); otherwise HOLD and mark the item satisfied.

In-flight downloads are detected from the queue, not a timer. A failed grab is blocklisted
by Radarr/Sonarr's Failed Download Handling and simply skipped on the next pass — so
repeated failures walk down the ranking until one sticks. **Failed Download Handling must
be enabled** (it is by default).

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
| `CONFIG_PATH` | `/config/config.toml` | Path to the TOML config. |
| `STATE_PATH` | `/data/state.json` | Path to the optimizer state file. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `TZ` | container default | Affects cron evaluation and log timestamps. |

If neither Radarr nor Sonarr is configured, the container exits 1.

### `config.toml`

```toml
dry_run = false   # log only, never grab or unmonitor

[unmonitor]
enabled = true
cron_schedule = "0 4 * * *"
run_on_start = true
[unmonitor.radarr]
days = 30
release_type = "digitalRelease"   # digitalRelease|physicalRelease|inCinemas|releaseDate|dateAdded
require_cutoff_met = true
[unmonitor.sonarr]
days = 30
release_type = "airDateUtc"       # airDateUtc|dateAdded
require_cutoff_met = true

[optimizer]
enabled = false
apps = ["radarr", "sonarr"]
queue_max = 0                     # only act when the queue has <= this many items (0 = empty)
pick_order = "random"             # random|ordered
process_interval_seconds = 10     # settle delay after a grab so it surfaces in the queue
queue_recheck_seconds = 60
list_refresh_minutes = 60
reevaluate_after_days = 30
```

The `[optimizer.topsis]` block (weights, size envelopes, score floor, swap gates — all
per-profile) is documented inline in [`config.example.toml`](config.example.toml). The
defaults match the tuning validated during development; you should only need to touch them
to add new profile names or adjust targets.

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
      CONFIG_PATH: /config/config.toml
      STATE_PATH: /data/state.json
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
cp .env.example .env             # fill in URLs + API keys + CONFIG_PATH/STATE_PATH
cp config.example.toml config.toml
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
