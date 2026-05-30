# Assistant Tools API

Milestone 3 exposes a minimal internal HTTP contract for assistant-owned
surfaces that need the platform to perform privileged work.

The surface is still the user-friendly frontend: dashboards, quick actions,
review screens, and presentations of assistant state. The Tools API is only
the backend bridge for platform-owned effects such as queuing a normal agent
run or logging an event.

## Environment

Assistant runs write `/workspace/.assistant/env` with:

```bash
ASSISTANT_ID='budget-bot'
ASSISTANT_TOOLS_BASE_URL='http://assistant-tools'
ASSISTANT_SURFACE_BASE_URL='https://example.com/surfaces/budget-bot'
```

If `EMAIL_AGENT_ASSISTANT_TOOLS_TOKEN` or `ASSISTANT_TOOLS_TOKEN` is configured,
the file also includes:

```bash
ASSISTANT_TOOLS_TOKEN='...'
```

Source the file before running scripts that call the Tools API:

```bash
set -a
. /workspace/.assistant/env
set +a
```

## Python Example

```python
import os

import httpx


async def queue_run():
    headers = {"X-Assistant-Id": os.environ["ASSISTANT_ID"]}
    if token := os.environ.get("ASSISTANT_TOOLS_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(base_url=os.environ["ASSISTANT_TOOLS_BASE_URL"]) as client:
        response = await client.post(
            "/v1/runs",
            headers=headers,
            json={
                "reason": "surface_capture",
                "input": {"source": "dashboard"},
                "idempotency_key": "surface-capture-123",
            },
        )
        response.raise_for_status()
        return response.json()
```

## curl Example

```bash
auth_header=()
if [ -n "${ASSISTANT_TOOLS_TOKEN:-}" ]; then
  auth_header=(-H "Authorization: Bearer $ASSISTANT_TOOLS_TOKEN")
fi

curl -sS "$ASSISTANT_TOOLS_BASE_URL/v1/runs" \
  -H "Content-Type: application/json" \
  -H "X-Assistant-Id: $ASSISTANT_ID" \
  "${auth_header[@]}" \
  -d '{"reason":"surface_capture","input":{"source":"dashboard"},"idempotency_key":"surface-capture-123"}'
```
