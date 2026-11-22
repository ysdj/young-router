# The Web bundle is shared across CPU architectures. Build its JS and editor
# page with the pnpm version named in the RN workspace.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS web
WORKDIR /src/rn
COPY rn/package.json rn/pnpm-lock.yaml rn/pnpm-workspace.yaml ./
COPY rn/apps/web/package.json apps/web/package.json
COPY rn/packages/shared/package.json packages/shared/package.json
RUN npm install --global "$(node -p 'require("./package.json").packageManager')" \
    && pnpm --filter @young-router/web install --frozen-lockfile
COPY rn/apps/web apps/web
COPY rn/packages/shared/src packages/shared/src
RUN pnpm run build:web

# Stage upstream workers and their dependencies on the target architecture.
FROM node:22-bookworm-slim AS workers
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/ scripts/
RUN python3 scripts/update_bun_runtime.py --output /stage/runtime/bin \
    && python3 scripts/update_modelprint.py --output /stage/adapters/modelprint \
    && python3 scripts/update_llm_fingerprint.py --output /stage/adapters/llm-fingerprint-detector \
    && python3 scripts/update_connect_workbuddy.py --output /stage/adapters/connect-workbuddy \
    && python3 scripts/update_connect_qoder.py --output /stage/adapters/connect-qoder \
    && python3 scripts/update_dsh_vision_router.py --output /stage/adapters/dsh-vision-router \
    && python3 scripts/update_our_free_model.py --output /stage/adapters/our-free-model \
    && cp /usr/local/bin/node /stage/runtime/bin/node \
    && cp /usr/local/LICENSE /stage/runtime/bin/NODE_LICENSE

FROM python:3.12-slim-bookworm
WORKDIR /opt/young-router
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium chromium-sandbox \
        fonts-liberation fonts-noto-cjk procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 young-router \
    && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home --shell /usr/sbin/nologin young-router \
    && install -d -m 0700 -o 1000 -g 1000 /data
COPY LITELLM_VERSION runtime/LITELLM_VERSION
RUN python -m pip install --no-cache-dir \
    "litellm[proxy]==$(cat runtime/LITELLM_VERSION)" 'fastapi==0.140.3' Pillow PyYAML

COPY LICENSE VERSION sitecustomize.py ./
COPY young_router young_router
COPY --from=web /src/rn/apps/web/dist web
COPY --from=workers /stage/adapters young_router/adapters
COPY --from=workers /stage/runtime/bin runtime/bin

ENV HOME=/data \
    YOUNG_ROUTER_HOME=/data \
    YOUNG_ROUTER_HOST_PROFILE=gateway \
    YOUNG_ROUTER_PROXY_BIND=0.0.0.0 \
    LITELLM_CONFIG_FILE=/data/config.yaml \
    LITELLM_RUNTIME_CONFIG=/data/.litellm-runtime/served.yaml \
    LITELLM_PORT=12390 \
    LITELLM_NUM_WORKERS=2 \
    YOUNG_ROUTER_BROWSER=chromium \
    PYTHONPATH=/opt/young-router \
    PATH=/opt/young-router/runtime/bin:$PATH
USER 1000:1000
WORKDIR /data
EXPOSE 12390
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ["LITELLM_PORT"] + "/health/liveliness", timeout=3).close()'
ENTRYPOINT ["python", "-m", "young_router.container"]
