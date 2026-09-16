from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shlex
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

logger = logging.getLogger("daytona-codex")

DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/"
DEFAULT_CODEX_MODEL = "z-ai/glm-5.2:free"
DEFAULT_CODEX_PROVIDER = "openrouter"
PROVIDER_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")

# Codex runs its own bubblewrap sandbox by default, which cannot start inside
# Daytona (it needs CAP_NET_ADMIN to configure the loopback device). Permission
# checking stays on, but Daytona's own isolation is the sandbox of record.
DEFAULT_CODEX_SANDBOX = "danger-full-access"

# The Daytona agent process itself listens on IDE_PORT_DEFAULT inside the sandbox,
# so the VS Code server probes forward from there for a port it can actually bind.
IDE_PORT_DEFAULT = 2280
IDE_PORT_SCAN_RANGE = 25
IDE_READY_TIMEOUT_SECONDS = 120
IDE_PREVIEW_TTL_SECONDS = 3600

# Docker is opt-in: it needs root inside the sandbox, so it changes the trust
# boundary of the runtime the browser console hands to Codex.
DOCKER_ENABLED_VALUES = {"1", "true", "yes", "on"}
CONTAINER_PREVIEW_TTL_SECONDS = 3600


class PromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    timeout_seconds: int = Field(default=600, ge=10, le=1_800)


class CodeRunRequest(BaseModel):
    code: str = Field(min_length=1, max_length=20_000)


class RepoRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=2_000)
    branch: str | None = Field(default=None, max_length=200)
    replace_existing: bool = False


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=300_000)


class ContainerRequest(BaseModel):
    image: str = Field(min_length=1, max_length=300)
    command: str | None = Field(default=None, max_length=2_000)
    name: str = Field(default="app", min_length=1, max_length=63)
    ports: list[int] = Field(default_factory=list, max_length=8)
    env: dict[str, str] = Field(default_factory=dict)
    volumes: dict[str, str] = Field(default_factory=dict)


class ContainerStopRequest(BaseModel):
    name: str = Field(default="app", min_length=1, max_length=63)


class AttachRequest(BaseModel):
    sandbox_id: str | None = Field(default=None, max_length=64)


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
        self.repository: dict[str, Any] | None = None
        self.ide: dict[str, Any] | None = None
        self._ide_preview: dict[str, Any] | None = None
        self.docker: dict[str, Any] | None = None
        self._container_previews: dict[str, dict[str, Any]] = {}
        self.logs: list[RunLog] = []
        self._restore_sandbox_id()

    def _state_file(self) -> str:
        return os.getenv("SANDBOX_STATE_FILE", os.path.join(os.getcwd(), ".sandbox-state"))

    def _restore_sandbox_id(self) -> None:
        """Reloads the last sandbox id so a restart reattaches instead of leaking one."""
        try:
            with open(self._state_file(), encoding="utf-8") as handle:
                sandbox_id = handle.read().strip()
        except OSError:
            return
        if sandbox_id:
            self.sandbox_id = sandbox_id

    def _persist_sandbox_id(self) -> None:
        if not self.sandbox_id:
            return
        try:
            with open(self._state_file(), "w", encoding="utf-8") as handle:
                handle.write(self.sandbox_id)
        except OSError:
            logger.warning("Could not persist the sandbox id to %s", self._state_file())

    @staticmethod
    def _docker_enabled() -> bool:
        return os.getenv("ENABLE_DOCKER", "").strip().lower() in DOCKER_ENABLED_VALUES

    def _settings(self) -> dict[str, str]:
        api_key = os.getenv("DAYTONA_API_KEY")
        if not api_key:
            raise RuntimeError("DAYTONA_API_KEY is not configured")
        provider = os.getenv("CODEX_PROVIDER", DEFAULT_CODEX_PROVIDER).strip().lower()
        if not PROVIDER_ID_PATTERN.fullmatch(provider):
            raise RuntimeError("CODEX_PROVIDER must be a simple provider identifier")
        provider_key = (
            os.getenv("CODEX_API_KEY")
            or os.getenv("OPENROUTER_KEY")
            or os.getenv("TOKEN_ROUTER_API_KEY", "")
        )
        if not provider_key:
            raise RuntimeError("CODEX_API_KEY is not configured")
        return {
            "provider": provider,
            "provider_name": os.getenv("CODEX_PROVIDER_NAME", provider).strip() or provider,
            "provider_url": os.getenv("CODEX_BASE_URL", DEFAULT_OPENROUTER_URL),
            "model": os.getenv("CODEX_MODEL", DEFAULT_CODEX_MODEL),
            "codex_sandbox": os.getenv("CODEX_SANDBOX", DEFAULT_CODEX_SANDBOX),
            "provider_key": provider_key,
        }

    def status(self) -> dict[str, Any]:
        provider_key = (
            os.getenv("CODEX_API_KEY")
            or os.getenv("OPENROUTER_KEY")
            or os.getenv("TOKEN_ROUTER_API_KEY")
        )
        return {
            "configured": bool(os.getenv("DAYTONA_API_KEY") and provider_key),
            "sandbox_created": self.sandbox is not None,
            "sandbox_id": self.sandbox_id,
            "bootstrap_complete": self.bootstrap_complete,
            "provider": os.getenv("CODEX_PROVIDER", DEFAULT_CODEX_PROVIDER),
            "model": os.getenv("CODEX_MODEL", DEFAULT_CODEX_MODEL),
            "provider_base_url": os.getenv("CODEX_BASE_URL", DEFAULT_OPENROUTER_URL),
            "repository": self.repository,
            "ide": self._ide_update(),
            "docker": self.docker,
        }

    def _ide_update(self) -> dict[str, Any]:
        """Builds the IDE state, carrying a signed preview URL forward only while it is valid.

        The token is embedded in the preview URL, so it is safe for the browser to
        fetch directly, but it stays server-side until it is certain to be usable.
        """
        if self.ide is None:
            ide: dict[str, Any] = {"running": False}
        else:
            ide = dict(self.ide)

        preview = self._ide_preview
        fresh = bool(
            preview
            and preview.get("url")
            and preview.get("expires_at")
            and datetime.now(timezone.utc)
            + timedelta(seconds=60)
            < datetime.fromisoformat(preview["expires_at"])
        )
        if fresh:
            ide["url"] = preview["url"]
            ide["expires_at"] = preview["expires_at"]
            ide["expires_in_seconds"] = max(
                1,
                int(
                    (
                        datetime.fromisoformat(preview["expires_at"])
                        - datetime.now(timezone.utc)
                    ).total_seconds()
                ),
            )
        else:
            if self.ide is not None:
                ide["url"] = None
                ide["unavailable_reason"] = (
                    "The VS Code preview link expired. Launch VS Code again for a "
                    "fresh link."
                )
        return ide

    @staticmethod
    def _validate_repo_url(repo_url: str) -> str:
        parsed = urlparse(repo_url.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Only public HTTPS repository URLs are supported; credentials, "
                "query strings, and fragments are not allowed."
            )
        if not parsed.path.strip("/"):
            raise ValueError("Repository URL must include a repository path.")
        return repo_url.strip()

    @staticmethod
    def _validate_branch(branch: str | None) -> str | None:
        if branch is None:
            return None
        value = branch.strip()
        if (
            not value
            or len(value) > 200
            or value.startswith("/")
            or value.endswith("/")
            or ".." in value
            or value.endswith(".")
            or "@{" in value
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", value)
        ):
            raise ValueError("Branch must be a valid Git branch name.")
        return value

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
            # Reattach to the persisted sandbox first: Daytona caps total disk, so
            # creating a fresh sandbox per restart exhausts the quota.
            if self.sandbox_id:
                try:
                    return self.attach(self.sandbox_id)
                except Exception:
                    logger.warning(
                        "Could not reattach to sandbox %s; creating a new one.",
                        self.sandbox_id,
                        exc_info=True,
                    )
                    self.sandbox_id = None
            client = self._client()
            self.sandbox = client.create()
            self.sandbox_id = str(
                getattr(self.sandbox, "id", None)
                or getattr(self.sandbox, "sandbox_id", None)
                or "created"
            )
            self._persist_sandbox_id()
            self._record("system", "Daytona sandbox created.", 0)
            return self.status()

    def _client(self) -> Any:
        """Builds the Daytona client, reusing it when the manager already has one."""
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
        if self.daytona is None:
            self.daytona = Daytona(DaytonaConfig(api_key=api_key))
        return self.daytona

    def attach(self, sandbox_id: str | None = None) -> dict[str, Any]:
        """Reattaches to an existing sandbox, which avoids leaking a new one per restart."""
        with self._lock:
            target = (sandbox_id or self.sandbox_id or "").strip()
            if not target:
                raise ValueError("No sandbox id is known; create a sandbox first.")
            if not re.fullmatch(r"[A-Za-z0-9-]{16,64}", target):
                raise ValueError("Sandbox id must be a UUID-like identifier.")
            self.sandbox = self._client().get(target)
            self.sandbox_id = target
            self._persist_sandbox_id()
            self._record("system", f"Attached to Daytona sandbox {target}.", 0)
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
            provider_toml = json.dumps(settings["provider"])
            provider_name_toml = json.dumps(settings["provider_name"])
            base_url_toml = json.dumps(settings["provider_url"])
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
    config_file.write('model = __MODEL_TOML__\n')
    config_file.write('model_provider = ' + __PROVIDER_TOML__ + '\n')
    config_file.write('approval_policy = "never"\n')
    config_file.write('sandbox_mode = "workspace-write"\n')
    config_file.write("\n[model_providers." + __PROVIDER_TOML__ + "]\n")
    config_file.write('name = ' + __PROVIDER_NAME_TOML__ + '\n')
    config_file.write('base_url = __BASE_URL_TOML__\n')
    config_file.write('env_key = "OPENAI_API_KEY"\n')
    config_file.write('wire_api = "responses"\n')
    config_file.write("supports_websockets = false\n")

