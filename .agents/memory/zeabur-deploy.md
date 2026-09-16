# Zeabur deployment

## Status: DEPLOYED

- URL: https://ghostshell.zeabur.app (dashboard at `/api/`)
- Project `ghostshell` = `6aaabdb7905b4aaea95dc53e`
- Environment `production` = `6aaabdb71d7bf7f6aa52dc77`
- Service `ghostshell` = `6aaabde4183d8ffbc6da28c8`
- Runs on dedicated server `6aaa48a55cc5986c39fa5126` (region
  `server-6aaa48a55cc5986c39fa5126`)

Service env vars: `DAYTONA_API_KEY`, `OPENROUTER_KEY`, `ENABLE_DOCKER=false`.
Zeabur also injects its own `PASSWORD` and `PORT=${WEB_PORT}` (8080); the app
reads `PORT` at the shell level, so no app change was needed.

`ENABLE_DOCKER=false` is deliberate: the Zeabur container has no Docker socket,
so enabling it would only produce failures. Docker routes stay available for
local/Daytona runs.

Verified live: `/api/healthz` -> `configured: true`, model
`z-ai/glm-5.2:free`; `/api/` -> 200 (18959 bytes); `/` -> 404 by design.

## How the blocker was cleared

The server was rented but ZeaburOS was never installed. Fixed by calling
`installK3s(serverID, operationID, sshRootPassword)` with the server's SSH
password, then `reconcileServer`. `hasK3s` went false -> true and
provisioningStatus `PROVISIONING` -> `READY` in about 30s.

`installK3s` needs the SSH password inline, so it was staged in a mode-600
tempfile and passed via `curl --data @file` with `jq --rawfile`, keeping it out
of both argv and shell history, then shredded. Passwords containing shell
metacharacters survive this route; they do not survive naive interpolation.

## Gotchas that cost time

- **Deploy must run after a token restore.** The CLI silently emptied
  `~/.config/zeabur/cli.yaml` and fell back to browser login, which fails
  headlessly (`xdg-open`). `deploy -i=false` then exits 0 with *no output* and
  creates nothing. Always check the project's `services` list after a deploy
  that printed nothing. Fix: rewrite `cli.yaml` as `token: <ZEABUR>` (mode 600).
- **Generated domain**: `addDomain` wants the bare label (`ghostshell`), not
  the FQDN; passing `ghostshell.zeabur.app` returns `DOMAIN_UNAVAILABLE`.
  `checkDomainAvailable` is a **mutation**, not a query.
- **Set env vars before relying on them**, or restart after: variables added
  post-deploy do not reach the running container. `configured` was `false`
  until `service restart`.
- Region codes for `createProject` are `server-<serverID>`; the `regions` list
  values (`fra1`, `tpe0`) are rejected.
- `zeabur project ls` errors `NOT_FOUND: No such server` for this account; use
  the GraphQL API.
- `service exec` with nested quotes mangles env probes; prefer a `python -c`
  or single-quoted heredoc payload.

## Context: why the original attempt failed

Shared clusters are deprecated (docs:
https://zeabur.com/docs/en-US/server/shared-cluster). Every project must run on
a rented Server with ZeaburOS (k3s) installed. The server existed but was a
plain Ubuntu box: `createProject(region: "fra1")` was rejected, and
`reconcileServer` returned `kubernetes API unreachable: dial tcp
<server-ip>:6443: connect: connection refused` with `hasK3s: false`.

If a server ever looks offline again (`hasK3s: false`, port 6443 closed while
22 is open), re-run `installK3s` + `reconcileServer` as above; do not re-rent.

## CLI gotchas

- CLI token lives in `~/.config/zeabur/cli.yaml`; the `ZEABUR` env var also
  works against `https://api.zeabur.com/graphql` with a Bearer token.
- `zeabur project create` requires `--region`, and the valid values are not
  discoverable from `--help` or binary strings.
- Interactive prompts can be driven with
  `script -qec "<cmd>" /dev/null < <(printf '\r')`.
- `zeabur profile info -i=false` is a quick way to confirm auth is working.

## Deployment artifacts (verified)

- `Dockerfile` — python:3.13-slim, installs `requirements.txt`, copies
  `fastapi_app`, `CMD uvicorn ... --port ${PORT:-12000}`.
- `.dockerignore` — excludes `.venv`, `.git`, `.env`, `.sandbox-state`, and
  the Node monorepo (`artifacts/`, `lib/`, `scripts/`, pnpm files).
- `requirements.txt` — pinned (daytona 0.214.0, fastapi 0.141.1,
  pydantic-settings 2.15.0, uvicorn 0.53.0).

## Redeploying

    cd /workspace/project/Ghostshell
    npx --yes zeabur@latest deploy --create --name ghostshell \
      --project-id 6aaabdb7905b4aaea95dc53e -i=false --json

Confirm the output contains a `service_id`; an empty response means the CLI
lost auth (see above).
