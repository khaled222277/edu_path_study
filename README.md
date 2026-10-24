# EduPath Study V2

Multilingual (Arabic/English) study platform for university students.

- **Phase 1 (done)** — auth, onboarding, university/subject catalogue.
- **Phase 2 (done)** — admin lecture ingestion into Postgres + ChromaDB.
- **Phase 3 (done)** — grounded study chat over the lecture corpus.
- **Phase 4 (done)** — validated Arabic/English quiz generation.
- **Phase 5 (done)** — the student's own progress dashboard.

## Layout

```
app/
  models/       SQLAlchemy models
  schemas/      Pydantic request/response schemas
  controllers/  business logic
  routers/      thin FastAPI route definitions
  services/     vector_store.py (Chroma collection + bge-m3); RAG/LLM logic later
  auth/         Google OAuth flow + JWT issue/verify
  db/           session factory + Alembic migrations
  main.py
ingestion/      extract_text.py, chunking.py, images.py + the two CLIs
frontend/       React (Vite)
data/chroma_db/ persistent vector store
data/images/    extracted figures, one directory per lecture id
scripts/        seed script, image backfill
```

## Setup

### 1. Database

Postgres 16 runs in Docker:

```bash
docker compose up -d
```

Credentials live in `.env` (gitignored); `docker-compose.yml` reads them from there.

### 2. Backend

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

alembic upgrade head            # create tables
python scripts/seed.py          # one university + 3 year-5 IT subjects

uvicorn app.main:app --reload   # http://localhost:8000
```

Interactive API docs: <http://localhost:8000/docs>

### 3. Google OAuth

Create an OAuth 2.0 Client ID (type: Web application) at
<https://console.cloud.google.com/apis/credentials>, then:

- Add `http://localhost:8000/auth/google/callback` as an **Authorized redirect URI**
- Put the client ID and secret into `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`

`/auth/google/login` returns 503 until those two are set.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

## Ingestion (Phase 2, admin-only)

Students never upload anything. Lectures are ingested offline via CLI.

### Prerequisites

- **Ollama** running locally with a vision model: `ollama pull qwen3-vl:4b`.
  It reads slide labels far better than `llava:latest` — measured against two
  figures with hand-verified ground truth, qwen3-vl:4b recovered 9/9 and 6/6
  labels where llava managed 3/9 and 0/6.
