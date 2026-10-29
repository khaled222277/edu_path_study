# EduPath Study V2: Handoff

Last updated: 2026-10-29. State: everything up to `fix/ci-container-followup` is merged into `main`.
`pilot/readiness` and `pilot/final-checks` (stacked) are NOT merged yet; read sections 4.8-4.9 before merging.

## 1. What the project is

A multilingual (Arabic/English) study platform for university students.
Students sign in with Google, pick a university and subject, then:
- chat with a tutor grounded in lecture material (with citations and figure sources),
- take generated quizzes,
- see a progress dashboard.
Only admins ingest lectures, through CLI scripts. The corpus is one university (Homs), year 5 IT.
Owner: Khaled Sharaf. Goal: a tool for real students, with a pilot next.

## 2. Stack

- Backend: FastAPI (Python 3.13 locally and in CI), SQLAlchemy 2, Alembic, Postgres 16 (Docker).
- Auth: Google OAuth (Authlib), then a JWT in an HTTP-only cookie (7 days, HS256).
- Vector store: ChromaDB at `data/chroma_db/`, BAAI/bge-m3 embeddings, chunks of 800 characters with 100 overlap.
- Reranker: FlashRank (default). A bge reranker exists behind a setting (see section 6).
- LLM: local Ollama. llama3.1:8b for chat and quiz, qwen3-vl:4b for figure description during ingestion.
- Ingestion: PyMuPDF with a Tesseract OCR fallback. Runs on the host, not in the container.
- Frontend: React 19 + Vite, no router, no state library.
- Layout: routers → controllers → services → models/schemas.

## 3. Branch history

fix/hardening → perf/config-pass → perf/chat-streaming → perf/quiz-latency → quality/arabic-eval → fix/citation-validation → fix/ci-container-followup (merged) → pilot/readiness → pilot/final-checks (not merged)

## 4. What was done, by phase

### 4.1 Hardening (fix/hardening)
- Added the missing `flashrank` dependency to requirements.
- `ENVIRONMENT=production` refuses to start with placeholder secrets, a default DB password, or `cookie_secure=False`. `.env.example` documents every variable.
- Timeouts on all Ollama calls; generic error messages (no exception text to clients); a clean 503 when Ollama or Chroma is down.
- Rate limiting: auth 10/min per IP, chat 20/min and quiz 5/min per user (in-memory). CORS restricted. JWTs without `exp` rejected.
- Tests: first suite, plus a mutation check showing the tests catch regressions.
- Audit verdicts: the event loop was NOT blocked (routes were plain `def`), and IDOR/ownership checks were already correct.

### 4.2 Performance and config (perf/config-pass)
- Correction of my earlier premise: `get_collection()` did not rebuild the embedder per request. The real problems were the first-request model load (about 29 s), a cold-start race, and a stale Chroma client after ingestion by another process (a real bug, now fixed with a retry-and-reset).
- Model load moved to startup warm-up. Tuning values (models, TOP_K, candidate count, rerank threshold, chunk settings, OLLAMA_BASE_URL) moved into settings with validation.
- Frontend handles 429/503 with messages in the student's working language. The UI itself is English-only.
- Ingestion prints a summary of figures skipped on errors.

### 4.3 Chat latency (perf/chat-streaming)
- Ollama keep-alive plus a chat-model warm-up at boot. The 48 s first turn could not be reproduced (cold load measured at 4-5 s).
- New streaming endpoint (async route, Ollama async client, own DB session for the stream). Citations arrive with the first token. Disconnect releases the Ollama call. Nothing is saved on errors or Stop.
- Frontend streaming with a Stop button (AbortController), correct Arabic direction, partial text kept on error.
- Time to first text: about 0.3-0.8 s (was 4-5 s cold, then nothing until the whole answer was ready).
- `EMBED_DEVICE` setting (default cpu).

### 4.4 Quiz latency (perf/quiz-latency)
- Model calls are 99.8% of quiz time. English passes first try 94% of the time; Arabic wastes about 40% of calls.
- Tried and REVERTED: structured output (worse), feedback on retry (no gain), lower temperature (noise), lowering the strict attempt cap (it would push items into the relaxed phase).
- Kept: `QUIZ_FIRST_PASS_ATTEMPTS=2` (about -10% model calls, modest).
- Real win: progressive delivery. The first question appears in about 1.5 s instead of after 6-19 s. Partial results are kept on Stop or on an outage.
- `QUIZ_CONCURRENCY` (default 1). With `OLLAMA_NUM_PARALLEL=1` it makes Arabic slower. With `OLLAMA_NUM_PARALLEL=3` on a test instance: English -54%, Arabic -33%, at about +0.9 GB VRAM and about 10% slower single-stream speed. To use it: set `OLLAMA_NUM_PARALLEL=3` in Ollama's own environment, restart Ollama, set `QUIZ_CONCURRENCY=3`, then check `ollama ps` still shows 100% GPU.
- Removing the duplicated user question from the chat prompt was REJECTED after eval (Arabic out-of-scope answered in English 8/8, follow-ups lost the thread). `tests/test_chat_prompt.py` pins the current prompt.

