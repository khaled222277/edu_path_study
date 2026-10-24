import { useEffect, useRef, useState } from "react";

import {
  answerQuestion,
  completeQuiz,
  getMySubjects,
  getQuizResults,
  streamQuiz,
} from "./api.js";
import { friendlyError } from "./errors.js";
import {
  applyQuizEvent,
  initialQuizState,
  quizEndNote,
  quizFailurePlan,
  quizGenerating,
} from "./stream.js";

const QUESTION_COUNTS = [3, 5, 10];

export default function Quiz({ subject = null, onExit }) {
  // Reached from a dashboard card, `subject` is supplied. Reached from the
  // sidebar it is null, so the page offers a picker first rather than
  // rendering against nothing.
  const [picked, setPicked] = useState(null);
  const [subjects, setSubjects] = useState([]);
  const [questions, setQuestions] = useState(null);
  const [attemptId, setAttemptId] = useState(null);
  const [index, setIndex] = useState(0);
  // Feedback for the current question, cleared when moving on.
  const [feedback, setFeedback] = useState(null);
  const [results, setResults] = useState(null);
  const [count, setCount] = useState(5);
  const [language, setLanguage] = useState("en");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // Questions arrive one at a time. `generating` is true while more may come,
  // `requested` is how many were asked for, and `note` says why a quiz has fewer.
  const [generating, setGenerating] = useState(false);
  const [requested, setRequested] = useState(0);
  const [note, setNote] = useState(null);
  // The AbortController of the quiz being written; null when none is.
  const abortRef = useRef(null);

  // The subject actually being quizzed: the prop when routed from a card,
  // otherwise whatever the picker selected.
  const active = subject ?? picked;

  useEffect(() => {
    if (subject) return;
    getMySubjects().then(setSubjects).catch((e) => setError(friendlyError(e, language)));
  }, [subject]);

  // Leaving the page stops the quiz being written: closing the request tells the
  // server to stop, and what was already written stays as a shorter quiz.
  useEffect(
    () => () => {
      const controller = abortRef.current;
      abortRef.current = null;
      controller?.abort();
    },
    [],
  );

  async function begin() {
    if (busy) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    setError(null);
    setNote(null);

    let state = initialQuizState();
    let shown = false;
    const isCurrent = () => abortRef.current === controller;
    const noteFor = (kind, params, tone) =>
      setNote({ text: friendlyError({ kind }, language, params), tone });

    try {
      await streamQuiz(active.id, count, language, {
        signal: controller.signal,
        onEvent: (ev) => {
          if (!isCurrent()) return;
          state = applyQuizEvent(state, ev);
          setRequested(state.requested);
          setGenerating(quizGenerating(state));
          if (state.attemptId !== null) setAttemptId(state.attemptId);
          if (state.questions.length === 0) return;
          setQuestions(state.questions);
          if (!shown) {
            // The first question is ready: start the quiz. The rest keep arriving.
            shown = true;
            setIndex(0);
            setFeedback(null);
            setResults(null);
            setBusy(false);
          }
        },
      });
      if (!isCurrent()) return;
      const end = quizEndNote(state);
      if (end) noteFor(end.kind, end.params, end.tone);
    } catch (e) {
      if (!isCurrent()) return;
      const plan = quizFailurePlan(e, state);
      if (plan.keep) {
        noteFor(plan.cause.kind, plan.params, plan.tone);
      } else {
        // Nothing arrived: back to the setup screen. Message follows the quiz
        // language the student picked.
        setQuestions(null);
        if (plan.banner) setError(friendlyError(plan.cause, language));
      }
    } finally {
      if (isCurrent()) {
        abortRef.current = null;
        setGenerating(false);
        setBusy(false);
      }
    }
  }

  function stopGenerating() {
    abortRef.current?.abort();
  }

  async function choose(optionIndex) {
    if (feedback || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await answerQuestion(attemptId, questions[index].id, optionIndex);
      setFeedback({ ...res, chosen: optionIndex });
    } catch (e) {
      setError(friendlyError(e, language));
    } finally {
      setBusy(false);
    }
  }

  async function next() {
    if (index + 1 >= questions.length && generating) return; // the next one is still coming
    if (index + 1 < questions.length) {
      setIndex(index + 1);
      setFeedback(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // Normally the last answer already closed the quiz. If it did not -- the
      // student stopped generation, or answered everything before the server
      // finished settling -- close it now so the results can be read.
      if (!feedback?.is_complete) await completeQuiz(attemptId);
      setResults(await getQuizResults(attemptId));
    } catch (e) {
      setError(friendlyError(e, language));
    } finally {
      setBusy(false);
    }
  }

  // --- subject picker (only when the page was opened without a subject) ---
  if (!active) {
    return (
      <div className="quiz">
        <h1 className="page-title">Quizzes</h1>
        <p className="page-sub">
          Pick a subject to test yourself on. Questions are written from your
          own lecture material.
        </p>

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
                <button onClick={() => setPicked({ id: s.id, name: s.name })}>
                  Start quiz
                </button>
              </div>
            </li>
          ))}
        </ul>

        {subjects.length === 0 && (
          <p className="hint">No subjects are set up for your year yet.</p>
        )}
      </div>
    );
  }

  // --- results screen ---
  if (results) {
    const pct = Math.round((results.score / results.total_questions) * 100);
    return (
      <div className="quiz">
        <h1 className="page-title">Quiz results</h1>
        <p className="page-sub" dir="auto">
          {active.name}
        </p>

        <div className="score-panel">
          <div className={`score-big ${pct >= 70 ? "good" : ""}`}>{pct}%</div>
          <div className="stat-label">
            {results.score} of {results.total_questions} correct
          </div>
          <div className="meter">
            <div className="meter-fill" style={{ width: `${pct}%` }} />
          </div>
        </div>

        <h2 className="section-title">Review</h2>
        <ol className="review">
          {results.items.map((item, qi) => (
            <li key={item.id} className="review-item">
              <div className="review-head">
                <span className={`qnum ${item.is_correct ? "correct" : "wrong"}`}>
                  {qi + 1}
                </span>
                <span className="question-text" dir="auto">
                  {item.question_text}
                </span>
              </div>

              <ul className="options">
                {item.options.map((opt, i) => {
                  const isCorrect = i === item.correct_answer_index;
                  const isChosen = i === item.chosen_answer_index;
                  const state = isCorrect
                    ? "correct"
                    : isChosen
                      ? "wrong"
                      : "neutral";
                  return (
                    <li key={i} className={`option static ${state}`}>
                      <span className="option-mark" aria-hidden="true">
                        {isCorrect ? "✓" : isChosen ? "✕" : ""}
                      </span>
                      {/* dir="auto" on the label only: the marker must stay on
                          the same side whatever language the option is in. */}
                      <span className="option-text" dir="auto">
                        {opt}
                      </span>
                      {isChosen && !isCorrect && (
                        <span className="option-tag">your answer</span>
                      )}
                    </li>
                  );
                })}
              </ul>

              <div className="cite" dir="auto">
                {item.source_citation}
              </div>
            </li>
          ))}
        </ol>
        <button onClick={onExit}>Done</button>
      </div>
    );
  }

  // --- setup screen ---
  if (!questions) {
    return (
      <div className="quiz">
        <h1 className="page-title">Quiz</h1>
        <p className="page-sub" dir="auto">
          {active.name}
        </p>

        {error && <p className="error">{error}</p>}

        <div className="setup-panel">
          <label>
            Questions
            <select value={count} onChange={(e) => setCount(Number(e.target.value))}>
              {QUESTION_COUNTS.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>

          <label>
            Language
            <select value={language} onChange={(e) => setLanguage(e.target.value)}>
              <option value="en">English</option>
              <option value="ar">العربية</option>
            </select>
          </label>

          <div className="setup-actions">
            <button onClick={begin} disabled={busy}>
              {busy ? "Generating…" : "Start quiz"}
            </button>
            {/* Came in via the picker? Back returns there rather than
                dropping the student all the way out to the dashboard. */}
            {busy ? (
              <button className="btn-secondary" onClick={stopGenerating}>
                Cancel
              </button>
            ) : (
              <button
                className="btn-secondary"
                onClick={() => (subject ? onExit() : setPicked(null))}
              >
                Back
              </button>
            )}
          </div>

          {busy && (
            <p className="hint">
              Writing questions from your lectures — this takes a moment.
            </p>
          )}
        </div>
      </div>
    );
  }

  // --- question screen ---
  const question = questions[index];
  // While questions are still being written the quiz is `requested` long; once
  // the writing stops it is as long as it turned out to be.
  const total = generating ? Math.max(requested, questions.length) : questions.length;
  const progress = ((index + (feedback ? 1 : 0)) / total) * 100;
  const atLastWritten = index + 1 >= questions.length;

  return (
    <div className="quiz">
      <header className="quiz-header">
        <div>
          <div className="quiz-progress-label">
            Question {index + 1} of {total}
          </div>
          <div className="quiz-subject" dir="auto">
            {active.name}
          </div>
        </div>
        <button className="btn-secondary" onClick={onExit}>
          Quit
        </button>
      </header>

      <div className="meter quiz-meter">
        <div className="meter-fill" style={{ width: `${progress}%` }} />
      </div>

      {generating && (
        <p className="quiz-generating" role="status">
          <span>
            Writing question {Math.min(questions.length + 1, total)} of {total}…
          </span>
          <button className="btn-small" onClick={stopGenerating}>
            Stop
          </button>
        </p>
      )}
      {note && <p className={`quiz-note quiz-note-${note.tone}`}>{note.text}</p>}

      <p className="question-text lead" dir="auto">
        {question.question_text}
      </p>

      <ul className="options">
        {question.options.map((opt, i) => {
          // Unchanged: nothing is revealed until `feedback` exists, which only
          // happens after the server has recorded the answer.
          let cls;
          if (feedback) {
            if (i === feedback.correct_answer_index) cls = "correct";
            else if (i === feedback.chosen) cls = "wrong";
          }
          const mark = !feedback
            ? String.fromCharCode(65 + i)
            : cls === "correct"
              ? "✓"
              : cls === "wrong"
                ? "✕"
                : String.fromCharCode(65 + i);
          return (
            <li key={i}>
              <button
                className={`option ${cls ?? (feedback ? "neutral" : "")}`}
                onClick={() => choose(i)}
                disabled={!!feedback || busy}
              >
                <span className="option-mark" aria-hidden="true">
                  {mark}
                </span>
                {/* dir="auto" on the label only — option order and marker side
                    are fixed, so Arabic options never reorder or jump. */}
                <span className="option-text" dir="auto">
                  {opt}
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      {error && <p className="error">{error}</p>}

      {feedback && (
        <div className={`feedback ${feedback.is_correct ? "correct" : "wrong"}`}>
          <div className="feedback-head">
            {feedback.is_correct ? "Correct" : "Not quite"}
          </div>
          <div className="cite" dir="auto">
            {feedback.source_citation}
          </div>
          <button onClick={next} disabled={busy || (atLastWritten && generating)}>
            {!atLastWritten
              ? "Next question"
              : generating
                ? "Writing the next question…"
                : "See results"}
          </button>
        </div>
      )}
    </div>
  );
}
