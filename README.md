# Daytona Codex Sandbox

This project provides a FastAPI control plane and a cloud-coder browser workspace
for running Codex headlessly inside an isolated Daytona sandbox.

## Configuration

Copy `.env.example` into your deployment's secret store; never commit populated
secret files. `DAYTONA_API_KEY` is required for the coding sandbox. Model
providers use the generic OpenAI-compatible variables below:

- `CODEX_API_KEY` — preferred provider key; it takes precedence over legacy keys.
- `CODEX_PROVIDER` — config identifier such as `nebius` or `openrouter`.
- `CODEX_PROVIDER_NAME` — display/provider name written to Codex config.
- `CODEX_BASE_URL` — provider endpoint URL.
- `CODEX_MODEL` — model ID that you have verified supports Codex tool use.

OpenRouter remains the default for compatibility (`OPENROUTER_KEY` and
`TOKEN_ROUTER_API_KEY` are accepted fallbacks). To use a Nebius Serverless
Endpoint, set `CODEX_PROVIDER=nebius`, set `CODEX_API_KEY`, and copy the
OpenAI-compatible endpoint URL and supported model ID from the Nebius console.
The API status deliberately reports configuration state rather than any key.

See [the Nebius cloud-coding architecture](docs/NEBIUS_CLOUD_CODING.md) for the
recommended split between the long-running API, isolated coding executor,
Serverless Endpoints, Serverless Jobs, secrets, custom domains, and Git App
workflow.

## Use

Open the `/api/` preview route to use the browser console. The JSON API is:

- `GET /api/healthz` — configuration and runtime status
- `POST /api/sandbox` — create a Daytona sandbox
- `POST /api/sandbox/bootstrap` — check/install Git and Codex
- `POST /api/sandbox/ide` — install and start openvscode-server, auto-selecting a free port
- `POST /api/sandbox/code` with `{"code":"print('hello')"}`
- `POST /api/sandbox/prompt` with `{"prompt":"...","timeout_seconds":600}`
- `GET /api/sandbox/logs` — recent run logs
- `POST /api/workspace/repository` with `{"repo_url":"https://github.com/org/repo","branch":"main"}`
- `GET /api/workspace/tree` — files in the cloned repository
- `GET /api/workspace/file?path=README.md` — read a workspace file
- `PUT /api/workspace/file` with `{"path":"README.md","content":"..."}` — save a workspace file

The workspace accepts public HTTPS Git repositories. Repository credentials are
not accepted in URLs, and private repository authentication is not implemented.
Codex prompts are scoped to the cloned repository when one is configured.
The VS Code tab embeds a signed Daytona preview URL, which authenticates the
iframe without requiring a request header the browser cannot set. The sandbox
agent already holds port 2280, so the IDE picks the first free port from 2280
upward and reports the URL for that port. Preview links expire after an hour;
launching VS Code again issues a fresh one.

The service is intentionally a single-sandbox development MVP. Add
authentication, per-user sandbox ownership, quotas, and durable job storage
before exposing arbitrary code execution to untrusted public users.