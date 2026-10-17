FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
# Package updates enter only through a reviewed image build. Record and require
# the resulting immutable image digest before the controller may use it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium xvfb x11vnc novnc websockify nginx ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /profile /opt/roo-browser && chown 10000:10000 /profile
COPY --chmod=0444 hermes/browser/gate.py hermes/browser/supervisor.py /opt/roo-browser/
COPY --chmod=0444 hermes/browser/nginx.conf /opt/roo-browser/nginx.conf
ENV HOME=/profile DISPLAY=:99 XDG_CACHE_HOME=/tmp/cache XDG_RUNTIME_DIR=/tmp/runtime
USER 10000:10000
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-I", "/opt/roo-browser/supervisor.py"]
