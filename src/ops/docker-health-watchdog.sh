#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${WATCHDOG_SERVICE_NAME:?WATCHDOG_SERVICE_NAME is required}"
CHECK_INTERVAL_SECONDS="${WATCHDOG_INTERVAL_SECONDS:-60}"
COMPOSE_CMD="${DOCKER_COMPOSE_CMD:-docker compose}"

while true; do
  container_id="$(${COMPOSE_CMD} ps -q "${SERVICE_NAME}" 2>/dev/null | head -n 1 || true)"

  if [[ -z "${container_id}" ]]; then
    echo "[watchdog] service=${SERVICE_NAME} container=missing action=up"
    ${COMPOSE_CMD} up -d "${SERVICE_NAME}" || true
    sleep "${CHECK_INTERVAL_SECONDS}"
    continue
  fi

  health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container_id}" 2>/dev/null || echo unknown)"
  echo "[watchdog] service=${SERVICE_NAME} container=${container_id} health=${health_status}"

  if [[ "${health_status}" == "unhealthy" ]]; then
    echo "[watchdog] service=${SERVICE_NAME} action=restart"
    ${COMPOSE_CMD} restart "${SERVICE_NAME}" || true
  fi

  sleep "${CHECK_INTERVAL_SECONDS}"
done
