import { useEffect, useState } from "react";

import {
  answerQuestion,
  getMySubjects,
  getQuizResults,
  startQuiz,
} from "./api.js";
import { friendlyError } from "./errors.js";

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

  // The subject actually being quizzed: the prop when routed from a card,
  // otherwise whatever the picker selected.
  const active = subject ?? picked;

  useEffect(() => {
    if (subject) return;
    getMySubjects().then(setSubjects).catch((e) => setError(friendlyError(e, language)));
  }, [subject]);

  async function begin() {
    setBusy(true);
    setError(null);
    try {
      const quiz = await startQuiz(active.id, count, language);
      setAttemptId(quiz.attempt_id);
      setQuestions(quiz.questions);
      setIndex(0);
      setFeedback(null);
      setResults(null);
    } catch (e) {
      // Message follows the quiz language the student picked.
      setError(friendlyError(e, language));
    } finally {
      setBusy(false);
    }
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
    if (index + 1 < questions.length) {
      setIndex(index + 1);
      setFeedback(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
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
            <button
              className="btn-secondary"
              onClick={() => (subject ? onExit() : setPicked(null))}
            >
              Back
            </button>
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
  const progress = ((index + (feedback ? 1 : 0)) / questions.length) * 100;

  return (
    <div className="quiz">
      <header className="quiz-header">
        <div>
          <div className="quiz-progress-label">
            Question {index + 1} of {questions.length}
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
          <button onClick={next} disabled={busy}>
            {index + 1 < questions.length ? "Next question" : "See results"}
          </button>
        </div>
      )}
    </div>
  );
}
