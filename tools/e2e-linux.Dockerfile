# The image tools/e2e-linux.sh runs the Playwright suite in: Microsoft's own
# Playwright image, which ships the exact browser build its Playwright npm
# package release was tested against — the version has to match
# web/package.json's @playwright/test exactly, or a browser this version of
# Playwright never shipped (or a protocol change it does not expect) makes the
# result meaningless. "-noble" is Ubuntu 24.04, the same base ubuntu-24.04
# pins in .github/workflows/ci.yml and tools/test-linux.Dockerfile already
# uses; bumping PLAYWRIGHT_VERSION is a deliberate edit here alongside
# web/package.json's @playwright/test version.
#
# uv and a uv-managed CPython 3.13 are added on top: the backend the browser
# drives is Python, and Playwright's own image has neither. The unprivileged
# "runner" user matches tools/test-linux.Dockerfile's uid 1001 (the GitHub
# Actions runner's own), so ownership and permission checks behave as they do
# there and on the appliance, not as they do for root — root could write
# /data even without the platform-detection fix this proves.

ARG PLAYWRIGHT_VERSION=v1.63.0-noble
ARG UV_VERSION=0.9.9

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM mcr.microsoft.com/playwright:${PLAYWRIGHT_VERSION}
COPY --from=uv /uv /uvx /usr/local/bin/
# The image already ships an unprivileged account at uid 1001 — "pwuser", for
# running the browsers it bundles — which is also the GitHub Actions runner's
# own uid; renamed to "runner" so both Dockerfiles read the same way rather
# than depending on which name happened to own that uid.
RUN usermod -l runner -d /home/runner -m pwuser && groupmod -n runner pwuser
# The image's own OS timezone is Etc/UTC. proskenion.core.setup._detected_timezone()
# reads /etc/timezone for the first-run wizard's Welcome step (§10.4) — on the
# real appliance that file already reads "Pacific/Auckland", baked in at image
# build time, so the wizard's "detected" value and its own later check that
# only Pacific/Auckland is accepted (§4.9) never disagree. tzdata is already
# installed (it ships with the base image); only which zone it points at
# needs to change, to match what a real appliance's own OS actually has.
RUN ln -snf /usr/share/zoneinfo/Pacific/Auckland /etc/localtime \
    && echo "Pacific/Auckland" > /etc/timezone
