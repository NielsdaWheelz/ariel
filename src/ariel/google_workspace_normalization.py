from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any, Literal


_MAX_BLOCK_CHARS = 2000
_MAX_BLOCKS = 12
_MAX_TEXT_PART_BYTES = 262144
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_REPLY_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")


BlockKind = Literal["body", "quote", "signature", "forwarded"]
MessageDirection = Literal["sent", "received", "draft"]


@dataclass(frozen=True, slots=True)
class NormalizedTextBlock:
    block_id: str
    kind: BlockKind
    text: str
    digest: str
    truncated: bool
    source_mime_type: str
    charset: str | None


@dataclass(frozen=True, slots=True)
class NormalizedEmailAddress:
    raw: str
    name: str | None
    email: str | None


@dataclass(frozen=True, slots=True)
class NormalizedGmailAttachment:
    attachment_id: str | None
    filename: str | None
    mime_type: str
    size: int | None
    content_id: str | None
    inline: bool


@dataclass(frozen=True, slots=True)
class NormalizedGmailBody:
    preferred_mime_type: str | None
    text: str
    html_text: str | None
    html_security: dict[str, Any]
    blocks: tuple[NormalizedTextBlock, ...]
    truncated: bool
    body_digest: str
    decode_notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalizedGmailMessage:
    provider_account_id: str
    message_id: str
    thread_id: str
    history_id: str | None
    rfc_message_id: str | None
    in_reply_to: str | None
    references: str | None
    subject: str | None
    subject_key: str | None
    sender: NormalizedEmailAddress | None
    recipients: tuple[NormalizedEmailAddress, ...]
    cc: tuple[NormalizedEmailAddress, ...]
    bcc: tuple[NormalizedEmailAddress, ...]
    reply_to: tuple[NormalizedEmailAddress, ...]
    internal_date_ms: int | None
    header_date: str | None
    direction: MessageDirection
    labels: tuple[str, ...]
    attachments: tuple[NormalizedGmailAttachment, ...]
    body: NormalizedGmailBody
    provider_url: str
    raw_payload_digest: str


@dataclass(frozen=True, slots=True)
class NormalizedCalendarPerson:
    email: str | None
    display_name: str | None
    self: bool


@dataclass(frozen=True, slots=True)
class NormalizedCalendarAttendee:
    email: str | None
    display_name: str | None
    response_status: str | None
    optional: bool
    organizer: bool
    self: bool


@dataclass(frozen=True, slots=True)
class NormalizedCalendarDateTime:
    value: str | None
    timezone: str | None
    all_day: bool


