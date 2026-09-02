# Daytona Codex Sandbox

This project provides a FastAPI control plane and a small browser console for
running Codex headlessly inside an isolated Daytona sandbox.

## Configuration

Store these values as Replit Secrets:

- `DAYTONA_API_KEY`
- `OPENROUTER_KEY`

The defaults are already configured for:

- Gateway: `https://openrouter.ai/api/v1/`
- Model: `z-ai/glm-5.2:free`

You can override the gateway with `CODEX_BASE_URL` and the model with
`CODEX_MODEL`.

## Use

Open the `/api/` preview route to use the browser console. The JSON API is:

- `GET /api/healthz` — configuration and runtime status
- `POST /api/sandbox` — create a Daytona sandbox
- `POST /api/sandbox/bootstrap` — check/install Git and Codex
- `POST /api/sandbox/code` with `{"code":"print('hello')"}`
- `POST /api/sandbox/prompt` with `{"prompt":"...","timeout_seconds":600}`
- `GET /api/sandbox/logs` — recent run logs

The service is intentionally a single-sandbox development MVP. Add
authentication, per-user sandbox ownership, quotas, and durable job storage
before exposing arbitrary code execution to untrusted public users.