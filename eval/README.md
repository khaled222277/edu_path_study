# Chat prompt evaluation

A fixed set of 20 questions for judging changes to how the chat prompt is built, and the
tooling to compare two versions of it on the real model.

```bash
python scripts/eval_chat_prompt.py                                   # all 20, two seeds
python scripts/eval_chat_prompt.py --only q18 q19 q20 --seeds 44 45 46 47 48 49
python scripts/eval_chat_prompt.py --report eval/results/duplicate_question.json --markdown out.md
```

`chat_questions.json` holds the questions (English and Arabic; narrow, broad, two that the
material cannot answer, and two multi-turn follow-ups). The topics were checked to occur in the
ingested lectures. The script retrieves chunks once per question so retrieval cannot differ,
builds the prompt both ways, asks the real chat model, and records the answer, the exact
prompt and answer token counts (Ollama's prompt cache is reset before every call, otherwise
it hides most of a repeated system prompt) and a few automatic checks: citation present,
citation names a source it was given, answer in the question's language, "not found" exactly
when it should be, expected terms mentioned. The checks only catch the obvious; read the answers.

## The question that was asked, and the answer

Both chat routes send the student's question twice: as a bare history turn (the session
already holds it when the prompt is built) and again inside the context block. Is the first
copy waste?

**No. Removing it saves 1.5% of the prompt and makes some answers worse, so it was not merged.**

- **Tokens.** 15,824 -> 15,592 over the 20 prompts: about 12 tokens each (the duplicated text is
  only the question). Answer time did not change (80 s for 40 answers either way).
- **Over all 20 questions x 2 seeds** the automatic checks are indistinguishable: citations 29/36
  vs 29/36, language 39/40 vs 38/40, "not found" handled correctly 37/40 vs 38/40, expected
  terms 30/36 vs 29/36. One-answer differences in both directions are noise.
- **But three cases moved consistently** when re-run with eight seeds each
  (`results/duplicate_question_followup_subset.md`):

| Case, 8 seeds | duplicate kept (today) | duplicate removed |
| --- | --- | --- |
| q18 - Arabic question the material cannot answer: replies in Arabic | **8/8** | **0/8** (always English) |
| q19 - English follow-up "explain that again": stays on the previous topic | **8/8** | **4/8** (drifts to an unrelated chunk) |
| q20 - Arabic follow-up "can you simplify that?": stays on topic | **8/8** | **6/8** |
| q06 - narrow factual question: answers instead of "not found" | 2/8 | **7/8** (improves) |

What the bare turn is doing: it **anchors a follow-up to the answer it refers to** (without it
the long context block, which was retrieved from the follow-up alone and so is unrelated,
becomes the whole conversation), and it **tells the model which language to reply in** (the
context is English; the Arabic question sits at its very end). For narrow factual questions it
sometimes makes the model more hesitant, which is why q06 got *better* without it. It is a
trade-off, not a free saving.

`tests/test_chat_prompt.py` pins the current shape and explains why, so a future change has to
be deliberate and re-run this evaluation.

## Limits of this evidence

20 questions, one model (`llama3.1:8b`), temperature 0.2 with fixed seeds. The aggregate is
too small to detect small effects; the three cases above are not small. The automatic checks
are crude (e.g. "expected terms" misses valid paraphrases).

## What this suggests trying instead

- Follow-ups retrieve from the follow-up text alone ("explain that again"), which returns
  unrelated chunks in both variants. Retrieving with the previous question as well would fix the
  root cause (retrieval is out of scope here).
- The "not found" check is an English substring. An Arabic refusal ("لم يتم ذكر ...") does not
  contain it, so the sources are still shown under it. Matching on a language-neutral signal
  would be better.
- To save the tokens without losing the anchor, try moving the question to the *end* of the
  context block only (and keeping the bare turn), or shortening the system prompt (it is most
  of the 700 prompt tokens). Use this harness to check any such change.

## Follow-up questions (retrieval)