@dataclass(frozen=True, slots=True)
class NormalizedCalendarEvent:
    provider_account_id: str
    calendar_id: str
    event_id: str
    ical_uid: str | None
    recurring_event_id: str | None
    status: str | None
    summary: str | None
    description_blocks: tuple[NormalizedTextBlock, ...]
    organizer: NormalizedCalendarPerson | None
    creator: NormalizedCalendarPerson | None
    attendees: tuple[NormalizedCalendarAttendee, ...]
    start: NormalizedCalendarDateTime
    end: NormalizedCalendarDateTime
    all_day: bool
    recurrence: tuple[str, ...]
    location: str | None
    conference_data: dict[str, Any] | None
    reminders: dict[str, Any] | None
    updated: str | None
    etag: str | None
    provider_url: str | None
    hangout_link: str | None
    raw_payload_digest: str


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.omitted_tag_stack: list[str] = []
        self.omitted_tag_counts: dict[str, int] = {"script": 0, "style": 0, "head": 0}
        self.hidden_tag_stack: list[str] = []
        self.hidden_text_parts: list[str] = []
        self.links: list[dict[str, Any]] = []
        self.link_stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = {name.lower(): value for name, value in attrs}
        if tag in {"script", "style", "head"}:
            self.omitted_tag_stack.append(tag)
            self.omitted_tag_counts[tag] += 1
            return
        if self.hidden_tag_stack:
            if tag not in _VOID_TAGS:
                self.hidden_tag_stack.append(tag)
            return
        if _html_attrs_hidden(attrs_by_name):
            if tag not in _VOID_TAGS:
                self.hidden_tag_stack.append(tag)
            return
        if tag == "a":
            href = attrs_by_name.get("href")
            if isinstance(href, str) and href.strip():
                self.link_stack.append({"href": href.strip(), "parts": []})
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = {name.lower(): value for name, value in attrs}
        if tag in {"script", "style", "head"}:
            self.omitted_tag_counts[tag] += 1
            return
        if self.hidden_tag_stack or _html_attrs_hidden(attrs_by_name):
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.omitted_tag_stack and self.omitted_tag_stack[-1] == tag:
            self.omitted_tag_stack.pop()
            return
        if self.hidden_tag_stack:
            if self.hidden_tag_stack[-1] == tag:
                self.hidden_tag_stack.pop()
            return
        if tag == "a" and self.link_stack:
            link = self.link_stack.pop()
            text = _clean_text("".join(str(part) for part in link["parts"]))
            href = str(link["href"])
            self.links.append(
                {
                    "text": text,
                    "destination": href,
                    "mismatch": _link_text_mismatches_destination(text, href),
                }
            )
        if not self.omitted_tag_stack and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.omitted_tag_stack:
            return
        if self.hidden_tag_stack:
            self.hidden_text_parts.append(data)
            return
        self.parts.append(data)
        for link in self.link_stack:
            cast_parts = link["parts"]
            if isinstance(cast_parts, list):
                cast_parts.append(data)

    def text(self) -> str:
        return _clean_text("".join(self.parts))

    def security(self) -> dict[str, Any]:
        hidden_text = _clean_text(" ".join(self.hidden_text_parts))
        return {
            "hidden_text_count": len([part for part in self.hidden_text_parts if part.strip()]),
            "hidden_text_digest": _text_digest(hidden_text) if hidden_text else None,
            "links": self.links,
            "link_mismatch_count": sum(1 for link in self.links if link["mismatch"]),
            "omitted_tag_counts": self.omitted_tag_counts,
            "conversion_notes": [
                note
                for note in (
                    "hidden_html_text_omitted" if hidden_text else None,
                    "link_destinations_preserved" if self.links else None,
                )
                if note is not None
            ],
        }


def normalize_gmail_message(
    payload: dict[str, Any],
    *,
    provider_account_id: str,
) -> NormalizedGmailMessage:
    message_id = _string_or_none(payload.get("id"))
    if message_id is None or not message_id.strip():
        raise ValueError("gmail_message_id_missing")
    message_id = message_id.strip()
    thread_id = _string_or_none(payload.get("threadId"))
    if thread_id is None or not thread_id.strip():
        raise ValueError("gmail_thread_id_missing")
    thread_id = thread_id.strip()
    mime_payload = payload.get("payload")
    if not isinstance(mime_payload, dict):
        raise ValueError("gmail_payload_missing")
    headers = _headers(mime_payload.get("headers", []))
    labels = tuple(str(label) for label in payload.get("labelIds", []) if label is not None)
    body, attachments = _normalize_gmail_body(mime_payload, message_id)
    subject = headers.get("subject")

    return NormalizedGmailMessage(
        provider_account_id=provider_account_id,
        message_id=message_id,
        thread_id=thread_id,
        history_id=_string_or_none(payload.get("historyId")),
        rfc_message_id=headers.get("message-id"),
        in_reply_to=headers.get("in-reply-to"),
        references=headers.get("references"),
        subject=subject,
        subject_key=_subject_key(subject),
        sender=_first_address(headers.get("from")),
        recipients=_addresses(headers.get("to")),
        cc=_addresses(headers.get("cc")),
        bcc=_addresses(headers.get("bcc")),
        reply_to=_addresses(headers.get("reply-to")),
        internal_date_ms=_int_or_none(payload.get("internalDate")),
        header_date=headers.get("date"),
        direction=_message_direction(labels),
        labels=labels,
        attachments=tuple(attachments),
        body=body,
        provider_url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
        raw_payload_digest=_json_digest(payload),
    )


