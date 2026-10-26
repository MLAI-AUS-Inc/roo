# Container infrastructure

The Dockerfile, Compose files, and NGINX configuration live here. The build
context is `roo-standalone/`, so Dockerfile COPY paths stay relative to the application.
Environment
files, skills, and local data remain in `roo-standalone/`. Compose project names and named
volumes are preserved so existing local stacks retain their data.

From the repository root, start the local Postman stack:

```bash
docker network inspect roo-admin-gateway >/dev/null 2>&1 || docker network create roo-admin-gateway
docker compose --env-file roo-standalone/.env -f infra/docker-compose.yml -f infra/docker-compose.postman.yml up -d --build roo nginx-postman
```

See [Postman setup](../postman/README.md) for credentials and database prerequisites.

`docker-compose.yml` defines Public Roo and production NGINX. Admin, bridge,
payment-reminder, timesheet, and studio-report files define separate projects.
Start those explicitly with their own environment configuration. The Postman
and timesheet-command files are overrides combined with the public file.

For commands run from `roo-standalone/`, use `--env-file .env -f ../infra/<compose-file>`.
Explicit `--env-file` keeps Compose variable interpolation tied to the app's
environment rather than the infrastructure directory.

Build the application image directly from the repository root:

```bash
docker build -f infra/Dockerfile -t roo roo-standalone
```
