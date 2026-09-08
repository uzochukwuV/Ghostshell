---
name: API artifact preview routing
description: Preview-path behavior for the FastAPI API artifact and browser-owned assets.
---

API artifacts are served through the `/api` preview path rather than the service root. Browser-owned assets such as favicons should use an explicit `/api/...` URL, and preview checks should target the artifact path instead of `/`.

**Why:** The platform health/probe request may still hit the service root and return 404 even while the `/api/` preview is healthy; relying on browser default asset resolution creates misleading 404 console noise.

**How to apply:** Keep the app route and asset links scoped to `/api`, and verify the preview at the artifact's configured preview path.