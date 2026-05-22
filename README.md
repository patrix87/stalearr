# stalearr

Unmonitor Radarr movies and Sonarr episodes a configurable number of days
after release, so the *arr apps stop chasing pointless upgrades.

## What it does

On a cron schedule (default: daily at 04:00), for each configured app:

1. Fetch all items.
2. Keep only items that are currently monitored.
3. Optionally require that the quality cutoff has been met.
4. Compute the age from the configured release date field.
5. If age >= `DAYS`, unmonitor in a single bulk API call.

Radarr uses `PUT /api/v3/movie/editor`. Sonarr uses `PUT /api/v3/episode/monitor`.

## Configuration

All configuration is via environment variables.

### Global

| Variable | Default | Notes |
| --- | --- | --- |
| `CRON_SCHEDULE` | `0 4 * * *` | Standard 5-field cron. |
| `RUN_ON_START` | `true` | Run one pass at container start before sleeping to the next cron tick. |
| `DRY_RUN` | `false` | Log candidates, send no writes. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `TZ` | container default | Affects cron evaluation and log timestamps. |

### Radarr (optional; set both URL and API key to enable)

| Variable | Default | Notes |
| --- | --- | --- |
| `RADARR_URL` | unset | e.g. `http://radarr:7878`. |
| `RADARR_API_KEY` | unset | From Radarr Settings -> General. |
| `RADARR_DAYS` | `30` | Days since the release date field. |
| `RADARR_RELEASE_TYPE` | `digitalRelease` | One of `digitalRelease`, `physicalRelease`, `inCinemas`, `releaseDate`, `dateAdded`. |
| `RADARR_REQUIRE_CUTOFF_MET` | `true` | If true, only unmonitor movies whose file has reached the quality profile cutoff. |

### Sonarr (optional; set both URL and API key to enable)

| Variable | Default | Notes |
| --- | --- | --- |
| `SONARR_URL` | unset | e.g. `http://sonarr:8989`. |
| `SONARR_API_KEY` | unset | From Sonarr Settings -> General. |
| `SONARR_DAYS` | `30` | Days since the release date field. |
| `SONARR_RELEASE_TYPE` | `airDateUtc` | One of `airDateUtc`, `dateAdded`. Operates per episode. |
| `SONARR_REQUIRE_CUTOFF_MET` | `true` | If true, only unmonitor episodes whose file has reached the quality profile cutoff. |

If neither Radarr nor Sonarr is configured, the container exits 1.

## Docker Compose

Pull the published image from GitHub Container Registry:

```yaml
services:
  stalearr:
    image: ghcr.io/patrix87/stalearr:latest
    container_name: stalearr
    restart: unless-stopped
    environment:
      # Schedule and behavior
      TZ: America/Toronto
      CRON_SCHEDULE: "0 4 * * *"
      RUN_ON_START: "true"
      DRY_RUN: "false"
      LOG_LEVEL: INFO

      # Radarr (omit both URL and API_KEY to disable Radarr handling)
      RADARR_URL: http://radarr:7878
      RADARR_API_KEY: ${RADARR_API_KEY}
      RADARR_DAYS: "30"
      # One of: digitalRelease, physicalRelease, inCinemas, releaseDate, dateAdded
      RADARR_RELEASE_TYPE: digitalRelease
      RADARR_REQUIRE_CUTOFF_MET: "true"

      # Sonarr (omit both URL and API_KEY to disable Sonarr handling)
      SONARR_URL: http://sonarr:8989
      SONARR_API_KEY: ${SONARR_API_KEY}
      SONARR_DAYS: "30"
      # One of: airDateUtc, dateAdded
      SONARR_RELEASE_TYPE: airDateUtc
      SONARR_REQUIRE_CUTOFF_MET: "true"
```

Drop a `.env` next to the compose file with `RADARR_API_KEY=...` and
`SONARR_API_KEY=...`, then `docker compose up -d`. Pin to a specific
tag (e.g. `:v0.1.0`) instead of `:latest` for reproducible deploys.

To build locally from a clone instead, replace the `image:` line with
`build: .`.

## Local development

```sh
# Install dev deps and run tests
uv sync
uv run pytest

# Lint
uv run ruff check .
uv run ruff format --check .

# Run against a real Radarr/Sonarr without Docker
cp .env.example .env       # fill in URLs + API keys
uv run --env-file .env python -m stalearr
```

Set `DRY_RUN=true` in `.env` first to log candidates without writing anything.

## Releasing

Two workflows live in `.github/workflows/`:

- **ci.yml** — runs `ruff check`, `ruff format --check`, and `pytest` on every
  push to `main` and every pull request.
- **release.yml** — builds a `linux/amd64` image and pushes it to
  `ghcr.io/patrix87/stalearr`.

Triggers and resulting tags:

| Trigger | Tags pushed |
| --- | --- |
| Push tag `v1.2.3` | `1.2.3`, `1.2`, `1`, `latest` |
| Manual `workflow_dispatch` | (none — image built but no semver tag applies) |

Releases only fire on `v*` tag pushes; commits to `main` run CI but do not
rebuild the image. To cut a release:

```sh
git tag v0.1.0
git push --tags
```

One-time repo setup before the first release run:

- **Settings -> Actions -> General -> Workflow permissions**: set to
  *Read and write permissions* (so the workflow can push to GHCR).
- After the first push, open the package at
  `https://github.com/patrix87?tab=packages`, set visibility to **Public**,
  and confirm it linked to the repo (the `org.opencontainers.image.source`
  label in the Dockerfile handles this automatically).

## Maintenance

Update pinned dependency versions:

```sh
uv lock --upgrade
```
