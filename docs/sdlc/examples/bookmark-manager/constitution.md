# Bookmark Manager — Constitution v1

## 1. Vision

### Problem
Developers accumulate hundreds of browser bookmarks that become impossible to
organize, search, or access across devices. Browser bookmark UIs are clunky
and don't support tagging or full-text search.

### Solution
A web app with a REST API backend that lets users save, tag, search, and
organize bookmarks. Clean UI, fast search, accessible from any device.

### Scope (v1)
- User accounts (signup, login, logout)
- Save bookmarks with title, URL, description
- Tag bookmarks for organization
- Search bookmarks (title, URL, description, tags)
- Import/export bookmarks

### Non-Scope (v1)
- No browser extension (users paste URLs manually)
- No social/sharing features
- No bookmark folders/hierarchy (tags only)
- No automatic URL metadata fetching
- No mobile app (responsive web only)
- No team/organization accounts
- No two-factor authentication
- No bookmark archiving/wayback

---

## 2. Core Abstractions

| Concept | Definition |
|---------|------------|
| **User** | An authenticated account that owns bookmarks |
| **Bookmark** | A saved URL with title, optional description, and tags |
| **Tag** | A label for grouping bookmarks (e.g., "devtools", "recipes") |
| **Session** | An authenticated user session (JWT token) |

---

## 3. Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│                     Frontend (SPA)                      │
│  React + TypeScript, served as static files             │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTPS
┌───────────────────────▼─────────────────────────────────┐
│                     API Server                          │
│  Node.js + Express, handles all business logic          │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│                     PostgreSQL                          │
│  Primary data store                                     │
└─────────────────────────────────────────────────────────┘
```

### Data Flow
Browser → API Server → PostgreSQL → Response → Browser

### Trust Model
- API server trusts PostgreSQL
- Frontend is untrusted (validate all input server-side)
- Users authenticated via JWT in Authorization header
- All user data is private to that user

---

## 4. Hard Constraints

| Constraint | Value |
|------------|-------|
| Backend Language | TypeScript (Node.js) |
| Frontend Language | TypeScript (React) |
| Database | PostgreSQL 15+ |
| Authentication | JWT (access token + refresh token) |
| API Style | REST, JSON request/response bodies |
| Hosting | Single VPS (API + static files + DB) |

---

## 5. Conventions

### API Design
- Base path: `/api/v1`
- Resources: plural nouns (`/bookmarks`, `/tags`, `/users`)
- Actions: HTTP verbs (GET, POST, PUT, DELETE)
- IDs: UUIDs (never sequential integers for security)

### Request/Response Format
- Content-Type: `application/json`
- Dates: ISO 8601 strings (`2024-01-15T10:30:00Z`)
- Pagination: `?page=1&limit=20`, response includes `total`, `page`, `limit`

### Errors
- Pattern: `{ "error": { "code": "E_CATEGORY_NAME", "message": "..." } }`
- HTTP status codes: 400 (validation), 401 (auth), 403 (forbidden), 404 (not found), 500 (server)

### Naming
- Database: snake_case (`created_at`, `user_id`)
- API JSON: camelCase (`createdAt`, `userId`)
- TypeScript: camelCase for variables, PascalCase for types

### Authentication
- Access token: JWT, 15 minute expiry, sent in `Authorization: Bearer <token>`
- Refresh token: opaque string, 7 day expiry, sent in HTTP-only cookie
- Password: bcrypt hashed, minimum 8 characters

---

## 6. Invariants

1. A bookmark MUST have a valid URL (parseable, http/https scheme)
2. A bookmark MUST belong to exactly one user
3. A user email MUST be unique (case-insensitive)
4. A tag name MUST be lowercase, 1-50 chars, alphanumeric + hyphens only
5. All API endpoints except /auth/* MUST require valid JWT
6. Deleting a user MUST delete all their bookmarks and tags
7. A bookmark's URL + user_id combination MUST be unique (no duplicate URLs per user)

---

## 7. API Overview

### Auth
```
POST   /api/v1/auth/signup     Create account
POST   /api/v1/auth/login      Get tokens
POST   /api/v1/auth/refresh    Refresh access token
POST   /api/v1/auth/logout     Invalidate refresh token
```

### Bookmarks
```
GET    /api/v1/bookmarks       List bookmarks (supports search, filter, pagination)
POST   /api/v1/bookmarks       Create bookmark
GET    /api/v1/bookmarks/:id   Get bookmark
PUT    /api/v1/bookmarks/:id   Update bookmark
DELETE /api/v1/bookmarks/:id   Delete bookmark
```

### Tags
```
GET    /api/v1/tags            List user's tags with counts
```

### Import/Export
```
POST   /api/v1/import          Import bookmarks (JSON or Netscape HTML)
GET    /api/v1/export          Export all bookmarks as JSON
```