def normalize_calendar_event(
    event: dict[str, Any],
    *,
    provider_account_id: str,
    calendar_id: str,
) -> NormalizedCalendarEvent:
    event_id = _string_or_none(event.get("id"))
    if event_id is None or not event_id.strip():
        raise ValueError("calendar_event_id_missing")
    event_id = event_id.strip()
    status = _string_or_none(event.get("status"))
    description = _string_or_none(event.get("description"))
    description_text = _description_text(description)
    description_blocks, _ = _text_blocks(
        description_text,
        prefix=f"calendar:{calendar_id}:{event_id}:description",
        source_mime_type="text/html" if _looks_like_html(description) else "text/plain",
        charset="utf-8",
    )
    start_raw = event.get("start")
    end_raw = event.get("end")
    if not isinstance(start_raw, dict):
        if status != "cancelled":
            raise ValueError("calendar_start_missing")
        start_raw = {}
    if not isinstance(end_raw, dict):
        if status != "cancelled":
            raise ValueError("calendar_end_missing")
        end_raw = {}
    start = _calendar_datetime(start_raw)
    end = _calendar_datetime(end_raw)
    if status != "cancelled" and start.value is None:
        raise ValueError("calendar_start_missing")
    if status != "cancelled" and end.value is None:
        raise ValueError("calendar_end_missing")
    attendees_raw = event.get("attendees", [])
    if attendees_raw is None:
        attendees_raw = []
    if not isinstance(attendees_raw, list):
        raise ValueError("calendar_attendees_invalid")

    return NormalizedCalendarEvent(
        provider_account_id=provider_account_id,
        calendar_id=calendar_id,
        event_id=event_id,
        ical_uid=_string_or_none(event.get("iCalUID")),
        recurring_event_id=_string_or_none(event.get("recurringEventId")),
        status=status,
        summary=_string_or_none(event.get("summary")),
        description_blocks=description_blocks,
        organizer=_calendar_person(event.get("organizer")),
        creator=_calendar_person(event.get("creator")),
        attendees=tuple(_calendar_attendee(attendee) for attendee in attendees_raw),
        start=start,
        end=end,
        all_day=start.all_day and end.all_day,
        recurrence=tuple(str(item) for item in event.get("recurrence", []) if item is not None),
        location=_string_or_none(event.get("location")),
        conference_data=_dict_or_none(event.get("conferenceData")),
        reminders=_dict_or_none(event.get("reminders")),
        updated=_string_or_none(event.get("updated")),
        etag=_string_or_none(event.get("etag")),
        provider_url=_string_or_none(event.get("htmlLink")),
        hangout_link=_string_or_none(event.get("hangoutLink")),
        raw_payload_digest=_json_digest(event),
    )


