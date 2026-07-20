# Self-sync deployment: a worked example

This is a **worked example** (a test case) of wiring the shim self-sync for a
real deployment. It is intentionally **not** in the shipped
`docker-compose.yml` — the network name and key mapping are specific to one
machine. Copy the pieces that match your setup.

## What self-sync needs

1. `OFFICECLI_MCP_OWUI_URL` — how the officecli-mcp container reaches OpenWebUI
   (e.g. `http://open-webui:8080` on a shared docker network).
2. `OFFICECLI_MCP_OWUI_API_KEY` — an OpenWebUI **admin** API key
   (Settings > Account). Keep it secret.
3. **Network reachability** — the container must resolve and reach the
   OpenWebUI service name. The default compose network is isolated; if
   OpenWebUI runs in a different compose project, join its network.

## Example: env wiring via a gitignored `.env`

Put the admin key in a `.env` next to your compose file (compose loads `.env`
automatically; the repo's `.gitignore` already excludes it):

```
OWUI_ADMIN_KEY=sk-your-admin-key
```

Map it into the container:

```yaml
    environment:
      OFFICECLI_MCP_OWUI_URL: "http://open-webui:8080"
      OFFICECLI_MCP_OWUI_API_KEY: "${OWUI_ADMIN_KEY:-}"
```

## Example: joining OpenWebUI's network

If OpenWebUI runs in another compose project (its network is e.g.
`open-webui-nogpu_default` — find yours with `docker network ls`), attach the
service to it as an **external** network:

```yaml
services:
  officecli-mcp:
    # ...
    networks:
      - default
      - open-webui-nogpu_default

networks:
  open-webui-nogpu_default:
    external: true
```

Symptoms when this is missing: the boot log shows
`httpx.ConnectError: [Errno -3] Temporary failure in name resolution` from
`shim_sync` — the container can't resolve `open-webui`.

## Verify

Watch the container log on start:

- `GET .../api/v1/tools/id/officecli_file 200` + `up to date` — already current.
- `... POST /api/v1/tools/create 200` + `created OpenWebUI tool` — created.
- `shim self-sync skipped: ... not set` — env not wired (manual paste mode).
- A `WARNING ... shim self-sync failed` — sync failed; the server keeps running
  (the tool in OpenWebUI is just stale).

Note: a 4xx/5xx from the Tools API is now logged as a failure (never as
"created"/"updated") — if you see a success log, it really succeeded.
