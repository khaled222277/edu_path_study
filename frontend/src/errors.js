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
  // A streamed answer that broke off part-way (an `error` event, a dropped
  // connection, or an unreadable event).
  interrupted: {
    en: "The answer was interrupted. Please try again.",
    ar: "انقطعت الإجابة. يرجى المحاولة مرة أخرى.",
  },
  // A streamed quiz that ended with fewer questions than were asked for.
  quiz_partial: {
    en: "Only {n} of {total} questions could be written from this material.",
    ar: "تم إنشاء {n} من أصل {total} أسئلة فقط من هذه المادة.",
  },
  quiz_interrupted: {
    en: "Some questions could not be generated. You can continue with the ones below.",
    ar: "تعذّر إنشاء بعض الأسئلة. يمكنك المتابعة بالأسئلة المعروضة.",
  },
  quiz_stopped: {
    en: "Generation stopped. You can answer the questions written so far.",
    ar: "تم إيقاف الإنشاء. يمكنك الإجابة عن الأسئلة التي كُتبت حتى الآن.",
  },
  // The student pressed Stop. Said plainly because the partial answer is shown
  // but was not saved: it will not be there after a reload.
  stopped: {
    en: "Stopped. This answer was not saved.",
    ar: "تم الإيقاف. لم يتم حفظ هذه الإجابة.",
  },
};

/** Which friendly message an HTTP status maps to, or null for "show the server's". */
/** A notice for the sign-in screen from the query string Google's callback redirected with, else null. */
export function signInNotice(search) {
  const code = new URLSearchParams(search ?? "").get("signin");
  if (code === "not_allowed") {
    return "This account cannot be used to sign in to the pilot. If you think this is a mistake, please contact your instructor.";
  }
  return null;
}

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

/**
 * The message to show for a failed request, in `lang` ("en" | "ar"). `params`
 * fills {placeholders} in the text, e.g. { n: 3, total: 5 }.
 */
export function friendlyError(err, lang = "en", params = {}) {
  const entry = MESSAGES[err?.kind];
  if (entry) {
    const text = entry[lang] ?? entry.en;
    return text.replace(/\{(\w+)\}/g, (match, key) => params[key] ?? match);
  }
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

/**
 * A failed fetch() -- backend down, offline, connection reset -- as an error the
 * UI can translate, instead of the browser's raw "Failed to fetch". An abort is
 * the caller cancelling on purpose, not a failure, so it passes through unchanged.
 */
export function networkFailure(cause) {
  if (cause?.name === "AbortError") return cause;
  // The message is the friendly English text, so screens that print err.message
  // directly (App, Dashboard) never show the browser's raw "Failed to fetch";
  // Chat and Quiz translate from `kind` instead.
  const err = new Error(MESSAGES.unavailable.en);
  err.kind = "unavailable";
  err.cause = cause;
  return err;
}