"Can you explain that again in simpler words?" retrieved *nothing about the topic* (the topic is
in the previous turn): the top chunks were three "This is a logo" figure descriptions. Searching
with the previous question added fixes that but breaks genuine topic changes, so the options
were measured (`python scripts/eval_followups.py`, 12 follow-ups and 10 topic changes in
`followups.json`; "hit" = one of the top 3 chunks is about the right topic):

| Query used for the search | follow-ups | topic changes | right chunks of 3 (follow-up / topic change) |
| --- | --- | --- | --- |
| the question as asked (before) | 4/12 | 9/10 | 0.50 / 2.50 |
| previous question + question | 12/12 | **5/10** | 2.25 / 0.70 |
| question, question, previous (weighted) | 11/12 | **3/10** | 2.33 / 0.70 |
| both retrievals interleaved | 9/12 | 9/10 | 1.08 / 1.80 |
| **model rewrites it as a standalone question (shipped)** | **12/12** | **9/10** | **2.42 / 2.30** |

Retrieval confidence does not separate the two kinds (best cosine distance 0.33-0.57 for
follow-ups, 0.23-0.57 for topic changes; the reranker scores everything ~0.99) and neither does
question length, so a rule cannot decide when to blend. The model can: it rewrites a dependent
question and returns an independent one unchanged. The rewrite is used only as the search query;
the answer prompt still contains the student's own words. It costs one short call on the loaded
model (0.35 s mean, 0.49 s max, 20 s timeout) per turn that has a previous question, and any
failure falls back to the question as asked. `CHAT_FOLLOWUP_REWRITE=false` turns it off.

Limits: 22 cases and a coarse "right topic" check, not gold chunks (that is `qa_set.json`). The
rewrite sometimes adds context that was not asked for (t06 "...in the context of bagging"); it
still retrieved correctly here. t05 ("What is a dendrogram?") misses under every strategy, so it
is a retrieval problem independent of this change.

## The refusal rule (language-neutral "not covered" signal)

