# Test Roo locally with Postman

## Start Roo

From the repository root:

```bash
cd src
docker network inspect roo-admin-gateway >/dev/null 2>&1 || docker network create roo-admin-gateway
docker compose --env-file .env -f ../infra/docker-compose.yml -f ../infra/docker-compose.postman.yml up -d --build roo nginx-postman
```

Configure `src/.env` first: use a development model key (such as
`OPENAI_API_KEY`) and a random `INTERNAL_MENTION_API_KEY`. Keep keys out of
the collection and version control. The local database must have the coworking
v3 schema; running its migration requires explicit approval. The schema was
prepared during this checkout's local setup, but a fresh Docker volume needs
its own setup.

The Postman override connects to the backend at
`http://host.docker.internal:8001`. Start the local backend for requests that
need its data. Use development Slack credentials and identities for
Slack-dependent skills. Background automations are disabled by this override.

## Import and configure

1. In Postman, choose **Import** and select
   [Roo-Local.postman_collection.json](Roo-Local.postman_collection.json).
2. Open the collection's **Variables** tab.
3. Set `mention_api_key` locally to `INTERNAL_MENTION_API_KEY` from `src/.env`.
4. Keep `roo_base_url` as `http://127.0.0.1:8088` and `backend_base_url` as
   `http://127.0.0.1:8001`.
5. Send **Roo readiness**, then **Agent — greeting**. Both should return 200.
6. Set `prompt` and send **Agent — custom prompt** to try your own request.

`slack_user_id` defaults to `U_TEST` for a basic greeting. Replace it with a
development Slack identity for identity-dependent requests; add `channel_id`
and `thread_ts` to a request when that feature needs Slack context.

Agent requests use bearer authentication automatically and set
`post_reply: false`, returning the reply to Postman. Skills can still perform
external actions, so use development services and harmless prompts.

## Troubleshoot and stop

- Connection refused: check that the containers are running and port 8088 is free.
- 401: the collection key does not match `src/.env`.
- 404 on `/api/mention`: check that the key is set and you are using port 8088.
- 502/504: inspect Roo's startup or request logs.
- Backend readiness 503: inspect its reported dependencies or pending migrations.

Run these from `src/`:

```bash
docker compose --env-file .env -f ../infra/docker-compose.yml -f ../infra/docker-compose.postman.yml ps
docker compose --env-file .env -f ../infra/docker-compose.yml -f ../infra/docker-compose.postman.yml logs --tail=100 roo nginx-postman
# Restart after changing src/.env:
docker compose --env-file .env -f ../infra/docker-compose.yml -f ../infra/docker-compose.postman.yml up -d roo nginx-postman
# Stop only the local agent and its Postman proxy; retain database volumes:
docker compose --env-file .env -f ../infra/docker-compose.yml -f ../infra/docker-compose.postman.yml stop roo nginx-postman
```
