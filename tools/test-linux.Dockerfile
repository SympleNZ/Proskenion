# The image tools/test-linux.sh runs the backend suite in: the same shape as
# the GitHub Actions runner the CI "checks" job uses — Ubuntu 24.04, uv, a
# uv-managed CPython 3.13, and an unprivileged user with uid 1001 (the
# runner's), so ownership and permission checks behave as they do there and
# as they do on the appliance, not as they do for root or on Windows.

ARG UV_VERSION=0.9.9
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ubuntu:24.04
RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        ca-certificates git openssl gzip tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
# ubuntu:24.04 ships an "ubuntu" user at uid 1000; the runner is 1001.
RUN useradd --uid 1001 --create-home --shell /bin/bash runner
