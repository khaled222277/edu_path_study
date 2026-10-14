import { useEffect, useState } from "react";

import { getDashboardActivity, getDashboardSummary } from "./api.js";

function Sparkline({ points }) {
  if (!points || points.length < 2) return null;
  const w = 120;
  const h = 28;
  const step = w / (points.length - 1);
  const d = points
    .map((p, i) => `${i === 0 ? "M" : "L"} ${i * step} ${h - (p / 100) * h}`)
    .join(" ");
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} width={w} height={h}
         aria-hidden="true">
      <path d={d} fill="none" stroke="currentColor" strokeWidth="2"
            strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function trendDirection(points) {
  if (!points || points.length < 2) return null;
  const delta = points[points.length - 1] - points[0];
  if (Math.abs(delta) < 1) return { label: "steady", cls: "flat" };
  return delta > 0
    ? { label: `+${Math.round(delta)}%`, cls: "up" }
    : { label: `${Math.round(delta)}%`, cls: "down" };
}

function ReadinessMeter({ value, subjectsWithAttempts }) {
  if (value === null || value === undefined) {
    return (
      <div className="stat">
        <div className="stat-value muted">—</div>
        <div className="stat-label">
          Take a quiz to see how ready you are
        </div>
      </div>
    );
  }
  return (
    <div className="stat">
      <div className="stat-value good">{value}%</div>
      <div className="stat-label">
        Overall readiness across {subjectsWithAttempts}{" "}
        {subjectsWithAttempts === 1 ? "subject" : "subjects"}
      </div>
      <div className="meter" role="img" aria-label={`Readiness ${value} percent`}>
        <div className="meter-fill" style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

function relativeTime(iso) {
  const then = new Date(iso);
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return then.toLocaleDateString();
}

const EVENT_LABEL = {
  chat_session: "Started a chat",
  quiz_completed: "Finished a quiz",
  quiz_in_progress: "Quiz left unfinished",
};

const EVENT_ICON = {
  chat_session: "C",
  quiz_completed: "Q",
  quiz_in_progress: "!",
};

export default function Dashboard({ user, onStudy, onQuiz }) {
  const [summary, setSummary] = useState(null);
  const [activity, setActivity] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([getDashboardSummary(), getDashboardActivity(10)])
      .then(([s, a]) => {
        setSummary(s);
        setActivity(a.events);
      })
      .catch((e) => setError(e.message));
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!summary) return <p className="hint">Loading your dashboard…</p>;

  const nothingYet =
    summary.total_completed_attempts === 0 &&
    summary.total_in_progress_attempts === 0 &&
    (activity?.length ?? 0) === 0;

  return (
    <div className="dashboard">
      <h1 className="page-title">Welcome back, {user.name}</h1>
      <p className="page-sub">
        {summary.subjects.length}{" "}
        {summary.subjects.length === 1 ? "subject" : "subjects"} in your year
      </p>

      <div className="stat-row">
        <ReadinessMeter
          value={summary.overall_readiness}
          subjectsWithAttempts={summary.subjects_with_attempts}
        />
        <div className="stat">
          <div className="stat-value">{summary.total_completed_attempts}</div>
          <div className="stat-label">Quizzes completed</div>
        </div>
        <div className="stat">
          <div className="stat-value">{summary.subjects.length}</div>
          <div className="stat-label">Active subjects</div>
        </div>
      </div>

      {nothingYet && (
        <p className="empty-note">
          You haven't studied anything yet. Pick a subject below to start a chat
          or take your first quiz.
        </p>
      )}

      <h2 className="section-title">Your subjects</h2>
      {summary.subjects.length === 0 && (
        <p className="hint">No subjects are set up for your year yet.</p>
      )}
      <ul className="subject-grid">
        {summary.subjects.map((s) => {
          const trend = trendDirection(s.trend);
          return (
            <li key={s.subject_id} className="subject-card">
              <div className="subject-head">
                <span className="badge badge-lg">
                  {s.subject_code.slice(0, 2).toUpperCase()}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div className="subject-name" dir="auto">
                    {s.subject_name}
                  </div>
                  <div className="subject-code" dir="auto">
                    {s.subject_code}
                  </div>
                </div>
              </div>

              {s.has_attempts ? (
                <>
                  <div className="subject-score">
                    <span className="score-value">{s.average_score}%</span>
                    {trend && (
                      <span className={`trend ${trend.cls}`}>{trend.label}</span>
                    )}
                  </div>
                  <div className="subject-meta">
                    {s.completed_attempts}{" "}
                    {s.completed_attempts === 1 ? "quiz" : "quizzes"}
                    {s.best_score !== null && ` · best ${s.best_score}%`}
                  </div>
                  <Sparkline points={s.trend} />
                </>
              ) : (
                <div className="subject-score">
                  <span className="score-none">No attempts yet</span>
                </div>
              )}

              {s.in_progress_attempts > 0 && (
                <div className="unfinished">
                  {s.in_progress_attempts} unfinished{" "}
                  {s.in_progress_attempts === 1 ? "quiz" : "quizzes"}
                </div>
              )}

              <div className="subject-actions">
                <button className="btn-secondary" onClick={() => onStudy(s)}>
                  Study
                </button>
                <button className="btn-small" onClick={() => onQuiz(s)}>
                  Quiz
                </button>
              </div>
            </li>
          );
        })}
      </ul>

      <h2 className="section-title">Recent activity</h2>
      {activity && activity.length === 0 && (
        <p className="hint">Nothing yet — your chats and quizzes will show here.</p>
      )}
      {activity && activity.length > 0 && (
        <ul className="activity">
          {activity.map((e) => (
            <li key={`${e.type}-${e.reference_id}`} className={`ev ${e.type}`}>
              <span className="ev-icon" aria-hidden="true">
                {EVENT_ICON[e.type] ?? "•"}
              </span>
              <div className="ev-body">
                <div className="ev-label">{EVENT_LABEL[e.type] ?? e.type}</div>
                <div className="ev-subject" dir="auto">
                  {e.subject_code} — {e.subject_name}
                </div>
              </div>
              {e.type === "quiz_completed" && (
                <span
                  className={`ev-score ${
                    e.total_questions && e.score / e.total_questions >= 0.7
                      ? "good"
                      : ""
                  }`}
                >
                  {e.score}/{e.total_questions}
                </span>
              )}
              <span className="ev-time">{relativeTime(e.occurred_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