The model used to be told to refuse with an English sentence ("not found in the lecture
material") and the app detected that sentence to hide the sources under a refusal. In Arabic
the model refused in Arabic, nothing matched, and the sources stayed. Now it is asked to begin
a refusal with a fixed token, `<<NOT_IN_MATERIAL>>`, which the app detects and strips before
the reply is shown or stored (`app/services/refusal.py`). The old English phrase is still
honoured.

`python scripts/eval_chat_prompt.py --vary refusal --after-rule <wording>` compares it with the
old rule on the 20 questions (3 seeds). Three wordings of the new rule were tried:

| Over 20 questions x 3 seeds | legacy rule (before) | "in the student's language" | **"same language as the question" (shipped)** | no language clause |
| --- | --- | --- | --- | --- |
| out-of-scope refusals recognised (6 asked) | 3/6 (never the Arabic one) | 6/6 | **6/6** | 6/6 |
| answerable questions wrongly refused (54) | 2/54 | 2/54 | 3/54 | 2/54 |
| answer in the question's language (60) | 58/60 | **55/60** | **60/60** | 59/60 |
| answerable replies carrying a citation (54) | 45/54 | 48/54 | 51/54 | 48/54 |

The first wording made the model answer the English question q08 in Arabic on every seed, so it
was not shipped; the wording that was shipped is the one in the third column
(`results/refusal_rule_shipped.md` has the answers side by side). The false-refusal difference
(2 vs 3 of 54) is one answer, within noise. These are 3 seeds on 20 questions: enough to catch
the Arabic refusal bug and the q08 language flip, not enough to rank small differences.

Note that `results/duplicate_question*.json` were produced with the previous (English-phrase)
prompt, before this change; re-running that experiment now would use the token prompt.

# Quality evaluation (answers and quizzes, Arabic first)

`qa_set.json` holds 45 questions (27 Arabic, 18 English) over the ingested lectures: narrow,
broad, follow-up, figure and 7 out-of-scope. Each has gold pages (`file` + `page`, the unit the
app cites) and a one-line expected answer; 11 are marked `uncertain` with a reason. **The Arabic
wording was written by a model and has not been checked by a native speaker.** Gold was chosen by
reading the chunks the app's own retrieval proposed, so it is biased towards pages the retriever
can find (one miss was added by hand); the file says so.

```bash
python scripts/eval_quality.py retrieval --label mine                      # gold-page recall, no chat model
python scripts/eval_quality.py chat --label mine --seeds 42 43 44          # answers + automatic checks
python scripts/eval_quality.py quiz --label mine --quizzes 6               # quiz validity / duplicates / support
python scripts/eval_quality.py compare eval/results/A.json eval/results/B.json
```

Useful options: `--top-k`, `--candidate-k`, `--min-confidence` (the reranker gate),
`--model` (chat/quiz model tag, must already be pulled), `--reuse-queries FILE` (search with the
exact text an earlier run used, so two configurations differ only in the setting under test) and
`--chroma-path DIR` (search a separate store). Results are written to `results/quality_*.json`.

The automatic metrics are **signals, not judgements**: nothing here can say whether an Arabic
answer reads well or is correct. That is what the review sheet is for.

## Results so far (automatic metrics only)

Retrieval, gold page present, over 23 Arabic and 15 English in-scope questions. This is
deterministic given the queries (4 repeats of the same configuration gave identical rows; across
two separate runs one Arabic question flipped on a ~0.0001 difference in the FlashRank score).
One question is 4% of Arabic, so differences of one question are noise.

| Configuration | AR in vector top-3 | AR after rerank top-3 | **AR in the prompt** | **EN in the prompt** |
| --- | --- | --- | --- | --- |
| FlashRank, top 3 of 10 (**today**) | 91 | 61 | **74** | **93** |
| FlashRank, top 5 of 10 | 91 | 61 | 91 | 93 |
| FlashRank, top 6 of 10 | 91 | 61 | 96 | 93 |
| FlashRank, top 3 of 20 | 91 | 52 | 65 | 80 |
| FlashRank, top 5 / 6 of 20 | 91 | 52 / 48 | 78 / 83 | 93 / 93 |
| FlashRank, confidence gate 0.5 / 0.99 (0.1 today) | 91 | 61 | 74 / 83 | 93 / 93 |
| no reranker (vector order), top 3 / 5 | 91 | - | 91 / 91 | 93 / 100 |
| **bge-reranker-v2-m3**, top 3 of 10 | 91 | **91** | **91** | **100** |
| bge, top 5 / 6 of 10 | 91 | 91 | 91 / 96 | 100 / 100 |
| bge, top 3 / 5 of 20 | 91 | 91 | 91 / 91 | 100 / 100 |

Reading it: the embedding search finds the gold page in its top 3 for 91% of Arabic questions, and
**FlashRank then throws some of them away** (74% reach the prompt). The multilingual reranker does
not; neither does simply keeping vector order. Paired against today's setting on the same queries,
bge gained 5 questions and lost none (4 Arabic, 1 English; exact sign test p = 0.062 overall,
0.125 for Arabic): encouraging, not conclusive on 38 questions. Widening to 20 candidates makes
FlashRank worse. Raising `TOP_K` helps FlashRank only by putting more text in the prompt.

Cost of the bge reranker, CPU, idle machine (14 queries): load 4 s vs 0.5 s; **1.8 s per query
for 10 candidates vs 0.2 s** (4.2 s vs 0.6 s for 20); about 0.6 GB more RAM once in use (process
3.8 GB vs 3.2 GB). It adds ~1.6 s before the first token. Weights are 2.27 GB in fp32.

Answers, 45 questions x 3 seeds (mean ± sd over seeds, %); the gold page is a property of the
retrieval so its sd is 0:

| Arabic | today | bge top 3 | bge top 5 | FlashRank top 5 | vector order |
| --- | --- | --- | --- | --- | --- |
| gold page in the prompt | 73.9 | **91.3** | 91.3 | 91.3 | 91.3 |
| in-scope wrongly refused | 5.8 ±2.5 | **0.0** | 1.4 ±2.5 | 2.9 ±2.5 | 2.9 ±2.5 |
| citations naming a retrieved page | 77.1 ±14.3 | **91.7 ±9.4** | 79.4 ±6.5 | 75.6 ±6.4 | 89.2 ±5.1 |
| citations naming an existing page | 77.1 ±14.3 | **94.0 ±7.8** | 81.4 ±5.2 | 75.6 ±6.4 | 90.6 ±2.7 |
| answers with a non-lecture Latin word | 24.7 ±5.7 | 19.8 ±2.1 | 23.5 ±5.7 | 24.7 ±7.7 | 21.0 ±11.9 |

| English | today | bge top 3 | bge top 5 | FlashRank top 5 | vector order |
| --- | --- | --- | --- | --- | --- |
| gold page in the prompt | 93.3 | **100** | 100 | 93.3 | 93.3 |
| in-scope wrongly refused | 6.7 | **0.0** | 0.0 | 6.7 | 0.0 |
| citations naming a retrieved page | 86.3 ±4.0 | **94.2 ±2.4** | 94.0 ±5.2 | 92.9 ±1.1 | 92.9 ±3.3 |
| citations naming an existing page | 90.3 ±2.1 | **97.6 ±2.1** | 97.5 ±2.4 | 96.0 ±1.3 | 94.1 ±1.9 |

Unchanged in every variant: answer language correct 100%, out-of-scope questions refused 100%,
Arabic Latin-letter share about 10% (mostly lecture terms such as "k-means"). **In the baseline,
10-23% of inline citations name a page that does not exist** (the model invents page numbers);
better retrieval reduces that but does not remove it.

Chunk size, rebuilt into separate stores (`data/chroma_experiments/`, gitignored; the real
collection was only read; rebuilt at 800/100 it reproduced the real 468 text chunks exactly):

| chunk size / overlap | AR in the prompt (FlashRank / bge) | EN in the prompt (FlashRank / bge) | prompt context |
| --- | --- | --- | --- |
| 400 / 50 | 65 / 83 | 73 / 93 | ~1,100 chars (half) |
| **800 / 100 (shipped)** | **74 / 91** | **93 / 100** | ~1,850 |
| 800 / 200 | 70 / 87 | 87 / 100 | ~1,880 |
| 1200 / 150 | 61 / 74 | 73 / 73 | ~2,700 |

The shipped size is the best of those tested. 400/50 gives half the context at top 3; with top 6
(equal context) it reached 87% / 87% with FlashRank and 100% / 100% with bge at the retrieval
level, **but this was not tested at the answer level and would need a re-ingest. Deferred**, see
below.

Quiz quality (6 quizzes per language, baseline): English 81% of replies valid, Arabic 52%; 30/30
questions produced in both; no repeated questions within a quiz; the correct option is the one
closest to its source chunk 43% of the time in both languages (25% would be chance; this check is
approximate and cross-lingual, so it can only flag, not certify).

## What is decided, and what is not

**No default was changed by this evaluation.** The evidence favours the bge reranker for
retrieval and for citation accuracy, but (a) 38 questions cannot make the retrieval result
significant, (b) it costs ~1.6 s per question, and (c) nobody has read the answers yet. To try it:
`RERANKER=bge-m3` in `.env`. Decide after the human review below.

## Human review (the real test)

`review_sheet.csv` (open it in Excel or LibreOffice; it has a UTF-8 BOM so Arabic displays
correctly) and `review_sheet.html` (the same items for easy reading) hold 60 items in random
order: 30 chat answers (15 questions, each answered once by the **current defaults** and once
with the **bge reranker**, mixed together, same seed) and 30 quiz items (15 Arabic, 15 English).
The reviewer cannot tell which variant produced an answer. **Do not give the reviewer
`review_key.json`**; it maps each id to its variant.

Fill in the last four columns of each row:

| Column | Chat answer | Quiz item |
| --- | --- | --- |
| `correct_0_1_2` | 0 wrong or misleading, 1 partly right / incomplete, 2 correct | is the option marked as correct really correct for the sources shown? 0 no, 1 arguable, 2 yes |
| `grounded_y_n` | `y` if every claim is supported by the sources shown, else `n` | `y` if the sources shown contain what is needed to answer |
| `arabic_quality_1_5` | Arabic rows only: 1 unreadable ... 5 natural, correct Arabic (grammar, terms, no stray foreign words). Leave blank for English | Arabic rows only, grading the question and options |
| `notes` | anything odd | e.g. ambiguous question, wrong distractor, two right answers |

Save as `eval/review_graded.csv` (keep it CSV, UTF-8) and run:

```bash
python scripts/eval_quality.py grades --graded eval/review_graded.csv
```

It joins the grades to the hidden key and prints, per variant, language and type: items graded,
mean `correct` (0-2), % fully correct, % grounded, and mean Arabic quality. Blank cells are
ignored, and a mistyped grade is skipped rather than fatal. With 7-8 paired answers per language
only large differences will show; grade everything and read the notes.

Regenerate or enlarge the sheet (different seed, other result files):

```bash
python scripts/eval_quality.py review-sheet --chat eval/results/quality_chat_baseline.json \
    eval/results/quality_chat_bge_k3_c10.json --quiz eval/results/quality_quiz_baseline.json --seed 8
```

With two or more chat files the same questions are used for every variant (one seed each).
Quiz generation does not use the reranker, so all quiz items come from the baseline run.

## Deferred, and for you to run

- **400-character chunks, top 6, bge, answer level.** The store was built locally
  (`python scripts/build_chunk_experiment.py --chunk-size 400 --chunk-overlap 50`); adopting it
  for real needs a re-ingest of every lecture. To measure answers first (about 15 minutes):
  ```bash
  RERANKER=bge-m3 python scripts/eval_quality.py chat --label chunk400_bge_k6 \
      --reuse-queries eval/results/quality_retrieval_baseline.json --seeds 42 43 44 \
      --top-k 6 --candidate-k 10 --chroma-path data/chroma_experiments/c400_o50
  ```
- **bge reranker on the GPU.** Not measured. The venv has a CPU-only torch (`2.13.0+cpu`), and
  the model's fp32 weights (2.27 GB) next to the 5.3 GB chat model would leave almost nothing of
  an 8 GB card, which could evict the chat model. Check yourself: `nvidia-smi` with the chat model
  loaded (`ollama ps`), then a CUDA torch build and `RERANKER_DEVICE=cuda`; watch that `ollama ps`
  still says 100% GPU afterwards. Half precision would halve the memory but is not implemented.
- **Other chat/quiz models** (not pulled or loaded by me). Machine: RTX 5060 Laptop, 8 GB VRAM,
  31 GB RAM; installed: `llama3.1:8b` (4.9 GB), `qwen3-vl:4b`, `nomic-embed-text`. Plausible
  for Arabic at 4-bit on 8 GB (from general knowledge, not verified on this machine; check size
  and Arabic support on the Ollama library page first):
  ```bash
  ollama pull aya-expanse:8b     # Cohere, trained for 23 languages incl. Arabic
  ollama pull qwen2.5:7b         # strong multilingual 7B
  ollama pull qwen3:8b           # newer; may print <think> blocks that the quiz/chat parsers do not strip
  python scripts/eval_quality.py chat --model aya-expanse:8b --label aya \
      --reuse-queries eval/results/quality_retrieval_baseline.json --seeds 42 43 44
  python scripts/eval_quality.py quiz --model aya-expanse:8b --label aya
  ```
  Asking Ollama for a different model makes it swap models in VRAM, which briefly unloads the
  chat model; do it while nobody is using the app. Compare with `compare`, then add the new
  chat files to a fresh `review-sheet` for a blind comparison.

## Chat bugs: "repeat that in Arabic" and foreign characters (branch `fix/chat-transform-and-script-leak`)

Two observed bugs (subject KDD): an Arabic answer with a Chinese character and U+FFFD in it, and
a follow-up "okay i can't get it could repeat all that in arabic" answered with a logo and a DNA
figure. All numbers below are from `llama3.1:8b`, temperature 0.2, seeds 41-52 (conversations) or
41-43 (eval sets), on a throwaway SQLite database and a **copy** of `data/chroma_db`. Raw results:
`eval/results/chat_bugs/`. Scripts: `scripts/repro_followup.py`, `eval_transform.py`,
`eval_leakage.py`, `eval_figures.py` (all via `scripts/chat_sandbox.py`, which refuses paths inside `data/`).

### Hypotheses for the logo/DNA answer

| | verdict | evidence |
| --- | --- | --- |
| (a) the rewrite gives a topic-less / wrong query | **sometimes** | With the exact two turns the rewrite was topical every time (24/24, `ما هو التصنيف`). Over 35 previous questions + "repeat all that in arabic": 14/35 rewrites carry "in Arabic" into the search and a few drift (e.g. "the winner of the 2018 World Cup"); 20/35 kept at least one figure chunk, 6/35 kept three. |
| (b) retrieval returns irrelevant figure chunks and nothing refuses | **yes, whenever the search is not topical** | Searching the follow-up as typed (rewrite off or failed): 35/35 cases and 24/24 conversations keep the same three chunks (RECAP logo on lec03 p2 and lec09 p2, DNA bases on lec02 p23); the model never refused (3 sources shown 24/24). With a working rewrite: 0/36 kept a figure. I could not see why the rewrite did not save the original session; that is not verifiable from here. |
| (c) the model repeats itself | **yes** | 7/36 answers to the follow-up repeated a paragraph (4/24 short first answer, 3/12 long). 0/96 ordinary eval answers did. |
| (d) "No Arabic text is present" is stored in figure chunks | **yes** | 108 of 423 figure-description chunks (26%) contain it, and "Arabic" in the follow-up matches it. All three chunks above contain it. |

Not reproduced: Turn 1 (`حسنا ما هو التصنيف`) had no foreign characters in 24/24 runs. The CJK/U+FFFD showed
up in the *follow-up* (1/24 on the short first answer, 7/12 on a long one), always in the same slot: "a
model (or function)" became `(أو 関係)` or `(أو 関�function)`.

### Transform requests skip retrieval (`app/services/transform_intent.py`)

A message is a transform request only if **every** word is request vocabulary (politeness, "that/it/all",
translate/repeat/simplify..., languages, "example/steps", "get/understand") and at least one word is a request
word. One topic word ("entropy", "NLP", "k-means") and it is an ordinary question. False-positive risk: a
real question made only of such words ("give an example", counted as a transform here) is answered from the
previous answer. False negatives cost nothing: the turn is searched as before. No model call is used for this.

`eval/transform_cases.json`: 20 transform cases (10 English, 10 Arabic question language) + 10 near-miss real
questions, 3 seeds. Before = `main`, after = this branch.

| | n | searched | stays on topic* | language as asked | no new sources | repeated paragraph |
| --- | --- | --- | --- | --- | --- | --- |
| transform, English | 36 | 100% -> 0% | 94% -> 92% | 92% -> 100% | 8% -> 100% | 3% -> 0% |
| transform, Arabic | 24 | 100% -> 0% | 88% -> 100% | 100% -> 100% | 12% -> 100% | 4% -> 0% |
| near-miss (real questions) | 30 | 100% -> 100% | 100% -> 100% | 100% -> 100% | still new sources, as they should | 0% -> 0% |

\* Coarse: the answer contains one of the previous topic's terms. The 3 flagged "after" rows are all t10,
where the model translated "entropy" as الارتفاع, so the term is missing but the content is the previous answer.
Before, 5 rows were really off topic (answers about the mean, dirty data, k-medoids) with unrelated sources shown.

### Foreign characters (`app/services/script_guard.py`)

Rate over the 32 single-turn eval questions of subject 7 (96 answers, 3 seeds), stored answers:

| | before | after |
| --- | --- | --- |
| Arabic questions | 4/57 answers (7.0%), 36 characters, all Cyrillic | 0/57 |
| English questions | 0/39 | 0/39 |

Leak events collected from all runs (22): 10 were foreign letters glued to Arabic (`الأтрибут`, Cyrillic for
"attribute"), 7 a foreign word alone, 5 foreign characters in front of a Latin term. Option (a), a stream filter,
was chosen over (b), regenerating: it costs nothing when there is no leak, and (b) cannot work with a streamed
answer without buffering all of it. Dropping only the foreign characters turned the first kind into a broken
fragment (`الأ`), so a word that mixes foreign and Arabic letters is dropped whole, any other word loses only
the foreign characters (`関�function` -> `function`). A foreign word standing alone leaves a gap such as
`(أو )`: 7 of 22 events. Compared with leaving the characters in: nothing the student sees is more broken than
before (the fragment case is the only one that got worse with character-only dropping, and it no longer exists),
but a sentence can lose a word. Only a word that contains Arabic or foreign letters is held back (until its end);
English streams token by token. Logs carry a count only.

### Figure descriptions (`FIGURE_CHUNKS=normal|demote|exclude`, default `normal`)

On questions that do not mention a figure/diagram/chart/image/picture (`scripts/eval_figures.py`):

| | normal | demote | exclude |
| --- | --- | --- | --- |
| figure share of kept chunks | 25% | 5% | 0% |
| "No Arabic text" chunks kept | 2-3% | 0% | 0% |
| gold page found (32 questions, all) | 23-24 | 23 | 23 |
| follow-up set, topic term found | 11/11, 8/9 | 11/11, 7/9 | 11/11, 8/9 |

Hit rates differ by at most one question, which is inside the run-to-run wobble seen on identical code (23
vs 24 hits on two runs of `normal`, then 23 five times in a row; cause not found). There is no measured gain in
finding the right page, one broad Arabic question (a14) loses a figure under both modes, so **the default is
unchanged**. The setting is there if the pilot shows figure noise; `demote` is the safer of the two.

### Repetition

After the transform change the observed turn repeated a paragraph 0/24 times (it was 7/36). Ordinary answers
0/96. No `repeat_penalty` change was tested: there is nothing left to improve at a measurable rate.

### Found, not fixed

- Latin-script leaks (`đại`, `afe` inside Arabic) pass a script filter by design.
- The model sometimes translates a term wrongly ("entropy" -> الارتفاع); needs a better Arabic model (deferred).
- The streamed text carries trailing newlines that the stored copy trims (identical otherwise; 13/96 rows).
- The Arabic phrasing of the transform cases was not checked by a native speaker.

## Quiz questions that cannot be answered (branch `fix/quiz-answerability`)

Observed: an Arabic KDD question "هذا الفكرة هي" ("this idea is") with four unrelated options, earlier "what is the
meaning of each cell in the model?" and a sentence fragment used as a question. `llama3.1:8b`, quiz temperature 0.5,
subject 7 (KDD), 20 quizzes x 5 questions per language and configuration, seeds 31000-31019, on a **copy** of
`data/chroma_db`. Raw results: `eval/results/quiz_quality/`. Tools: `scripts/eval_quiz_quality.py`
(`generate`, `report`, `eligibility`, `validator`, `modelcheck`, `compare`) and `scripts/quiz_checks.py`.

How items are scored: rules in `scripts/quiz_checks.py`, separate from the app's own validator so the validator is not
marking its own homework: **deictic** (this/that/the following/the figure/the table, هذا/هذه/الشكل/الجدول...), **fragment**
(no question form, or fewer than 3 words), **grammar** (masculine هذا before a -ة noun). These are signals; they cannot
judge Arabic quality or whether the marked answer is right, and the printed samples are for a human to read. Two rules
over-fire on English ("what does it mean if..." counts as a bare pointer: 4 of the 100 English items still flagged
after the changes are of that kind). Off-topic is only a proxy (embedding similarity of question + answer to the nearest
chunk, median 0.58 Arabic / 0.71 English) and no threshold is claimed.

### Does the hypothesis hold?

"A slide or figure description with little technical content" is **not confirmed**. Title, agenda and low-information
chunks produced 0 of 200 baseline items (only 3 of the 672 usable chunks have fewer than 25 real words, none is an
agenda). What the bad items come from is **figure descriptions**: 22 of the 32 bad Arabic and 16 of the 18 bad English
items (bad rate English 29% vs 4% for lecture text, Arabic 38% vs 24%), and fragments came from ordinary lecture text.
The exact observed question was not reproduced in 100 Arabic items (7 questions of 4 words or fewer, none like it).

### Before and after (per language; n = 100 items, 20 quizzes, per cell)

| | baseline | rules only | rules + figures off (new defaults) | figures off only |
| --- | --- | --- | --- | --- |
| self-contained, Arabic | 68% | 89% | **100%** | 82% |
| self-contained, English | 82% | 95% | **96%** | 95% |
| fragment, Arabic | 10% | 0% | 0% | 15% |
| questions produced per quiz | 5 / 5 | 5 / 5 | 5 / 5 | 5 / 5 |
| model-call validity (accepted/all), ar / en | 48% / 76% | 40% / 77% | 40% / 96% | 44% / 94% |
| seconds per quiz, ar / en | 17.6 / 9.0 | 21.4 / 9.2 | 22.4 / 8.0 | 20.1 / 7.7 |

Differences of a second or two per quiz are inside the run-to-run noise (single runs). Arabic takes more model calls
because more replies are rejected, which is the intended cost of rejecting rather than showing them.

### What shipped

- **Eligibility** (`QUIZ_MIN_CHUNK_CHARS`, `QUIZ_MIN_REAL_WORDS`, `QUIZ_MIN_ALPHA_SHARE`, `QUIZ_SKIP_BOILERPLATE_SLIDES`,
  `QUIZ_INCLUDE_FIGURES`): the first four remove only 6 more of 730 chunks and no agenda slide on this corpus (they are
  cheap insurance, on). `QUIZ_INCLUDE_FIGURES` **defaults to off** (it was on): it takes eligible chunks from 666 to 285,
  every lecture keeps 21-37 (`eligibility` prints the table), the round-robin still draws five lectures per quiz (4.95
  Arabic, 5.0 English), and a subject with few eligible chunks gives a shorter quiz, not an error (tested). The cost:
  content that exists only in a diagram is no longer quizzed (the regression lecture is 90 of 118 usable chunks figures).
  `QUIZ_INCLUDE_FIGURES=true` restores it.
- **Answerability rules** (`QUIZ_ANSWERABILITY_RULES`, on): reject a question that starts with or hinges on a
  demonstrative, points at a figure/table/code/"this concept"/"used here"/"in this context", has no question form, has
  fewer than 3 words, or (Arabic) puts masculine هذا before a -ة noun. Replayed over the 200 baseline items they reject
  23% of Arabic and 16% of English; over 61 hand-drafted known-good questions (`eval/qa_set.json`, `chat_questions.json`) the
  false-positive rate is 0/31 Arabic and 0/30 English (one Arabic false positive, "المذكورة في المحاضرات", was fixed
  before this number). The Arabic **feminine هذه before a masculine noun** check is deferred: broken plurals take هذه
  ("هذه الأساليب"), so it is not reliable as a cheap rule.
- **Model-based check** (`QUIZ_MODEL_CHECK`, **off**): one extra call (0.39 s Arabic, 0.31 s English per check) asking
  "standalone?" and "supported?". Replayed over the baseline: it rejects 62/100 Arabic and **96/100 English**, including plain
  good questions ("What is the primary goal of regression in a prediction task?"), and still accepts some the rules reject
  ("What is the purpose of the table in the given data?"). With this model it is not usable, so it stays off; a better
  prompt or model was not tried (out of scope).
- **Report this question**: `PUT/DELETE /quiz/{attempt}/items/{item}/report`, reasons `not_answerable | wrong_answer |
  unclear | other` (optional, no free text), 404 for another student's attempt, migration `e5a7c9b13d40`, the control on
  each question, `scripts/export_quiz_reports.py` (no student identity), and `delete_student.py` removes a student's reports.

### Limits

Single runs per configuration, 100 items each (a difference of 11 points at n = 100 is clear, 1 point is not). Answer
correctness (is the marked option right?) is not measured; several kept items have two defensible options. Arabic phrasing
quality is unjudged. Questions per quiz were 5 of 5 in every configuration, so "a short quiz" was only exercised by tests.
