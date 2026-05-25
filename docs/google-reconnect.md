# Google Reconnect

How to (re)connect Ariel's Google integration — including granting new scopes such as Gmail write or Calendar write.

## When you need this

- **First-time setup**: connect Google so Ariel can read your Mail/Calendar/Drive.
- **Account identity is missing or unusable**: reconnect to request `openid + email + profile` and populate `account_subject` plus `account_email`; current connect/reconnect callbacks and the connected-row schema require usable userinfo identity.
- **Bot says "I can't send mail / create events / share files"**: the relevant write scope was never granted. Reconnect with a `capability_intent`.
- **Google revoked the refresh token** (you removed access from [https://myaccount.google.com/permissions](https://myaccount.google.com/permissions), or it expired due to inactivity): reconnect to mint a fresh refresh token.

## API surface

All Google connector flows are HTTP endpoints on the API (loopback `127.0.0.1:8000` on the VPS). There is no Discord slash command — run these from a shell on the host.

If `ARIEL_LOCAL_AUTH_REQUIRED=true` (the prod default), every request needs `Authorization: Bearer <ARIEL_LOCAL_AUTH_TOKEN>`. Source it from the production env file for the curl calls below:

```bash
set -a; . /etc/ariel/ariel.env; set +a
auth=( -H "Authorization: Bearer $ARIEL_LOCAL_AUTH_TOKEN" )
```

Then every curl in this doc becomes `curl "${auth[@]}" …`.

| Endpoint | Purpose |
|---|---|
| `POST /v1/connectors/google/start` | First-time connect (when no connector exists) |
| `POST /v1/connectors/google/reconnect` | Refresh / add scopes on an existing connector |
| `GET /v1/connectors/google` | Inspect current connector state, granted scopes, account_email |
| `GET /v1/connectors/google/callback?state=…&code=…` | OAuth callback (Google redirects here automatically) |
| `DELETE /v1/connectors/google` | Disconnect (only if you want to start over) |

Each `start` / `reconnect` returns an `authorization_url`. You open that URL in a browser, grant the requested scopes on Google's consent screen, and Google redirects to the callback which completes the flow.

## The simple recipe

`capability_intent` accepts either a single value or a **comma-separated list**.
The scope sets union together, so you can bundle every capability you want into
one consent screen. Existing grants always carry forward — reconnect never
removes scopes.

### Clear missing-scope Google smoke blockers (one consent screen)

Run this when the manual smoke ledger shows Google rows as `not_enabled`. It
asks for every write and Drive scope Ariel currently knows about, in one
browser approval. This only fixes missing grants. If a capability still returns
`provider_permission_denied` after the requested scopes are granted, investigate
Google API enablement, OAuth app or admin restrictions, and Drive/calendar ACLs
instead of repeating reconnect.

```bash
curl -s "${auth[@]}" -X POST 'http://127.0.0.1:8000/v1/connectors/google/reconnect?capability_intent=cap.calendar.propose_slots,cap.calendar.create_event,cap.email.draft,cap.email.send,cap.email.archive,cap.drive.search,cap.drive.read,cap.drive.share' | jq -r '.oauth.authorization_url'
# → open the URL in a browser, click Allow once
```

`cap.calendar.create_event` covers create/update/respond. `cap.email.archive`
covers archive/trash/labels/undo (undo also needs a prior reversible mutation
receipt). If your OAuth client in Cloud Console is missing any of the
restricted scopes the consent URL requests, Google rejects with
`invalid_scope`; see the Cloud Console section below.

### Single-intent form (still works)

```bash
# Identity scopes only (openid + email + profile)
curl -s "${auth[@]}" -X POST http://127.0.0.1:8000/v1/connectors/google/reconnect | jq -r '.oauth.authorization_url'

# One capability at a time
curl -s "${auth[@]}" -X POST 'http://127.0.0.1:8000/v1/connectors/google/reconnect?capability_intent=cap.email.send' | jq -r '.oauth.authorization_url'
```

After each `curl`, check the granted state:

```bash
curl -s "${auth[@]}" http://127.0.0.1:8000/v1/connectors/google | jq '{status: .connector.status, email: .connector.account_email, scopes: .connector.granted_scopes}'
```

## Capability → scope reference

What each `capability_intent` adds beyond what you already have:

| capability_intent | Scope(s) added |
|---|---|
| `cap.calendar.list` | `calendar.readonly` |
| `cap.calendar.propose_slots` | `calendar.readonly + calendar.freebusy` |
| `cap.calendar.create_event` | `calendar.events` |
| `cap.calendar.update_event` | `calendar.events` |
| `cap.calendar.respond_to_event` | `calendar.events` |
| `cap.email.search` | `gmail.readonly` |
| `cap.email.read` | `gmail.readonly` |
| `cap.email.draft` | `gmail.compose` |
| `cap.email.send` | `gmail.send` |
| `cap.email.archive` | `gmail.modify` |
| `cap.email.trash` | `gmail.modify` |
| `cap.email.labels.modify` | `gmail.modify` |
| `cap.email.undo` | `gmail.modify` |
| `cap.drive.search` | `drive.metadata.readonly` |
| `cap.drive.read` | `drive.readonly` |
| `cap.drive.share` | `drive` |

Reconnect always unions the requested set with what you have. Granted scopes never get removed by reconnect — only by `DELETE /v1/connectors/google`.
Gmail send and draft are separate grants: `cap.email.send` adds `gmail.send`,
while `cap.email.draft` adds `gmail.compose`.

## Google Cloud Console — do you need to touch it?

**Usually no** if your OAuth app is in **Testing** mode and your email is in the test-users list. Open [https://console.cloud.google.com/apis/credentials/consent](https://console.cloud.google.com/apis/credentials/consent) to check:

1. **Publishing status** — if "Testing", you don't need scope verification. Up to 100 test users can grant any scope, including restricted ones (gmail.send, gmail.modify, drive). Google shows an "unverified app" warning at the consent screen — click "Advanced" → "Go to … (unsafe)" to proceed.
2. **Test users** — your Gmail address must be listed. If not, add it.
3. **Scopes** — must include every scope Ariel requests. If you initially set up the OAuth client with only the readonly scopes, you'll need to **Add or Remove Scopes** and pick the ones you now want to grant. Specifically:
   - `openid`, `…/auth/userinfo.email`, `…/auth/userinfo.profile` — non-sensitive, always available
   - `…/auth/gmail.readonly`, `…/auth/calendar.readonly`, `…/auth/calendar.freebusy`, `…/auth/drive.metadata.readonly` — sensitive
   - `…/auth/gmail.compose`, `…/auth/gmail.send`, `…/auth/gmail.modify`, `…/auth/calendar.events`, `…/auth/drive.readonly`, `…/auth/drive` — **restricted**

   In Testing mode you can request restricted scopes without Google's verification. In **Production** mode, restricted scopes require a verification submission (security assessment, CASA audit). If you only ever use the app yourself, leave it in Testing.

## Troubleshooting

- **`E_CONNECTOR_RECONNECT_INVALID_INTENT`** — one or more `capability_intent` values don't match the table above. When using the comma-separated form, any single bad value rejects the whole request — the error `details.reason` names the offending intent. Check for typos.
- **`access_denied` from Google** — you clicked Cancel on the consent screen, or your email isn't in the test-users list.
- **`invalid_scope` from Google** — the scope you requested isn't enabled on the OAuth client. Open Cloud Console → OAuth consent screen → **Add or Remove Scopes**.
- **`E_CONNECTOR_CALLBACK_FAILED` with `provider_invalid_payload`** — Google did not return usable userinfo identity. Confirm the OAuth consent screen includes `openid`, `userinfo.email`, and `userinfo.profile`, then retry the reconnect.
- **Granted scopes still don't include the new one after Allow** — verify the callback succeeded: check `curl "${auth[@]}" http://127.0.0.1:8000/v1/connectors/google/events | jq '.events[-5:]'` for a recent `evt.connector.google.reconnect.succeeded`.
- **Scopes are present but Drive or Calendar calls still fail with `provider_permission_denied` or `missing_calendar` diagnostics** — this is not a reconnect loop. Check Drive API enablement, OAuth app/admin restrictions, and whether the file, shared drive, attendee calendar, or calendar ID is actually visible to the connected account.
- **`E_LOCAL_AUTH_TOKEN_INVALID`** — you didn't load the `auth=(…)` array, or `/etc/ariel/ariel.env` doesn't have `ARIEL_LOCAL_AUTH_TOKEN`. Sanity-check with `echo "$ARIEL_LOCAL_AUTH_TOKEN" | wc -c` (≥33 chars including newline).

## Disconnect

To wipe the existing connector entirely and start fresh:

```bash
curl -s "${auth[@]}" -X DELETE http://127.0.0.1:8000/v1/connectors/google | jq
curl -s "${auth[@]}" -X POST   http://127.0.0.1:8000/v1/connectors/google/start | jq -r '.oauth.authorization_url'
```

`DELETE` revokes the local refresh token, stops active watch channels, clears local account identity, and marks the connector disconnected; `start` mints a fresh OAuth flow asking for the current default scope set.
