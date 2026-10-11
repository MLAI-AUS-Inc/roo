# Isolated Hermes task image

This image is an ACP executable, not a second Slack bot. Existing Roo services
own events, identity, schedules, delivery and action approval. The controller
must launch a fresh container and state directory for every task attempt, keep
its own credentials outside the container, and destroy the container before
exporting artifacts. A new task never loads another member's Hermes session.

The upstream source is `v2026.9.24` / `0.21.5`, commit
`f97608f178d1ffeca59860195ab7da295f7c8e5f`. The Dockerfile pins the verified
multiarch registry manifest. The standard image has a root s6 bootstrap; this
derived image deliberately uses its installed Python directly as UID 10000,
with an external init and pre-owned task mounts. No dashboard, setup wizard,
gateway, lazy installer, skill synchronization or native cron starts.

`acp_entry.py` adds the missing HTTP MCP capability advertisement in that exact
release. Its HTTP registration path already exists. Version, image revision
and server-source hash must match before the patch is applied; upgrades require
reviewing/removing the shim, repinning and repeating protocol tests.
The same pinned shim forwards the immutable task iteration limit to `AIAgent`:
the tagged ACP adapter otherwise omits it. It also disables persistent memory
and background self-review for task workers. The coordinator supplies authorized
memory as task input. Provider proxy limits and the outer deadline remain the
enforced spending/execution boundaries.

## Build and verify

From `roo-standalone`, authorized operators can build the image and perform the
offline import/provenance check:

```sh
docker compose -f docker-compose.hermes.yml --profile hermes-image build hermes-image
docker compose -f docker-compose.hermes.yml --profile hermes-image run --rm hermes-image
```

These commands were not run while authoring the implementation. The check does
not authenticate a provider or execute an agent task. The build downloads the
pinned public image; run it only in an authorized build environment.

The lifecycle launcher must use `--init --read-only --cap-drop ALL`,
`--security-opt no-new-privileges`, PID/CPU/memory limits, and an internal network
whose only approved outbound paths are the task broker/model proxy. It must
mount exactly the attempt's workspace at `/workspace`, state at `/state`, and
the generated `/state/config.yaml` read-only. Run the controller/file writer as
UID/GID 10000 so private mode-0600 configuration is readable by the task. Mount
bounded `/tmp` tmpfs; keep it executable until Chromium's actual pinned build
has been tested with a stricter setting. Never mount Docker/SSH agent sockets,
host home directories, backend source/config, or other task directories.

`HermesRunner` uses ACP v1 JSON lines: initialize, new session, prompt, streaming
updates, permission replies, and cancel. It rejects unadvertised HTTP support,
unknown response IDs, repeated permission IDs, wrong-session output and bounded
output/deadline violations. Only exact task-bound reviewed command fingerprints
can receive one-use approval. This is not a sandbox: ordinary tools can run
without an ACP approval callback, so filesystem/network policy and broker
authorization remain necessary.

Terminating a Docker CLI does not prove its container stopped. The controller
must stop/kill/remove the named task container in `finally`, including timeout,
disconnect, cancellation and normal completion. Then export only immutable
snapshots from `/workspace/artifacts`; the collector rejects symlinks, hardlinks,
special files, oversized trees and raw task credentials. Container/host volume
quotas are also required: output caps do not cap files written during execution.

## Browser and authentication

The pinned base includes headless Chromium/Playwright. The integrated model proxy
is text-only: navigation returns accessibility-tree text, and screenshot vision,
image analysis, browser vault, arbitrary CDP and generated browser execution
tools are excluded. Canvas-only/visual tasks are outside this first worker. `browser_enabled=True`
adds a pinned custom toolset of text/DOM browser tools; `browser.backend: off` disables Browser Use's
generated-code driver, not the browser toolset. Personal-profile reuse and
private-URL local fallback remain disabled. Browser traffic still needs an
enforced destination-aware egress path; environment proxy variables alone are
not enforcement. In the pinned source, `allow_private_urls: false` does not
block private destinations for a local browser; network enforcement is mandatory.
No authenticated browser profile or takeover service is enabled by default.
The optional browser sidecar supplies a bounded clean profile, TLS+operator
authentication and an exclusive expiring CDP/noVNC gate. It remains disconnected
from the task worker until controller handoff integration and host smoke checks
pass; see [operations](OPERATIONS.md). Durable authenticated profiles require
separate per-authority storage and independent cookie revocation. A broker grant does not constrain a cookie's
authority on its website.

Model access uses the task-scoped token for the fixed model proxy; the upstream
API key stays in the broker. Only the fixed task MCP server is supplied through
`session/new`. ACP and subprocess environment never receive Roo's backend,
Slack, administrative or deployment credentials. The broker must enforce task
expiry, cancellation, resource/action scope and usage budgets at every call.

## Verified sources

- [Tagged release](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.24)
- [Tagged Dockerfile](https://github.com/NousResearch/hermes-agent/blob/v2026.9.24/Dockerfile)
- [Tagged ACP server](https://github.com/NousResearch/hermes-agent/blob/v2026.9.24/acp_adapter/server.py)
- [Tagged ACP permission bridge](https://github.com/NousResearch/hermes-agent/blob/v2026.9.24/acp_adapter/permissions.py)
- [ACP v1 session contract](https://agentclientprotocol.com/protocol/v1/session-setup)
- [ACP v1 permission contract](https://agentclientprotocol.com/protocol/v1/tool-calls)

## Deployment boundaries

The [operations guide](OPERATIONS.md) covers the internal-network backend proxy,
restricted public-web CONNECT proxy, root-owned XFS quota helper installation,
optional operator browser, explicit controller contracts and required Linux
acceptance checks. The default feature flags remain off.
