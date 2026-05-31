# Apple Shortcuts Surface API

Milestone 4 adds one static bearer token per assistant for API-style surface
routes. Use it for Apple Shortcuts or similar clients that cannot use the
owner browser Basic Auth flow.

Create a token from the admin API:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  -X POST \
  "https://email-assistant.example.com/admin/assistants/$ASSISTANT_ID/surface-token"
```

The response includes the token once:

```json
{
  "assistant_id": "budget-bot",
  "token_id": "st-...",
  "token": "st_..."
}
```

Send Shortcut requests only to API routes under the assistant surface:

```bash
curl -sS \
  -H "Authorization: Bearer $SURFACE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount":14.5,"merchant":"Pret","category":"Lunch"}' \
  "https://email-assistant.example.com/surfaces/$ASSISTANT_ID/api/capture-expense"
```

Bearer tokens are deliberately limited to `/surfaces/{assistant_id}/api/...`.
Browser pages and `/_action/run` continue to use admin Basic Auth.

Revoke the current active token:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  -X DELETE \
  "https://email-assistant.example.com/admin/assistants/$ASSISTANT_ID/surface-token"
```
