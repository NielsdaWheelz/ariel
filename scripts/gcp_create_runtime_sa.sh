#!/usr/bin/env bash
set -euo pipefail

info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
ok()    { printf '\033[1;32m    ✓ %s\033[0m\n' "$1"; }
fail()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$1"; }

SA_NAME="ariel-runtime"
SA_DISPLAY_NAME="Ariel runtime"
KEY_PATH="${HOME}/.ariel-secrets/gcp-pubsub-sa.json"

# ── 1. Check prerequisites ─────────────────────────────────────────────
info "Checking prerequisites"
if ! command -v gcloud >/dev/null 2>&1; then
  fail "gcloud required. Install from https://cloud.google.com/sdk/docs/install"
  exit 1
fi
ok "gcloud $(gcloud --version 2>/dev/null | head -n1)"

if [ -z "${GCP_PROJECT:-}" ]; then
  fail "GCP_PROJECT must be set (e.g. export GCP_PROJECT=my-project)"
  exit 1
fi
ok "GCP_PROJECT=${GCP_PROJECT}"

# ── 2. Compute SA email ────────────────────────────────────────────────
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"

# ── 3. Create the service account if missing ───────────────────────────
info "Ensuring service account ${SA_EMAIL}"
if gcloud iam service-accounts describe "${SA_EMAIL}" --project "${GCP_PROJECT}" >/dev/null 2>&1; then
  ok "service account already exists"
else
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name "${SA_DISPLAY_NAME}" \
    --project "${GCP_PROJECT}"
  ok "created service account"
fi

# ── 4. Ensure the key output directory exists ──────────────────────────
info "Preparing key directory $(dirname "${KEY_PATH}")"
mkdir -p "$(dirname "${KEY_PATH}")"
chmod 700 "$(dirname "${KEY_PATH}")"
ok "directory ready (mode 700)"

# ── 5. Rotation safety: back up an existing key ────────────────────────
if [ -f "${KEY_PATH}" ]; then
  BACKUP_PATH="${KEY_PATH}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
  info "Existing key found; rotating"
  cp "${KEY_PATH}" "${BACKUP_PATH}"
  chmod 600 "${BACKUP_PATH}"
  printf '\033[1;33m    ! WARNING: an existing key was backed up to %s.\033[0m\n' "${BACKUP_PATH}"
  printf '\033[1;33m    ! Update the deployment to use the new key, then revoke and delete the old one.\033[0m\n'
fi

# ── 6. Create a new JSON key ───────────────────────────────────────────
info "Creating new key at ${KEY_PATH}"
gcloud iam service-accounts keys create "${KEY_PATH}" \
  --iam-account "${SA_EMAIL}" \
  --project "${GCP_PROJECT}"

# ── 7. Lock down the key ───────────────────────────────────────────────
chmod 600 "${KEY_PATH}"
ok "key written (mode 600)"

# ── 8. Summary ─────────────────────────────────────────────────────────
info "Done"
printf '    SA email   (paste into RUNTIME_SA_EMAIL for scripts/gcp_provision_pubsub.sh):\n'
printf '      \033[1m%s\033[0m\n' "${SA_EMAIL}"
printf '    Key path   (paste into ARIEL_GOOGLE_APPLICATION_CREDENTIALS_PATH):\n'
printf '      \033[1m%s\033[0m\n' "${KEY_PATH}"
printf '    Once the new key is in use, revoke and delete old keys:\n'
printf '      gcloud iam service-accounts keys list --iam-account %s --project %s\n' "${SA_EMAIL}" "${GCP_PROJECT}"
printf '      gcloud iam service-accounts keys delete <KEY_ID> --iam-account %s --project %s\n\n' "${SA_EMAIL}" "${GCP_PROJECT}"
