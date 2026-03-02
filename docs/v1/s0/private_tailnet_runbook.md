# Slice 0 PR-02 private tailnet runbook

this runbook defines a repeatable private deployment for slice 0:

- ariel backend stays localhost-only.
- phone access is through private tailnet https.
- access defaults to deny and is granted only to explicit identities/devices.
- restart preserves existing timeline/history and keeps appending to the same active session.

## 1) prepare runtime secrets + database

`make bootstrap` handles steps 1–3 (env, db, tailscale serve) in one command. the steps below are kept for reference or manual runs.

```bash
make env-init
# edit .env.local with your real runtime values
make db-up
make db-upgrade
```

`ARIEL_MODEL_API_KEY` must exist only on the server host (not in client code, not in tracked files).  
when needed, explicit shell env vars still override `.env.local`.

## 2) start ariel as localhost-only

```bash
make run
```

the app must not bind `0.0.0.0` or another public interface.

## 3) expose over private tailnet https only

```bash
tailscale serve --https=443 http://127.0.0.1:8000
tailscale serve status
tailscale funnel status
```

required posture:

- `tailscale serve status` shows proxying to `http://127.0.0.1:8000`.
- `tailscale funnel status` shows no public funnel.
- phone opens `https://<node>.<tailnet>.ts.net/` from an allowlisted tailnet identity/device.

## 4) apply explicit allowlist policy

start from `deploy/tailscale/policy.example.json`, replace placeholder identities, then apply the policy in the tailnet admin.

minimum policy rule shape:

- `src`: only explicit allowlisted identities/device tags.
- `dst`: `tag:ariel:443` (or equivalent explicit destination).
- no `*` and no `autogroup:internet` source.

negative check:

- from a non-allowlisted identity/device, access to the ariel tailnet dns endpoint must fail.

## 5) verify private ingress posture (scripted)

export serve status to json, then run the verifier:

```bash
tailscale serve status --json > /tmp/ariel-serve-status.json
# export your active tailnet policy JSON (or equivalent) to:
# /tmp/ariel-policy.json

.venv/bin/python scripts/verify_private_tailnet_posture.py \
  --serve-status-json /tmp/ariel-serve-status.json \
  --policy-json /tmp/ariel-policy.json \
  --allowed-identity user:alice@example.com \
  --allowed-identity tag:alice-phone \
  --protected-destination tag:ariel:443 \
  --backend-port 8000
```

expected output:

- `private posture check passed`

## 6) verify credential behavior and secret safety

positive:

- with a valid provider key configured server-side, a normal message turn completes.

negative:

- unset or invalidate the server-side key and send a message.
- expected API envelope: `E_MODEL_CREDENTIALS`.
- timeline still records `evt.model.failed` and `evt.turn.failed`.
- response/timeline must not contain raw secret values.

## 7) verify restart durability and active-session continuity

1. send a message from phone (or API) and confirm it appears in timeline.
2. restart the ariel service process.
3. reconnect and reload timeline: prior turn remains visible.
4. send another message.
5. confirm timeline contains both turns in order under the same active session id.
