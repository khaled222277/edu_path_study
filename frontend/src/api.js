export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

/** fetch wrapper that always sends the httpOnly session cookie. */
async function request(path, options = {}) {
  const res = await fetch(`${API_URL}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (res.status === 204) return null;

  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(body?.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
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
