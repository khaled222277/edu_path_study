# Quiz generation measurements

Raw results from `scripts/bench_quiz.py`. Every file holds, per quiz and per model
call, the timings, tokens, outcome and rejection reasons, so any table can be
recomputed:

```bash
python scripts/bench_quiz.py --report  bench_results/quiz_baseline.json
python scripts/bench_quiz.py --compare bench_results/quiz_t050.json bench_results/quiz_t030.json
```

Model `llama3.1:8b` on an RTX 5060 Laptop GPU (8 GB), Ollama 0.40.7, subjects 7 and 6
alternating, 5 questions per quiz.

| File | What it is | Quizzes per language | Chunk seeds |
| --- | --- | --- | --- |
| `quiz_baseline.json` | Step 0, behaviour before any change | 10 | 1000-1009 |
| `quiz_structured.json` | Ollama `format` JSON schema on every call | 10 | 1000-1009 |
| `quiz_feedback.json` | on a retry, tell the model why it was rejected | 10 | 1000-1009 |
| `quiz_structured_feedback.json` | both | 10 | 1000-1009 |
| `quiz_t050.json` `quiz_t030.json` `quiz_t015.json` | temperature 0.5 / 0.3 / 0.15, **interleaved** | 12 | 1000-1011 |
| `quiz_fp0.json` `quiz_fp2.json` | first-pass budget off / 2, **interleaved** | 12 | 5000-5011 (held out) |

## Read this before comparing seconds

The laptop throttled during the `structured_feedback` run and part of the `feedback`
run (a single call took 5-6 s instead of 2 s, in *English* too, where the prompts were
unchanged). **Compare call counts and acceptance rates across the sequential runs, not
seconds.** The interleaved runs (`t0xx`, `fp*`) alternate the variants quiz by quiz,
so any slowdown hits them equally and their seconds are comparable.

Run-to-run noise is large: the same configuration on the same chunks gave 1.64
(`baseline`) and 2.28 (`t050`) Arabic calls per question. Per-quiz standard deviation
is about 0.8 on a mean of 2.0. Differences under roughly 20% between two runs of this
size are not detectable.

## What was tried, and what happened (Arabic unless noted)

| Option | Result | Kept? |
| --- | --- | --- |
| Structured output (JSON schema) | Removed malformed JSON, bad item shape and wrong option count entirely, but content got worse (Latin leakage 13 -> 23, duplicates 7 -> 12, English figure references 1 -> 8): **+10% calls per question** | no |
| Feedback on retry | 1.64 -> 1.86 calls per question; on the same hard chunks retries were accepted 17/30 with feedback vs 18/30 without (the model does not ramble: 92 vs 97 tokens) | no |
| Temperature 0.3 | -8% calls, inside the noise; duplicates 11 -> 22 | no |
| Temperature 0.15 | +17% calls; duplicates 11 -> 31, wrong option count 0 -> 8 | no |
| **First-pass budget of 2 attempts per chunk** | **-11% calls, -15% mean / -26% p90 quiz time** on held-out chunks; questions accepted via the relaxed rules 1 -> 0 | **yes** (`QUIZ_FIRST_PASS_ATTEMPTS=2`) |

Why the first-pass budget: per-call acceptance on pooled default-config data (110 Arabic
chunks) was 68% for a fresh chunk, 46% on a chunk's 2nd strict attempt, 32% on its 3rd and
15% on attempts 4-8, and 25% of all calls were spent beyond a chunk's 3rd attempt. Giving
up on a chunk early and trying the next one costs less than persisting. Hard chunks are
revisited, with the rest of their budget and then the relaxed rules, only if the quiz is
still short, so no validation rule is loosened. Lowering `QUIZ_STRICT_ATTEMPTS` instead
would have pushed hard chunks into the relaxed phase, which tolerates Latin script.

The structured-output and feedback code was removed after measurement. To repeat those
experiments, pass `format=<schema for a list of one {question, options[4], answer}>` to
`ollama.generate`, or append "Your previous reply was rejected. Fix this: ..." to the
prompt on retries.
