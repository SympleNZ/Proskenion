# The soak rehearsal's container (P7-T9, D3): the appliance's base, Debian 13,
# with the application installed into a virtual environment the way a package
# installs it on the CM5 — its locked runtime dependencies and nothing else —
# and the harness beside it, as the bundle `python -m tests.soak bundle` makes.
#
# Built by tests/soak/rehearse-in-docker.sh from a context of exactly the files
# it needs; see that script.

FROM debian:trixie

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        python3 python3-venv python3-pip tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml uv.lock README.md /src/
COPY proskenion /src/proskenion
# The locked runtime set, as build_package.sh wheels it: `uv export` reads
# uv.lock, so the rehearsal runs the versions the appliance runs.
RUN python3 -m venv /opt/auditorium/venv \
    && /opt/auditorium/venv/bin/pip install -q uv \
    && /opt/auditorium/venv/bin/uv export --frozen --no-dev --no-emit-project \
        --no-hashes -o /tmp/requirements.txt \
    && /opt/auditorium/venv/bin/pip install -q -r /tmp/requirements.txt \
    && /opt/auditorium/venv/bin/pip install -q --no-deps /src \
    && /opt/auditorium/venv/bin/pip uninstall -q -y uv

COPY tests/__init__.py /opt/soak/tests/__init__.py
COPY tests/stubs /opt/soak/tests/stubs
COPY tests/soak /opt/soak/tests/soak

ENV PYTHONPATH=/opt/soak TZ=Pacific/Auckland PYTHONUNBUFFERED=1
WORKDIR /opt/soak
ENTRYPOINT ["/opt/auditorium/venv/bin/python", "-m", "tests.soak"]