### 4.5 Quality (quality/arabic-eval)
- Foreign-script words in Arabic quiz items now reject the item instead of being stripped.
- Language-neutral refusal sentinel token (`<<NOT_IN_MATERIAL>>`), so Arabic refusals hide sources.
- Follow-up questions are rewritten into standalone search queries (`query_rewrite.py`).
- Sweeper for half-written quiz attempts after a crash.
- Eval set: 45 questions (about 25 Arabic) in `eval/qa_set.json` (Arabic wording written by a model, NOT checked by a native speaker). Metrics script: `scripts/eval_quality.py`. Blind review sheet: `eval/review_sheet.csv|html`, key in `eval/review_key.json` (never share the key with a reviewer).
- Findings, 3 seeds each:
  - Gold page in top 3 vector candidates: Arabic 91%. After FlashRank it falls to 74% (English 93%).
  - bge reranker or plain vector order keeps Arabic at 91%. bge lifts English to 100% (sign test p = 0.062; promising, not proven).
  - With bge: Arabic false refusals 5.8% → 0%, correct citations 77% → 92%.
  - Cost of bge: about 1.8 s per query on CPU (vs 0.2 s) and about 0.6 GB more RAM. GPU run NOT done.
  - 800/100 chunks beat 400/50, 800/200 and 1200/150. 20 candidates made FlashRank worse.
  - Quiz validity: English 81%, Arabic 52% of replies (62% first attempt).
- DEFAULTS WERE NOT CHANGED. Human grading was skipped by the owner, who judged the output by eye. See section 7.

### 4.6 Citation validation (fix/citation-validation)
- The model invents about 16-22% of citations. Now every citation is validated against the chunks retrieved for that turn. Invalid ones are removed (not flagged). Arabic-Indic digits handled. The sources list only contains retrieved sources.
- Streaming holds a bracket back until it is complete and judged, so an invalid citation never appears and is then withdrawn. The final `done` event text equals the streamed text.
- What students see: invalid citations dropped from about 22% to about 1% (Arabic) and from about 16% to about 3% (English).
- Limit: a valid but wrong citation (the page was retrieved but does not support the claim) cannot be detected automatically.

### 4.7 CI and container (fix/ci-container-followup)
- CI runs on push to any branch, on pull requests and on manual dispatch. Python job: 3.13, offline tests (`HF_HUB_OFFLINE=1`). Frontend job: `npm ci`, `npm test`, `npm run build`. Caches for pip and npm. First real run: green (test 1m46s, frontend 9s).
- Container: `HF_HOME=/models` on a named volume (no 2 GB re-download per start), an `api` compose service behind the `app` profile, `OLLAMA_BASE_URL` set to `host.docker.internal:11434`, an `EDUPATH_DATA_DIR` override. Image 2.58 GB, server listening in about 2.5 s, background warm-up about 127 s.
- Smoke test passed against a copy of data/ (health, auth, subjects, sessions, retrieval parity with the host).
- "No sources" AdaBoost answer: NOT reproduced (0 of 44 runs). Opt-in `CHAT_TRACE_LOG=true` writes two trace lines per turn. A later live run on the new code showed `refused=False`, `sources_shown=3`, `rewritten=True`. Most likely the earlier screenshot came from older code (likely, not proven).

