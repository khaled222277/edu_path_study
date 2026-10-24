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
