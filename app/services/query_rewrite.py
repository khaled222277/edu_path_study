"""Search for what a follow-up question MEANS, not for what it says.

"Can you explain that again in simpler words?" retrieves nothing about the topic, because
the topic is in the previous turn. Pasting the previous question into the query fixes
follow-ups but wrecks a genuine change of subject (measured: topic changes 9/10 -> 5/10),
and neither retrieval confidence nor question length can tell the two apart. So the model
is asked to rewrite the latest question as a standalone one, or return it unchanged if it
already is. The rewrite is used ONLY as the search query: the prompt the student's answer
is generated from still contains their own words (the answer prompt's shape matters; see
eval/README.md).

Every failure falls back to the original question, so this can make retrieval better but
never make a chat turn fail.
"""

import logging

from app.config import settings
from app.services.llm import LLM_ERRORS, get_client, keep_alive

log = logging.getLogger(__name__)

REWRITE_PROMPT = """You rewrite a student's latest question so it can be understood without the conversation.

Previous question: {previous}
Latest question: {question}

If the latest question depends on the previous one (it says "that", "it", "again", "an example", or is otherwise incomplete), rewrite it as one complete standalone question that names the topic. If it is already a complete question about its own topic, return it unchanged. Keep the language of the latest question. Output ONLY the question."""

# A rewrite longer than this is the model rambling, not a search query.
MAX_REWRITE_CHARS = 400
# This call sits in front of every follow-up, so it must not be allowed to hang.
REWRITE_TIMEOUT_SECONDS = 20.0
REWRITE_OPTIONS = {"temperature": 0, "num_predict": 80}


def _usable(rewritten: str) -> str | None:
    """The first line of the model's output if it looks like a question, else None."""
    lines = [line.strip().strip('"“”«»').strip() for line in rewritten.strip().splitlines()]
    first = next((line for line in lines if line), "")
    if not first or len(first) > MAX_REWRITE_CHARS:
        return None
    return first


def rewrite_followup(previous: str, question: str) -> str:
    """`question` as a standalone search query, or `question` itself if that fails."""
    try:
        response = get_client(REWRITE_TIMEOUT_SECONDS).chat(
            model=settings.chat_model,
            messages=[{"role": "user", "content": REWRITE_PROMPT.format(previous=previous, question=question)}],
            options=REWRITE_OPTIONS,
            keep_alive=keep_alive(),
        )
    except LLM_ERRORS:
        log.warning("follow-up rewrite unavailable; searching with the question as asked")
        return question
    except Exception:
        log.warning("follow-up rewrite failed; searching with the question as asked", exc_info=True)
        return question

    usable = _usable((response.get("message") or {}).get("content") or "")
    if usable is None:
        log.warning("follow-up rewrite was unusable; searching with the question as asked")
        return question
    log.debug("search query %r rewritten as %r", question, usable)
    return usable


def retrieval_query(previous_question: str | None, question: str) -> str:
    """What to search the lectures with for `question`, asked right after `previous_question`."""
    if not settings.chat_followup_rewrite or not previous_question or not previous_question.strip():
        return question
    return rewrite_followup(previous_question.strip(), question)
