# Tier 1 — the part that actually catches an early program release — needs nothing
# but Python and network access, so that is what the default image contains.
#
# Tier 2 (seat maps) and `assist: drive` additionally need Chromium. Build with
#   --build-arg WITH_BROWSER=1
# to include it. It roughly quadruples the image, and the booking host may refuse
# automated sessions anyway, so it is opt-in.
FROM python:3.11-slim AS base

ARG WITH_BROWSER=0
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    TG_CONFIG_FILE=/app/config.yaml

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
 && if [ "$WITH_BROWSER" = "1" ]; then \
        pip install --no-cache-dir '.[browser]' \
     && playwright install --with-deps chromium; \
    fi

# Runtime state lives here; mount a volume so the database and any browser profile
# survive container replacement.
RUN mkdir -p /app/data && useradd -r -u 10001 -d /app tg && chown -R tg /app
USER tg

VOLUME ["/app/data"]
EXPOSE 8756

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8756/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tg", "run"]
