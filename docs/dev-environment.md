# Local Dev Environment

## Scope

How to run an isolated local dev stack on the same host as production without
touching the prod database, the running systemd services, or the live Discord
bot. For the production deployment itself see
[production-runbook.md](production-runbook.md).

## Layout

Two parallel stacks coexist on the host:

| Concern             | Production (systemd)                 | Dev (`make dev-*`)                          |
| ------------------- | ------------------------------------ | ------------------------------------------- |
| Env file            | `.env.local`                         | `.env.dev`                                  |
| Env-file selector   | (default — `.env` + `.env.local`)    | `ARIEL_ENV_FILE=.env.dev`                   |
| Postgres container  | `ariel-postgres`                     | `ariel-postgres-dev`                        |
| Postgres port       | `127.0.0.1:5433`                     | `127.0.0.1:5435`                            |
| Postgres volume     | `ariel-postgres-data`                | `ariel-postgres-dev-data`                   |
| API bind            | `127.0.0.1:8000`                     | `127.0.0.1:8001`                            |
| Discord bot token   | prod token in `.env.local`           | (operator paste a separate dev bot token)   |
| Process supervisor  | `systemd` (`ariel-api`, `-worker`, `-discord`, `-pubsub`) | foreground `make dev-api`, `dev-worker`, `dev-discord` |

The two stacks share nothing at runtime: different DB, different port, different
process. Running `make dev-up`, `make dev-api`, etc. never reads `.env.local`
and never touches the prod container.

## Mechanism

A single env var, `ARIEL_ENV_FILE`, controls which env file the app and dev
helpers load. Both [`src/ariel/config.py`](../src/ariel/config.py) (`AppSettings`
via pydantic-settings) and [`src/ariel/dev_db.py`](../src/ariel/dev_db.py)
honor it: when set, only that file is read; when unset, the default
`.env` + `.env.local` stack is used so prod/systemd behavior is unchanged.

`alembic upgrade head` inherits the same selection because
[`alembic/env.py`](../alembic/env.py) builds `AppSettings()` to discover the
DB URL.

## First-time setup

1. Create the dev env file from the template and edit it. The dev DB defaults
   are wired into `.env.dev.example` — only `ARIEL_OPENAI_API_KEY` and any
   optional Discord/Google tokens need attention.

   ```sh
   make dev-init                # copies .env.dev.example → .env.dev
   $EDITOR .env.dev             # paste your OpenAI key, etc.
   ```

2. Start the dev Postgres and run migrations.

   ```sh
   make dev-up
   make dev-upgrade
   ```

3. Run any service(s) you want against the dev DB.

   ```sh
   make dev-api                 # ariel API on 127.0.0.1:8001
   make dev-worker              # background worker (separate terminal)
   make dev-discord             # Discord bot (separate terminal, only if dev token set)
   ```

`make dev-shell` opens an interactive subshell with `ARIEL_ENV_FILE=.env.dev`
exported — useful for `alembic`, `psql`, or ad-hoc `python -m ariel.*` work
without retyping the env var.

## Coexistence with prod

Prod runs as four systemd units (`ariel-api`, `ariel-worker`, `ariel-discord`,
`ariel-pubsub`) backed by `.env.local` and the `ariel-postgres` container on
`:5433`. Dev runs as foreground `make` targets backed by `.env.dev` and the
`ariel-postgres-dev` container on `:5435`. Confirm both are healthy at once:

```sh
systemctl is-active ariel-api ariel-worker ariel-discord ariel-pubsub
make db-status         # ariel-postgres on :5433
make dev-status        # ariel-postgres-dev on :5435
curl -fsS http://127.0.0.1:8000/v1/health  # prod
curl -fsS http://127.0.0.1:8001/v1/health  # dev (only while `make dev-api` is running)
```

## Discord in dev

The prod bot connects to Discord with the token in `.env.local`. Reusing that
same token from `make dev-discord` would race the prod bot — both processes
would receive every owner DM and the user would see duplicate replies.

Two options:

- **Skip the Discord surface in dev.** Leave `ARIEL_DISCORD_BOT_TOKEN` unset
  in `.env.dev` and do not run `make dev-discord`. The dev API still works
  for direct HTTP smoke tests; provider events go through the dev worker.
- **Run a separate dev bot.** Create a second Discord application + bot,
  invite it to a private dev guild, and paste its token, guild ID, channel
  ID, and user ID into `.env.dev`. The prod and dev bots then sit on
  different Discord identities and do not collide.

## Alembic against dev

`make dev-upgrade` is equivalent to:

```sh
ARIEL_ENV_FILE=.env.dev .venv/bin/alembic upgrade head
```

For downgrades, history, or revision-authoring, run alembic directly inside
`make dev-shell`:

```sh
make dev-shell
alembic history
alembic downgrade -1
exit
```

## Connecting psql

```sh
make dev-config        # prints database_url, db_user, db_name, port, etc.
PGPASSWORD=dev-only-password psql -h 127.0.0.1 -p 5435 -U ariel -d ariel
```

## Resetting dev state

The dev DB is disposable. To start from a clean schema:

```sh
make dev-destroy       # removes container + volume
make dev-up
make dev-upgrade
```

`dev-down` removes the container but preserves the volume; `dev-destroy`
removes both. Neither touches the prod container.

## Guarantees

- `make dev-*` targets never read `.env.local`.
- `make db-*` targets continue to operate against whatever `ARIEL_DATABASE_URL`
  resolves to in `.env` + `.env.local` (i.e., the prod container while
  `.env.local` points at `:5433`). Do not run them while you want dev
  isolation — use `make dev-*` instead.
- The prod systemd units inherit no shell env, so a developer exporting
  `ARIEL_ENV_FILE=.env.dev` in their interactive shell cannot accidentally
  redirect a prod systemd restart at the dev DB.

## Acceptance checklist

- `docker ps` shows both `ariel-postgres` (prod, :5433) and
  `ariel-postgres-dev` (dev, :5435) running.
- `systemctl is-active ariel-api` returns `active` while `make dev-api` is
  also serving requests on `:8001`.
- A write performed through the dev API only appears in the dev DB
  (`psql -p 5435`), not the prod DB (`psql -p 5433`).
- `make verify` passes.
