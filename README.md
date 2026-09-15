# Daytona Codex Sandbox

This project provides a FastAPI control plane and a cloud-coder browser workspace
for running Codex headlessly inside an isolated Daytona sandbox.

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
- `POST /api/sandbox/ide` — install and start openvscode-server on Daytona port 2280
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
The VS Code tab uses Daytona's private preview link for the sandbox IDE and
opens the repository as its default folder.

The service is intentionally a single-sandbox development MVP. Add
authentication, per-user sandbox ownership, quotas, and durable job storage
before exposing arbitrary code execution to untrusted public users.