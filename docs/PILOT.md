# Pilot guide

For whoever runs EduPath Study V2 for a small group of real students. It describes what was
actually tried; anything not tried is listed under "Not verified".

## 1. What runs where

| Part | Where | Notes |
|---|---|---|
| Postgres 16 | Docker (`docker-compose.yml`, service `postgres`) | data in the `edupath_pgdata` volume |
| API | Docker (`edupath-api` image, compose profile `app`) | Python 3.13, 2.57 GB image |
| Ollama (llama3.1:8b chat, qwen3-vl:4b ingestion) | **on the host**, never in a container | the API reaches it at `host.docker.internal:11434` |
| Frontend | built with `npm run build` and served by any static host / reverse proxy | not in the image |
| Ingestion | on the host (needs Tesseract with the Arabic pack) | offline admin task |

## 2. First deployment

```bash
cp .env.example .env              # then edit: see "Secrets" below
docker compose up -d              # Postgres only
docker build -t edupath-api:latest .
alembic upgrade head              # from the host; back up first if the database already has users (section 6)
python scripts/seed.py            # universities + subjects (or add_subject.py for your own)
```

### Secrets (`.env`, never committed)

`ENVIRONMENT=production` makes the API **refuse to start** with placeholder secrets,
a default database password or `COOKIE_SECURE=false` (checked; the error lists every problem).
Set `JWT_SECRET`, `SESSION_SECRET` (long random strings), `POSTGRES_PASSWORD`,
`GOOGLE_CLIENT_ID/SECRET`, `GOOGLE_REDIRECT_URI`, `FRONTEND_URL`, `COOKIE_SECURE=true`
(serve over HTTPS).

### Model cache (once)

The container runs with `HF_HUB_OFFLINE=1` and never downloads models. Put bge-m3
(~2.3 GB) into the `edupath_models` volume once; both ways are in the README under
"Seeding the model cache". Without it the API starts but chat answers "search unavailable".

### Ingest the lectures (on the host)

```bash
cd ingestion
python ingest_lecture.py --university-id 1 --subject-id 4 --file lecture.pdf --dry-run   # check first
python ingest_lecture.py --university-id 1 --subject-id 4 --file lecture.pdf
```

This writes `data/chroma_db` and `data/images`, which the container mounts (`./data`).
Restart the API container afterwards so it reopens the store.

### Start

```bash
docker compose --profile app up -d api
curl http://localhost:8000/health        # {"status":"ok"}
```

Measured on the development machine: the server answers `/health` about 3 s after start;
the background warm-up (embedding model, reranker, chat model) finished 21-24 s after start
with the chat model already resident in Ollama, and about 2 minutes when Ollama had to load
it from disk. Students can use the app during warm-up; the first request just waits.

On Linux, Ollama must listen beyond loopback for the container to reach it
(`OLLAMA_HOST=0.0.0.0`, firewall port 11434 off from outside): see the README.

### Behind a reverse proxy

Set `TRUSTED_PROXY=true` (and `TRUSTED_PROXY_HOPS` if more than one proxy) **only** when a proxy
you control sets `X-Forwarded-For` and the API is not reachable except through it. Otherwise
every student shares the proxy's address in the login rate limit, or - if you enable it
without a proxy - anyone can pick their own address.

## 3. Creating the first students

There is no student registry: a student signs in with Google, a row is created on first
sign-in, then they choose university and year (onboarding) and see that year's subjects.
**Nothing restricts which Google accounts may sign in.** For a pilot, keep the Google OAuth
consent screen in *Testing* mode and list the pilot students as test users (Google Cloud
console, "OAuth consent screen", "Test users"); only they can then complete sign-in.
Tell each student: sign in, pick the university and year, open a subject.

## 4. Reading feedback and trace logs

**Feedback.** Students have a thumbs up / down under every answer (down offers a short
comment). Export:

```bash
python scripts/export_feedback.py --out feedback.csv            # everything
python scripts/export_feedback.py --rating down --since 2026-11-01
```

Columns: when, rating, comment, subject, the question, the answer, how many sources it
showed, message id. **Who** rated is not exported. Open it in Excel (UTF-8 with BOM, so Arabic
displays); cells that look like formulas are neutralised.

**Trace logs** answer "why did this answer have no sources / the wrong ones". Set
`CHAT_TRACE_LOG=true`, restart, and each chat turn writes two `chat-trace` lines:

