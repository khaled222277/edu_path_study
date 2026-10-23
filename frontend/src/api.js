import { kindForStatus, networkFailure } from "./errors.js";
import { createNdjsonParser } from "./stream.js";

// Optional chaining so this module also loads under plain Node (the unit tests).
export const API_URL = import.meta.env?.VITE_API_URL || "http://localhost:8000";

/** The Error for a non-2xx response, tagged so the UI can pick a friendly message. */
function httpError(status, body) {
  const err = new Error(body?.detail || `Request failed (${status})`);
  err.status = status;
  // "rate_limited" (429) / "unavailable" (503) get a translated message in the UI.
  err.kind = kindForStatus(status);
  return err;
}

/** fetch wrapper that always sends the httpOnly session cookie. */
async function request(path, options = {}) {
  const res = await fetch(`${API_URL}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (res.status === 204) return null;

  const body = await res.json().catch(() => null);
  if (!res.ok) throw httpError(res.status, body);
  return body;
}

export const getMe = () => request("/auth/me");

export const getUniversities = () => request("/universities");

export const submitOnboarding = (universityId, year) =>
  request("/onboarding", {
    method: "POST",
    body: JSON.stringify({ university_id: universityId, year }),
  });

export const logout = () => request("/auth/logout", { method: "POST" });

/** Subjects for the signed-in student's own university + year. */
export const getMySubjects = () => request("/subjects");

export const createSession = (subjectId) =>
  request("/chat/sessions", {
    method: "POST",
    body: JSON.stringify({ subject_id: subjectId }),
  });

export const listSessions = () => request("/chat/sessions");

export const getSession = (sessionId) => request(`/chat/sessions/${sessionId}`);

export const sendMessage = (sessionId, content) =>
  request(`/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });

/**
 * Ask a question and receive the answer as it is written.
 *
 * Calls `onEvent` for each event (see stream.js) and resolves when the stream
 * ends. Rejects with the same tagged errors as the other calls if the request is
 * refused (401, 403, 429, 503...) before any text; with an `unavailable` error if
 * the connection drops part-way; and with the browser's AbortError if `signal`
 * is aborted -- which is how the Stop button works.
 */
export async function streamMessage(sessionId, content, { signal, onEvent }) {
  let res;
  try {
    res = await fetch(`${API_URL}/chat/sessions/${sessionId}/messages/stream`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      signal,
    });
  } catch (e) {
    throw networkFailure(e);
  }

  if (!res.ok) {
    throw httpError(res.status, await res.json().catch(() => null));
  }

  const reader = res.body.getReader();
  // `stream: true` holds back an incomplete UTF-8 sequence until its remaining
  // bytes arrive, so an Arabic letter split across two network chunks is intact.
  const decoder = new TextDecoder("utf-8");
  const parser = createNdjsonParser();
  try {
    for (;;) {
      let chunk;
      try {
        chunk = await reader.read();
      } catch (e) {
        throw networkFailure(e);
      }
      if (chunk.done) break;
      for (const event of parser.push(decoder.decode(chunk.value, { stream: true }))) {
        onEvent(event);
      }
    }
    for (const event of parser.push(decoder.decode())) onEvent(event);
    for (const event of parser.flush()) onEvent(event);
  } finally {
    // Closing the body is what tells the server to stop generating.
    reader.cancel().catch(() => {});
  }
}

export const startQuiz = (subjectId, questionCount, language) =>
  request("/quiz/start", {
    method: "POST",
    body: JSON.stringify({
      subject_id: subjectId,
      question_count: questionCount,
      language,
    }),
  });

export const answerQuestion = (attemptId, itemId, chosenAnswerIndex) =>
  request(`/quiz/${attemptId}/answer`, {
    method: "POST",
    body: JSON.stringify({ item_id: itemId, chosen_answer_index: chosenAnswerIndex }),
  });

export const getQuizResults = (attemptId) => request(`/quiz/${attemptId}/results`);

export const getQuizHistory = () => request("/quiz/history");

export const getDashboardSummary = () => request("/dashboard/summary");

export const getDashboardActivity = (limit = 10) =>
  request(`/dashboard/activity?limit=${limit}`);

export const googleLoginUrl = `${API_URL}/auth/google/login`;
