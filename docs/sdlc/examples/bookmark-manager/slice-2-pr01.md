# PR-01: Bookmark CRUD Backend

## Goal

Add bookmark model, storage, service logic, and API routes with full test coverage.

## Builds On

Slice 1 (auth) complete — users table and JWT auth middleware exist.

## Acceptance

- authenticated user can create a bookmark with url + title, gets 201 with bookmark data.
- authenticated user can list their bookmarks, paginated, newest first. other users' bookmarks are not returned.
- authenticated user can get a single bookmark by id. returns 404 for nonexistent or unowned bookmarks.
- authenticated user can update their bookmark. only provided fields change.
- authenticated user can delete their bookmark.
- creating a bookmark with a duplicate URL (per user) returns a conflict error.
- migration creates bookmarks table with all L2 constraints (field types, lengths, unique constraint, cascade delete).
- all API routes follow L0 conventions (error format, pagination, auth).

## Non-Goals

- No frontend UI (Slice 6).
- No tags (Slice 3).
- No search or filtering (Slice 4).
- No import/export (Slice 5).