- **Tesseract** on PATH, *with the Arabic language pack*. Check with
  `tesseract --list-langs`; if `ara` is absent, download `ara.traineddata` from
  <https://github.com/tesseract-ocr/tessdata> into your `tessdata/` directory
  (on Windows, `C:\Program Files\Tesseract-OCR\tessdata\`, needs admin).
  Without it, `-l ara+eng` fails with *"Failed loading language 'ara'"* and every
  scanned Arabic page errors.
- First run downloads **bge-m3** (~2.2 GB) into the HuggingFace cache.

### Add a subject

```bash
cd ingestion
python add_subject.py --university-id 1 --year 5 --name "Networking" --code "NET501"
```

### Ingest a lecture

```bash
# sanity-check extraction quality first -- writes nothing
python ingest_lecture.py --university-id 1 --subject-id 4 --file lecture.pdf --dry-run

# commit it
python ingest_lecture.py --university-id 1 --subject-id 4 --file lecture.pdf
```

Useful flags: `--skip-images` (skip figure description entirely), `--vision-model`,
`--ocr-min-alnum`, `--chunk-size` / `--chunk-overlap`, `--device cuda`.

Re-running on an unchanged file is a no-op: the file's SHA-256 is stored on the
`lectures` row and checked first.

### How it works

| Step | Detail |
| --- | --- |
| Extract | `extract_text.extract_pdf_pages()` — **one** pass; per-page native/OCR decision |
| OCR gate | off by default (`--ocr-min-alnum 0`) — see note below |
| Compose | Pages joined with `[Page N]` markers; report derived from the same pages |
| Chunk | `RecursiveCharacterTextSplitter`, 800 chars / 100 overlap |
| Page tracing | Markers stripped before splitting; page spans tracked by offset |
| Embed | `BAAI/bge-m3`, normalized, cosine — **no** `query:`/`passage:` prefix |
| Images | PyMuPDF extract → size/aspect filter → de-dup → **content/decorative classification** → save PNG → vision-model description |
| Store | One Chroma collection at `data/chroma_db/`; figure files under `data/images/` |

Every chunk carries `university_id`, `year`, `subject_id`, `lecture_id`,
`source_filename`, `page_number`, and `content_type` (`text` or
`image_description`) so Phase 3 retrieval can scope and cite. Image chunks also
carry `image_path` — `{lecture_id}/page_{n}_{i}.png`, relative to `data/images/`
— which is what lets a citation show the figure itself.

Image filtering drops anything under 150×150, under 3 KB, or outside a 0.2–5.0
aspect ratio — that removes logos, header bands, and vertical rules. Identical
images repeated across slides are described once.

### Decorative vs content classification

The size/aspect filter is about *shape*, so it cannot reject a 460×600 textbook
cover or a glossy branding badge — those look exactly like real figures by every
measurement available. Four reached students as citations from `kdd_lec01`
alone (the KDD monogram, a "data MINING" badge, and the Tan et al. and Han et
al. book covers).

Keyword heuristics were considered and rejected for the reason the OCR gate
taught: they do not generalise. A book-cover photograph matches no "logo"
vocabulary, and neither would a stock photo or generic clipart. Instead the
vision model is asked one cheap binary question per image — CONTENT or
DECORATIVE — *before* the expensive description is generated. Only CONTENT
images are described, saved, and indexed; DECORATIVE ones produce no chunk, no
PNG, and cannot be cited. Every rejection is logged with its page.

Two things to know if you touch this:

- **`num_predict` must be generous (300).** qwen3-vl reasons before answering
  and the reasoning is drawn from the same token budget as the reply. At 20
  tokens the budget went entirely to reasoning and `response` came back empty
  for *all* 14 probe images. `think=False` does not avoid this.
- **No verdict means keep.** A failed or unparseable classification leaves the
  image in. Losing one real diagram costs a student more than keeping one logo.

Disable per-run with `--skip-image-classification`.

### Pruning decoratives from an existing corpus

```bash
python scripts/prune_decorative_images.py --dry-run   # classify + report only
python scripts/prune_decorative_images.py             # delete chunks + PNGs
```

Classifies each *stored PNG* (the exact bytes being served, not a fresh
extraction), deletes the Chroma chunk and file for anything decorative, and
leaves everything classified CONTENT completely untouched — descriptions are
never regenerated. Removals are written to `data/decorative_pruned.txt` with
lecture, page, and the stored description, for auditing over-rejection.

Every image that survives the filter is written to
`data/images/{lecture_id}/page_{n}_{i}.png`, where `i` counts the kept images on
that page. Saving is downstream of filtering and independent of describing, so
the set of files on disk is exactly the set of images the pipeline chose to keep.

### Backfilling figures for older lectures

Lectures ingested before figures were persisted have descriptions in Chroma but
no files on disk. The backfill re-extracts them with the same filter and stamps
`image_path` onto the existing records — it never calls the vision model, so
reviewed descriptions are left untouched:

```bash
python scripts/backfill_lecture_images.py --dry-run   # report the matching
python scripts/backfill_lecture_images.py             # write files + metadata
```

Descriptions are matched to images **by page**, not by position: a failed
description leaves no record, and a figure re-described later is appended out of
order, so the id's trailing index is not a reliable pointer into the image list.
The script reports any page whose description and image counts disagree rather
than guessing.

## Figure images in chat

Serving is a route, not a `StaticFiles` mount: `GET
/lectures/images/{lecture_id}/{filename}` requires a signed-in student whose
university and year match the lecture's subject — the same rule chat and quiz
use. Filenames are matched against a strict pattern rather than sanitised, so
nothing outside `data/images/` is reachable.

Chat citations for `image_description` chunks carry an `image_url` alongside the
usual filename and page; the frontend renders it as a thumbnail under the
citation, click to enlarge. Text citations are unchanged and have no `image_url`.

### Why the OCR gate is off

OCR on a *pictorial* page (a figure slide with no text layer) returns symbol
soup. An alphanumeric-ratio filter was tried to drop it, but auditing all 11
pages it rejected showed it cannot work: noise scored 0.737–0.845 and **real
content scored 0.744–0.845** — fully overlapping. Code listings and data tables
are punctuation-dense, so they look exactly like noise. It was discarding the
PlayTennis training set, sklearn examples, and worked entropy problems.

Symbol soup ranks poorly in retrieval and is harmless; losing teaching material
is not. The mechanism is retained behind `--ocr-min-alnum` should a corpus ever
need it, but it defaults to off.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | liveness |
| GET | `/auth/google/login` | redirects to Google consent |
| GET | `/auth/google/callback` | upserts user, sets httpOnly JWT cookie, redirects to frontend |
| GET | `/auth/me` | current user + `onboarding_required`, else 401 |
| POST | `/auth/logout` | clears the cookie |
| POST | `/onboarding` | `{ university_id, year }` |
| GET | `/universities` | public, for the onboarding dropdown |
| GET | `/universities/{id}/subjects` | optional `?year=` filter |
| GET | `/subjects` | the signed-in student's own university + year |
| POST | `/chat/sessions` | start a session for `{subject_id}`; 403 if out of scope |
| GET | `/chat/sessions` | the student's sessions, most recent first |
| GET | `/chat/sessions/{id}` | full message history |
| POST | `/chat/sessions/{id}/messages` | ask a question, get a grounded answer + citations |
| POST | `/chat/sessions/{id}/messages/stream` | same turn, streamed as NDJSON events (below) |
| POST | `/quiz/start` | generate a quiz; answers withheld until answered |
| POST | `/quiz/start/stream` | same quiz, delivered question by question as NDJSON events (below) |
| POST | `/quiz/{id}/answer` | record an answer, reveal correctness |
| POST | `/quiz/{id}/complete` | close an attempt early |
| GET | `/quiz/{id}/results` | full review once complete |
| GET | `/quiz/history` | past attempts, most recent first |
| GET | `/dashboard/summary` | per-subject scores + overall readiness |
| GET | `/dashboard/activity` | merged chat/quiz feed, `?limit=` |

## Dashboard (Phase 5)

Scoped entirely to the signed-in student — the endpoints take no identifying
parameter, so every aggregate derives from the session cookie and there is
nothing to pass that would reach another student's data.

Three deliberate choices:

- **Overall readiness is a mean over subjects, not over attempts.** Drilling one
  subject twenty times cannot drown out the others. A student with 100% in one
  subject and 50% across four attempts in another reads as 75%, not 60%.
- **A subject with no completed quiz shows `null`, never 0%.** Zero implies a
  quiz was taken and failed; that is a different and misleading story. Such
  subjects are also excluded from readiness rather than dragging it to zero.
- **Unfinished attempts are excluded from every score but still shown.** They
  appear on the subject card as "unfinished" and in the activity feed timestamped
  when they were started, so an interrupted quiz never silently disappears. Each
  attempt appears in the feed exactly once — completed or in-progress, never both.

## Study chat (Phase 3)

`query → bge-m3 vector search (top 10, subject-scoped) → FlashRank rerank (top 3)
→ llama3.1:8b with a grounding prompt`

The model answers **only** from retrieved context, cites every claim as
`[file.pdf, page N]`, and replies "not found in the lecture material" rather
than falling back on general knowledge. It answers in whatever language the
student asked in. Figure descriptions are cited like any other source.

### Why reranking is confidence-gated

FlashRank's `ms-marco-MultiBERT-L-12` is strong on English but collapses on
cross-lingual pairs — an Arabic question against English lecture text scored
0.0005–0.0008 versus 0.69–0.999 for English, and its reorderings were wrong
(it demoted the correct "How to Handle Missing Data" chunk out of the top 5).
bge-m3 ranks those correctly by itself, so when the cross-encoder's top score
falls below `RERANK_MIN_CONFIDENCE` (0.1) the vector ordering is kept.

## Tests

The suite runs fully offline and in about a second: Postgres is replaced by
in-memory SQLite, and Ollama, ChromaDB and the reranker are faked. Any test that
would touch a real one fails with an explicit error instead.

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

| File | Covers |
| --- | --- |
| `test_quiz_validation.py` | Latin leakage in Arabic items, placeholder options, option count, duplicates |
| `test_quiz_service.py` | strict attempts, then relaxed attempts for Arabic only; Ollama outage aborts |
| `test_retrieval.py` | reranker skipped below 0.1 confidence, top-3 selection, subject scoping |
| `test_auth.py` | valid, expired, tampered, forged and missing session cookies |
| `test_authorization.py` | one student cannot read another's chat session, quiz or dashboard |
| `test_error_handling.py` | clean 503s when Ollama/Chroma fail; rate limiting |
| `test_config.py` | production startup refuses placeholder secrets and insecure cookies |
| `test_vector_store.py` | embedder built once under concurrency, stale-client recovery, startup warm-up |
| `test_quiz_trace.py` | quiz rejections are named, decisions unchanged, per-call timings and tokens |
| `test_quiz_options.py` | quiz settings; the two-pass chunk budget never loosens a rule |
| `test_chat_prompt.py` | pins the (deliberately duplicated) question in the chat prompt; see `eval/README.md` |
| `test_quiz_concurrency.py` | parallel chunks: bounded by what is still needed, ordering, cancellation, outages |
| `test_quiz_stream.py` | streamed quiz: event order, storage, partial results, Stop, answering mid-generation |
| `test_ingestion_summary.py` | skipped/unclassified figures reported by lecture and page |
| `test_chat_stream.py` | streaming route: event order, errors, auth, rate limit, disconnect cleanup, no thread starvation |
| `test_keep_alive.py` | model residency: keep-alive on every call, warm-up steps independent |
| `test_embed_device.py` | EMBED_DEVICE validation and fallback to cpu |

Frontend (pure logic, Node's built-in runner, no install needed):

```bash
cd frontend && npm test
```

The quiz screen's React wiring has an opt-in DOM test (`jsdom` + Testing Library, which are
deliberately not project dependencies):

```bash
cd frontend
npm install --no-save jsdom @testing-library/react @testing-library/dom
npm run test:ui
```

### Benchmark

```bash
python scripts/bench_chat.py --subject-id 7                   # needs Ollama + an ingested subject
python scripts/bench_chat.py --subject-id 7 --warm-up-first   # as the API behaves after boot
python scripts/bench_chat.py --subject-id 7 --cold-llm        # evict the chat model first
```

Times retrieval stage by stage (cold vs warm), then streamed chat turns: time to the
first token (what a student waits for before text appears) and the total. It never
writes to Postgres.

### Streaming quizzes

`POST /quiz/start/stream` takes the same body as `/quiz/start` and shares its rate-limit
allowance, but sends the quiz as it is written (`application/x-ndjson`):

| Event | Meaning |
| --- | --- |
| `{"type":"start","attempt_id","subject_id","language","requested"}` | the quiz exists; sent with the first question |
| `{"type":"question","index","question":{id,question_text,options,source_citation}}` | one per validated question |
| `{"type":"done","attempt_id","total_questions","requested","partial"}` | finished; `partial` if fewer than `requested` could be written |
| `{"type":"error","detail","attempt_id","total_questions","partial":true}` | broke off part-way; the questions already sent are kept |

Each question is stored the moment it validates, so the student can answer question 1
(`POST /quiz/{id}/answer`) while question 4 is still being written. While that happens
the attempt's `total_questions` is the number *requested*; it becomes the number actually
produced when the stream ends. A failure on one question never discards the earlier ones.
Failures before the first question (Ollama or Chroma down, no usable material) are the
same HTTP 503 / 422 as `/quiz/start`, and leave nothing behind. If the student stops or
disconnects, the quiz keeps the questions already sent; a quiz nobody received is deleted.

`/quiz/{id}/answer` and `/complete` take a row lock (`SELECT ... FOR UPDATE`) so they cannot
interleave with the end-of-stream bookkeeping. SQLite ignores `FOR UPDATE`, so that part is
exercised only on Postgres.

### Quiz speed and quality

`scripts/bench_quiz.py` measures quiz generation against the real Ollama and Chroma and
`bench_results/` holds the raw data and what each experiment concluded
(`bench_results/README.md`). The short version: model calls are 99.8% of quiz time; English
already passes on the first call 94% of the time; Arabic wastes about 40% of its calls. Tuning
knobs (`QUIZ_FIRST_PASS_ATTEMPTS`, `QUIZ_TEMPERATURE`, ...) are in `.env.example`.

`QUIZ_CONCURRENCY` (default 1) writes several questions at once, but only pays off if Ollama
is set to serve requests in parallel (`OLLAMA_NUM_PARALLEL=3`); against a default Ollama it
makes Arabic quizzes slower. Measurements and how to enable it: `bench_results/README.md`.

```bash
python scripts/bench_quiz.py --label mine --out bench_results/quiz_mine.json   # 10 quizzes per language
python scripts/bench_quiz.py --variants "base;fp0=quiz_first_pass_attempts:0"  # interleaved A/B
python scripts/bench_quiz.py --compare bench_results/quiz_baseline.json bench_results/quiz_mine.json
```

### Judging prompt changes

`eval/` holds a fixed set of 20 questions and `scripts/eval_chat_prompt.py`, which compares two versions of the chat
prompt on the real model (answers side by side, exact token counts, quality checks). It was used to decide
that the question sent twice in the prompt should stay: `eval/README.md`.

### Streaming answers

`POST /chat/sessions/{id}/messages/stream` takes the same body and enforces the same
auth, ownership and rate limit (one shared allowance with the plain route) but
answers as `application/x-ndjson`, one JSON object per line:

| Event | Meaning |
| --- | --- |
| `{"type":"citations","citations":[...]}` | sources, sent with the first token |
| `{"type":"token","text":"..."}` | a piece of the answer, in order |
| `{"type":"done","message":{...},"citations":[...]}` | stored message; `citations` here is authoritative (empty for a "not found" answer) |
| `{"type":"error","detail":"..."}` | the answer broke off part-way; nothing was stored |

Problems before the first token (Ollama or Chroma down) are ordinary HTTP errors,
exactly like the plain route. The question and answer are stored together, only
once the answer is complete: a failure or a client that disconnects leaves nothing
behind, and a disconnect closes the Ollama request so it stops generating.

Chat latency settings: `OLLAMA_KEEP_ALIVE` keeps the model loaded between students,
and the startup warm-up loads it before the first request. `EMBED_DEVICE` picks the
query-embedding device. See `.env.example`.

## Migrations

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```