```
chat-trace retrieve session=.. subject=.. prior_messages=.. rewritten=True query_chars=.. chunks=[('file.pdf', 44), ...]
chat-trace reply    session=.. refused=False sentinel_at=None legacy_phrase=False raw_chars=.. citations_written=3 citations_invalid=1 chunks=3 sources_shown=3 final_chars=..
```

`refused=True` with a long answer, or `chunks=[]`, or `citations_invalid>0` is the thing to look for.
The question text is **not** logged unless `CHAT_TRACE_QUERIES=true` as well - switch that on
only for a specific investigation and tell the students. Read with `docker logs edupath-api | grep chat-trace`.

Logs never contain cookies or tokens; OAuth codes in request lines are redacted
(`tests/test_log_hygiene.py`).

## 5. Rate limits and sessions

Chat 20/minute and quiz 5/minute per student, login 10/minute per IP (settings
`RATE_LIMIT_*`); in-memory, per process. Sessions last 7 days; **Sign out now really ends the
token** (it is denylisted until it would have expired). Tokens issued before the upgrade that
introduced this have no id and cannot be revoked; they expire within 7 days.
Another student's chat or quiz id returns 404, indistinguishable from a missing one.

## 6. Backup and restore

```bash
python scripts/backup.py backup                       # -> backups/edupath-<time>/  (Postgres dump + data/chroma_db + data/images)
python scripts/backup.py backup --include-lectures    # also the source PDFs
python scripts/backup.py verify backups/edupath-20261026-101500
```

Run it before every `alembic upgrade`, before re-ingesting, and nightly during the pilot
(schedule it with Task Scheduler / cron). Copy the folder off the machine.

Restore (rehearse into a scratch database first):

```bash
# rehearsal: nothing of the real database is touched
docker exec edupath-postgres psql -U edupath -d postgres -c "CREATE DATABASE restore_test"
python scripts/backup.py restore backups/edupath-... --database restore_test --target-data-dir ./restore_test_data --yes
# real restore: stop the API first; --force moves the current data/ aside instead of overwriting
docker compose --profile app stop api
python scripts/backup.py restore backups/edupath-... --force --yes
docker compose --profile app up -d api
```

The restore replaces the tables of the target database (`pg_restore --clean`) and refuses a
backup whose checksums do not match. The round trip (including Arabic text) was tested on a
throwaway Postgres container; it has **not** been run against your real database.

## 7. Updating, and rolling back

1. `python scripts/backup.py backup`
2. `git pull`, `docker build -t edupath-api:latest .`, `alembic upgrade head`
3. `docker compose --profile app up -d api`; check `/health`, open an old chat (sources should show), ask one question, press a thumb.

**Run `alembic upgrade head` before starting the new image.** The new code reads columns and
tables that older databases lack: with the old schema, `/health` and most pages work but opening
a chat fails (observed in testing).

**Rollback.** The new migrations are additive (`chat_messages.sources`, `message_feedback`,
`revoked_tokens`), so the *previous* image also runs on the upgraded database: redeploy the old
image (`docker tag` the previous build before replacing it) and nothing else is needed. To remove
the new structures as well: `alembic downgrade 13c704704310` (this drops stored sources,
feedback and the logout denylist). If anything is damaged, restore the last backup (section 6).

## 8. Known limitations

- **Arabic answer quality has not been reviewed by a human.** The evaluation set and a blind
  review sheet exist (`eval/`), but the grades are not in. Do not present Arabic quality as measured.
- **Valid-but-wrong citations cannot be detected.** The app removes citations to pages that were not
  retrieved (about 16-22% of what the model writes, measured), but a citation to a retrieved page that
  does not actually support the sentence looks valid.
- The model can still answer from its own knowledge or phrase things its sources do not say; the
  Sources list shows what was retrieved, not proof of every claim.
- Citation brackets inside Arabic text are isolated left-to-right in the page; this was unit-tested
  but not looked at in a real browser.
- Sign-in is not restricted by account (section 3); rate limits are per process.
- One machine, one Ollama: heavy simultaneous use queues behind the GPU.
- Reopened chats show the sources stored from now on; older replies have none.

## 9. Not verified

- A full chat call through the container (it would write to the real database).
- The first GitHub Actions run (workflow written and parsed, never executed).
- Linux `host-gateway` networking and `OLLAMA_HOST=0.0.0.0`.
- A cold ~2.3 GB model download inside the container.
- `alembic upgrade head` on the real Postgres (migrations were tested on SQLite, upgrade and downgrade).