def _normalize_gmail_body(
    payload: dict[str, Any],
    message_id: str,
) -> tuple[NormalizedGmailBody, list[NormalizedGmailAttachment]]:
    plain_parts: list[tuple[str, str]] = []
    html_parts: list[tuple[str, str]] = []
    html_security: dict[str, Any] = _empty_html_security()
    attachments: list[NormalizedGmailAttachment] = []
    notes: list[str] = []

    for part in _walk_mime_parts(payload):
        mime_type = _mime_type(part)
        headers = _headers(part.get("headers", []))
        body = part.get("body", {})
        filename = _string_or_none(part.get("filename"))
        attachment_id = _string_or_none(body.get("attachmentId"))
        disposition = _content_disposition(headers.get("content-disposition"))

        if filename or attachment_id or disposition == "attachment":
            attachments.append(
                NormalizedGmailAttachment(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime_type=mime_type,
                    size=_int_or_none(body.get("size")),
                    content_id=_strip_angle_brackets(headers.get("content-id")),
                    inline=disposition == "inline",
                )
            )

        if not mime_type.startswith("text/"):
            continue
        encoded = _string_or_none(body.get("data"))
        if encoded is None:
            if attachment_id:
                notes.append(f"skipped external text attachment {attachment_id}")
            continue
        decoded = _base64url_decode(encoded)
        if decoded is None:
            notes.append(f"invalid base64url body data in {mime_type}")
            continue
        if len(decoded) > _MAX_TEXT_PART_BYTES:
            notes.append(f"body too large in {mime_type}; skipped")
            continue
        charset = _charset(headers.get("content-type"))
        text = _decode_bytes(decoded, charset, notes)
        if mime_type == "text/html":
            html_text, html_part_security = _html_to_text_and_security(text)
            html_parts.append((charset, html_text))
            html_security = _merge_html_security(html_security, html_part_security)
        elif mime_type == "text/plain":
            plain_parts.append((charset, _clean_text(text)))

    plain_text = _join_text(text for _, text in plain_parts)
    html_text = _join_text(text for _, text in html_parts)
    preferred_mime_type = None
    preferred_charset = None
    preferred_text = ""

    if plain_text and not _inferior_plain_text(plain_text, html_text):
        preferred_mime_type = "text/plain"
        preferred_charset = plain_parts[0][0] if plain_parts else "utf-8"
        preferred_text = plain_text
    elif html_text:
        preferred_mime_type = "text/html"
        preferred_charset = html_parts[0][0] if html_parts else "utf-8"
        preferred_text = html_text

    blocks, truncated = _text_blocks(
        preferred_text,
        prefix=f"gmail:{message_id}:body",
        source_mime_type=preferred_mime_type or "text/plain",
        charset=preferred_charset,
    )
    return (
        NormalizedGmailBody(
            preferred_mime_type=preferred_mime_type,
            text="\n\n".join(block.text for block in blocks),
            html_text=html_text or None,
            html_security=html_security,
            blocks=blocks,
            truncated=truncated,
            body_digest=_text_digest(preferred_text),
            decode_notes=tuple(notes),
        ),
        attachments,
    )


