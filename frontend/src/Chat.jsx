import { useEffect, useRef, useState } from "react";

import Quiz from "./Quiz.jsx";
import {
  API_URL,
  createSession,
  getMySubjects,
  getSession,
  listSessions,
  streamMessage,
} from "./api.js";
import { friendlyError, guessLanguage, restoreDraft, withoutPending } from "./errors.js";
import { MessageText } from "./messageText.js";
import {
  applyEvent,
  citationsByMessage,
  failurePlan,
  initialStreamState,
  streamOutcome,
  upsertAnswer,
} from "./stream.js";

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
  // The in-flight answer's AbortController; null when nothing is streaming. A
  // send only touches the screen while it is still the one stored here.
  const abortRef = useRef(null);
  // Follow the answer as it grows, unless the student scrolled up to read.
  const stickRef = useRef(true);

  useEffect(() => {
    getMySubjects().then(setSubjects).catch((e) => setError(friendlyError(e)));
    listSessions().then(setSessions).catch((e) => setError(friendlyError(e)));
  }, []);

  // Arriving from a dashboard card goes straight into a session for that
  // subject rather than showing the picker again.
  useEffect(() => {
    if (initialSubject && !session) startSession(initialSubject.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSubject?.id]);

  useEffect(() => {
    if (!stickRef.current) return;
    // Instant while streaming: a smooth scroll restarts on every update and lags.
    bottomRef.current?.scrollIntoView({ behavior: sending ? "auto" : "smooth" });
  }, [messages, sending]);

  // Leaving a chat (another session, back to the list, unmounting) abandons its
  // answer: closing the request tells the server to stop and to save nothing.
  useEffect(
    () => () => {
      const controller = abortRef.current;
      abortRef.current = null; // detach first, so the aborted send stays off-screen
      controller?.abort();
      setSending(false);
    },
    [session?.id],
  );

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
      setError(friendlyError(e));
    }
  }

  async function openSession(sessionId) {
    setError(null);
    try {
      const detail = await getSession(sessionId);
      setSession({ id: detail.id, subject_id: detail.subject_id });
      setMessages(detail.messages);
      setCitations(citationsByMessage(detail.messages));
    } catch (e) {
      setError(friendlyError(e));
    }
  }

  async function handleSend(event) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || sending) return;

    const lang = guessLanguage(content);
    const stamp = Date.now();
    const userId = `pending-${stamp}`;
    const answerId = `stream-${stamp}`;
    const controller = new AbortController();
    abortRef.current = controller;
    stickRef.current = true;

    setDraft("");
    setSending(true);
    setError(null);
    // Optimistic echo so the student sees their question immediately.
    setMessages((prev) => [...prev, { id: userId, role: "user", content }]);

    let state = initialStreamState();
    const isCurrent = () => abortRef.current === controller;
    const paint = (extra) => setMessages((prev) => upsertAnswer(prev, answerId, state, extra));
    // Tokens arrive far faster than a screen refreshes; painting once per frame
    // keeps the text steady instead of re-rendering on every few characters.
    // The handle is local to this send: a send that was abandoned (the student
    // switched chats) must not be able to cancel the next one's frame.
    let frame = null;
    const schedule = () => {
      if (frame != null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        if (isCurrent()) paint();
      });
    };
    const cancelFrame = () => {
      if (frame != null) cancelAnimationFrame(frame);
      frame = null;
    };
    // Nothing worth keeping: take the attempt off the screen and give the text back.
    const undo = () => {
      setMessages((prev) => prev.filter((m) => m.id !== userId && m.id !== answerId));
      setDraft((current) => restoreDraft(current, content));
    };
    // The answer ended without completing: see failurePlan for the policy.
    const handleFailure = (error) => {
      const plan = failurePlan(error, state);
      const message = friendlyError(plan.cause, lang);
      if (plan.keepPartial) {
        // Part of an answer is on screen: keep it and say why it stops.
        paint({ note: message, noteTone: plan.tone });
        setDraft((current) => restoreDraft(current, content));
      } else {
        // Nothing worth keeping (refused, offline, stopped at once): nothing was
        // stored, so take the echo out and hand the text back to edit or resend.
        undo();
        if (plan.banner) setError(message);
      }
    };

    try {
      await streamMessage(session.id, content, {
        signal: controller.signal,
        onEvent: (ev) => {
          state = applyEvent(state, ev);
          if (ev.type === "citations") {
            // Sources are known before the first word: show them straight away.
            setCitations((prev) => ({ ...prev, [answerId]: state.citations }));
          }
          if (ev.type === "token" || ev.type === "citations") schedule();
        },
      });
      cancelFrame();
      if (!isCurrent()) return;

      if (streamOutcome(state) === "done") {
        paint(); // swaps the placeholder for the stored message, with its real id
        const storedId = state.message.id;
        setCitations((prev) => {
          const { [answerId]: _placeholder, ...rest } = prev;
          return { ...rest, [storedId]: state.citations };
        });
      } else {
        handleFailure(null); // an `error` event, or the connection just closed
      }
    } catch (e) {
      cancelFrame();
      if (isCurrent()) handleFailure(e);
    } finally {
      cancelFrame();
      if (isCurrent()) {
        abortRef.current = null;
        setSending(false);
      }
    }
  }

  function handleStop() {
    abortRef.current?.abort();
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

      <div
        className="messages"
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
        }}
      >
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
          <div
            key={m.id}
            className={`msg msg-${m.role}${m.streaming ? " msg-streaming" : ""}`}
            dir="auto"
          >
            <div className="msg-body">
              <MessageText text={m.content} />
            </div>
            {m.note && <div className={`msg-note msg-note-${m.noteTone}`}>{m.note}</div>}
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

        {sending && !messages.some((m) => m.streaming) && (
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
        {sending ? (
          <button type="button" className="btn-secondary" onClick={handleStop}>
            Stop
          </button>
        ) : (
          <button type="submit" disabled={!draft.trim()}>
            Send
          </button>
        )}
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
