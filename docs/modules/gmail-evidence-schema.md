# Gmail Message Evidence Schema

## Role

This document is the typed-output contract for `google.gmail.message_evidence.v1`,
the payload returned by `cap.email.read` (single message and thread modes).

It exists so that the validator `_is_google_gmail_message_evidence_output` and the
constructors in `src/ariel/google_connector.py` are checked against a stable shape.
Constructor and validator changes are verified against this document.

See [google-workspace-reasoning-cutover.md](google-workspace-reasoning-cutover.md)
for the surrounding architecture and capability semantics, and
[../boundaries.md](../boundaries.md) for the general typed-output rule.

## Top-Level Shape

Validator: `_is_google_gmail_message_evidence_output` at
`src/ariel/google_connector.py:4224`.

Required keys on every payload:

- `schema_version`: must equal `"google.gmail.message_evidence.v1"`
- `mode`: one of `"message"`, `"thread"`, `"thread_context"`
- `status`: must equal `"succeeded"`
- `retrieved_at`: RFC3339 string, non-empty
- `published_at`: present (value may be `null`)
- `evidence`: object (see below)
- `read_outcome`: object (see below)

Mode-specific keys:

- `mode == "message"`: `message` object (validated by
  `_gmail_message_metadata_is_bounded`).
- `mode == "thread"` or `"thread_context"`: `thread` object plus `messages` list.

The key `"results"` is forbidden. The payload is rejected if any key outside the
enumerated set above is present.

Constructors:

- Message mode: `_gmail_read_message_output` at `src/ariel/google_connector.py:1367`.
- Thread modes: `_gmail_read_thread_output` at `src/ariel/google_connector.py:1451`.
- Degraded message outcomes: `_gmail_message_unavailable_output` at
  `src/ariel/google_connector.py:3499`.

## `evidence` Object

Allowed keys, and no others:

- `source_kind`: `"gmail_message"` for message mode; `"gmail_thread"` for thread
  modes.
- `message_id`
- `thread_id`
- `anchor_message_id`
- `body_digest`
- `blocks`
- `truncated`
- `decode_notes`
- `html_security`

Keys matching the forbidden text patterns `body`, `description`, `html`, `text`,
`raw`, `content` are rejected by `_has_forbidden_text_key`. Raw body text never
leaves the evidence boundary; only the block list under `blocks` and the digest
under `body_digest` may carry body-derived content.

## `read_outcome` Object

Exactly three keys: `status`, `reason_code`, `recovery`. No others.

`status` is one of:

- `"ok"`
- `"body_too_large"`
- `"decode_failed"`
- `"no_body"`

Constructor: `_gmail_read_outcome` at `src/ariel/google_connector.py:3472`.

## Body-Digest Invariant

The contract between `read_outcome.status` and the body-bearing evidence fields
is biconditional. Validator enforcement at
`src/ariel/google_connector.py:4276-4284`.

- `read_outcome.status == "ok"` iff:
  - `evidence.body_digest` is a non-empty string, and
  - `evidence.blocks` is a non-empty list of blocks valid under
    `_gmail_evidence_blocks_are_valid` (`google_connector.py:4187`).
- `read_outcome.status != "ok"` iff:
  - `evidence.body_digest is None`, and
  - `evidence.blocks == []`.

Data-model equivalent: `NormalizedGmailBody.body_digest: str | None`
(`src/ariel/google_workspace_normalization.py:109`). The message constructor at
`google_connector.py:1440` and the thread variant at `google_connector.py:1544`
pass this value through unchanged; when no message in a thread has an extractable
body, the thread-level `body_digest` is `None`.

## Hidden-Content Policy

`_html_attrs_hidden` at `src/ariel/google_workspace_normalization.py:628` decides
whether an HTML element counts as hidden for the purpose of populating
`evidence.html_security.hidden_text_count`.

A node counts as hidden if and only if one of these holds:

- the HTML `hidden` attribute is present, or
- `aria-hidden="true"`, or
- the inline `style` attribute contains one of the markers `display:none`,
  `visibility:hidden`, `opacity:0` (whitespace-insensitive, case-insensitive).

The CSS rule `font-size:0` is not a hide marker. It is a layout/whitespace hack
used by MJML-style email templates; children with their own `font-size` are
visible and must not be reported as hidden.

## `message` Metadata Bounding

Validator: `_gmail_message_metadata_is_bounded` at
`src/ariel/google_connector.py:4135`.

Allowed top-level keys on `message`: `provider_account_id`, `message_id`,
`thread_id`, `history_id`, `rfc_message_id`, `in_reply_to`, `references`,
`subject`, `subject_key`, `sender`, `recipients`, `cc`, `bcc`, `reply_to`,
`internal_date`, `internal_date_ms`, `header_date`, `direction`, `labels`,
`label_ids`, `attachments`, `body`, `provider_url`, `raw_payload_digest`.

`provider_account_id` is required and must be a non-empty string. Keys matching
forbidden text patterns are rejected.

The `body` sub-object is either `null` or a dict whose allowed keys are exactly:
`preferred_mime_type`, `truncated`, `body_digest`, `decode_notes`,
`html_security`. The keys `text`, `html_text`, and `blocks` are forbidden inside
`body` — raw or block-shaped body content never appears in message metadata; it
lives only under `evidence.blocks` at the payload root.

## Thread Mode Constraints

In `mode == "thread"` and `"thread_context"`:

- `thread` allowed keys exactly: `provider_account_id`, `thread_id`,
  `history_id`, `message_count`, `anchor_message_id`.
- `thread.provider_account_id` and `thread.thread_id` are non-empty strings.
- `evidence.source_kind == "gmail_thread"`.
- `evidence.thread_id == thread.thread_id`.
- `len(messages) <= _MAX_GOOGLE_RESULTS`.
- Every entry in `messages` is a bounded metadata dict with `message_id`
  non-empty and `thread_id == thread.thread_id`.

## Message Mode Constraints

In `mode == "message"`:

- `evidence.source_kind == "gmail_message"`.
- `evidence.message_id == message.message_id` and both are non-empty.
- `evidence.thread_id == message.thread_id`.
- On `read_outcome.status == "ok"`, `message.thread_id` is a non-empty string.
- On degraded outcomes (`body_too_large`, `decode_failed`, `no_body`),
  `message.thread_id` is `None` or a string.
