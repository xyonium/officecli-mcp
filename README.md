# officecli-mcp

An [MCP](https://modelcontextprotocol.io/) server that wraps [OfficeCLI](https://github.com/iOfficeAI/OfficeCLI) so it can be used from **OpenWebUI** (and any streamable-HTTP MCP client). It solves OfficeCLI's core limitation for remote clients: OfficeCLI's built-in MCP mode only accepts **local file paths**, which doesn't work when the LLM client runs in a different container/pod and never holds the file bytes.

`officecli-mcp` adds a **handle-based file layer**: an HTTP upload endpoint accepts office documents and returns a `file_id`; MCP tools then operate on that `file_id`. OfficeCLI's built-in rendering returns HTML (as text) and screenshots (as base64 images) directly to the LLM, closing the _render → look → fix_ loop.

## How it works

```
OpenWebUI (pod A)                         officecli-mcp (pod B)
┌──────────────────────────────┐          ┌─────────────────────────────────┐
│ LLM ──► native MCP client    │  HTTP    │ FastMCP (streamable-HTTP)       │
│         (streamable-HTTP) ────┼──────────►  tools: create, view_html,     │
│                                │          │         view_screenshot, edit… │
│ Native Tool "officecli_upload"│          │                                 │
│   reads __files__, fetches    │  HTTP    │ HTTP /files  (upload → file_id)│
│   bytes, POSTs ───────────────┼──────────► /files/{id} (download)        │
│   returns file_id to LLM      │          │                                 │
└──────────────────────────────┘          │ officecli binary (auto-pulled) │
                                          └─────────────────────────────────┘
```

- The LLM never sees raw bytes — only a short `file_id` handle.
- Bytes move server-to-server (OpenWebUI REST → our `/files`), never through the model context.
- `officecli` is downloaded on first start (latest release for the host platform); the image stays small and decoupled from OfficeCLI version churn.

## Transport

- **Primary**: streamable-HTTP (OpenWebUI native MCP, v0.6.31+, is streamable-HTTP-only — connect directly, no mcpo needed).
- **Fallback**: stdio (wrap with [mcpo](https://github.com/open-webui/mcpo) for OpenAPI/OpenWebUI if needed).

## Status

🚧 Design phase — see [`docs/superpowers/specs/`](docs/superpowers/specs/).

## License

Apache-2.0 (same as OfficeCLI).
