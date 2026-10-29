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

**Restrict sign-in for the pilot.** Set `ALLOWLIST_ENABLED=true` and list the students:

```bash
python scripts/manage_allowlist.py add you@example.com            # add YOURSELF first, or you are locked out
python scripts/manage_allowlist.py import pilot_students.txt      # one address per line, # comments allowed
python scripts/manage_allowlist.py list
python scripts/manage_allowlist.py remove someone@example.com
```

With it on, a Google account that is not listed is turned back after sign-in with one generic message
("This account cannot be used to sign in to the pilot..."), no account row is created, nothing says who
is listed, and the log records only a short hash of the address. Matching ignores case. A student you
remove is signed out at their next request. With it off (the default) anyone with a Google account can
sign in. As a second layer, keep the Google OAuth consent screen in *Testing* mode with the same people
as test users.

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

Run it before every `alembic upgrade`, before re-ingesting, and nightly during the pilot. Copy the
folder off the machine: a backup on the same disk does not survive the disk.

### Scheduled backups

`backup` verifies what it wrote, keeps the newest `--keep` backups (default 7), appends one line per
run to `backups/backup.log`, and exits non-zero on failure (2 = the backup failed, 3 = it did not
verify). Old backups are pruned only after a good one, so a failing job never eats the ones that work.

**Windows** (Task Scheduler). Nothing is registered until you ask:

```powershell
.\scripts\schedule_backup.ps1 -DryRun                        # shows what it would create
.\scripts\schedule_backup.ps1 -Register -At 02:30 -Keep 14    # create the daily task
Get-ScheduledTaskInfo -TaskName "EduPath Backup"             # LastTaskResult 0 = fine
.\scripts\schedule_backup.ps1 -Unregister                    # remove it
```

The task runs as you, while you are signed in. For an always-on server create it by hand with "Run
whether user is logged on or not".

**Linux** (cron, `crontab -e`):

```cron
30 2 * * *  cd /srv/edupath && .venv/bin/python scripts/backup.py backup --keep 14 || echo "EduPath backup FAILED" | mail -s "EduPath backup" you@example.com
```

Either way, read `backups/backup.log` (section 11).

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
`revoked_tokens`, `allowed_emails`), so the *previous* image also runs on the upgraded database:
redeploy the old image (`docker tag` the previous build before replacing it) and nothing else is
needed. To remove the new structures as well: `alembic downgrade 13c704704310`.

Exactly what a downgrade drops (rehearsed on a Postgres 16 container holding old Arabic rows):
the `sources` column (every stored Sources list), all thumbs/comments, the logout denylist and the
sign-in allowlist. It keeps every user, chat session, message (text intact), quiz attempt and question
(byte-identical dump of those tables before and after, and again after upgrading once more).
Stored sources, feedback and the allowlist do not come back after a downgrade and a new upgrade.
If anything is damaged, restore the last backup (section 6).

## 8. Known limitations

- **Arabic answer quality has not been reviewed by a human.** The evaluation set and a blind
  review sheet exist (`eval/`), but the grades are not in. Do not present Arabic quality as measured.
- **Valid-but-wrong citations cannot be detected.** The app removes citations to pages that were not
  retrieved (about 16-22% of what the model writes, measured), but a citation to a retrieved page that
  does not actually support the sentence looks valid.
- The model can still answer from its own knowledge or phrase things its sources do not say; the
  Sources list shows what was retrieved, not proof of every claim.
- Citation brackets inside Arabic text are isolated left-to-right. Checked in real Chrome at one window
  size (`eval/results/ui_check/`); not checked on a phone or in other browsers.
- Sign-in is open to any Google account unless `ALLOWLIST_ENABLED=true` (section 3); rate limits are per
  process.
- One machine, one Ollama: heavy simultaneous use queues behind the GPU.
- Reopened chats show the sources stored from now on; older replies have none.

## 9. Not verified

Checked since the first version of this guide: the migrations on a real Postgres 16 (throwaway
container, old schema with Arabic rows, up, down, up), the quiz row lock under concurrent answer and
settle, the UI in real Chrome with a copy of the data, the first GitHub Actions run (reported green by
the owner). Still not verified:

- A full chat and quiz through the running **container** against the real database.
- `alembic upgrade head` on **your** Postgres (only a throwaway copy of the old schema was used).
- Linux `host-gateway` networking and `OLLAMA_HOST=0.0.0.0`; a cold ~2.3 GB model download in the container.
- Google sign-in end to end (the browser checks used a minted cookie); the allowlist redirect was tested
  with a faked Google reply, not a real one.
- The scheduled-backup task actually firing on your machine (the script was dry-run only).
- The phone layout, Safari and Firefox.
- Several students at once on one GPU.

## 10. Go / no-go checklist

Tick every line before the first student gets access. If a line cannot be ticked, that is the no-go.

