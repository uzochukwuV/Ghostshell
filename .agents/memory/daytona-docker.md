---
name: Docker inside a Daytona sandbox
description: Running Docker and pulling images inside a Daytona sandbox, and exposing container ports through preview links.
---

Docker works inside the sandbox, but only as root. The unprivileged `daytona`
user has an empty capability set (`CapEff: 0000000000000000`) and cannot run a
daemon, so `sudo -n` is required. The sandbox grants passwordless sudo to root,
where capabilities are full (`000001ffffffffff`).

Verified in a live sandbox: `apt-get install docker.io` succeeds, `dockerd`
initializes with the overlay2 driver and the docker0 bridge, and pulls from
Docker Hub work. `docker pull`, `docker run`, `docker build`, and `-p` port
publishing all work; a published container port is reachable from the sandbox
host and can be exposed externally with `create_signed_preview_url(port)`.

The daemon must be started detached or it dies with the session. Use
`subprocess.Popen(["sudo","-n","dockerd"], start_new_session=True, stdin=DEVNULL)`
with stdout to a log file. A `nohup ... &` inside an `sh -lc` string does not
survive; only a session-detached Popen does.

`dockerd` logs `Not using native diff for overlay2 ... running in a user
namespace`, which only degrades image build performance. The accounting is per
sandbox: ~3 GB on `/` total, so prune images between runs.

**Why:** An earlier measurement of the non-root user's capabilities was wrongly
read as "Docker is impossible here". The capability set must be checked as the
user that will actually run the daemon.

**How to apply:** Enable Docker only where a sandbox is meant to run containers.
It needs internal `sudo` and unbounded image pulls, which is a different trust
boundary from the code-runner path the app uses today, so treat it as an
opt-in capability rather than enabling it for every sandbox.