print(json.dumps({"steps": results, "codex_home": config_dir}))
critical_exit_codes = [results[-2]["exit_code"], results[-1]["exit_code"]]
raise SystemExit(0 if all(code == 0 for code in critical_exit_codes) else 1)
""".replace("__MODEL_TOML__", model_toml).replace(
                "__PROVIDER_TOML__", provider_toml
            ).replace("__PROVIDER_NAME_TOML__", provider_name_toml).replace(
                "__BASE_URL_TOML__", base_url_toml
            )
            result = self._code_run(bootstrap_code, "bootstrap")
            if result["exit_code"] == 0:
                self.bootstrap_complete = True
            return {"status": self.status(), "log": result}

    def run_code(self, code: str) -> dict[str, Any]:
        with self._lock:
            return self._code_run(code, "code")

    def start_ide(self) -> dict[str, Any]:
        with self._lock:
            if self.sandbox is None:
                self.create()
            assert self.sandbox is not None

            home_json = json.dumps(self.sandbox.get_user_home_dir())
            folder_json = json.dumps(
                self.repository["path"] if self.repository else self.sandbox.get_user_home_dir()
            )
            ide_code = f"""
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

home = {home_json}
default_folder = {folder_json}
preferred_port = {IDE_PORT_DEFAULT}
scan_range = {IDE_PORT_SCAN_RANGE}
ready_timeout = {IDE_READY_TIMEOUT_SECONDS}
install_root = os.path.join(home, ".openvscode-server")
binary = os.path.join(install_root, "bin", "openvscode-server")
pid_file = os.path.join(home, ".openvscode-server.pid")
log_file = os.path.join(home, ".openvscode-server.log")
profile_file = os.path.join(home, ".openvscode-server.env")

def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0

