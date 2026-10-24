// Drives the REAL Quiz.jsx in jsdom against a scripted /quiz/start/stream.
//
// Opt-in (npm run test:ui): it needs jsdom and Testing Library, which are not
// project dependencies. Install them without touching package.json:
//   npm install --no-save jsdom @testing-library/react @testing-library/dom
// The component is bundled with esbuild (already present as a dependency of vite).
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost/" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const React = (await import("react")).default;
const { render, screen, fireEvent, cleanup, waitFor, act } = await import("@testing-library/react");
const { default: Quiz } = await import("./.build/quiz.bundle.mjs");

const enc = new TextEncoder();
const subject = { id: 7, name: "Data Mining", code: "DM501" };

/** A scripted server. */
function server({ startStatus = 200 } = {}) {
  const calls = { answer: [], complete: 0, results: 0, startSignal: null };
  let controller;
  let aborted = false;
  const body = new ReadableStream({
    start(c) { controller = c; },
    cancel() { aborted = true; },
  });
  const q = (id) => ({ id, question_text: `Question text ${id}`, options: [`opt-${id}-a`, `opt-${id}-b`, `opt-${id}-c`, `opt-${id}-d`], source_citation: `lec.pdf, page ${id}` });
  const api = {
    calls,
    q,
    get aborted() { return aborted || calls.startSignal?.aborted; },
    push(obj) { controller.enqueue(enc.encode(JSON.stringify(obj) + "\n")); },
    start(requested) { api.push({ type: "start", attempt_id: 9, subject_id: 7, language: "en", requested }); },
    question(id, index) { api.push({ type: "question", index, question: q(id) }); },
    done(total, requested, partial) { api.push({ type: "done", attempt_id: 9, total_questions: total, requested, partial }); controller.close(); },
    error(total) { api.push({ type: "error", detail: "x", attempt_id: 9, total_questions: total, partial: true }); controller.close(); },
    drop() { controller.error(new TypeError("network error")); },
  };
  let complete = false;
  globalThis.fetch = async (url, init = {}) => {
    const path = new URL(url).pathname;
    if (path === "/quiz/start/stream") {
      calls.startSignal = init.signal;
      init.signal?.addEventListener("abort", () => { try { controller.error(new DOMException("aborted", "AbortError")); } catch {} });
      if (startStatus !== 200) return new Response(JSON.stringify({ detail: "x" }), { status: startStatus });
      return new Response(body);
    }
    const m = path.match(/^\/quiz\/9\/(\w+)$/);
    if (m && m[1] === "answer") {
      const sent = JSON.parse(init.body);
      calls.answer.push(sent);
      return new Response(JSON.stringify({
        item_id: sent.item_id, is_correct: sent.chosen_answer_index === 0, correct_answer_index: 0,
        source_citation: "lec.pdf, page 1", answered_count: calls.answer.length, total_questions: 99,
        is_complete: api.nextIsComplete ?? false,
      }));
    }
    if (m && m[1] === "complete") { calls.complete++; complete = true; return new Response(JSON.stringify({ attempt_id: 9 })); }
    if (m && m[1] === "results") {
      calls.results++;
      if (!complete && !api.nextIsComplete) return new Response(JSON.stringify({ detail: "Finish the quiz first" }), { status: 409 });
      return new Response(JSON.stringify({
        attempt_id: 9, subject_id: 7, language: "en", score: 1, total_questions: calls.answer.length,
        created_at: "2026-01-01T00:00:00Z", completed_at: "2026-01-01T00:01:00Z",
        items: calls.answer.map((a, i) => ({ id: a.item_id, question_text: `Question text ${a.item_id}`, options: ["a", "b", "c", "d"], correct_answer_index: 0, chosen_answer_index: a.chosen_answer_index, is_correct: a.chosen_answer_index === 0, source_citation: "x" })),
      }));
    }
    throw new Error("unexpected fetch " + path);
  };
  return api;
}

const mount = () => render(React.createElement(Quiz, { subject, onExit: () => {} }));
const clickStart = () => fireEvent.click(screen.getByRole("button", { name: /start quiz/i }));
const tick = () => act(async () => { await new Promise((r) => setTimeout(r, 15)); });

afterEach(() => cleanup());

test("the quiz opens on the first question while the rest are still being written", async () => {
  const s = server();
  mount();
  clickStart();
  await tick();
  assert.ok(screen.getByRole("button", { name: /generating/i }), "waiting for the first question");
  assert.equal(screen.queryByText(/Question text 1/), null);

  await act(async () => { s.start(3); s.question(1, 0); });
  await tick();
  assert.ok(screen.getByText("Question text 1"));
  assert.ok(screen.getByText(/Question 1 of 3/));
  assert.ok(screen.getByText(/Writing question 2 of 3/), "the generating indicator");
  assert.ok(screen.getByRole("button", { name: "Stop" }));
});

test("the student can answer question 1 at once; the next button waits for question 2", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(3); s.question(1, 0); }); await tick();

  fireEvent.click(screen.getByText("opt-1-a"));
  await waitFor(() => assert.ok(screen.getByText("Correct")));
  assert.deepEqual(s.calls.answer, [{ item_id: 1, chosen_answer_index: 0 }]);
  const wait = screen.getByRole("button", { name: /writing the next question/i });
  assert.equal(wait.disabled, true, "nothing to go to yet");

  await act(async () => { s.question(2, 1); }); await tick();
  const next = screen.getByRole("button", { name: "Next question" });
  assert.equal(next.disabled, false);
  fireEvent.click(next);
  await tick();
  assert.ok(screen.getByText("Question text 2"));
  assert.ok(screen.getByText(/Question 2 of 3/));
});

