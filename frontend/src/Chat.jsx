import { useEffect, useRef, useState } from "react";

import Quiz from "./Quiz.jsx";
import {
  API_URL,
  createSession,
  getMySubjects,
  getSession,
  listSessions,
  sendMessage,
} from "./api.js";

export default function Chat({ user, initialSubject = null }) {
  const [subjects, setSubjects] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [session, setSession] = useState(null);
  const [messages, setMessages] = useState([]);
  // Citations are returned per-reply, not stored on the message row, so they
  // are kept here keyed by message id.
  const [citations, setCitations] = useState({});
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const [quizSubject, setQuizSubject] = useState(null);
  // The figure currently opened full-size, as { src, label }.
  const [zoomed, setZoomed] = useState(null);

  const bottomRef = useRef(null);

  useEffect(() => {
    getMySubjects().then(setSubjects).catch((e) => setError(e.message));
    listSessions().then(setSessions).catch((e) => setError(e.message));
  }, []);

  // Arriving from a dashboard card goes straight into a session for that
  // subject rather than showing the picker again.
  useEffect(() => {
    if (initialSubject && !session) startSession(initialSubject.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSubject?.id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  // Escape closes the enlarged figure, so the student never loses the chat
  // behind an overlay they cannot dismiss from the keyboard.
  useEffect(() => {
    if (!zoomed) return undefined;
    const onKey = (e) => e.key === "Escape" && setZoomed(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomed]);

  async function startSession(subjectId) {
    setError(null);
    try {
      const created = await createSession(Number(subjectId));
      setSession(created);
      setMessages([]);
      setCitations({});
      setSessions(await listSessions());
    } catch (e) {
      setError(e.message);
    }
  }

  async function openSession(sessionId) {
    setError(null);
    try {
      const detail = await getSession(sessionId);
      setSession({ id: detail.id, subject_id: detail.subject_id });
      setMessages(detail.messages);
      setCitations({});
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleSend(event) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || sending) return;

    setDraft("");
    setSending(true);
    setError(null);
    // Optimistic echo so the student sees their question immediately.
    setMessages((prev) => [
      ...prev,
      { id: `pending-${Date.now()}`, role: "user", content },
    ]);

    try {
      const reply = await sendMessage(session.id, content);
      setMessages((prev) => [...prev, reply.message]);
      setCitations((prev) => ({ ...prev, [reply.message.id]: reply.citations }));
    } catch (e) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  }

  const subjectName = (id) => {
    const s = subjects.find((x) => x.id === id);
    return s ? `${s.code} — ${s.name}` : `subject ${id}`;
  };

  if (quizSubject) {
    return <Quiz subject={quizSubject} onExit={() => setQuizSubject(null)} />;
  }

  if (!session) {
    return (
      <div>
        <h1 className="page-title">Study chat</h1>
        <p className="page-sub">Pick a subject to start studying.</p>

        {error && <p className="error">{error}</p>}

        <ul className="subject-grid">
          {subjects.map((s) => (
            <li key={s.id} className="subject-card">
              <div className="subject-head">
                <span className="badge badge-lg">
                  {s.code.slice(0, 2).toUpperCase()}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div className="subject-name" dir="auto">
                    {s.name}
                  </div>
                  <div className="subject-code" dir="auto">
                    {s.code}
                  </div>
                </div>
              </div>
              <div className="subject-actions">
                <button className="btn-secondary" onClick={() => startSession(s.id)}>
                  Chat
                </button>
                <button className="btn-small" onClick={() => setQuizSubject(s)}>
                  Quiz
                </button>
              </div>
            </li>
          ))}
        </ul>
        {subjects.length === 0 && (
          <p className="hint">No subjects are set up for your year yet.</p>
        )}

        {sessions.length > 0 && (
          <>
            <h2 className="section-title">Previous sessions</h2>
            <ul className="session-list">
              {sessions.map((s) => (
                <li key={s.id}>
                  <button className="session-row" onClick={() => openSession(s.id)}>
                    <span className="ev-icon" aria-hidden="true">C</span>
                    <span className="session-meta">
                      <span className="session-subject" dir="auto">
                        {subjectName(s.subject_id)}
                      </span>
                      <span className="ev-time">
                        {new Date(s.created_at).toLocaleString()}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="chat">
      <header className="chat-header">
        <div>
          <div className="chat-title" dir="auto">
            {subjectName(session.subject_id)}
          </div>
          <div className="chat-sub">Grounded in your lecture material</div>
        </div>
        <button className="btn-secondary" onClick={() => setSession(null)}>
          All subjects
        </button>
      </header>

      <div className="messages">
        {messages.length === 0 && (
          <p className="hint chat-empty">
            Ask anything from your lectures — in Arabic or English.
          </p>
        )}

        {/* dir="auto" sits on the bubble, not just the text, so the Sources
            block follows the reply's language too. It cannot move the bubble:
            which side it takes comes from align-self by role, and align-self
            ignores direction. */}
        {messages.map((m) => (
          <div key={m.id} className={`msg msg-${m.role}`} dir="auto">
            <div className="msg-body">{m.content}</div>
            {citations[m.id]?.length > 0 && (
              <div className="citations">
                <div className="citations-label">Sources</div>
                <ul>
                  {/* No dir="auto" here: these inherit the bubble's direction
                      so the list sits under the Sources label on the same side.
                      The filenames are Latin, so bidi still lays each one out
                      left-to-right within the line. */}
                  {citations[m.id].map((c, i) => {
                    const label = `${c.source_filename}, page ${c.page_number}`;
                    return (
                      <li key={i}>
                        {label}
                        {c.content_type === "image_description" && " (figure)"}
                        {/* The figure itself, when we have the file. Loaded
                            from the API host, which sends the session cookie
                            with the request the same way fetch does. */}
                        {c.image_url && (
                          <button
                            type="button"
                            className="citation-thumb"
                            title="Click to enlarge"
                            onClick={() =>
                              setZoomed({ src: API_URL + c.image_url, label })
                            }
                          >
                            <img
                              src={API_URL + c.image_url}
                              alt={`Figure from ${label}`}
                              loading="lazy"
                            />
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
          </div>
        ))}

        {sending && (
          <div className="msg msg-assistant">
            <div className="msg-body typing">Thinking…</div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {error && <p className="error">{error}</p>}

      <form className="composer" onSubmit={handleSend}>
        <input
          dir="auto"
          value={draft}
          placeholder="Ask a question…"
          onChange={(e) => setDraft(e.target.value)}
          disabled={sending}
        />
        <button type="submit" disabled={sending || !draft.trim()}>
          Send
        </button>
      </form>

      {/* dir="ltr": the overlay is chrome, not content, so it keeps its
          layout regardless of the language of the answer behind it. */}
      {zoomed && (
        <div
          className="figure-overlay"
          dir="ltr"
          role="dialog"
          aria-modal="true"
          aria-label={zoomed.label}
          onClick={() => setZoomed(null)}
        >
          {/* Clicking the image should not dismiss what you just opened. */}
          <figure className="figure-zoom" onClick={(e) => e.stopPropagation()}>
            <img src={zoomed.src} alt={`Figure from ${zoomed.label}`} />
            <figcaption>{zoomed.label}</figcaption>
          </figure>
          <button
            type="button"
            className="figure-close"
            aria-label="Close figure"
            onClick={() => setZoomed(null)}
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}
