// Pure logic for the streamed chat answer: no React, no fetch, so it is unit-testable.
//
// The server sends newline-delimited JSON (see app/controllers/chat_stream.py):
//   {"type":"citations","citations":[...]}   sources, sent with the first token
//   {"type":"token","text":"..."}            zero or more
//   {"type":"done","message":{...},"citations":[...]}   the stored message; authoritative
//   {"type":"error","detail":"..."}          instead of done, if it broke mid-answer

/**
 * Incremental NDJSON parser. `push` takes decoded text of any size -- a chunk can
 * end mid-line or even mid-JSON -- and returns the events completed so far.
 * `flush` returns a final line that had no trailing newline.
 */
export function createNdjsonParser() {
  let buffer = "";

  const parseLine = (line) => {
    const trimmed = line.trim(); // also drops the \r of a CRLF
    if (!trimmed) return null;
    try {
      return JSON.parse(trimmed);
    } catch {
      const err = new Error("Malformed stream event");
      err.kind = "interrupted";
      throw err;
    }
  };

  return {
    push(text) {
      buffer += text;
      const lines = buffer.split("\n");
      buffer = lines.pop(); // the unfinished line stays for the next chunk
      return lines.map(parseLine).filter((ev) => ev !== null);
    },
    flush() {
      const rest = buffer;
      buffer = "";
      const ev = parseLine(rest);
      return ev ? [ev] : [];
    },
  };
}

export const initialStreamState = () => ({
  started: false, // true once there is something to show (sources or text)
  text: "",
  citations: [],
  message: null, // the stored assistant message, once `done` arrives
  error: null, // detail of an `error` event
});

/** Fold one event into the state. Unknown event types are ignored. */
export function applyEvent(state, event) {
  switch (event?.type) {
    case "citations":
      return { ...state, started: true, citations: event.citations ?? [] };
    case "token":
      return { ...state, started: true, text: state.text + (event.text ?? "") };
    case "done":
      // The stored text is what a reload would show, so make the screen match it
      // (the server trims it), and take its citations: they are empty when the
      // answer was "not found in the lecture material".
      return {
        ...state,
        started: true,
        text: event.message?.content ?? state.text,
        citations: event.citations ?? state.citations,
        message: event.message ?? null,
      };
    case "error":
      return { ...state, error: event.detail ?? "error" };
    default:
      return state;
  }
}

/**
 * How a stream that has ended turned out: "done", "error" (the server said it
 * failed), or "interrupted" (the connection closed with neither -- a crash).
 */
export function streamOutcome(state) {
  if (state.message) return "done";
  if (state.error) return "error";
  return "interrupted";
}

/**
 * What to do when an answer ends without completing.
 *
 * `error` is whatever the request threw, or null when the stream simply closed
 * with neither `done` nor `error`. `state` is how much of the answer arrived.
 *
 *   keepPartial  leave the question and the partial answer on screen, with a note
 *   !keepPartial take the attempt off the screen and hand the question text back
 *   banner       also show the message in the error banner (never for Stop: the
 *                student asked for it)
 *   cause        what to translate into the message: { kind: "stopped" } for an
 *                abort, the error itself, or { kind: "interrupted" }
 *   tone         "muted" for Stop, "error" for anything that broke
 */
export function failurePlan(error, state) {
  const aborted = error?.name === "AbortError";
  const cause = aborted ? { kind: "stopped" } : (error ?? { kind: "interrupted" });
  if (state.text) {
    return { keepPartial: true, banner: false, cause, tone: aborted ? "muted" : "error" };
  }
  return { keepPartial: false, banner: !aborted, cause, tone: null };
}

/**
 * The messages list with the in-flight answer inserted or updated in place.
 * `extra` carries flags such as { note } for an answer that stopped early.
 */
export function upsertAnswer(messages, id, state, extra = {}) {
  const answer = {
    id: state.message?.id ?? id,
    role: "assistant",
    content: state.text,
    created_at: state.message?.created_at,
    streaming: !state.message && !state.error && !extra.note,
    ...extra,
  };
  const i = messages.findIndex((m) => m.id === id);
  if (i === -1) return [...messages, answer];
  const next = messages.slice();
  next[i] = answer;
  return next;
}

// --- a quiz written question by question -----------------------------------------
//
// Events (see app/controllers/quiz_stream.py):
//   {"type":"start","attempt_id","subject_id","language","requested"}
//   {"type":"question","index","question":{id,question_text,options,source_citation}}
//   {"type":"done","total_questions","requested","partial"}
//   {"type":"error","detail","total_questions","partial":true}   it broke off part-way

export const initialQuizState = () => ({
  attemptId: null,
  requested: 0,
  questions: [],
  done: false, // a `done` event arrived
  failed: false, // an `error` event arrived
  partial: false, // fewer questions than requested
  totalQuestions: null, // how many there will be, once known
});

/** Fold one quiz event into the state. Unknown events are ignored. */
export function applyQuizEvent(state, event) {
  switch (event?.type) {
    case "start":
      return { ...state, attemptId: event.attempt_id, requested: event.requested ?? 0 };
    case "question": {
      const q = event.question;
      // A repeated id is a duplicate delivery, not a new question.
      if (!q || state.questions.some((x) => x.id === q.id)) return state;
      return { ...state, questions: [...state.questions, q] };
    }
    case "done":
      return {
        ...state,
        done: true,
        partial: !!event.partial,
        totalQuestions: event.total_questions ?? state.questions.length,
      };
    case "error":
      return {
        ...state,
        failed: true,
        partial: true,
        totalQuestions: event.total_questions ?? state.questions.length,
      };
    default:
      return state;
  }
}

/** True while more questions may still arrive. */
export const quizGenerating = (state) => !state.done && !state.failed;

/**
 * The note to show under a quiz whose stream ended on its own, or null when it
 * ended cleanly with every question. `params` are for the {n} / {total} text.
 */
export function quizEndNote(state) {
  const params = { n: state.questions.length, total: state.requested };
  if (state.failed) return { kind: "quiz_interrupted", tone: "error", params };
  if (!state.done) return { kind: "quiz_interrupted", tone: "error", params }; // closed with no verdict
  if (state.partial) return { kind: "quiz_partial", tone: "muted", params };
  return null;
}

/**
 * What to do when the request itself failed or was stopped (as opposed to the
 * server reporting a clean end or an `error` event).
 *
 *   keep    questions are on screen: leave them, add a note, let the student carry on
 *   !keep   nothing arrived: back to the setup screen
 *   banner  show `cause` in the error banner (never for Stop: the student asked)
 *   cause   what to translate: { kind: "quiz_stopped" } for an abort, else the error
 */
export function quizFailurePlan(error, state) {
  const aborted = error?.name === "AbortError";
  const params = { n: state.questions.length, total: state.requested };
  if (state.questions.length > 0) {
    return aborted
      ? { keep: true, banner: false, cause: { kind: "quiz_stopped" }, tone: "muted", params }
      : { keep: true, banner: false, cause: { kind: "quiz_interrupted" }, tone: "error", params };
  }
  return { keep: false, banner: !aborted, cause: error ?? { kind: "interrupted" }, tone: null, params };
}
