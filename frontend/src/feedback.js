// Thumbs up / down under assistant replies: the small pure parts, kept apart so they can be tested.

export const COMMENT_MAX = 500;

/** A reply can be rated once it is a stored assistant message (it has a numeric id). */
export function canRate(message) {
  return message?.role === "assistant" && Number.isInteger(message.id) && !message.streaming;
}

/** Ratings that came with a loaded chat, keyed by message id: { 12: "down" }. */
export function feedbackByMessage(messages) {
  const out = {};
  for (const m of messages ?? []) {
    if (canRate(m) && (m.feedback === "up" || m.feedback === "down")) out[m.id] = m.feedback;
  }
  return out;
}

/** What clicking `clicked` does to a reply currently rated `current`: the new rating, or null to clear. */
export function nextRating(current, clicked) {
  return current === clicked ? null : clicked;
}

/** A comment ready to send: trimmed, cut to the limit, null when empty. */
export function cleanComment(text) {
  const t = (text ?? "").trim().slice(0, COMMENT_MAX);
  return t === "" ? null : t;
}
