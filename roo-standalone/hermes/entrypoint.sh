#!/bin/sh
set -eu
# The lifecycle controller pre-owns the task mounts. No setup wizard, gateway,
# host profile sync, package installation or s6 root bootstrap runs here.
exec /opt/hermes/.venv/bin/python /opt/roo-hermes/acp_entry.py "$@"
