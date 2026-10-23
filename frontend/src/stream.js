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
