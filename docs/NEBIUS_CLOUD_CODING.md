# Nebius cloud-coding target architecture

This document turns Ghostshell from a single, long-lived Daytona development MVP
into a production-oriented cloud-coding service that can use Nebius for model
inference, deployment, and asynchronous work. It deliberately distinguishes
**request-serving** components from **untrusted coding execution**: do not put
arbitrary repository code in the same trust boundary as the public API.

## Recommended split

| Concern | Recommended runtime | Why |
| --- | --- | --- |
| Ghostshell FastAPI API, browser console, webhook receiver | A long-running Nebius container/Kubernetes/VM workload behind HTTPS | It owns sessions, auth, signed cookies, and short API requests. A serverless job is not a replacement for an HTTP control plane. |
| Codex prompt or build/test run | A dedicated Daytona sandbox initially; evaluate a Nebius sandbox/Token Factory runtime where it provides equivalent isolation | Repository code and agent tools are untrusted. The worker needs a per-run filesystem, CPU/memory/time budget, and explicit network policy. |
| Model inference | A Nebius Serverless Endpoint using its OpenAI-compatible endpoint URL and a model supported for tool use | The control plane remains provider-neutral while inference can be managed and scaled independently. |
| Slow, retryable work | Nebius Serverless Jobs | Good for queued repository indexing, dependency cache warming, SBOM/scanning, test/build artifacts, and post-push checks. |
| State | Managed Postgres plus object storage | Replaces in-memory manager state, `.sandbox-state`, and transient logs with auditable per-workspace records. |

A practical first release keeps Daytona as the execution backend and moves only
Ghostshell's public service and model endpoint to Nebius. A later executor
adapter can target a Nebius sandbox runtime after proving parity for filesystem,
Git, terminal streaming, network egress, port preview, cancellation, and
resource limits.

## What changed in Ghostshell

Ghostshell now accepts a generic OpenAI-compatible provider rather than writing
an `openrouter` Codex configuration unconditionally:

```sh
DAYTONA_API_KEY=...
CODEX_PROVIDER=nebius
CODEX_PROVIDER_NAME=Nebius
CODEX_BASE_URL=<copy the OpenAI-compatible endpoint URL from Nebius>
CODEX_MODEL=<a Nebius model ID verified to support tool use>
CODEX_API_KEY=<Nebius endpoint API key>
```

`CODEX_API_KEY` has precedence over the legacy `OPENROUTER_KEY` and
`TOKEN_ROUTER_API_KEY`. Ghostshell only reports whether it is configured; it
never returns the provider key in the health response. `CODEX_PROVIDER` is
restricted to a simple identifier before it is interpolated into Codex's TOML
configuration, preventing an environment value from changing unrelated config.

Validate the selected Nebius model with a real `codex exec` smoke prompt before
routing user work to it. Tool support, response API compatibility, context size,
and rate limits are deployment prerequisites—not assumptions based on a model
name.

## Deployment plan

1. **Build and publish immutable images.** Build the included Dockerfile in CI,
   attach a commit SHA and SBOM, push to a private registry, and deploy only by
   digest. Run the API as a non-root user and add health/readiness probes for
   `/api/healthz`.
2. **Put the API behind a private ingress first.** Terminate TLS at Nebius,
   enforce an identity-aware proxy/OIDC login, CSRF protection, a strict CORS
   allowlist, and per-user/org rate limits before exposing `/api/`.
3. **Use separate environments.** Give development, staging, and production
   different projects/accounts, endpoint keys, domains, database credentials,
   sandbox quotas, and Git Apps. Never use a production provider key in a
   preview deployment.
4. **Persist work.** Introduce `workspaces`, `runs`, `artifacts`, and `audit`
   tables. Submit a durable run record before dispatch; workers claim it with a
   lease, update progress, and store output in object storage. The UI should
   poll or consume SSE from the API, not connect directly to a job runtime.
5. **Make cancellation real.** A user cancel must update the run record and
   signal the active sandbox/job. Enforce wall-clock, idle, output-size, CPU,
   memory, disk, process-count, and egress limits server-side.

## Serverless Jobs workflow

Use a queue-backed dispatcher in the API:

```text
POST /runs -> DB row (queued) -> dispatcher submits Nebius Job
  -> job leases row -> creates/attaches executor -> streams logs to object storage
  -> job stores commit/artifact metadata -> API reports terminal state
```

Give every job an idempotency key (`workspace_id`, requested revision, task
hash), a bounded retry policy, a dead-letter state, and an explicit cancellation
check between clone, install, Codex, test, and publish stages. Do not place a
long Codex process in an HTTP request: jobs should be polled by the dispatcher
and their results surfaced from durable state.

## Secrets and identity

- Store `DAYTONA_API_KEY`, `CODEX_API_KEY`, Git App private keys, webhook
  secrets, database credentials, and signing keys in Nebius's managed secret
  facility (or an approved external secret manager). Inject them at runtime;
  never bake them into images, `.env`, client JavaScript, task payloads, logs,
  or workspace files.
- Prefer short-lived, scoped credentials. The API's model key should be scoped
  to inference only; each job should receive only the secrets required for its
  stage. Rotate keys and redeploy/restart consumers automatically.
- Replace raw repository URLs and unauthenticated public clone-only access with
  a GitHub/GitLab App installation flow. Store encrypted installation metadata,
  mint short-lived installation tokens inside the worker, and redact remote URLs
  and tokens in all logs.
- Sign preview URLs with a short TTL and user/workspace binding. The existing
  Daytona signed URL must be treated as a bearer secret; never store it in the
  database or return it to another user.

## Domains, previews, and Git lifecycle

Use `app.example.com` for the API/UI and `*.preview.example.com` for isolated
workspace previews. Generate a unique hostname per workspace/run, route it only
to the corresponding executor port, require the owning user session, set a short
TTL, and destroy its DNS/route with the run. Do not expose an executor's raw
provider preview URL as a permanent application domain.

For Git, record the immutable source commit at run creation, create a protected
server-side work branch such as `ghostshell/<run-id>`, and make commits with a
bot identity. The worker should push only that branch, open a pull request via
the Git provider API, and never push directly to protected branches. Require CI,
review, signed commits where available, and branch protection before merge.

## Milestones and acceptance checks

1. **Provider portability (now):** Configure Nebius through `CODEX_*`, run a
   non-destructive tool-use smoke test, and retain OpenRouter fallback.
2. **Secure API:** OIDC, workspace ownership checks, secrets injection, audit
   events, and no unauthenticated execution routes.
3. **Durable jobs:** Queue, database run state, cancellation, retries, object
   storage logs/artifacts, and worker metrics.
4. **Git collaboration:** Git App, PR-only writes, webhook-driven status, and
   per-repo policy controls.
5. **Preview/domain isolation:** Custom domains, TLS, authenticated short-lived
   previews, explicit port allowlists, teardown, and security tests.
6. **Nebius executor evaluation:** Run the same conformance suite against the
   candidate sandbox runtime before switching from Daytona.

Track cost and reliability by workspace: endpoint tokens, job runtime, sandbox
runtime, egress, storage, queue age, retries, cancellation latency, and failed
Git pushes. Enforce budgets before dispatch rather than after a costly run.
