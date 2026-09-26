# The systemd-as-PID-1 container the Phase 6 harness runs in.
#
# verify-in-docker.sh lints units with systemd-analyze, which tells us a file
# parses. It cannot tell us that OnFailure= fires, that StartLimitBurst=3 means
# three, that a path unit notices a request and stands down while the helper
# runs, or that a drop-in under /run changes a running service's watchdog
# window. Those are the properties the appliance depends on when nobody is
# there, so they are tested against a real systemd.
#
# The image is the appliance's own base — Debian 13 (§5.1) — with systemd and
# the two interpreters the root-side scripts use. Nothing appliance-specific is
# baked in: verify-systemd-in-docker.sh installs the units, scripts and
# libraries from the working tree, so what runs here is what the image build
# would install.

FROM debian:trixie

ENV DEBIAN_FRONTEND=noninteractive
ENV container=docker

# python3-cryptography and python3-venv are what make the update cases real
# rather than mocked: the package verifier is Ed25519 over the trust anchors,
# and the third step of an apply builds an environment from the package's own
# wheels with no network at all.
#
# dosfstools and e2fsprogs are what make the A/B slot cases real: the boot
# partition is FAT32 (§2.3) and the properties under test are properties of
# FAT, and a root slot is an ext4 filesystem written to a partition and then
# mounted. Both are loop devices here; the container is already --privileged.
#
# nginx, openssl and curl are what make the first-install case end where a
# real appliance does: the web interface served over HTTPS, not only the
# application answering on 127.0.0.1:8000. The first real install, on 24
# September 2026, got that far and no further, for three reasons none of
# which the application's own port could show.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        systemd systemd-sysv python3 python3-venv python3-pip \
        python3-cryptography zstd ca-certificates \
        dosfstools e2fsprogs \
        nginx openssl curl \
    && rm -rf /var/lib/apt/lists/* \
    # Nothing in the default boot is wanted: the harness starts what it tests.
    && find /etc/systemd/system /lib/systemd/system -path '*.wants/*' \
        -not -name '*journald*' -not -name '*systemd-tmpfiles*' -delete

# The application user, as build.sh creates it (uid/gid 900, no login).
RUN groupadd --system --gid 900 auditorium \
    && useradd --system --uid 900 --gid auditorium --no-create-home \
       --home-dir /data/app --shell /usr/sbin/nologin auditorium

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
