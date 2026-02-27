# Bookmark Manager — Slice Roadmap

## Slice 0: Bootstrap
**Goal**: Project scaffolding and database setup.

**Outcome**:
- Backend compiles and runs
- Database migrations run
- Health check endpoint works

**Dependencies**: None

**Acceptance**:
- `npm run dev` starts the server
- `GET /api/v1/health` returns `{ "status": "ok" }`
- PostgreSQL tables created via migrations


## Slice 1: Authentication
**Goal**: Users can create accounts and log in.

**Outcome**:
- Signup creates a user account
- Login returns JWT tokens
- Protected routes require valid token

**Dependencies**: Slice 0

**Acceptance**:
- Signup with email/password creates user
- Login with valid credentials returns tokens
- Request to /bookmarks without token returns 401
- Request to /bookmarks with valid token succeeds


## Slice 2: Bookmark CRUD
**Goal**: Users can save and manage bookmarks.

**Outcome**:
- Create bookmark with title/URL
- List user's bookmarks
- Update and delete bookmarks

**Dependencies**: Slice 1

**Acceptance**:
- Create bookmark, verify it appears in list
- Update bookmark title, verify change persists
- Delete bookmark, verify it's gone
- User A cannot see User B's bookmarks


## Slice 3: Tags
**Goal**: Users can organize bookmarks with tags.

**Outcome**:
- Create bookmark with tags
- Filter bookmarks by tag
- List all tags with counts

**Dependencies**: Slice 2

**Acceptance**:
- Create bookmark with tags, verify tags in response
- Filter by tag, only matching bookmarks returned
- GET /tags shows all user's tags with bookmark counts


## Slice 4: Search
**Goal**: Users can search their bookmarks.

**Outcome**:
- Search by title, URL, description
- Search by tag
- Results paginated

**Dependencies**: Slice 2, 3

**Acceptance**:
- Search "github" finds bookmarks with github in title/URL
- Search returns paginated results
- Empty search returns all bookmarks


## Slice 5: Import/Export
**Goal**: Users can import and export bookmarks.

**Outcome**:
- Export all bookmarks as JSON
- Import from JSON
- Import from browser HTML format

**Dependencies**: Slice 3

**Acceptance**:
- Export creates valid JSON with all bookmarks
- Import JSON creates bookmarks
- Import HTML from Chrome/Firefox works


## Slice 6: Frontend
**Goal**: Web UI for all features.

**Outcome**:
- Login/signup pages
- Bookmark list with search
- Add/edit bookmark forms

**Dependencies**: Slice 1, 2, 3, 4

**Acceptance**:
- User can sign up and log in
- User can add, edit, delete bookmarks
- Search and tag filtering works
- Responsive on mobile


## Dependency Graph

```
Slice 0: Bootstrap
    │
    ▼
Slice 1: Authentication
    │
    ▼
Slice 2: Bookmark CRUD
    │
    ├───────────────┐
    ▼               ▼
Slice 3: Tags    (parallel)
    │               │
    ├───────────────┘
    ▼
Slice 4: Search
    │
    ├───────────────┐
    ▼               │
Slice 5: Import     │
                    │
                    ▼
              Slice 6: Frontend
```
