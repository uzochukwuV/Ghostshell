---
name: Daytona preview links and reserved ports
description: How to expose a sandbox service through a Daytona preview link that a browser iframe can load.
---

Two independent traps break the VS Code tab in a Daytona sandbox.

Port 2280 is already taken. The Daytona agent runs as pid 1 and listens on 2280
(along with 22220, 22222, 33333), so a server told to bind 2280 never binds; the
preview link for that port proxies to the agent, whose routes mostly 404 while
`/version` returns the agent's own version. Start the service on the first port
that a bind test says is free, then issue the preview link for that port.

Preview links need the signed variant for iframes. `get_preview_link()` returns a
URL plus a separate token that must be sent as an `X-Daytona-Preview-Token`
header, which an iframe cannot add. `create_signed_preview_url(port,
expires_in_seconds=...)` embeds the token in the URL so a plain browser GET works.
Expiry range is 1-86400 seconds; past the expiry the URL 401s.

**Why:** Both failures look like a routing or readiness bug, but the responses
differ in a way that identifies the cause: unauthenticated plain link gives 401
with `"code":"UNAUTHORIZED"`, while the wrong port gives an agent 404.

**How to apply:** Probe a port by actually binding it before launching, verify the
service answers HTTP locally before minting a link, keep the signed URL and its
expiry server-side, and re-issue a link once it is close to expiring rather than
handing the browser a stale one.