---
name: Codex gateway compatibility
description: Compatibility constraints for running the current Codex CLI through an OpenAI-compatible gateway.
---

Current Codex CLI releases only support the Responses wire API for custom
providers. Gateways that do not support the Responses WebSocket transport need
`supports_websockets = false` so Codex uses HTTP streaming.

**Why:** The CLI ignored a plain base URL override until it was configured as a
custom provider, and the gateway connection failed while WebSockets were
enabled.

**How to apply:** Define a non-reserved custom provider with `base_url`,
`env_key`, and `wire_api = "responses"`. Set `supports_websockets = false`
unless the selected gateway explicitly supports the Responses WebSocket
transport.