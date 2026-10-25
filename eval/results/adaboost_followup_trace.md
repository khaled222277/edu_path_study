# "No sources" after a follow-up: investigation record

Sequence: "what is data mining", then "could you explain adaboost algo" in one KDD session (subject 7).
Real Chroma (read only), real llama3.1:8b, throwaway in-memory SQLite or a throwaway SQLite server. Not the user's Postgres.

| what was run | runs | rewritten query | chunks kept | refusal token | sources shown |
|---|---|---|---|---|---|
| controller, seeds fixed | 6 | "Could you explain the Adaboost algorithm?" 6/6 | lec08 pp 44, 37, 41 (reranked) 6/6 | 0 | 3 in 6/6 |
| controller, 30 unseeded-style seeds | 30 | same 30/30 | same | 0 (none mid-answer, no legacy phrase) | 3 in 30/30 |
| real HTTP stream route | 8 | (inside server) | 3 | - | early and final list: 3 in 8/8 |
| rewrite alone, 40 calls | 40 | same wording 40/40 | pp 44, 37, 41 | - | - |
| query left as typed (rewrite off) | 6 | "could you explain adaboost algo" | lec08 pp 44, 39, 41 | 0 | 3 in 6/6 |
| rewrite forced wrong (returns the previous question) | 6 | "what is data mining" | lec01 pp 7, 8, lec02 p2 | 6/6, one short sentence | 0 in 6/6 (short refusal, not an explanation) |

Without inline citations but with sources: 2/30 and 2/8 (the model sometimes does not write brackets; the Sources list is still shown).

Verdict: not reproduced. (a) bad rewrite, (c) validation emptying the list, (e) the "algo" typo: not it. (b) the model ignoring
chunks: not seen. (d) refusal token: only plausible mechanism left, a token or the phrase "not found in the lecture material"
appearing anywhere in an otherwise explained answer sets refused=True and withdraws the sources while the text stays. 0/44 here.
The frontend was also read: Chat.jsx keeps sources per message id and only clears them when a session is opened or created.

CHAT_TRACE_LOG=true now records which of these it was.