def port_is_bindable(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False

def pick_port():
    for candidate in range(preferred_port, preferred_port + scan_range):
        if port_is_bindable(candidate):
            return candidate
    return None

def read_state():
    try:
        with open(pid_file, "r", encoding="utf-8") as file:
            state = json.loads(file.read().strip())
        return state if isinstance(state, dict) else {{}}
    except (OSError, ValueError):
        return {{}}

def alive(pid, port):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return bool(port) and port_in_use(int(port))

def http_code(port, path="/", timeout=3):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except Exception:
        return None

if not os.path.exists(binary):
    if not shutil.which("curl") or not shutil.which("tar"):
        installer = subprocess.run(
            ["sh", "-lc", "apt-get update -y && apt-get install -y curl ca-certificates tar"],
            capture_output=True, text=True, timeout=900, check=False,
        )
        if installer.returncode:
            print(json.dumps({{"error": installer.stderr[-4000:]}}))
            raise SystemExit(installer.returncode)
    metadata = urllib.request.urlopen(
        "https://api.github.com/repos/gitpod-io/openvscode-server/releases/latest",
        timeout=30,
    ).read()
    release = json.loads(metadata)
    assets = [
        asset["browser_download_url"]
        for asset in release.get("assets", [])
        if asset.get("name", "").endswith("linux-x64.tar.gz")
    ]
    if not assets:
        print(json.dumps({{"error": "No Linux x64 openvscode-server release was found."}}))
        raise SystemExit(1)
    archive = os.path.join(home, ".openvscode-server.tar.gz")
    completed = subprocess.run(
        ["curl", "-fsSL", assets[0], "-o", archive],
        capture_output=True, text=True, timeout=900, check=False,
    )
    if completed.returncode:
        print(json.dumps({{"error": completed.stderr[-4000:]}}))
        raise SystemExit(completed.returncode)
    extract_root = os.path.join(home, ".openvscode-extract")
    shutil.rmtree(extract_root, ignore_errors=True)
    os.makedirs(extract_root, exist_ok=True)
    completed = subprocess.run(
        ["tar", "-xzf", archive, "-C", extract_root],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if completed.returncode:
        print(json.dumps({{"error": completed.stderr[-4000:]}}))
        raise SystemExit(completed.returncode)
    extracted = [
        os.path.join(extract_root, item)
        for item in os.listdir(extract_root)
        if os.path.isdir(os.path.join(extract_root, item))
    ]
    if not extracted:
        print(json.dumps({{"error": "openvscode-server archive was empty."}}))
        raise SystemExit(1)
    shutil.rmtree(install_root, ignore_errors=True)
    shutil.move(extracted[0], install_root)
    shutil.rmtree(extract_root, ignore_errors=True)
    os.remove(archive)

state = read_state()
pid = state.get("pid")
port = state.get("port")

# Reuse a healthy server; otherwise start one on the first port the agent is not holding.
if not (alive(pid, port) and http_code(int(port)) is not None):
    port = pick_port()
    if port is None:
        print(json.dumps({{"error": "No free port was available in the sandbox for the VS Code server."}}))
        raise SystemExit(1)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    log = open(log_file, "a", encoding="utf-8")
    process = subprocess.Popen(
        [
            binary,
            "--host", "0.0.0.0",
            "--port", str(port),
            # The preview token authenticates every request, so the server itself
            # does not need its own connection token.
            "--without-connection-token",
            "--default-folder", default_folder,
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ.copy(),
    )
    pid = process.pid
    with open(pid_file, "w", encoding="utf-8") as file:
        file.write(json.dumps({{"pid": pid, "port": port}}))

    deadline = time.time() + ready_timeout
    code = http_code(port)
    while code is None and process.poll() is None and time.time() < deadline:
        time.sleep(1)
        code = http_code(port)
    if code is None:
        with open(log_file, encoding="utf-8", errors="replace") as handle:
            tail = handle.read()[-3000:]
        print(json.dumps({{
            "error": "openvscode-server did not answer on port %d." % port,
            "log_tail": tail,
        }}))
        raise SystemExit(1)

    # VS Code terminals inherit this, and code-server forces its own port when it
    # cannot rebind the one it was given.
    try:
        with open(profile_file, "w", encoding="utf-8") as handle:
            handle.write("IDE_PORT=%d\\nexport IDE_PORT\\n" % port)
    except OSError:
        pass

print(json.dumps({{
    "installed": os.path.exists(binary),
    "running": bool(pid),
    "pid": pid,
    "port": port,
    "folder": default_folder,
}}))
"""
            result = self._code_run(ide_code, "ide-start")
            try:
                details = json.loads(result["result"].splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise RuntimeError(
                    result["result"] or "VS Code server failed to start."
                ) from exc
            if result["exit_code"] != 0 or "error" in details:
                raise RuntimeError(details.get("error") or "VS Code server failed to start.")
            port = int(details["port"])
            # Reuse the current link when it still points at the running server;
            # minting a new one would orphan a live token on every launch click.
            preview = self._ide_preview
            if (
                not (
                    preview
                    and preview.get("port") == port
                    and preview.get("expires_at")
                    and datetime.now(timezone.utc) + timedelta(seconds=60)
                    < datetime.fromisoformat(preview["expires_at"])
                )
            ):
                signed = self.sandbox.create_signed_preview_url(
                    port, expires_in_seconds=IDE_PREVIEW_TTL_SECONDS
                )
                preview = {
                    "url": str(signed.url),
                    "port": port,
                    "expires_at": (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=IDE_PREVIEW_TTL_SECONDS)
                    ).isoformat(),
                }
                self._ide_preview = preview
            self.ide = {
                "provider": "openvscode-server",
                "port": port,
                "running": True,
                "folder": details.get("folder"),
            }
            return {"status": self.status(), "ide": self.status()["ide"], "run": result}

    def start_docker(self) -> dict[str, Any]:
        """Installs and starts dockerd inside the sandbox. Requires ENABLE_DOCKER."""
        with self._lock:
            if not self._docker_enabled():
                raise ValueError(
                    "Docker is disabled. Set ENABLE_DOCKER=true to expose containers "
                    "to the sandbox."
                )
            if self.sandbox is None:
                self.create()
            assert self.sandbox is not None

            code = r"""
import json
import os
import shutil
import subprocess
import time

def run(command, timeout=120, sudo=False):
    prefix = ["sudo", "-n"] if sudo else []
    return subprocess.run(prefix + command, capture_output=True, text=True,
                          timeout=timeout, check=False)

def docker_ready():
    probe = run(["docker", "info"], timeout=30)
    return probe.returncode == 0

if shutil.which("docker") is None or shutil.which("dockerd") is None:
    installer = run(["sh", "-lc",
        "apt-get update -y -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io"],
        timeout=900, sudo=True)
    if installer.returncode:
        print(json.dumps({"error": installer.stderr[-4000:] or installer.stdout[-4000:]}))
        raise SystemExit(1)

# The socket group lets the unprivileged sandbox user drive Docker without sudo,
# which keeps Codex's workspace-write sandbox usable.
user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip() or "daytona"
if not docker_ready():
    run(["pkill", "-f", "dockerd"], timeout=30, sudo=True)
    time.sleep(3)
    log = open("/tmp/dockerd.log", "a", encoding="utf-8")
    process = subprocess.Popen(
        ["sudo", "-n", "dockerd", "-G", user],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, stdin=subprocess.DEVNULL,
    )
    deadline = time.time() + 120
    while time.time() < deadline and not docker_ready():
        time.sleep(2)
    if not docker_ready():
        with open("/tmp/dockerd.log", encoding="utf-8", errors="replace") as handle:
            tail = handle.read()[-3000:]
        print(json.dumps({"error": "dockerd did not become ready.", "log_tail": tail}))
        raise SystemExit(1)

version = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
print(json.dumps({
    "installed": True,
    "running": True,
    "version": version.stdout.strip(),
    "socket_group": user,
}))
"""
            result = self._code_run(code, "docker-start")
            try:
                details = json.loads(result["result"].splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise RuntimeError(
                    result["result"] or "Docker failed to start."
                ) from exc
            if result["exit_code"] != 0 or "error" in details:
                raise RuntimeError(details.get("error") or "Docker failed to start.")
            self.docker = {
                "installed": True,
                "running": True,
                "version": details.get("version") or "",
                "socket_group": details.get("socket_group"),
            }
            return {"status": self.status(), "docker": self.docker, "run": result}

    @staticmethod
    def _validate_image(image: str) -> str:
        value = image.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?", value):
            raise ValueError("Image must be a Docker image reference.")
        return value

    def list_containers(self) -> dict[str, Any]:
        with self._lock:
            if self.sandbox is None or not (self.docker or {}).get("running"):
                return {"docker": self.docker, "containers": []}
            result = self.sandbox.process.exec(
                "docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'",
                timeout=60,
            )
            containers = []
            for line in (result.result or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    containers.append(
                        {
                            "name": parts[0],
                            "image": parts[1],
                            "status": parts[2],
                            "ports": parts[3] if len(parts) > 3 else "",
                        }
                    )
            return {"docker": self.docker, "containers": containers}

    def run_container(
        self,
        image: str,
        command: str | None,
        name: str,
        ports: list[int],
        env: dict[str, str] | None = None,
        volumes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._docker_enabled():
                raise ValueError("Docker is disabled. Set ENABLE_DOCKER=true first.")
            if not (self.docker or {}).get("running"):
                self.start_docker()
            assert self.sandbox is not None
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                raise ValueError("Container name must be a simple identifier.")
            for port in ports:
                if not 1 <= port <= 65535:
                    raise ValueError("Ports must be between 1 and 65535.")

            image_ref = self._validate_image(image)
            launch = ["docker", "run", "-d", "--name", name]
            for port in ports:
                launch += ["-p", f"{port}:{port}"]
            for key, value in (env or {}).items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError("Environment variable names must be simple identifiers.")
                launch += ["-e", f"{key}={value}"]
            for volume, target in (volumes or {}).items():
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", volume):
                    raise ValueError("Volume names must be simple identifiers.")
                if not target.startswith("/") or ".." in target:
                    raise ValueError("Volume targets must be absolute paths.")
                launch += ["-v", f"{volume}:{target}"]
            launch.append(image_ref)

            # Pull before inspecting: an absent image makes inspect fail, and the
            # container would then start with the wrong command shape.
            pull = self.sandbox.process.exec(
                f"docker pull {shlex.quote(image_ref)}", timeout=1800
            )
            if pull.exit_code != 0:
                raise RuntimeError((pull.result or "").strip() or "docker pull failed.")

            if command:
                # Match `docker run <image> <args>`: an image built with a shell
                # entrypoint (bash -c) takes the command as one argument, while a
                # plain image needs it run through a shell. Only the entrypoint is
                # fetched, and the format string is concatenated rather than
                # interpolated: an f-string would collapse the template braces.
                inspected = self.sandbox.process.exec(
                    "docker image inspect "
                    + shlex.quote(image_ref)
                    + " --format '{{json .Config.Entrypoint}}'",
                    timeout=120,
                )
                try:
                    entrypoint = json.loads((inspected.result or "").strip() or "[]")
                except ValueError:
                    entrypoint = []
                shell_entrypoint = bool(entrypoint) and str(entrypoint[-1]).strip() in {
                    "-c",
                    "-lc",
                }
                if shell_entrypoint:
                    launch.append(command)
                else:
                    launch += ["sh", "-lc", command]

            # Replace any previous container with the same name so relaunches work.
            self.sandbox.process.exec(
                f"docker rm -f {shlex.quote(name)}", timeout=120
            )
            result = self.sandbox.process.exec(
                " ".join(shlex.quote(part) for part in launch), timeout=900
            )
            if result.exit_code != 0:
                # Surface the daemon's message, including a name collision.
                detail = (result.result or "").strip()
                raise RuntimeError(detail or "docker run failed.")

            logs = self.sandbox.process.exec(
                f"docker logs --tail 40 {shlex.quote(name)} 2>&1", timeout=60
            )
            previews: dict[str, Any] = {}
            for port in ports:
                signed = self.sandbox.create_signed_preview_url(
                    port, expires_in_seconds=CONTAINER_PREVIEW_TTL_SECONDS
                )
                previews[str(port)] = {
                    "url": str(signed.url),
                    "expires_at": (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=CONTAINER_PREVIEW_TTL_SECONDS)
                    ).isoformat(),
                }
            self._container_previews[name] = previews
            details = {
                "name": name,
                "image": image,
                "command": command,
                "ports": ports,
                "previews": previews,
                "logs": (logs.result or "")[-4000:],
            }
            return {
                "status": self.status(),
                "container": details,
                "run": self._record("docker", f"Started container {name}.", 0),
            }

    def stop_container(self, name: str) -> dict[str, Any]:
        with self._lock:
            if self.sandbox is None:
                raise ValueError("No sandbox has been created yet.")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                raise ValueError("Container name must be a simple identifier.")
            result = self.sandbox.process.exec(
                f"docker rm -f {shlex.quote(name)}", timeout=180
            )
            if result.exit_code != 0:
                raise RuntimeError((result.result or "").strip() or "docker rm failed.")
            self._container_previews.pop(name, None)
            return {
                "status": self.status(),
                "run": self._record("docker", f"Stopped container {name}.", 0),
            }

    def prompt(self, prompt: str, timeout_seconds: int) -> dict[str, Any]:
        # Only the bootstrap check holds the lock: a Codex run can take
        # minutes, and holding the manager lock that long wedges every
        # other route behind this request.
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
        base_url_json = json.dumps(settings["provider_url"])
        key_json = json.dumps(settings["provider_key"])
        model_json = json.dumps(settings["model"])
        provider_json = json.dumps(settings["provider"])
        provider_name_json = json.dumps(settings["provider_name"])
        codex_sandbox_json = json.dumps(settings["codex_sandbox"])
        repo_path_json = json.dumps(
            self.repository["path"] if self.repository else ""
        )
        code = f"""
import os
import subprocess

prompt = {prompt_json}
working_directory = {repo_path_json}
environment = os.environ.copy()
environment["OPENAI_API_KEY"] = {key_json}
environment["OPENAI_BASE_URL"] = {base_url_json}
environment["CODEX_MODEL"] = {model_json}
command = [
    "codex", "exec",
    "--skip-git-repo-check",
    "--sandbox", {codex_sandbox_json},
    "--config", "model_provider=" + {provider_json},
    "--config", "model_providers." + {provider_json} + ".name=" + {provider_name_json},
    "--config", "model_providers." + {provider_json} + ".base_url=" + {base_url_json},
    "--config", "model_providers." + {provider_json} + '.env_key="OPENAI_API_KEY"',
    "--config", "model_providers." + {provider_json} + '.wire_api="responses"',
    "--config", "model_providers." + {provider_json} + ".supports_websockets=false",
    "--model", {model_json},
]
if working_directory:
    command[2:2] = ["--cd", working_directory]
command.append(prompt)
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

    def clone_repository(
        self, repo_url: str, branch: str | None, replace_existing: bool
    ) -> dict[str, Any]:
        with self._lock:
            validated_url = self._validate_repo_url(repo_url)
            validated_branch = self._validate_branch(branch)
            if self.sandbox is None:
                self.create()
            assert self.sandbox is not None
            if self.repository and not replace_existing:
                raise ValueError(
                    "A repository is already configured. Enable replace existing to clone another one."
                )

            url_json = json.dumps(validated_url)
            branch_json = json.dumps(validated_branch)
            code = f"""
import json
import os
import shutil
import subprocess

repo_url = {url_json}
branch = {branch_json}
repo_path = os.path.expanduser("~/workspace/repo")
if os.path.exists(repo_path):
    shutil.rmtree(repo_path)
os.makedirs(os.path.dirname(repo_path), exist_ok=True)
command = ["git", "clone", "--depth", "1"]
if branch:
    command.extend(["--branch", branch])
command.extend([repo_url, repo_path])
completed = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
if completed.returncode:
    print(json.dumps({{"error": (completed.stderr or completed.stdout)[-5000:]}}))
    raise SystemExit(completed.returncode)
revision = subprocess.run(
    ["git", "-C", repo_path, "rev-parse", "--short", "HEAD"],
    capture_output=True, text=True, check=False,
).stdout.strip()
branch_result = subprocess.run(
    ["git", "-C", repo_path, "branch", "--show-current"],
    capture_output=True, text=True, check=False,
).stdout.strip()
print(json.dumps({{
    "path": repo_path,
    "branch": branch_result or branch,
    "revision": revision,
    "url": repo_url,
}}))
"""
            result = self._code_run(code, "repository-clone")
            if result["exit_code"] != 0:
                raise RuntimeError(result["result"] or "Repository clone failed.")
            try:
                repository = json.loads(result["result"].splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise RuntimeError("Repository clone returned an invalid result.") from exc
            if "error" in repository:
                raise RuntimeError(repository["error"])
            self.repository = repository
            return {"status": self.status(), "run": result}

    def _require_repository(self) -> str:
        if not self.repository:
            raise ValueError("Clone a repository before using workspace files.")
        return str(self.repository["path"])

    @staticmethod
    def _validate_relative_path(path: str) -> str:
        value = path.replace("\\", "/").strip()
        normalized = posixpath.normpath(value)
        if (
            not value
            or normalized in {".", ".."}
            or normalized.startswith("../")
            or normalized.startswith("/")
            or "\x00" in value
            or normalized == ".git"
            or normalized.startswith(".git/")
        ):
            raise ValueError("Path must stay inside the cloned repository.")
        return normalized

    def file_tree(self) -> dict[str, Any]:
        with self._lock:
            repo_path = self._require_repository()
            root_json = json.dumps(repo_path)
            code = f"""
import json
import os

root = {root_json}
entries = []
for current, directories, files in os.walk(root):
    directories[:] = sorted(directory for directory in directories if directory != ".git")
    relative_dir = os.path.relpath(current, root)
    if relative_dir != ".":
        entries.append({{"path": relative_dir.replace(os.sep, "/"), "type": "directory"}})
    for filename in sorted(files):
        full_path = os.path.join(current, filename)
        relative_path = os.path.relpath(full_path, root).replace(os.sep, "/")
        try:
            size = os.path.getsize(full_path)
        except OSError:
            size = 0
        entries.append({{"path": relative_path, "type": "file", "size": size}})
        if len(entries) >= 2000:
            break
    if len(entries) >= 2000:
        break
print(json.dumps(entries))
"""
            result = self._code_run(code, "file-tree")
            if result["exit_code"] != 0:
                raise RuntimeError(result["result"] or "Could not read the repository tree.")
            try:
                return {"repository": self.repository, "entries": json.loads(result["result"])}
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Repository tree returned an invalid result.") from exc

    def read_file(self, path: str) -> dict[str, Any]:
        with self._lock:
            repo_path = self._require_repository()
            relative_path = self._validate_relative_path(path)
            path_json = json.dumps(relative_path)
            root_json = json.dumps(repo_path)
            code = f"""
import json
import os

root = {root_json}
relative_path = {path_json}
full_path = os.path.realpath(os.path.join(root, relative_path))
root_real = os.path.realpath(root)
if not (full_path == root_real or full_path.startswith(root_real + os.sep)):
    raise SystemExit("Path escapes repository")
if not os.path.isfile(full_path):
    raise SystemExit("File not found")
size = os.path.getsize(full_path)
if size > 300000:
    raise SystemExit("File is too large to open in the workspace")
with open(full_path, "r", encoding="utf-8", errors="replace") as file:
    content = file.read()
print(json.dumps({{"path": relative_path, "content": content, "size": size}}))
"""
            result = self._code_run(code, "file-read")
            if result["exit_code"] != 0:
                raise RuntimeError(result["result"] or "Could not read the file.")
            try:
                return json.loads(result["result"])
            except (ValueError, TypeError) as exc:
                raise RuntimeError("File read returned an invalid result.") from exc

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        with self._lock:
            repo_path = self._require_repository()
            relative_path = self._validate_relative_path(path)
            path_json = json.dumps(relative_path)
            content_json = json.dumps(content)
            root_json = json.dumps(repo_path)
            code = f"""
import json
import os

root = {root_json}
relative_path = {path_json}
content = {content_json}
full_path = os.path.realpath(os.path.join(root, relative_path))
root_real = os.path.realpath(root)
if not full_path.startswith(root_real + os.sep):
    raise SystemExit("Path escapes repository")
os.makedirs(os.path.dirname(full_path), exist_ok=True)
with open(full_path, "w", encoding="utf-8") as file:
    file.write(content)
print(json.dumps({{"path": relative_path, "size": len(content.encode("utf-8"))}}))
"""
            result = self._code_run(code, "file-write")
            if result["exit_code"] != 0:
                raise RuntimeError(result["result"] or "Could not write the file.")
            try:
                return {"file": json.loads(result["result"]), "run": result}
            except (ValueError, TypeError) as exc:
                raise RuntimeError("File write returned an invalid result.") from exc

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


@app.post("/api/sandbox/attach")
def attach_sandbox(request: AttachRequest) -> dict[str, Any]:
    try:
        return manager.attach(request.sandbox_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Sandbox attach failed")
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


@app.post("/api/sandbox/ide")
def start_sandbox_ide() -> dict[str, Any]:
    try:
        return manager.start_ide()
    except Exception as exc:
        logger.exception("VS Code server failed to start")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/sandbox/docker")
def start_sandbox_docker() -> dict[str, Any]:
    try:
        return manager.start_docker()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Docker failed to start")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sandbox/containers")
def list_sandbox_containers() -> dict[str, Any]:
    try:
        return manager.list_containers()
    except Exception as exc:
        logger.exception("Container listing failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/sandbox/containers")
def run_sandbox_container(request: ContainerRequest) -> dict[str, Any]:
    try:
        return manager.run_container(
            request.image,
            request.command,
            request.name,
            request.ports,
            request.env,
            request.volumes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Container start failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/sandbox/containers/stop")
def stop_sandbox_container(request: ContainerStopRequest) -> dict[str, Any]:
    try:
        return manager.stop_container(request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Container stop failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/sandbox/prompt")
def prompt_codex(request: PromptRequest) -> dict[str, Any]:
    try:
        return manager.prompt(request.prompt, request.timeout_seconds)
    except Exception as exc:
        logger.exception("Codex prompt failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/workspace/repository")
def clone_repository(request: RepoRequest) -> dict[str, Any]:
    try:
        return manager.clone_repository(
            request.repo_url, request.branch, request.replace_existing
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Repository clone failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/workspace/tree")
def workspace_tree() -> dict[str, Any]:
    try:
        return manager.file_tree()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Workspace tree failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/workspace/file")
def workspace_file(path: str) -> dict[str, Any]:
    try:
        return manager.read_file(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Workspace file read failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.put("/api/workspace/file")
def save_workspace_file(request: FileWriteRequest) -> dict[str, Any]:
    try:
        return manager.write_file(request.path, request.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Workspace file write failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sandbox/logs")
def sandbox_logs() -> dict[str, Any]:
    return {"logs": manager.recent_logs()}


@app.get("/api/")
def web_console() -> HTMLResponse:
    return HTMLResponse(CONSOLE_HTML)


@app.get("/api/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/favicon.ico")
def root_favicon() -> Response:
    return Response(status_code=204)


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="icon" href="/api/favicon.ico" />
  <title>Daytona Cloud Coder</title>
  <style>
    :root { color-scheme:dark; --bg:#0c0e11; --panel:#15181d; --panel2:#1d222a; --panel3:#111419; --line:#2a3039; --text:#eef0eb; --muted:#8d96a3; --accent:#d8f36a; --blue:#8eb8ff; --danger:#ff8585; }
    * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 Inter,ui-sans-serif,system-ui,sans-serif; overflow:hidden; }
    button,input,textarea { font:inherit; } button { border:1px solid var(--line); border-radius:8px; background:var(--panel2); color:var(--text); cursor:pointer; padding:8px 11px; font-weight:700; } button:hover { border-color:var(--accent); } button.primary { background:var(--accent); color:#12170b; border-color:var(--accent); } button:disabled { opacity:.5; cursor:wait; }
    input,textarea { width:100%; border:1px solid var(--line); border-radius:8px; background:var(--panel3); color:var(--text); outline:none; padding:9px 10px; } input:focus,textarea:focus { border-color:var(--accent); } label { display:block; color:var(--muted); font-size:11px; margin:0 0 5px; text-transform:uppercase; letter-spacing:.08em; }
    header { height:62px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--line); padding:0 20px; background:#101318; } .brand { display:flex; align-items:center; gap:11px; } .mark { width:28px; height:28px; border-radius:8px; background:var(--accent); color:#12170b; display:grid; place-items:center; font-weight:900; } .brand strong { letter-spacing:-.02em; } .brand small { color:var(--muted); margin-left:8px; }
    .top-actions { display:flex; gap:8px; align-items:center; } .pill { border:1px solid var(--line); border-radius:999px; color:var(--muted); padding:6px 10px; font-size:12px; } .pill.online { color:var(--accent); border-color:#53652c; }
    .workspace { height:calc(100vh - 62px); display:grid; grid-template-columns:260px minmax(380px,1fr) 360px; min-height:0; } .sidebar,.chat { min-height:0; background:var(--panel); } .sidebar { border-right:1px solid var(--line); overflow:auto; padding:17px 14px; } .chat { border-left:1px solid var(--line); display:flex; flex-direction:column; }
    .section-title { display:flex; justify-content:space-between; align-items:center; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.1em; margin:0 0 10px; } .section { margin-bottom:22px; } .field { margin-bottom:10px; } .inline { display:flex; gap:7px; } .inline > * { min-width:0; } .hint { color:var(--muted); font-size:11px; margin-top:8px; }
    .tree { border-top:1px solid var(--line); padding-top:8px; } .tree button { display:block; width:100%; text-align:left; border:0; background:transparent; color:#cbd0d7; padding:5px 6px; font-size:12px; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .tree button:hover { background:var(--panel2); color:var(--text); } .tree .dir { color:var(--blue); } .tree-empty { color:var(--muted); font-size:12px; padding:6px; }
    .editor { min-width:0; display:flex; flex-direction:column; background:#0f1216; } .editor-head { border-bottom:1px solid var(--line); min-height:45px; display:flex; align-items:center; gap:8px; padding:0 13px; overflow:auto; } .tabs { display:flex; align-self:stretch; gap:2px; } .tab { display:flex; align-items:center; gap:7px; padding:0 12px; color:var(--muted); border-bottom:2px solid transparent; white-space:nowrap; cursor:pointer; font-size:12px; } .tab.active { color:var(--text); border-color:var(--accent); background:#14181d; } .editor-title { color:var(--muted); font-size:12px; margin-left:auto; white-space:nowrap; } .editor-body { flex:1; min-height:0; display:flex; flex-direction:column; } #editor { flex:1; min-height:0; resize:none; border:0; border-radius:0; padding:22px; background:#0f1216; font:13px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace; tab-size:2; } .editor-footer { min-height:47px; border-top:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; padding:0 14px; color:var(--muted); font-size:11px; } .editor-footer strong { color:var(--accent); font-weight:500; }
    .chat-head { border-bottom:1px solid var(--line); padding:16px; } .chat-head h2 { margin:0 0 3px; font-size:15px; } .chat-head p { color:var(--muted); margin:0; font-size:12px; } .messages { flex:1; overflow:auto; padding:16px; display:flex; flex-direction:column; gap:12px; } .message { border:1px solid var(--line); border-radius:11px; padding:11px 12px; background:var(--panel2); font-size:13px; white-space:pre-wrap; word-break:break-word; } .message.user { background:#202b35; border-color:#334658; } .message .who { color:var(--muted); text-transform:uppercase; font-size:10px; letter-spacing:.1em; margin-bottom:5px; } .message pre { white-space:pre-wrap; color:#cdd3cb; font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; margin:10px 0 0; border-top:1px solid var(--line); padding-top:9px; max-height:260px; overflow:auto; } .chat-compose { padding:12px; border-top:1px solid var(--line); } #prompt { min-height:78px; resize:vertical; } .compose-row { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-top:8px; } .compose-row input { width:90px; } .empty { color:var(--muted); font-size:12px; text-align:center; padding:30px 10px; }
    .status-list { display:grid; gap:6px; margin-top:10px; } .status-item { display:flex; justify-content:space-between; gap:8px; font-size:11px; color:var(--muted); } .status-item span:last-child { color:var(--text); text-align:right; max-width:150px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .error { color:var(--danger); font-size:12px; margin:8px 0; }
     .view-switch { display:flex; gap:4px; margin-right:7px; } .view-switch button { padding:5px 9px; font-size:11px; } .view-switch button.active { background:var(--panel2); border-color:var(--accent); color:var(--accent); } .view-switch button:disabled { cursor:not-allowed; }
     .ide-body { flex:1; min-height:0; position:relative; background:#0b0d10; } .ide-body[hidden], .ide-body iframe[hidden] { display:none; } .ide-body iframe { width:100%; height:100%; min-height:600px; border:0; display:block; } .ide-empty { display:grid; place-items:center; height:100%; min-height:600px; color:var(--muted); padding:30px; text-align:center; } .ide-empty strong { display:block; color:var(--text); margin-bottom:6px; }
    @media (max-width:1050px) { .workspace { grid-template-columns:230px minmax(320px,1fr); } .chat { grid-column:1/-1; height:360px; border-left:0; border-top:1px solid var(--line); } body { overflow:auto; } .workspace { height:auto; min-height:calc(100vh - 62px); } .editor { min-height:600px; } } @media (max-width:650px) { header { padding:0 12px; } .brand small { display:none; } .workspace { display:block; } .sidebar,.chat,.editor { border:0; border-bottom:1px solid var(--line); } .sidebar { max-height:none; } .editor { min-height:560px; } }
  </style>
</head>
<body>
  <header>
    <div class="brand"><div class="mark">D</div><div><strong>Daytona Cloud Coder</strong><small>Codex workspace</small></div></div>
    <div class="top-actions"><span class="pill" id="connection-badge">Connecting…</span><button id="refresh">Refresh</button></div>
  </header>
  <div class="workspace">
    <aside class="sidebar">
      <div class="section">
        <div class="section-title"><span>Repository</span><span id="repo-state">Not connected</span></div>
        <div class="field"><label for="repo-url">Public HTTPS Git URL</label><input id="repo-url" placeholder="https://github.com/org/project.git" /></div>
        <div class="field"><label for="branch">Branch (optional)</label><input id="branch" placeholder="main" /></div>
        <label style="text-transform:none;letter-spacing:0;display:flex;gap:7px;align-items:center;margin:8px 0 12px"><input id="replace-repo" type="checkbox" style="width:auto" /> Replace current repository</label>
        <button class="primary" id="clone" style="width:100%">Clone repository</button>
        <div class="hint">Public HTTPS repositories only. Credentials in URLs are rejected.</div>
      </div>
      <div class="section">
        <div class="section-title"><span>Files</span><button id="reload-tree" style="padding:3px 7px;font-size:11px">Reload</button></div>
        <div class="tree" id="tree"><div class="tree-empty">Clone a repository to browse files.</div></div>
      </div>
      <div class="section">
        <div class="section-title"><span>Sandbox</span></div>
        <div class="inline"><button id="create">Create</button><button id="bootstrap">Install toolchain</button></div>
        <button id="start-ide" style="width:100%;margin-top:8px">Open VS Code</button>
        <div class="status-list" id="status"></div>
      </div>
    </aside>
    <main class="editor">
      <div class="editor-head"><div class="view-switch"><button id="view-code" class="active">Code</button><button id="view-ide" disabled>VS Code</button></div><div class="tabs" id="tabs"><div class="empty" style="padding:12px">Open a file from the explorer.</div></div><div class="editor-title" id="editor-title">No file selected</div></div>
      <div class="editor-body" id="code-view"><textarea id="editor" spellcheck="false" placeholder="Select a file to view and edit it."></textarea></div>
      <div class="ide-body" id="ide-view" hidden><div class="ide-empty" id="ide-empty"><div><strong>VS Code is not running</strong>Launch it from the Sandbox controls to open the full browser IDE.</div></div><iframe id="ide-frame" title="VS Code in Daytona" allow="clipboard-read; clipboard-write" hidden></iframe></div>
      <div class="editor-footer"><span id="file-meta">No file open</span><button class="primary" id="save" disabled>Save file</button></div>
    </main>
    <section class="chat">
      <div class="chat-head"><h2>Codex chat</h2><p>Ask Codex to inspect and change the cloned repository.</p></div>
      <div class="messages" id="messages"><div class="empty">Your Codex conversation will appear here.</div></div>
      <div class="chat-compose"><textarea id="prompt" placeholder="Ask Codex to implement a change, explain a file, or review the workspace…"></textarea><div class="compose-row"><input id="timeout" type="number" min="10" max="1800" value="600" title="Timeout seconds" /><button class="primary" id="send-prompt">Send to Codex</button></div></div>
    </section>
  </div>
  <script>
    const base = location.pathname.startsWith("/api") ? "/api" : "";
    const $ = (id) => document.getElementById(id);
    const state = { status:null, entries:[], open:[], active:null, contents:{}, ideUrl:null };
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;" }[char]));
    async function call(path, options = {}) {
      const response = await fetch(base + path, { headers: {"Content-Type":"application/json"}, ...options });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
      return data;
    }
    function renderStatus(data) {
      state.status = data;
      const repo = data.repository;
      $("repo-state").textContent = repo ? "Connected" : "Not connected";
      $("connection-badge").textContent = data.sandbox_created ? "Sandbox online" : (data.configured ? "Ready to connect" : "Secrets missing");
      $("connection-badge").className = "pill" + (data.sandbox_created ? " online" : "");
      const rows = [["Sandbox", data.sandbox_created ? "created" : "not created"], ["Toolchain", data.bootstrap_complete ? "ready" : "not installed"], ["Provider", data.provider || "configured provider"], ["Model", data.model || "—"], ["Repository", repo ? (repo.branch || "default") : "—"]];
      $("status").innerHTML = rows.map(([key,value]) => `<div class="status-item"><span>${escapeHtml(key)}</span><span title="${escapeHtml(value)}">${escapeHtml(value)}</span></div>`).join("");
      applyIde(data.ide);
      if (repo && !$("repo-url").value) { $("repo-url").value = repo.url || ""; $("branch").value = repo.branch || ""; }
    }
    function setView(view) {
      const ide = view === "ide";
      $("code-view").hidden = ide;
      $("ide-view").hidden = !ide;
      $("view-code").classList.toggle("active", !ide);
      $("view-ide").classList.toggle("active", ide);
    }
    function applyIde(ide) {
      const running = Boolean(ide && ide.running);
      const available = Boolean(running && ide.url);
      $("view-ide").disabled = !available;
      $("start-ide").textContent = running ? "Open VS Code" : "Launch VS Code";
      if (ide && !available) {
        $("ide-frame").hidden = true;
        $("ide-frame").removeAttribute("src");
        state.ideUrl = null;
        $("ide-empty").hidden = false;
        $("ide-empty").innerHTML = `<div><strong>VS Code preview is not available</strong>${escapeHtml(ide.unavailable_reason || "Launch VS Code to create a preview link.")}</div>`;
        if (running) setView("code");
      }
      if (available && ide.url !== state.ideUrl) {
        state.ideUrl = ide.url;
        $("ide-frame").src = ide.url;
        $("ide-frame").hidden = false;
        $("ide-empty").hidden = true;
      }
    }
    function renderTree() {
      if (!state.entries.length) { $("tree").innerHTML = '<div class="tree-empty">No files found.</div>'; return; }
      $("tree").innerHTML = state.entries.map((entry) => {
        const depth = entry.path.split("/").length - 1;
        const icon = entry.type === "directory" ? "▾" : "·";
        return `<button class="${entry.type === "directory" ? "dir" : ""}" data-path="${escapeHtml(entry.path)}" style="padding-left:${6 + depth * 12}px">${icon} ${escapeHtml(entry.path.split("/").pop())}</button>`;
      }).join("");
      $("tree").querySelectorAll("button").forEach((button) => { if (button.classList.contains("dir")) button.disabled = true; else button.onclick = () => openFile(button.dataset.path); });
    }
    function renderTabs() {
      $("tabs").innerHTML = state.open.length ? state.open.map((path) => `<div class="tab ${path === state.active ? "active" : ""}" data-tab="${escapeHtml(path)}">${escapeHtml(path.split("/").pop())}</div>`).join("") : '<div class="empty" style="padding:12px">Open a file from the explorer.</div>';
      $("tabs").querySelectorAll(".tab").forEach((tab) => tab.onclick = () => selectFile(tab.dataset.tab));
    }
    function selectFile(path) {
      state.active = path; $("editor").value = state.contents[path] || ""; $("editor-title").textContent = path; $("file-meta").textContent = `${path} · ${new Blob([$("editor").value]).size} bytes`; $("save").disabled = false; renderTabs();
    }
    async function openFile(path) {
      try { if (state.contents[path] === undefined) { const data = await call("/workspace/file?path=" + encodeURIComponent(path)); state.contents[path] = data.content; } if (!state.open.includes(path)) state.open.push(path); selectFile(path); }
      catch (error) { addMessage("system", error.message); }
    }
    function addMessage(who, text, log) {
      const empty = $("messages").querySelector(".empty"); if (empty) empty.remove();
      const message = document.createElement("div"); message.className = "message " + (who === "user" ? "user" : "");
      message.innerHTML = `<div class="who">${escapeHtml(who)}</div><div>${escapeHtml(text)}</div>${log ? `<pre>${escapeHtml(log)}</pre>` : ""}`;
      $("messages").appendChild(message); $("messages").scrollTop = $("messages").scrollHeight;
    }
    async function refresh() {
      try { const data = await call("/sandbox"); renderStatus(data); if (data.repository) { const tree = await call("/workspace/tree"); state.entries = tree.entries; renderTree(); } } catch (error) { addMessage("system", error.message); }
    }
    async function busy(button, task) { button.disabled = true; try { const data = await task(); renderStatus(data.status || data); await refresh(); if (data.ide) applyIde(data.ide); return data; } catch (error) { addMessage("system", error.message); } finally { button.disabled = false; } }
    $("clone").onclick = () => busy($("clone"), async () => { const data = await call("/workspace/repository", {method:"POST", body:JSON.stringify({repo_url:$("repo-url").value, branch:$("branch").value || null, replace_existing:$("replace-repo").checked})}); state.entries = (await call("/workspace/tree")).entries; renderTree(); addMessage("system", "Repository cloned into the Daytona sandbox.", data.run && data.run.result); return data; });
    $("create").onclick = () => busy($("create"), () => call("/sandbox", {method:"POST", body:"{}"}));
    $("bootstrap").onclick = () => busy($("bootstrap"), () => call("/sandbox/bootstrap", {method:"POST", body:"{}"}));
    $("start-ide").onclick = () => busy($("start-ide"), async () => { const data = await call("/sandbox/ide", {method:"POST", body:"{}"}); applyIde(data.ide); if (data.ide && data.ide.url) { setView("ide"); addMessage("system", "Open VS Code is running in the Daytona sandbox."); } else { addMessage("system", (data.ide && data.ide.unavailable_reason) || "Open VS Code is running, but no preview link was issued."); } return data; });
    $("view-code").onclick = () => setView("code");
    $("view-ide").onclick = () => { if (!$("view-ide").disabled) setView("ide"); };
    $("refresh").onclick = refresh; $("reload-tree").onclick = async () => { try { state.entries = (await call("/workspace/tree")).entries; renderTree(); } catch (error) { addMessage("system", error.message); } };
    $("editor").oninput = () => { $("file-meta").textContent = `${state.active || "No file"} · ${new Blob([$("editor").value]).size} bytes · unsaved`; };
    $("save").onclick = () => busy($("save"), async () => { const data = await call("/workspace/file", {method:"PUT", body:JSON.stringify({path:state.active, content:$("editor").value})}); state.contents[state.active] = $("editor").value; addMessage("system", `Saved ${state.active}.`, data.run && data.run.result); return {status:state.status}; });
    $("send-prompt").onclick = () => busy($("send-prompt"), async () => { const prompt = $("prompt").value.trim(); if (!prompt) throw new Error("Enter a task for Codex first."); addMessage("user", prompt); $("prompt").value = ""; const data = await call("/sandbox/prompt", {method:"POST", body:JSON.stringify({prompt, timeout_seconds:Number($("timeout").value)})}); addMessage("codex", data.run ? (data.run.exit_code === 0 ? "Codex completed the request." : "Codex returned an error.") : "Codex finished.", data.run && data.run.result); return data; });
    refresh();
  </script>
</body>
</html>"""