### 4.8 Pilot readiness (pilot/readiness)
- Sources stored per assistant message (`chat_messages.sources`, nullable JSON; old rows read as empty). Plain and stream routes store the same list the response returns. Reopened chats show their Sources.
- Citations render as `<bdi dir="ltr">` isolates (markup only; stored text unchanged).
- Thumbs up/down per answer with an optional comment (`message_feedback`, `PUT/DELETE /chat/messages/{id}/feedback`, 404 for other students' replies). `scripts/export_feedback.py` writes a CSV with no user identity and formula-safe cells.
- `TRUSTED_PROXY` / `TRUSTED_PROXY_HOPS` (default off): client IP from `X-Forwarded-For` for the login rate limit; the login routes had been bypassing it and now use `client_ip`.
- `scripts/backup.py` (backup / verify / restore, checksums, never overwrites without `--force`).
- Logs: OAuth `code` redacted from the access log; question text only with `CHAT_TRACE_QUERIES`.
- 404 (not 403) for another student's chat or quiz id. Logout now revokes the token (`revoked_tokens`, jti denylist; tokens issued before this change have no jti and expire within 7 days).
- Container: Python 3.13 base, `HF_HUB_OFFLINE=1`, compose-owned model volume, seeding and `OLLAMA_HOST` docs. CI actions at v7 (Node 24).
- `docs/PILOT.md` written.

### 4.9 Final checks (pilot/final-checks)
- Migrations on a real Postgres 16 (throwaway container): old schema (`13c704704310`) filled with Arabic rows, `upgrade head`, `downgrade`, `upgrade` again. Old tables byte-identical after each step. A downgrade drops exactly: stored sources, all feedback, the logout denylist, the allowlist. Reusable tests behind `EDUPATH_TEST_PG_CONTAINER` (`tests/pg_helpers.py`).
- Quiz row lock (`FOR UPDATE`) exercised on Postgres for the first time (answer vs. settle, both orders, random interleavings). It found one real bug: a worker adding a question after the stream settled left `total_questions` below the stored count. Fixed in `add_streamed_item`.
- Real Chrome check (screenshots and notes in `eval/results/ui_check/`). It found: `PUT`/`DELETE` missing from CORS (every thumb click failed in a browser), class-name collisions with Quiz styles, citations wrapping mid-bracket. All fixed.
- jsdom + Testing Library DOM tests for `Chat.jsx` (`npm run test:ui`, a CI step): streaming, reconciliation, Stop, error note, 429/503, sources and ratings on reopen, thumbs and rollback.
- Pilot access control: `ALLOWLIST_ENABLED` (default off) + `allowed_emails` + `scripts/manage_allowlist.py`. A non-listed Google account is refused after OAuth with one generic redirect, no row, no cookie, and only a short hash in the log; removing an address ends that student's session.
- Error logging: every exception log site in `app/` and `ingestion/` now writes class + code + location (`app/log_safe.py`), never the message or a traceback (these can quote a student's question, a prompt or lecture text). `LOG_TRACEBACKS=true` restores them for local debugging. A source-scan test guards new call sites.
- Scheduled backups: `backup` verifies, prunes to `--keep` (default 7), logs one line per run, non-zero exit on failure; `scripts/schedule_backup.ps1` (registers nothing without `-Register`); cron example in PILOT.md.
- `scripts/delete_student.py` (dry run by default, optional export). PILOT.md gained a go/no-go checklist, a daily routine and an incident section.

## 5. Test status
- Python: 801 passed, 12 skipped offline; 813 passed with `EDUPATH_TEST_PG_CONTAINER` set to a throwaway Postgres container (the 12 are the real-Postgres tests).
- Frontend: 89 unit tests (`npm test`), 21 DOM tests (`npm run test:ui`), build OK. CI runs all of them.
- Nearly every behaviour change was mutation-checked; each phase's report lists the results. Two mutants were equivalent (a redundant `..` check in the backup tar filter; `jwt.decode(None)` already failing closed).

## 6. Settings reference (names only; see `.env.example` for values)
ENVIRONMENT, JWT/session secrets, COOKIE_SECURE, OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE, CHAT_MODEL, QUIZ_MODEL, vision model, TOP_K, candidate count, RERANK_MIN_CONFIDENCE, RERANKER (flashrank | bge-m3), RERANKER_DEVICE, EMBED_DEVICE, QUIZ_FIRST_PASS_ATTEMPTS, QUIZ_CONCURRENCY, CHAT_TRACE_LOG, CHAT_TRACE_QUERIES, LOG_TRACEBACKS, TRUSTED_PROXY, TRUSTED_PROXY_HOPS, ALLOWLIST_ENABLED, EDUPATH_DATA_DIR, HF_HUB_OFFLINE (container).

## 7. Open decisions and known issues

Decisions still open (need a human):
- **Reranker.** Keep FlashRank by default. bge (`RERANKER=bge-m3`) looked better on Arabic and English in the eval (not proven, p = 0.062 for English) at about +1.6 s per query on CPU; GPU not measured. Cheap alternative: plain vector order for Arabic (same 91%, no cost). Waiting on human grades.
- **Arabic model.** `aya-expanse:8b` and `qwen2.5:7b` were suggested, never run. Arabic quiz validity (52% of replies) is the biggest quality lever left.
- **Human review of Arabic quality.** The review sheet exists (`eval/`), no grades are in; the Arabic eval questions were model-written. Do not present Arabic quality as measured.
- **Merging** `pilot/readiness` and `pilot/final-checks`, and whether the pilot starts with `ALLOWLIST_ENABLED=true` (recommended).

Still unverified:
- A full chat and quiz through the running container against the real database; `alembic upgrade head` on the real Postgres (a throwaway copy of the old schema was used).
- Google sign-in end to end (browser checks used a minted cookie; the allowlist was tested with a faked Google reply).
- The scheduled-backup task firing on the owner's machine; Linux `host-gateway` and `OLLAMA_HOST=0.0.0.0`; a cold bge-m3 download in the container.
- Phone layout, Safari, Firefox; several students at once on one GPU; bge on GPU.

Defects noted, not fixed:
- A valid-but-wrong citation (retrieved page that does not support the claim) cannot be detected.
- The model still invents 16-22% of citations; the app hides them rather than preventing them (a prompt change needs evidence first).
- Foreign-script and weak items still appear in Arabic quizzes (near-identical options, fragments as questions).
- Follow-up retrieval depends on the rewrite model.
- Rate limits are in memory per process (Redis if more than one worker). Tokens issued before the logout change cannot be revoked.
- The thumbs-down comment form is cramped inside an RTL bubble (works, looks rough).
- Stale Chroma client retry covers two read paths; ingestion Ollama errors are warnings only.
- A local Postgres password was printed once in a Claude Code session (via `docker compose config`). Rotate it if that transcript was stored or shared; never run that command.

## 8. Next level (candidate directions, not decided)
- Multi-university support (today: one university, year 5 IT; the schema already has universities and years).
- An admin UI for ingestion and the allowlist (today: CLI scripts).
- Redis rate limits and a multi-worker deployment.
- More subjects and lectures; re-measure retrieval per subject.
- Mobile layout and other browsers.
- Evaluation with real student feedback: use the thumbs CSV and trace logs as the next eval set, then revisit reranker and model.
- Native-speaker review of Arabic answers and of the Arabic eval questions.

## 9. How to run (Windows, PowerShell, project root)

```
conda activate edupath_ai_v4
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
cd frontend; npm ci; npm test; npm run build; cd ..
alembic upgrade head
uvicorn app.main:app           # wait for the "model warm-up finished" log line
cd frontend; npm run dev       # http://localhost:5173
```
Container:
```
docker build -t edupath-api:latest .
docker compose --profile app up -d api
# then open http://localhost:8000/health (stop uvicorn first so port 8000 is free)
docker compose logs api        # if it does not start
```
Opt-in Postgres tests: start a throwaway `postgres:16` container publishing 5432 and run `EDUPATH_TEST_PG_CONTAINER=<name> pytest`; remove it afterwards. DOM tests: `cd frontend; npm run test:ui`.
Debug "no sources" cases: set `CHAT_TRACE_LOG=true`, restart, reproduce, and read the two `chat-trace` lines (retrieve and reply).

## 10. Working rules that worked with Claude Code

- One branch per task, one commit per item, tests for every behaviour change, and a mutation check.
- Measure before changing; revert anything inside run-to-run noise. Several of my own suggestions were disproved this way, and that was the right outcome.
- Never fabricate numbers. Say what could not be verified.
- Never touch Ollama's configuration, unload or restart models, or start a second Ollama instance without asking. Postgres checks use a throwaway container, never the real one.
- Keep prompts unchanged unless evidence on the eval set supports the change (`tests/test_chat_prompt.py` pins the current prompt).
- Use throwaway SQLite or copies of data/ for live checks. Do not print `.env`. Leave unrelated files (`.claude/settings.json`, `graphify-out/`) alone.
- Never share `eval/review_key.json` with a reviewer.

## 11. Starter prompts for the next sessions

Claude Code: "Read docs/HANDOFF.md and docs/PILOT.md first. Start from `pilot/final-checks` (or `main` once merged), run `pytest` and `npm test`, report the counts, then do <the next item> on a new branch, following the rules in section 10."

Claude chat: paste this file and say what you want to do next (for example: plan the pilot start, interpret feedback, decide on the reranker or model).
