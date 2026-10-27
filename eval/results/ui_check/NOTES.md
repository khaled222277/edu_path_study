# Browser check (2026-10-27)

Real Chrome (headless, driven over the DevTools protocol with Node's built-in WebSocket; no new
dependencies) against: the real frontend (Vite dev server), a throwaway SQLite API with one
made-up user (cookie minted locally, no Google sign-in), a COPY of `data/`, and the real host
Ollama (llama3.1:8b, not touched). Screenshots contain no secrets or personal data (made-up
user "UI Check"). The scripts are not in the repo.

## Seen in the browser

| Check | Result | Screenshot |
|---|---|---|
| Arabic answer: layout and direction | bubble resolves to RTL; each `[file.pdf, page N]` is one left-to-right unit, brackets and the final full stop on the correct side | `02_arabic_answer.png`, `03_...`, `04_...` |
| Citation placement, 11 hand-written cases (static page, real `MessageText` markup, app CSS) | plain text is wrong when a citation starts a line (whole paragraph turns left-to-right) and when it follows an Arabic comma (bracket split across lines); the isolate fixes both | `bidi_check.png`, `bidi_check_hard.png` |
| English answer + Sources | answer, one citation element, Sources list shown | `01_english_answer.png` |
| Sources after reload and reopening the session | Sources blocks present for both answers (they come from the stored list) | `04_...` |
| Thumbs up / down + comment | comment box opens on thumbs-down, rating and comment saved, pressed state shown, both survive a reload | `03_...`, `04_...` |
| Stop mid-answer | streaming ends, partial text kept, Arabic note "stopped, this answer was not saved", Send re-enabled | `05_after_stop.png` |
| 429 and 503 messages, English and Arabic | correct translated banner each time, typed question put back in the box | `06_429_en/ar.png`, `06_503_en/ar.png` |
| Logout, then the old cookie | page returns to sign-in; the old token gets 401 from `/auth/me` | `07_after_logout.png` |

## Bugs this found (unit tests could not)
1. `PUT`/`DELETE` were missing from the CORS allowed methods, so every thumb click failed in a browser. Fixed, `tests/test_cors.py`.
2. My new `.feedback` and `.cite` class names collided with existing Quiz styles (the first would have turned the quiz feedback box into a row). Renamed.
3. A citation could wrap in the middle of its brackets. It is now an inline-block unit.

## Not as it would be in production
- 429 and 503 were **simulated** by intercepting the request in the browser (Ollama was not made to fail, and the rate limiter was off). They show the UI's handling, not the server producing them.
- The broken figure thumbnail in the Sources screenshots is an artefact of the throwaway database (no lecture rows for the copied images).
- Sign-in through Google was not exercised; the cookie was minted.
- Only Chrome, one window size (about 1100 px wide). Not checked: phone layout, Safari/Firefox, RTL Sources list on a narrow screen.
- Layout nit seen: the thumbs-down comment form is cramped next to the thumbs in an RTL bubble (works, looks rough).