test("a full run ends with the results and no leftover indicator", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(2); s.question(1, 0); s.question(2, 1); s.done(2, 2, false); }); await tick();
  assert.equal(screen.queryByText(/Writing question/), null, "all written, no indicator");
  assert.ok(screen.getByText(/Question 1 of 2/));

  fireEvent.click(screen.getByText("opt-1-a")); await waitFor(() => screen.getByText("Correct"));
  fireEvent.click(screen.getByRole("button", { name: "Next question" })); await tick();
  s.nextIsComplete = true; // the server completes on the last answer
  fireEvent.click(screen.getByText("opt-2-a")); await waitFor(() => screen.getByText("Correct"));
  fireEvent.click(screen.getByRole("button", { name: "See results" })); await tick();
  await waitFor(() => assert.ok(screen.getByText(/Quiz results/)));
  assert.equal(s.calls.complete, 0, "already complete: no extra call");
});

test("Stop keeps the questions written so far, says so, and the quiz can still be finished", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(5); s.question(1, 0); s.question(2, 1); }); await tick();

  fireEvent.click(screen.getByRole("button", { name: "Stop" })); await tick();
  assert.ok(s.aborted, "the request was aborted so the server stops generating");
  assert.ok(screen.getByText(/Generation stopped/));
  assert.equal(screen.queryByText(/Writing question/), null);
  assert.ok(screen.getByText("Question text 1"), "what was written stays");
  assert.ok(screen.getByText(/Question 1 of 2/), "the total becomes what exists, not what was asked");
  assert.equal(document.querySelector(".error"), null, "stopping is not an error");

  fireEvent.click(screen.getByText("opt-1-a")); await waitFor(() => screen.getByText("Correct"));
  fireEvent.click(screen.getByRole("button", { name: "Next question" })); await tick();
  fireEvent.click(screen.getByText("opt-2-a")); await waitFor(() => screen.getByText("Correct"));
  fireEvent.click(screen.getByRole("button", { name: "See results" })); await tick();
  await waitFor(() => assert.ok(screen.getByText(/Quiz results/)));
  assert.equal(s.calls.complete, 1, "the quiz was closed explicitly, since the last answer did not");
});

test("Cancel before the first question returns to setup quietly", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" })); await tick();
  assert.ok(s.aborted);
  assert.equal(document.querySelector(".error"), null);
  assert.equal(screen.getByRole("button", { name: /start quiz/i }).disabled, false, "can start again");
});

test("a refusal (429) before the first question shows the translated message on the setup screen", async () => {
  server({ startStatus: 429 });
  mount(); clickStart(); await tick();
  await waitFor(() => assert.match(document.querySelector(".error").textContent, /too many requests/i));
  assert.ok(screen.getByRole("button", { name: /start quiz/i }));
});

test("the same refusal is shown in Arabic when the quiz language is Arabic", async () => {
  server({ startStatus: 503 });
  mount();
  const languageSelect = [...document.querySelectorAll("select")].find((el) => [...el.options].some((o) => o.value === "ar"));
  fireEvent.change(languageSelect, { target: { value: "ar" } });
  clickStart(); await tick();
  await waitFor(() => assert.match(document.querySelector(".error").textContent, /الذكاء الاصطناعي/));
});

test("a failure part-way keeps the questions and explains", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(5); s.question(1, 0); s.question(2, 1); s.error(2); }); await tick();
  assert.match(document.querySelector(".quiz-note").textContent, /Some questions could not be generated/);
  assert.ok(document.querySelector(".quiz-note-error"));
  assert.ok(screen.getByText(/Question 1 of 2/));
  assert.equal(screen.queryByText(/Writing question/), null, "no spinner left behind");
  assert.equal(screen.queryByRole("button", { name: "Stop" }), null);
});

test("a dropped connection part-way also keeps the questions", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(5); s.question(1, 0); }); await tick();
  await act(async () => { s.drop(); }); await tick();
  assert.match(document.querySelector(".quiz-note").textContent, /Some questions could not be generated/);
  assert.ok(screen.getByText("Question text 1"));
  assert.equal(screen.queryByText(/Writing question/), null);
});

test("running out of material is a calm note, not an error", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(5); s.question(1, 0); s.question(2, 1); s.done(2, 5, true); }); await tick();
  assert.match(document.querySelector(".quiz-note").textContent, /Only 2 of 5 questions/);
  assert.ok(document.querySelector(".quiz-note-muted"));
});

test("leaving the page mid-quiz aborts the request", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(5); s.question(1, 0); }); await tick();
  cleanup(); // unmount
  await tick();
  assert.ok(s.aborted, "navigating away stops the server generating");
});

test("a late event after Stop does not bring the indicator back", async () => {
  const s = server();
  mount(); clickStart(); await tick();
  await act(async () => { s.start(5); s.question(1, 0); }); await tick();
  fireEvent.click(screen.getByRole("button", { name: "Stop" })); await tick();
  await act(async () => { try { s.question(2, 1); } catch {} }); await tick();
  assert.equal(screen.queryByText(/Writing question/), null);
  assert.ok(screen.getByText(/Question 1 of 1/));
});
