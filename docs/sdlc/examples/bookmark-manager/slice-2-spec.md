# Slice 2: Bookmark CRUD — Spec

## Goal

Users can create, read, update, and delete bookmarks.

## Acceptance Criteria

### user creates a bookmark
- **given**: authenticated user
- **when**: POST to bookmarks endpoint with url and title
- **then**: bookmark is created, returns 201 with bookmark data

### user lists their bookmarks
- **given**: authenticated user with bookmarks
- **when**: GET bookmarks endpoint
- **then**: returns only their bookmarks, paginated, newest first

### user views a single bookmark
- **given**: authenticated user who owns the bookmark
- **when**: GET bookmark by id
- **then**: returns bookmark data. returns 404 if not found or not owned.

### user updates a bookmark
- **given**: authenticated user who owns the bookmark
- **when**: PUT bookmark with changed fields
- **then**: bookmark is updated. only owner can update.

### user deletes a bookmark
- **given**: authenticated user who owns the bookmark
- **when**: DELETE bookmark by id
- **then**: bookmark is removed. only owner can delete.

### duplicate URL is rejected
- **given**: user already has a bookmark with this URL
- **when**: POST with same URL
- **then**: returns conflict error

## Key Decisions

**Data model**: Bookmark has id (UUID), user_id (FK to users), url (text, max 2048), title (text, 1-500 chars), description (optional text, max 2000), created_at, updated_at. Unique constraint on (user_id, url). Cascade delete when user is deleted.

**API surface**: REST CRUD on `/api/v1/bookmarks` and `/api/v1/bookmarks/:id`. Standard L0 error format and pagination conventions apply.

**Ownership**: all operations are scoped to the authenticated user. no cross-user access. return 404 (not 403) for other users' bookmarks to avoid existence leaks.

## Out of Scope

- Tags (Slice 3)
- Search (Slice 4)
- Import/export (Slice 5)
- Frontend UI (Slice 6)
- Bookmark sharing between users
- URL validation or server-side title fetching
