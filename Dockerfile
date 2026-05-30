ARG UV_VERSION=0.7.13
FROM python:3.13-slim AS uv-builder

ARG UV_VERSION
ARG TARGETARCH
COPY script/install-uv.sh /install-uv.sh
ENV UV_VERSION=${UV_VERSION}
ENV TARGETARCH=${TARGETARCH}
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/var/cache/apt \
    chmod +x /install-uv.sh && /install-uv.sh

FROM python:3.13-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY --from=uv-builder /usr/local/bin/uv* /usr/local/bin/

RUN mkdir optimizarr && echo '__version__ = "0.0.0"' > optimizarr/__init__.py && touch README.md

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.13-slim AS runner

LABEL org.opencontainers.image.title="optimizarr"
LABEL org.opencontainers.image.description="TOPSIS-based release optimizer for Radarr/Sonarr, plus a configurable unmonitor job."
LABEL org.opencontainers.image.source="https://github.com/patrix87/optimizarr"
LABEL org.opencontainers.image.url="https://github.com/patrix87/optimizarr"
LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv /app/.venv

CMD ["python", "-m", "optimizarr"]
