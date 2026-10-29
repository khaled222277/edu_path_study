import { useEffect, useState } from "react";

import Chat from "./Chat.jsx";
import Dashboard from "./Dashboard.jsx";
import Quiz from "./Quiz.jsx";
import {
  getMe,
  getUniversities,
  googleLoginUrl,
  logout,
  submitOnboarding,
} from "./api.js";
import { signInNotice } from "./errors.js";

export default function App() {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Dashboard is the landing view; a subject card routes into chat or quiz.
  const [view, setView] = useState({ name: "dashboard", subject: null });

  useEffect(() => {
    getMe()
      .then(setUser)
      .catch((err) => {
        // 401 just means "not signed in yet" — not an error worth showing.
        if (err.status !== 401) setError(err.message);
      })
      .finally(() => setLoading(false));
  }, []);

  async function handleSignOut() {
    await logout();
    setUser(null);
    setView({ name: "dashboard", subject: null });
  }

  if (loading) {
    return (
      <div className="centered">
        <p className="hint">Loading…</p>
      </div>
    );
  }

  const ready = user && !user.onboarding_required;

  if (!ready) {
    return (
      <div className="centered">
        <main className="card">
          {error && <p className="error">{error}</p>}
          {!user && <SignIn />}
          {user?.onboarding_required && <Onboarding onDone={setUser} />}
        </main>
      </div>
    );
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true" />
          <div className="brand-name">EduPath Study</div>
        </div>
        {view.subject && (
          <div className="crumb" dir="auto">
            <span>{view.subject.name}</span>
          </div>
        )}
        <div className="spacer" />
        <button className="link-button" onClick={handleSignOut}>
          Sign out
        </button>
        <div className="avatar" title={user.name}>
          {initials(user.name)}
        </div>
      </header>

      <div className="shell-body">
        <nav className="sidebar">
          <button
            className={`nav-item ${view.name === "dashboard" ? "active" : ""}`}
            onClick={() => setView({ name: "dashboard", subject: null })}
          >
            <span className="dot" />
            Dashboard
          </button>
          <button
            className={`nav-item ${view.name === "chat" ? "active" : ""}`}
            onClick={() => setView({ name: "chat", subject: view.subject })}
          >
            <span className="dot" />
            Study Chat
          </button>
          <button
            className={`nav-item ${view.name === "quiz" ? "active" : ""}`}
            onClick={() => setView({ name: "quiz", subject: view.subject })}
          >
            <span className="dot" />
            Quizzes
          </button>

          {view.subject && (
            <>
              <div className="nav-heading">Current subject</div>
              <div className="nav-subject active" dir="auto">
                <span className="badge">
                  {view.subject.name.slice(0, 2).toUpperCase()}
                </span>
                {view.subject.name}
              </div>
            </>
          )}
        </nav>

        <main className="main">
          {error && <p className="error">{error}</p>}

          {view.name === "dashboard" && (
            <Dashboard
              user={user}
              onStudy={(s) =>
                setView({
                  name: "chat",
                  subject: { id: s.subject_id, name: s.subject_name },
                })
              }
              onQuiz={(s) =>
                setView({
                  name: "quiz",
                  subject: { id: s.subject_id, name: s.subject_name },
                })
              }
            />
          )}

          {view.name === "chat" && (
            <Chat user={user} initialSubject={view.subject} />
          )}

          {view.name === "quiz" && (
            <Quiz
              subject={view.subject}
              onExit={() => setView({ name: "dashboard", subject: null })}
            />
          )}
        </main>
      </div>
    </div>
  );
}

function initials(name) {
  return (name || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

function SignIn() {
  const notice = signInNotice(window.location.search);
  useEffect(() => {
    // the notice is shown once; drop the query string so a refresh starts clean
    if (notice) window.history.replaceState(null, "", window.location.pathname);
  }, [notice]);
  return (
    <>
      <h1>EduPath Study</h1>
      {notice && <p className="error" role="alert">{notice}</p>}
      <p>Sign in to reach your lectures, tutor, and quizzes.</p>
      <button onClick={() => (window.location.href = googleLoginUrl)}>
        Sign up with Google
      </button>
    </>
  );
}

function Onboarding({ onDone }) {
  const [universities, setUniversities] = useState([]);
  const [universityId, setUniversityId] = useState("");
  const [year, setYear] = useState(1);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    getUniversities()
      .then((list) => {
        setUniversities(list);
        if (list.length) setUniversityId(String(list[0].id));
      })
      .catch((err) => setError(err.message));
  }, []);

  async function handleSubmit(event) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      onDone(await submitOnboarding(Number(universityId), Number(year)));
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <h1>Tell us where you study</h1>
      <form onSubmit={handleSubmit}>
        <label>
          University
          <select
            value={universityId}
            onChange={(e) => setUniversityId(e.target.value)}
            required
          >
            {universities.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </select>
        </label>

        <label>
          Year
          <input
            type="number"
            min="1"
            max="7"
            value={year}
            onChange={(e) => setYear(e.target.value)}
            required
          />
        </label>

        {error && <p className="error">{error}</p>}

        <button type="submit" disabled={submitting || !universityId}>
          {submitting ? "Saving…" : "Continue"}
        </button>
      </form>
    </>
  );
}

