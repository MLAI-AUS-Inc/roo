# Registry manifest inspected 2026-10-05; exact upstream v0.21.5 source:
# f97608f178d1ffeca59860195ab7da295f7c8e5f. A digest pins both platform images.
FROM nousresearch/hermes-agent:v2026.9.24@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7

USER root
RUN mkdir -p /state /workspace /opt/roo-hermes \
    && chown 10000:10000 /state /workspace
COPY --chmod=0555 hermes/entrypoint.sh /opt/roo-hermes/entrypoint.sh
COPY --chmod=0444 hermes/acp_entry.py /opt/roo-hermes/acp_entry.py
ENV HOME=/state HERMES_HOME=/state HERMES_WRITE_SAFE_ROOT=/workspace \
    HERMES_ACP_SKIP_CONFIGURED_MCP=1 HERMES_DISABLE_LAZY_INSTALLS=1 \
    PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/tmp/hermes-cache \
    XDG_RUNTIME_DIR=/tmp/hermes-runtime
USER 10000:10000
WORKDIR /workspace
# The launcher supplies --init, a read-only root, task-only mounts and an internal
# network. Browser tools are opt-in per task; the base includes headless Chromium.
ENTRYPOINT ["/opt/roo-hermes/entrypoint.sh"]
CMD []
