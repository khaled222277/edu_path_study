// Messages for the two failures a student can do something about.
//
//   429 -- the API's rate limit tripped (too many chat / quiz requests)
//   503 -- Ollama or the vector store is down, slow, or a quiz could not be built
//
// The UI chrome is English-only, so there is no app-wide language to follow.
// These messages instead follow the language the student is working in: the
// quiz language they picked, or the script they just typed in chat. Anything
// else falls back to the server's own message.

const MESSAGES = {
  rate_limited: {
    en: "Too many requests. Please wait a moment and try again.",
    ar: "عدد الطلبات كبير جدًا. يرجى الانتظار قليلًا ثم المحاولة مرة أخرى.",
  },
  unavailable: {
    en: "The AI service is busy or unavailable. Please try again.",
    ar: "خدمة الذكاء الاصطناعي مشغولة أو غير متاحة حاليًا. يرجى المحاولة مرة أخرى.",
  },
};

/** Which friendly message an HTTP status maps to, or null for "show the server's". */
export function kindForStatus(status) {
  if (status === 429) return "rate_limited";
  if (status === 503) return "unavailable";
  return null;
}

const isArabic = (ch) => /[؀-ۿݐ-ݿ]/.test(ch);

/**
 * "ar" or "en" for what the student typed. Same rule as the backend's
 * detect_language (app/services/quiz_service.py), so chat and quiz agree:
 * mostly-Arabic is Arabic, barely-Arabic is English, and mixed text is decided
 * by the script the sentence opens in -- "ما هو الـ clustering في data mining"
 * wants Arabic although most of its letters are Latin.
 */
export function guessLanguage(text) {
  const letters = [...String(text ?? "")].filter((ch) => /\p{L}/u.test(ch));
  if (letters.length === 0) return "en";

  const ratio = letters.filter(isArabic).length / letters.length;
  if (ratio >= 0.7) return "ar";
  if (ratio < 0.15) return "en";
  return isArabic(letters[0]) ? "ar" : "en";
}

/** The message to show for a failed request, in `lang` ("en" | "ar"). */
export function friendlyError(err, lang = "en") {
  const entry = MESSAGES[err?.kind];
  if (entry) return entry[lang] ?? entry.en;
  return err?.message || "Something went wrong. Please try again.";
}

/** The message list without the optimistic echo that never made it to the server. */
export function withoutPending(messages, pendingId) {
  return messages.filter((m) => m.id !== pendingId);
}

/**
 * What the composer should hold after a failed send. The failed text comes back
 * so it can be edited and resent, unless the student has already started typing
 * something else -- never overwrite that.
 */
export function restoreDraft(current, failedContent) {
  return current && current.trim() ? current : failedContent;
}
