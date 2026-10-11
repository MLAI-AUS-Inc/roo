#!/bin/sh
set -eu
# This is a DNS hostname, not a URL/port/path. Reject nginx/shell injection and
# IP literals before substituting into the reviewed static routing template.
host="${ROO_HERMES_BACKEND_HOST:-}"
if ! printf '%s\n' "$host" | awk -F. '
  NR > 1 || NF < 2 || length($0) > 253 { exit 1 }
  { for (i=1;i<=NF;i++) if (length($i)>63 || $i !~ /^[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]$/ && $i !~ /^[A-Za-z0-9]$/) exit 1 }
  $NF !~ /^[A-Za-z][A-Za-z]+$/ { exit 1 }
'; then
    echo 'ROO_HERMES_BACKEND_HOST must be an explicit DNS hostname' >&2
    exit 1
fi
umask 077
envsubst '${ROO_HERMES_BACKEND_HOST}' < /opt/roo/nginx.conf.template > /tmp/roo-nginx.conf
exec nginx -c /tmp/roo-nginx.conf -g 'daemon off;'
