# Ghostshell

FastAPI control plane that drives a browser workspace and runs Codex headlessly
inside an isolated Daytona sandbox.

## Layout

- `fastapi_app/main.py` — the whole control plane: `DaytonaSandboxManager` plus the
  HTTP routes and the dashboard.
- `.env` — holds `DAYTONA_API_KEY`, `OPENROUTER_KEY`, and `ENABLE_DOCKER`.

## Running

```sh
set -a && . ./.env && set +a
.venv/bin/python -m uvicorn fastapi_app.main:app --host 0.0.0.0 --port 12000
```

Docker support inside the sandbox is gated by `ENABLE_DOCKER`; leave it unset to
keep every container route disabled.

Long-running work must be started in the background and polled. A foreground
command is killed at 90% of the runtime idle limit, so something like a Codex
prompt has to be sent with `curl ... > /tmp/out.json` under `setsid nohup` and
then polled. A client-side timeout does not cancel the server-side work.

`DaytonaSandboxManager.prompt` holds `_lock` only for its bootstrap check. The
Codex run itself happens outside the lock, because holding it for the minutes a
run takes wedges every other route — `healthz` (lock-free) keeps answering while
`/api/sandbox/containers` hangs, which is the tell. Do not move a long call back
under the lock.

## Daytona behaviour worth remembering

These are all confirmed against a live sandbox, not assumed.

- **Total disk is capped (30 GiB) across every sandbox in the account.** Creating a
  new sandbox per app restart exhausts it and fails with `Total disk limit
  exceeded`. The manager therefore persists its sandbox id to `.sandbox-state` and
  reattaches on `create()`; `POST /api/sandbox/attach` does the same explicitly.
- **Egress is allowlisted by SNI.** GitHub, PyPI, and OpenRouter return `200`;
  `example.com`, Google, AWS, and `srs.midnight.network` are reset (`000`). An
  image that fetches resources at startup will exit inside the sandbox even though
  the same image works on the host.
- **`get_preview_link()` is not browser-usable.** Standard preview links need an
  `X-Daytona-Preview-Token` header, which an `iframe` cannot send. Use
  `create_signed_preview_url(port, expires_in_seconds)` — the token is embedded in
  the URL, so it is safe in an `iframe`. Treat the URL as a secret and never log it.
- **Codex's own bubblewrap sandbox cannot start inside Daytona**: it fails with
  `bwrap: loopback: Failed RTMNewADDR: Operation not permitted` (no
  `CAP_NET_ADMIN`), which blocks *every* command, not just Docker. The app passes
  `--sandbox "$CODEX_SANDBOX"`, defaulting to `danger-full-access`, because
  Daytona's isolation is the sandbox of record. Approval checking stays on.

## Free OpenRouter models

`z-ai/glm-5.2:free` does **not** advertise `tools` in `supported_parameters`, so
Codex fails with `No endpoints found that support tool use`. Filter models by
`tools in supported_parameters` before setting `CODEX_MODEL`. Working free
choices include `nex-agi/nex-n2.5-pro:free` and
`nvidia/nemotron-3.5-lightning:free`; `google/gemma-4-31b-it:free` rate-limits,
and `thinkingmachines/inkling:free` is 403 outside a listed agentic harness.

## Midnight proof server

The image is `midnightntwrk/proof-server:8.1.0`. It runs its default command
(`midnight-proof-server --port $PORT` with `PORT=6300`), so do not override it —
the image entrypoint is a shell, so an appended command would be misparsed.

It downloads proving keys from `srs.midnight.network`, which the sandbox cannot
reach, so the keys must be pre-seeded. They land under `/.cache/midnight/zk-params`
inside the container, and the cache directory root is what gets mounted:

```sh
docker run -d --name proof-server -p 6300:6300 -e PORT=6300 \
  -v zkparams:/.cache midnightntwrk/proof-server:8.1.0
```

The volume holds `midnight/zk-params/...`, so mounting it at `/.cache` puts the
keys where the server looks. Mounting at `/zk` or at
`/.cache/midnight/zk-params` fails and the container exits `1` with an
`srs.midnight.network` error. `POST /api/sandbox/containers` accepts a `volumes`
map for exactly this. Verified `{"status":"ok"}` on `/health` and `8.1.0` on
`/version`, both in-sandbox and through the signed preview URL.