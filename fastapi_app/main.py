from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("daytona-codex")

DEFAULT_TOKEN_ROUTER_URL = "https://api.tokenrouter.com/v1"
DEFAULT_CODEX_MODEL = "z-ai/glm-5.3-free"


class PromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    timeout_seconds: int = Field(default=600, ge=10, le=1_800)


class CodeRunRequest(BaseModel):
    code: str = Field(min_length=1, max_length=20_000)


@dataclass
class RunLog:
    kind: str
    text: str
    exit_code: int | None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class DaytonaSandboxManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.daytona: Any | None = None
        self.sandbox: Any | None = None
        self.sandbox_id: str | None = None
        self.bootstrap_complete = False
        self.logs: list[RunLog] = []

    def _settings(self) -> dict[str, str]:
        api_key = os.getenv("DAYTONA_API_KEY")
        if not api_key:
            raise RuntimeError("DAYTONA_API_KEY is not configured")
        return {
            "token_router_url": os.getenv(
                "TOKEN_ROUTER_BASE_URL", DEFAULT_TOKEN_ROUTER_URL
            ),
            "model": os.getenv("CODEX_MODEL", DEFAULT_CODEX_MODEL),
            "token_router_key": os.getenv("TOKEN_ROUTER_API_KEY", ""),
        }

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(
                os.getenv("DAYTONA_API_KEY") and os.getenv("TOKEN_ROUTER_API_KEY")
            ),
            "sandbox_created": self.sandbox is not None,
            "sandbox_id": self.sandbox_id,
            "bootstrap_complete": self.bootstrap_complete,
            "model": os.getenv("CODEX_MODEL", DEFAULT_CODEX_MODEL),
            "token_router_base_url": os.getenv(
                "TOKEN_ROUTER_BASE_URL", DEFAULT_TOKEN_ROUTER_URL
            ),
        }

    def _record(self, kind: str, text: str, exit_code: int | None) -> dict[str, Any]:
        safe_text = text.strip()
        self.logs.append(RunLog(kind=kind, text=safe_text, exit_code=exit_code))
        self.logs = self.logs[-100:]
        return {
            "kind": kind,
            "result": safe_text,
            "exit_code": exit_code,
            "created_at": self.logs[-1].created_at,
        }

    def create(self) -> dict[str, Any]:
        with self._lock:
            if self.sandbox is not None:
                return self.status()

            try:
                from daytona import Daytona, DaytonaConfig
            except ImportError as exc:
                raise RuntimeError(
                    "The Daytona Python SDK is not installed"
                ) from exc

            api_key = os.getenv("DAYTONA_API_KEY")
            if not api_key:
                raise RuntimeError("DAYTONA_API_KEY is not configured")

            # This is intentionally the same client initialization shape as the
            # Daytona quickstart, while keeping the key in Replit Secrets.
            config = DaytonaConfig(api_key=api_key)
            self.daytona = Daytona(config)
            self.sandbox = self.daytona.create()
            self.sandbox_id = str(
                getattr(self.sandbox, "id", None)
                or getattr(self.sandbox, "sandbox_id", None)
                or "created"
            )
            self._record("system", "Daytona sandbox created.", 0)
            return self.status()

    def _code_run(self, code: str, kind: str) -> dict[str, Any]:
        if self.sandbox is None:
            self.create()
        assert self.sandbox is not None
        response = self.sandbox.process.code_run(code)
        exit_code = getattr(response, "exit_code", None)
        result = str(getattr(response, "result", ""))
        return self._record(kind, result, exit_code)

    def bootstrap(self) -> dict[str, Any]:
        with self._lock:
            if self.bootstrap_complete:
                return {
                    "status": self.status(),
                    "log": self._record("bootstrap", "Already bootstrapped.", 0),
                }

            settings = self._settings()
            model_toml = json.dumps(settings["model"])
            base_url_toml = json.dumps(settings["token_router_url"])
            bootstrap_code = r"""
import json
import os
import shutil
import subprocess

def run(command):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        return {
            "command": " ".join(command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-5000:],
            "stderr": completed.stderr[-5000:],
        }
    except Exception as error:
        return {"command": " ".join(command), "exit_code": 1, "stderr": str(error)}

results = []
if not shutil.which("git"):
    results.append(run(["sh", "-lc", "apt-get update -y && apt-get install -y git"]))
else:
    results.append(run(["git", "--version"]))

if not shutil.which("node") or not shutil.which("npm"):
    results.append(run(["sh", "-lc", "apt-get update -y && apt-get install -y nodejs npm"]))
else:
    results.append(run(["node", "--version"]))
    results.append(run(["npm", "--version"]))

results.append(run(["npm", "install", "--global", "@openai/codex"]))
results.append(run(["git", "--version"]))
results.append(run(["codex", "--version"]))

config_dir = os.path.expanduser("~/.codex")
os.makedirs(config_dir, exist_ok=True)
with open(os.path.join(config_dir, "config.toml"), "w", encoding="utf-8") as config_file:
    config_file.write("model = __MODEL_TOML__\n")
    config_file.write('model_provider = "tokenrouter"\n')
    config_file.write('approval_policy = "never"\n')
    config_file.write('sandbox_mode = "workspace-write"\n')
    config_file.write("\n[model_providers.tokenrouter]\n")
    config_file.write('name = "Token Router"\n')
    config_file.write("base_url = __BASE_URL_TOML__\n")
    config_file.write('env_key = "OPENAI_API_KEY"\n')
    config_file.write('wire_api = "responses"\n')

print(json.dumps({"steps": results, "codex_home": config_dir}))
""".replace("__MODEL_TOML__", model_toml).replace(
                "__BASE_URL_TOML__", base_url_toml
            )
            result = self._code_run(bootstrap_code, "bootstrap")
            if result["exit_code"] == 0:
                self.bootstrap_complete = True
            return {"status": self.status(), "log": result}

    def run_code(self, code: str) -> dict[str, Any]:
        with self._lock:
            return self._code_run(code, "code")

    def prompt(self, prompt: str, timeout_seconds: int) -> dict[str, Any]:
        with self._lock:
            if not self.bootstrap_complete:
                bootstrap_result = self.bootstrap()
                if not self.bootstrap_complete:
                    return {
                        "status": self.status(),
                        "bootstrap": bootstrap_result,
                        "run": self._record(
                            "codex",
                            "Codex bootstrap failed; prompt was not executed.",
                            1,
                        ),
                    }

            settings = self._settings()
            prompt_json = json.dumps(prompt)
            base_url_json = json.dumps(settings["token_router_url"])
            key_json = json.dumps(settings["token_router_key"])
            model_json = json.dumps(settings["model"])
            code = f"""
import os
import subprocess

prompt = {prompt_json}
environment = os.environ.copy()
environment["OPENAI_API_KEY"] = {key_json}
environment["OPENAI_BASE_URL"] = {base_url_json}
environment["CODEX_MODEL"] = {model_json}
command = [
    "codex", "exec",
    "--skip-git-repo-check",
    "--sandbox", "workspace-write",
    "--config", "model_provider=\"tokenrouter\"",
    "--config", "model_providers.tokenrouter.name=\"Token Router\"",
    "--config", f"model_providers.tokenrouter.base_url={base_url_json}",
    "--config", "model_providers.tokenrouter.env_key=\"OPENAI_API_KEY\"",
    "--config", "model_providers.tokenrouter.wire_api=\"responses\"",
    "--model", {model_json},
    prompt,
]
completed = subprocess.run(
    command,
    capture_output=True,
    text=True,
    env=environment,
    timeout={timeout_seconds},
    check=False,
)
print(completed.stdout, end="")
if completed.stderr:
    print(completed.stderr, end="")
raise SystemExit(completed.returncode)
"""
            return {
                "status": self.status(),
                "run": self._code_run(code, "codex"),
            }

    def recent_logs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "kind": item.kind,
                    "result": item.text,
                    "exit_code": item.exit_code,
                    "created_at": item.created_at,
                }
                for item in reversed(self.logs)
            ]