- [ ] **Backup taken** and verified: `python scripts/backup.py backup`, then `verify`; copy kept off the machine.
- [ ] **Backup restored once** into a scratch database (section 6) and the app opened against it.
- [ ] **Migration applied** on the real database: `alembic upgrade head` (after the backup), `alembic current` shows `d4f9b2c7e613`.
- [ ] **ENVIRONMENT=production starts**: the container or `uvicorn` runs without the "Refusing to start" error (real secrets, `COOKIE_SECURE=true`, HTTPS).
- [ ] **Sign-in restricted**: `ALLOWLIST_ENABLED=true`, yourself and the pilot students listed (`manage_allowlist.py list`), and Google OAuth in *Testing* mode. Sign in with an unlisted account: you get the "cannot be used" message and no row appears.
- [ ] **`CHAT_TRACE_LOG=true`** on (and `CHAT_TRACE_QUERIES` **off** unless students were told). `LOG_TRACEBACKS` off.
- [ ] **Thumbs work**: ask a question, press thumbs-down, add a comment, reload, the thumb is still pressed; `python scripts/export_feedback.py` shows the row.
- [ ] **Reopened chat** shows its Sources.
- [ ] **Models warm**: `/health` is `ok` and the log shows "model warm-up finished"; one Arabic and one English question answered with sources.
- [ ] **Scheduled backup** registered (section 6) and `backups/backup.log` has an `OK` line from a real run.
- [ ] **Rollback rehearsed**: you know the previous image tag, ran `alembic downgrade` on a scratch copy, and read section 7.
- [ ] **Students told**: Arabic quality is not reviewed, citations can be wrong, what is logged, how to ask for deletion (section 12).

## 11. Daily routine during the pilot

About ten minutes. Run from the project folder.

1. **Feedback.** `python scripts/export_feedback.py --since <yesterday> --out feedback.csv`. Read every thumbs-down and its comment; note recurring subjects, pages and languages.
2. **Answers with no sources.** `docker logs edupath-api --since 24h | grep "chat-trace reply"` and look for lines with `refused=True` and `sources_shown=0`, `chunks=0` (nothing retrieved), or `citations_invalid` above 0. A refusal after a long answer (`raw_chars` large, `sentinel_at` not 0) is the case worth reporting.
3. **429 and 503.** `docker logs edupath-api --since 24h | grep -cE '" (429|503) '` and compare with yesterday. A rise in 429 means students hit the limit (raise `RATE_LIMIT_CHAT`); a rise in 503 means Ollama or Chroma trouble (section 12).
4. **Failures.** `docker logs edupath-api --since 24h | grep -E "failed|Traceback"`: each failure line carries the exception class, a code and where it happened, never the message.
5. **Backup age.** The last line of `backups/backup.log` must be `OK` and from last night; `Get-ChildItem backups | sort Name | select -last 3` (or `ls -t backups | head -3`).
6. **Disk.** Free space on the drive holding `backups/`, `data/` and the Docker volumes (`docker system df`). A pg_dump plus `data/` is a few hundred MB today.
7. **Warm-up.** `docker logs edupath-api | grep "warm-up"` after any restart.

## 12. Incidents

**Ollama is down** (students see "The AI service is busy or unavailable" / 503). The API stays up; `/health` stays `ok`. On the host: `ollama ps` and `ollama list`; start the Ollama app or service. Do not reconfigure it while students are online. When it is back the first answer is slow (model load, up to about two minutes); the app retries by itself. If `/health` is fine but chat says "search unavailable", that is Chroma or the embedding model, not Ollama: check `docker logs edupath-api | grep -E "retrieval|embedding"` and that the `edupath_models` volume still holds bge-m3.

**The database is down.** Every route fails with 500. `docker ps` / `docker logs edupath-postgres`; `docker compose up -d postgres`. Data survives a container restart (the `edupath_pgdata` volume). If the volume is damaged: stop the API, restore the latest backup (section 6), start the API, tell students what time the data goes back to.

**Wrong answers reported.** Ask for the question, the time, and the thumbs-down message id (it is in the CSV). Find the turn: `grep "chat-trace" ` around that time shows which pages were retrieved and whether citations were removed. Decide which it is: (a) the right page was not retrieved (retrieval; note it), (b) it was retrieved and the model ignored or misread it (model; note it), (c) the lecture itself is wrong or unclear. Do not change prompts, rerankers or models during the pilot without measuring on `eval/qa_set.json` first. If the answer is harmful or badly wrong and repeatable, you can switch chat off for a subject by removing its lectures from the index only with a backup in hand; otherwise just record it.

**A student asks for their data to be deleted.**

```bash
python scripts/delete_student.py student@example.com                       # shows what would go, deletes nothing
python scripts/delete_student.py student@example.com --export theirs.json  # give them a copy if they ask for one
python scripts/delete_student.py student@example.com --yes --remove-from-allowlist
```

That removes their chats, messages, ratings and comments, quiz attempts and questions, and the account.
It does **not** reach: backups already taken (they keep the data until they rotate out, `--keep`
days; delete the affected backup folders by hand if the request requires it), the access log (which
holds no question text), or the Google account itself. Their token stops working once the account is
gone. Tell the student all of this when you confirm.

**A student's account was misused / a token leaked.** Remove them from the allowlist (they are signed out at the next request) and, if needed, delete the account as above. Rotate `JWT_SECRET` to sign **everyone** out.

**A pilot-wide stop.** `docker compose --profile app stop api`. The data is untouched; restart with `up -d api`.
