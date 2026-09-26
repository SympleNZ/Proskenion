# A stand-in CM5 for rehearsing tests/soak/cm5.sh (P7-T9): systemd as PID 1,
# the application installed at /opt/auditorium/venv (from the soak rehearsal
# image, tests/soak/rehearsal.Dockerfile), the `auditorium` user as build.sh
# creates it (uid/gid 900) and an `admin` user to run sudo as.
#
# Built and driven by tests/soak/rehearse-cm5-in-docker.sh.

FROM proskenion-soak-rehearsal

ENV container=docker
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends systemd systemd-sysv curl \
    && rm -rf /var/lib/apt/lists/* \
    && find /etc/systemd/system /lib/systemd/system -path '*.wants/*' \
        -not -name '*journald*' -not -name '*systemd-tmpfiles*' -delete \
    && groupadd --system --gid 900 auditorium \
    && useradd --system --uid 900 --gid auditorium --no-create-home \
        --home-dir /data/app --shell /usr/sbin/nologin auditorium \
    && useradd --create-home --shell /bin/bash admin

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT []
CMD ["/sbin/init"]
