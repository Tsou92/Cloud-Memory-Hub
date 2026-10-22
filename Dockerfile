# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS source
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY scripts/__init__.py scripts/check_repository.py scripts/prepare_upstream.py ./scripts/
COPY upstream ./upstream
RUN python -m scripts.prepare_upstream

FROM node:24.10.0-bookworm-slim@sha256:b8d2197aff9129d16c801a3e3e1b2a873c4946480f5a310f38056df2268c38d9 AS web
RUN npm install --global pnpm@11.24.0
WORKDIR /workspace
COPY --from=source /workspace/.local/upstream/hy-memory/memhub/ ./
RUN pnpm --filter @one-memory/web... install --frozen-lockfile --ignore-scripts
ENV VITE_CORE_URL=/
RUN pnpm --filter @one-memory/web... -r build

FROM source AS python-build
RUN python -m pip install --no-cache-dir uv==0.12.5
COPY apps/hub ./apps/hub
RUN uv sync --locked --no-dev --no-editable --directory apps/hub

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home cmh \
    && mkdir -p /data /config /backups \
    && chown 1000:1000 /data /config /backups \
    && chmod 700 /data /config /backups
COPY --from=python-build /workspace/apps/hub/.venv /opt/cmh/venv
COPY --from=web /workspace/apps/web/dist /opt/cmh/web
COPY --from=source /workspace/.local/upstream/hy-memory/memhub/LICENSE /opt/cmh/licenses/MemHub-LICENSE
COPY --from=source /workspace/.local/upstream/hy-memory/memhub/NOTICE /opt/cmh/licenses/MemHub-NOTICE
COPY --from=source /workspace/.local/upstream/hy-memory/memhub/THIRD_PARTY_NOTICES.md /opt/cmh/licenses/THIRD_PARTY_NOTICES.md
COPY upstream/manifest.json /opt/cmh/upstream-manifest.json
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    CMH_DATA_DIR=/data CMH_CONFIG_DIR=/config \
    ONE_MEMORY_WEB_DIR=/opt/cmh/web XDG_CACHE_HOME=/data/cache \
    HF_HOME=/data/cache/huggingface HF_HUB_DISABLE_TELEMETRY=1 ANONYMIZED_TELEMETRY=False
ARG CMH_REVISION=development
LABEL org.opencontainers.image.title="Cloud Memory Hub" \
    org.opencontainers.image.revision=$CMH_REVISION
USER 1000:1000
WORKDIR /data
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/opt/cmh/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=3)"]
ENTRYPOINT ["/opt/cmh/venv/bin/python", "-m", "cloud_memory_hub"]
CMD ["serve"]
