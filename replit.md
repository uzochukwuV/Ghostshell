# Daytona Codex Sandbox

FastAPI control plane for creating Daytona sandboxes, bootstrapping Git/Codex,
launching openvscode-server, and sending headless coding prompts through an
OpenAI-compatible gateway.

## Run & Operate

- `PYTHONPATH=../.. python -m uvicorn fastapi_app.main:app --host 0.0.0.0 --port $PORT` — run the FastAPI server
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required secrets: `DAYTONA_API_KEY` and `OPENROUTER_KEY`
- Optional non-secret env: `CODEX_BASE_URL` and `CODEX_MODEL`

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: FastAPI + Uvicorn
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `fastapi_app/main.py` — FastAPI routes, Daytona lifecycle manager, Codex bootstrap, and embedded browser console
- `requirements.txt` — Python runtime dependencies
- `artifacts/api-server/.replit-artifact/artifact.toml` — preview routing and the FastAPI workflow

## Architecture decisions

- Daytona is created lazily on the first sandbox request, so opening the health check does not create billable external resources.
- The server passes the gateway secret to the sandbox only for a Codex run; it is never returned in API responses or browser code.
- Codex uses a custom OpenRouter provider with Responses API over HTTP streaming and WebSockets disabled for compatibility.

## Product

- Open `/api/` for the browser console.
- `POST /api/sandbox` creates the isolated Daytona runtime.
- `POST /api/sandbox/bootstrap` installs/checks Git and Codex.
- `POST /api/sandbox/ide` installs and starts openvscode-server on the first free port.
- `POST /api/sandbox/code` runs bounded Python snippets using Daytona's secure code runner.
- `POST /api/sandbox/prompt` sends a headless Codex task and returns its logs and exit code.
- `GET /api/sandbox/logs` returns recent sanitized run logs.
- The `/api/` console embeds the Daytona IDE beside the lightweight file editor
  and Codex chat.

## User preferences

- Keep provider credentials in Replit Secrets; never hardcode them.

## Gotchas

- The current Codex CLI requires custom providers to use the Responses wire API; gateways must support `/v1/responses`.
- A 429 from OpenRouter is surfaced as a failed Codex run instead of being retried by the FastAPI service.
- The IDE is intentionally launched lazily; installing openvscode-server downloads
  the latest Linux x64 release into the sandbox home directory.
- The Daytona agent process already listens on port 2280 inside the sandbox, so a
  server started there fails to bind and the preview link returns the agent's 404.
  The IDE scans from 2280 for the first port it can actually bind.
- Preview links need the signed variant for iframes: the plain link requires an
  `X-Daytona-Preview-Token` header browsers cannot set.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