manager = DaytonaSandboxManager()
app = FastAPI(
    title="Daytona Codex Sandbox",
    version="0.1.0",
    description="Run Codex headlessly inside isolated Daytona sandboxes.",
)


@app.get("/api/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "daytona-codex", **manager.status()}


@app.get("/api/sandbox")
def sandbox_status() -> dict[str, Any]:
    return manager.status()


@app.post("/api/sandbox")
def create_sandbox() -> dict[str, Any]:
    try:
        return manager.create()
    except Exception as exc:
        logger.exception("Daytona sandbox creation failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/sandbox/bootstrap")
def bootstrap_sandbox() -> dict[str, Any]:
    try:
        return manager.bootstrap()
    except Exception as exc:
        logger.exception("Sandbox bootstrap failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/sandbox/code")
def run_code(request: CodeRunRequest) -> dict[str, Any]:
    try:
        return {"status": manager.status(), "run": manager.run_code(request.code)}
    except Exception as exc:
        logger.exception("Sandbox code run failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/sandbox/prompt")
def prompt_codex(request: PromptRequest) -> dict[str, Any]:
    try:
        return manager.prompt(request.prompt, request.timeout_seconds)
    except Exception as exc:
        logger.exception("Codex prompt failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sandbox/logs")
def sandbox_logs() -> dict[str, Any]:
    return {"logs": manager.recent_logs()}


@app.get("/api/")
def web_console() -> HTMLResponse:
    return HTMLResponse(CONSOLE_HTML)


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Daytona Codex Sandbox</title>
  <style>
    :root { color-scheme: dark; --bg:#101114; --panel:#191b20; --panel2:#22252c; --text:#f4f1eb; --muted:#9b9da5; --line:#31343c; --accent:#e6ff75; --danger:#ff7b7b; }
    * { box-sizing:border-box; } body { margin:0; background:radial-gradient(circle at 70% -15%, #353c28 0, var(--bg) 42%); color:var(--text); font:15px/1.5 Inter, ui-sans-serif, system-ui, sans-serif; }
    main { max-width:1180px; margin:0 auto; padding:36px 22px 70px; } header { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:30px; }
    h1 { font-size:clamp(28px,4vw,48px); line-height:1.03; letter-spacing:-.05em; margin:4px 0 10px; max-width:680px; } h2 { font-size:16px; margin:0 0 14px; } p { color:var(--muted); margin:0; }
    .eyebrow { color:var(--accent); text-transform:uppercase; font-size:11px; letter-spacing:.16em; font-weight:700; } .badge { border:1px solid var(--line); border-radius:999px; color:var(--muted); padding:7px 11px; white-space:nowrap; font-size:12px; }
    .grid { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr); gap:16px; } .panel { background:rgba(25,27,32,.86); border:1px solid var(--line); border-radius:18px; padding:20px; box-shadow:0 20px 70px rgba(0,0,0,.18); } .wide { grid-column:1/-1; }
    label { color:var(--muted); display:block; font-size:12px; margin:0 0 7px; } textarea, input { width:100%; background:#0d0e11; border:1px solid var(--line); border-radius:11px; color:var(--text); padding:12px; font:inherit; outline:none; } textarea:focus, input:focus { border-color:var(--accent); } textarea { min-height:154px; resize:vertical; }
    .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; } .actions { margin-top:13px; display:flex; gap:9px; flex-wrap:wrap; } button { border:1px solid var(--line); background:var(--panel2); color:var(--text); border-radius:10px; padding:10px 13px; font-weight:700; cursor:pointer; } button:hover { border-color:var(--accent); } button.primary { background:var(--accent); color:#171a10; border-color:var(--accent); } button:disabled { cursor:wait; opacity:.55; }
    .status-list { display:grid; gap:9px; } .status-item { display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid var(--line); padding-bottom:9px; } .status-item:last-child { border:0; padding:0; } .value { color:var(--accent); text-align:right; overflow-wrap:anywhere; } .value.off { color:var(--danger); }
    pre { background:#0a0b0d; border:1px solid var(--line); border-radius:12px; padding:14px; min-height:90px; max-height:400px; overflow:auto; white-space:pre-wrap; word-break:break-word; color:#d4d7d0; font:12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; } .log { margin-top:10px; } .log-head { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.1em; }
    .hint { font-size:12px; margin-top:10px; } @media (max-width:760px) { main { padding:24px 14px 50px; } header { display:block; } .badge { display:inline-block; margin-top:18px; } .grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div><div class="eyebrow">Isolated build environment</div><h1>Talk to Codex inside Daytona.</h1><p>A small control room for creating a sandbox, checking its toolchain, and sending headless coding prompts through your configured router.</p></div>
      <div class="badge" id="connection-badge">Not connected</div>
    </header>
    <div class="grid">
      <section class="panel"><h2>Sandbox control</h2><p>Create the isolated runtime, then install Git and Codex once.</p><div class="actions"><button class="primary" id="create">Create sandbox</button><button id="bootstrap">Install Git + Codex</button><button id="refresh">Refresh status</button></div><div class="log"><div class="log-head">Latest result</div><pre id="control-output">No sandbox activity yet.</pre></div></section>
      <section class="panel"><h2>Runtime status</h2><div class="status-list" id="status"><div class="status-item"><span>Loading</span><span class="value">…</span></div></div></section>
      <section class="panel"><h2>Run Python in the sandbox</h2><label for="code">Python source</label><textarea id="code">print("Hello World from code!")</textarea><div class="actions"><button class="primary" id="run-code">Run code</button></div><div class="log"><div class="log-head">Sandbox output</div><pre id="code-output">Your result will appear here.</pre></div></section>
      <section class="panel"><h2>Prompt Codex</h2><label for="prompt">Task</label><textarea id="prompt" placeholder="Example: inspect the repository and add a health check endpoint. Explain the files you changed."></textarea><div class="row"><div style="flex:1;min-width:150px"><label for="timeout">Timeout (seconds)</label><input id="timeout" type="number" min="10" max="1800" value="600" /></div><div class="actions" style="margin-top:20px"><button class="primary" id="send-prompt">Send prompt</button></div></div><div class="log"><div class="log-head">Codex logs</div><pre id="prompt-output">Codex output will appear here.</pre></div></section>
      <section class="panel wide"><h2>Recent logs</h2><pre id="logs-output">No logs yet.</pre></section>
    </div>
  </main>
  <script>
    const base = location.pathname.startsWith("/api") ? "/api" : "";
    const $ = (id) => document.getElementById(id);
    const show = (id, value) => { $(id).textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); };
    async function call(path, options = {}) {
      const response = await fetch(base + path, { headers: {"Content-Type":"application/json"}, ...options });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
      return data;
    }
    function renderStatus(data) {
      const rows = [["Configured", data.configured ? "yes" : "no"], ["Sandbox", data.sandbox_created ? "created" : "not created"], ["Git + Codex", data.bootstrap_complete ? "ready" : "not installed"], ["Model", data.model], ["Router", data.token_router_base_url], ["ID", data.sandbox_id || "—"]];
      $("status").innerHTML = rows.map(([key,value]) => `<div class="status-item"><span>${key}</span><span class="value ${value === "no" ? "off" : ""}">${value}</span></div>`).join("");
      $("connection-badge").textContent = data.sandbox_created ? "Sandbox online" : (data.configured ? "Ready to connect" : "Secrets missing");
    }
    async function refresh() { try { const data = await call("/sandbox"); renderStatus(data); const logs = await call("/sandbox/logs"); show("logs-output", logs.logs.length ? logs.logs.map(x => `[${x.created_at}] ${x.kind} (${x.exit_code})\\n${x.result}`).join("\\n\\n") : "No logs yet."); } catch (error) { show("control-output", error.message); } }
    async function action(button, fn) { button.disabled = true; try { const data = await fn(); renderStatus(data.status || data); show("control-output", data.log || data); await refresh(); } catch (error) { show("control-output", error.message); } finally { button.disabled = false; } }
    $("create").onclick = () => action($("create"), () => call("/sandbox", {method:"POST", body:"{}"}));
    $("bootstrap").onclick = () => action($("bootstrap"), () => call("/sandbox/bootstrap", {method:"POST", body:"{}"}));
    $("refresh").onclick = refresh;
    $("run-code").onclick = () => action($("run-code"), async () => { const data = await call("/sandbox/code", {method:"POST", body:JSON.stringify({code:$("code").value})}); show("code-output", data.run); return data; });
    $("send-prompt").onclick = () => action($("send-prompt"), async () => { const data = await call("/sandbox/prompt", {method:"POST", body:JSON.stringify({prompt:$("prompt").value, timeout_seconds:Number($("timeout").value)})}); show("prompt-output", data.run || data); return data; });
    refresh();
  </script>
</body>
</html>"""