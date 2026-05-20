#!/usr/bin/env bash
set -euo pipefail

info()  { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
ok()    { printf '\033[1;32m    ✓ %s\033[0m\n' "$1"; }
fail()  { printf '\033[1;31m    ✗ %s\033[0m\n' "$1"; }

TOPIC="ariel-gmail-watch"
SUB="ariel-gmail-watch-sub"
DLQ_TOPIC="ariel-gmail-watch-dlq"
DLQ_SUB="ariel-gmail-watch-dlq-sub"

# ── 1. Check prerequisites ─────────────────────────────────────────────
info "Checking prerequisites"
if ! command -v gcloud >/dev/null 2>&1; then
  fail "gcloud required. Install from https://cloud.google.com/sdk/docs/install"
  exit 1
fi
ok "gcloud $(gcloud --version 2>/dev/null | head -1 | awk '{print $4}')"

if [ -z "${GCP_PROJECT:-}" ]; then
  fail "GCP_PROJECT is not set"
  exit 1
fi
ok "GCP_PROJECT=$GCP_PROJECT"

if [ -z "${RUNTIME_SA_EMAIL:-}" ]; then
  fail "RUNTIME_SA_EMAIL is not set"
  exit 1
fi
ok "RUNTIME_SA_EMAIL=$RUNTIME_SA_EMAIL"

PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')
PUBSUB_AGENT="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
GMAIL_PUBLISHER="serviceAccount:gmail-api-push@system.gserviceaccount.com"
RUNTIME_SA="serviceAccount:${RUNTIME_SA_EMAIL}"

# ── 2. Create source topic ─────────────────────────────────────────────
info "Creating source topic $TOPIC"
gcloud pubsub topics describe "$TOPIC" --project="$GCP_PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub topics create "$TOPIC" --project="$GCP_PROJECT"
ok "topic $TOPIC"

# ── 3. Create DLQ topic ────────────────────────────────────────────────
info "Creating DLQ topic $DLQ_TOPIC"
gcloud pubsub topics describe "$DLQ_TOPIC" --project="$GCP_PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub topics create "$DLQ_TOPIC" --project="$GCP_PROJECT"
ok "topic $DLQ_TOPIC"

# ── 4. Create DLQ subscription ─────────────────────────────────────────
info "Creating DLQ subscription $DLQ_SUB"
gcloud pubsub subscriptions describe "$DLQ_SUB" --project="$GCP_PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub subscriptions create "$DLQ_SUB" --project="$GCP_PROJECT" --topic="$DLQ_TOPIC"
ok "subscription $DLQ_SUB"

# ── 5. Create source subscription ──────────────────────────────────────
info "Creating source subscription $SUB"
gcloud pubsub subscriptions describe "$SUB" --project="$GCP_PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub subscriptions create "$SUB" --project="$GCP_PROJECT" --topic="$TOPIC" \
       --ack-deadline=60 \
       --enable-exactly-once-delivery \
       --dead-letter-topic="$DLQ_TOPIC" \
       --max-delivery-attempts=10 \
       --dead-letter-topic-project="$GCP_PROJECT"
ok "subscription $SUB"

# ── 6. IAM bindings ────────────────────────────────────────────────────
info "Applying IAM bindings"
gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project="$GCP_PROJECT" \
  --member="$GMAIL_PUBLISHER" --role="roles/pubsub.publisher" >/dev/null
ok "$TOPIC: $GMAIL_PUBLISHER → roles/pubsub.publisher"
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" --project="$GCP_PROJECT" \
  --member="$PUBSUB_AGENT" --role="roles/pubsub.publisher" >/dev/null
ok "$DLQ_TOPIC: $PUBSUB_AGENT → roles/pubsub.publisher"
gcloud pubsub subscriptions add-iam-policy-binding "$SUB" --project="$GCP_PROJECT" \
  --member="$RUNTIME_SA" --role="roles/pubsub.subscriber" >/dev/null
ok "$SUB: $RUNTIME_SA → roles/pubsub.subscriber"
gcloud pubsub subscriptions add-iam-policy-binding "$DLQ_SUB" --project="$GCP_PROJECT" \
  --member="$RUNTIME_SA" --role="roles/pubsub.subscriber" >/dev/null
ok "$DLQ_SUB: $RUNTIME_SA → roles/pubsub.subscriber"

# ── 7. Print env var lines ─────────────────────────────────────────────
info "Paste these into .env.local"
printf '  ARIEL_GOOGLE_PUBSUB_TOPIC=projects/%s/topics/%s\n' "$GCP_PROJECT" "$TOPIC"
printf '  ARIEL_GOOGLE_PUBSUB_SUBSCRIPTION=projects/%s/subscriptions/%s\n\n' "$GCP_PROJECT" "$SUB"