def _walk_mime_parts(part: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield part
    for child in part.get("parts", []) or []:
        if isinstance(child, dict):
            yield from _walk_mime_parts(child)


def _text_blocks(
    text: str,
    *,
    prefix: str,
    source_mime_type: str,
    charset: str | None,
) -> tuple[tuple[NormalizedTextBlock, ...], bool]:
    blocks: list[NormalizedTextBlock] = []
    truncated = False
    for kind, section_text in _sections(text):
        position = 0
        while position < len(section_text):
            if len(blocks) == _MAX_BLOCKS:
                truncated = True
                return tuple(blocks), truncated
            chunk = section_text[position : position + _MAX_BLOCK_CHARS]
            position += _MAX_BLOCK_CHARS
            digest = _text_digest(chunk)
            blocks.append(
                NormalizedTextBlock(
                    block_id=f"{prefix}:{len(blocks)}:{digest[:16]}",
                    kind=kind,
                    text=chunk,
                    digest=digest,
                    truncated=position < len(section_text),
                    source_mime_type=source_mime_type,
                    charset=charset,
                )
            )
            truncated = truncated or position < len(section_text)
    return tuple(blocks), truncated


def _sections(text: str) -> Iterable[tuple[BlockKind, str]]:
    current_kind: BlockKind = "body"
    current_lines: list[str] = []

    def flush() -> tuple[BlockKind, str] | None:
        section = _clean_text("\n".join(current_lines))
        if not section:
            return None
        return current_kind, section

    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        next_kind = current_kind
        if stripped == "--" or lowered.startswith("sent from my "):
            next_kind = "signature"
        elif "forwarded message" in lowered or lowered.startswith("begin forwarded message"):
            next_kind = "forwarded"
        elif line.lstrip().startswith(">"):
            next_kind = "quote"
        elif current_kind == "quote":
            next_kind = "body"

        if next_kind != current_kind:
            section = flush()
            if section is not None:
                yield section
            current_lines = []
            current_kind = next_kind
        current_lines.append(line)

    section = flush()
    if section is not None:
        yield section


def _html_to_text_and_security(value: str) -> tuple[str, dict[str, Any]]:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text(), parser.security()


def _html_to_text(value: str) -> str:
    text, _ = _html_to_text_and_security(value)
    return text


def _empty_html_security() -> dict[str, Any]:
    return {
        "hidden_text_count": 0,
        "hidden_text_digest": None,
        "links": [],
        "link_mismatch_count": 0,
        "omitted_tag_counts": {"script": 0, "style": 0, "head": 0},
        "conversion_notes": [],
    }


def _merge_html_security(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_digest = left.get("hidden_text_digest")
    right_digest = right.get("hidden_text_digest")
    omitted_left = left.get("omitted_tag_counts")
    omitted_right = right.get("omitted_tag_counts")
    omitted_counts: dict[str, int] = {}
    for tag in ("script", "style", "head"):
        left_count = omitted_left.get(tag, 0) if isinstance(omitted_left, dict) else 0
        right_count = omitted_right.get(tag, 0) if isinstance(omitted_right, dict) else 0
        omitted_counts[tag] = int(left_count) + int(right_count)
    links = []
    if isinstance(left.get("links"), list):
        links.extend(left["links"])
    if isinstance(right.get("links"), list):
        links.extend(right["links"])
    notes = []
    for source in (left.get("conversion_notes"), right.get("conversion_notes")):
        if not isinstance(source, list):
            continue
        for note in source:
            if isinstance(note, str) and note not in notes:
                notes.append(note)
    return {
        "hidden_text_count": int(left.get("hidden_text_count") or 0)
        + int(right.get("hidden_text_count") or 0),
        "hidden_text_digest": _text_digest(f"{left_digest}|{right_digest}")
        if left_digest and right_digest
        else left_digest or right_digest,
        "links": links,
        "link_mismatch_count": sum(
            1 for link in links if isinstance(link, dict) and link.get("mismatch") is True
        ),
        "omitted_tag_counts": omitted_counts,
        "conversion_notes": notes,
    }


def _html_attrs_hidden(attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    aria_hidden = attrs.get("aria-hidden")
    if isinstance(aria_hidden, str) and aria_hidden.strip().lower() == "true":
        return True
    style = attrs.get("style")
    if not isinstance(style, str):
        return False
    normalized = style.replace(" ", "").lower()
    return any(
        marker in normalized
        for marker in ("display:none", "visibility:hidden", "opacity:0", "font-size:0")
    )


def _link_text_mismatches_destination(text: str, href: str) -> bool:
    normalized_text = text.strip().lower()
    normalized_href = href.strip().lower()
    if not normalized_text or not normalized_href:
        return False
    if normalized_text == normalized_href:
        return False
    if normalized_text.startswith(("http://", "https://", "www.")):
        return normalized_text not in normalized_href
    return False


def _description_text(value: str | None) -> str:
    if value is None:
        return ""
    if _looks_like_html(value):
        return _html_to_text(value)
    return _clean_text(unescape(value))


def _looks_like_html(value: str | None) -> bool:
    return bool(value and re.search(r"</?[a-z][\s>/]", value, re.IGNORECASE))


def _clean_text(value: str) -> str:
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in unescape(value).splitlines()]
    compact: list[str] = []
    previous_blank = False
    for line in lines:
        blank = line == ""
        if not blank or not previous_blank:
            compact.append(line)
        previous_blank = blank
    return "\n".join(compact).strip()


def _join_text(values: Iterable[str]) -> str:
    return _clean_text("\n\n".join(value for value in values if value))


def _inferior_plain_text(plain_text: str, html_text: str) -> bool:
    lowered = plain_text.strip().lower()
    return bool(
        html_text
        and (
            not lowered
            or lowered in {"this message contains html.", "this message contains html only."}
            or (len(plain_text) < 80 and len(html_text) > len(plain_text) * 3)
        )
    )


def _base64url_decode(value: str) -> bytes | None:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode((value + padding).encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None


def _decode_bytes(value: bytes, charset: str, notes: list[str]) -> str:
    try:
        return value.decode(charset)
    except LookupError:
        notes.append(f"unknown charset {charset}; decoded as utf-8 with replacement")
        return value.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        notes.append(f"invalid {charset} text; decoded with replacement")
        return value.decode(charset, errors="replace")


def _charset(content_type: str | None) -> str:
    message = Message()
    if content_type:
        message["content-type"] = content_type
    return message.get_content_charset() or "utf-8"


def _content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    return message.get_content_disposition()


def _headers(raw_headers: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not isinstance(raw_headers, list):
        return headers
    for item in raw_headers:
        if not isinstance(item, dict):
            continue
        name = _string_or_none(item.get("name"))
        value = _string_or_none(item.get("value"))
        if name and value is not None:
            headers[name.lower()] = _decode_header_value(value)
    return headers


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        return value


def _addresses(value: str | None) -> tuple[NormalizedEmailAddress, ...]:
    if not value:
        return ()
    addresses: list[NormalizedEmailAddress] = []
    for name, email in getaddresses([value]):
        raw = f"{name} <{email}>" if name and email else email or name
        addresses.append(
            NormalizedEmailAddress(
                raw=raw,
                name=name or None,
                email=email.lower() or None,
            )
        )
    return tuple(addresses)


def _first_address(value: str | None) -> NormalizedEmailAddress | None:
    addresses = _addresses(value)
    return addresses[0] if addresses else None


def _message_direction(labels: tuple[str, ...]) -> MessageDirection:
    if "DRAFT" in labels:
        return "draft"
    if "SENT" in labels:
        return "sent"
    return "received"


def _subject_key(subject: str | None) -> str | None:
    if subject is None:
        return None
    key = _REPLY_PREFIX_RE.sub("", subject)
    key = _WHITESPACE_RE.sub(" ", key).strip().lower()
    return key or None


def _mime_type(part: dict[str, Any]) -> str:
    value = _string_or_none(part.get("mimeType")) or "application/octet-stream"
    return value.split(";", 1)[0].strip().lower() or "application/octet-stream"


def _calendar_person(value: Any) -> NormalizedCalendarPerson | None:
    if not isinstance(value, dict):
        return None
    return NormalizedCalendarPerson(
        email=_string_or_none(value.get("email")),
        display_name=_string_or_none(value.get("displayName")),
        self=bool(value.get("self", False)),
    )


def _calendar_attendee(value: Any) -> NormalizedCalendarAttendee:
    if not isinstance(value, dict):
        raise ValueError("calendar_attendee_invalid")
    return NormalizedCalendarAttendee(
        email=_string_or_none(value.get("email")),
        display_name=_string_or_none(value.get("displayName")),
        response_status=_string_or_none(value.get("responseStatus")),
        optional=bool(value.get("optional", False)),
        organizer=bool(value.get("organizer", False)),
        self=bool(value.get("self", False)),
    )


def _calendar_datetime(value: Any) -> NormalizedCalendarDateTime:
    if not isinstance(value, dict):
        value = {}
    date_value = _string_or_none(value.get("date"))
    date_time_value = _string_or_none(value.get("dateTime"))
    return NormalizedCalendarDateTime(
        value=date_value or date_time_value,
        timezone=_string_or_none(value.get("timeZone")),
        all_day=date_value is not None,
    )


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strip_angle_brackets(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip("<>")


def _text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value: dict[str, Any]) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(data.encode("utf-8")).hexdigest()
