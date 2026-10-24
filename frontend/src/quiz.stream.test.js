// Streamed quiz: the event reducer, the end-of-stream notes, the failure policy and streamQuiz().
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { completeQuiz, streamQuiz } from "./api.js";
import { friendlyError } from "./errors.js";
import {
  applyQuizEvent,
  initialQuizState,
  quizEndNote,
  quizFailurePlan,
  quizGenerating,
} from "./stream.js";

const q = (id) => ({ id, question_text: `Q${id}?`, options: ["a", "b", "c", "d"], source_citation: "f.pdf, page 1" });
const start = (requested = 5) => ({ type: "start", attempt_id: 7, subject_id: 1, language: "en", requested });
const question = (id, index) => ({ type: "question", index, question: q(id) });
const done = (total, partial = false, requested = 5) => ({ type: "done", attempt_id: 7, total_questions: total, requested, partial });
const fold = (events) => events.reduce(applyQuizEvent, initialQuizState());

// --- the reducer --------------------------------------------------------------------

test("start records the attempt and how many were asked for", () => {
  const s = fold([start(5)]);
  assert.equal(s.attemptId, 7);
  assert.equal(s.requested, 5);
  assert.deepEqual(s.questions, []);
  assert.equal(quizGenerating(s), true);
});

test("questions accumulate in arrival order", () => {
  const s = fold([start(), question(11, 0), question(12, 1), question(13, 2)]);
  assert.deepEqual(s.questions.map((x) => x.id), [11, 12, 13]);
  assert.equal(quizGenerating(s), true);
});

test("a repeated question id is a duplicate, not a new question", () => {
  const s = fold([start(), question(11, 0), question(11, 0), question(12, 1)]);
  assert.deepEqual(s.questions.map((x) => x.id), [11, 12]);
});

test("done stops the generating state and records the final count", () => {
  const s = fold([start(3), question(1, 0), question(2, 1), question(3, 2), done(3)]);
  assert.equal(quizGenerating(s), false);
  assert.equal(s.totalQuestions, 3);
  assert.equal(s.partial, false);
  assert.equal(quizEndNote(s), null, "a complete quiz needs no note");
});

test("a partial done says how many were written", () => {
  const s = fold([start(5), question(1, 0), question(2, 1), done(2, true, 5)]);
  assert.equal(s.partial, true);
  const note = quizEndNote(s);
  assert.equal(note.kind, "quiz_partial");
  assert.deepEqual(note.params, { n: 2, total: 5 });
  assert.equal(friendlyError({ kind: note.kind }, "en", note.params), "Only 2 of 5 questions could be written from this material.");
  assert.match(friendlyError({ kind: note.kind }, "ar", note.params), /2 من أصل 5/);
});

test("an error event keeps the questions and ends generation", () => {
  const s = fold([start(5), question(1, 0), question(2, 1), { type: "error", detail: "x", total_questions: 2, partial: true }]);
  assert.equal(s.failed, true);
  assert.deepEqual(s.questions.map((x) => x.id), [1, 2]);
  assert.equal(s.totalQuestions, 2);
  assert.equal(quizGenerating(s), false);
  assert.equal(quizEndNote(s).kind, "quiz_interrupted");
  assert.equal(quizEndNote(s).tone, "error");
});

test("a stream that closes without a verdict counts as interrupted", () => {
  const s = fold([start(5), question(1, 0)]);
  assert.equal(quizGenerating(s), true, "still looks open to the reducer");
  assert.equal(quizEndNote(s).kind, "quiz_interrupted");
});

test("unknown events and malformed questions change nothing", () => {
  const s = fold([start(), question(1, 0)]);
  assert.equal(applyQuizEvent(s, { type: "mystery" }), s);
  assert.equal(applyQuizEvent(s, undefined), s);
  assert.equal(applyQuizEvent(s, { type: "question" }), s);
});

test("the reducer does not mutate the state it was given", () => {
  const before = fold([start(), question(1, 0)]);
  const snapshot = JSON.stringify(before);
  applyQuizEvent(before, question(2, 1));
  applyQuizEvent(before, done(2));
  assert.equal(JSON.stringify(before), snapshot);
});

// --- failures of the request itself ---------------------------------------------------

const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
const some = fold([start(5), question(1, 0), question(2, 1)]);
const none = fold([start(5)]);

test("Stop with questions on screen keeps them, muted, with no error banner", () => {
  const plan = quizFailurePlan(abort, some);
  assert.deepEqual([plan.keep, plan.banner, plan.tone, plan.cause.kind], [true, false, "muted", "quiz_stopped"]);
  assert.match(friendlyError(plan.cause, "en", plan.params), /stopped/i);
  assert.match(friendlyError(plan.cause, "ar", plan.params), /^[؀-ۿ]/);
});

test("Cancel before any question goes back to setup silently", () => {
  const plan = quizFailurePlan(abort, none);
  assert.deepEqual([plan.keep, plan.banner], [false, false]);
});

test("the connection dropping with questions on screen keeps them with an error note", () => {
  const err = Object.assign(new Error("x"), { kind: "unavailable" });
  const plan = quizFailurePlan(err, some);
  assert.deepEqual([plan.keep, plan.banner, plan.tone, plan.cause.kind], [true, false, "error", "quiz_interrupted"]);
});

test("a refusal before any question (429, 503) goes back to setup with the translated banner", () => {
  const err = Object.assign(new Error("x"), { status: 429, kind: "rate_limited" });
  const plan = quizFailurePlan(err, none);
  assert.deepEqual([plan.keep, plan.banner, plan.cause.kind], [false, true, "rate_limited"]);
  assert.match(friendlyError(plan.cause, "en"), /too many requests/i);
  assert.match(friendlyError(plan.cause, "ar"), /^[؀-ۿ]/);

  const down = Object.assign(new Error("x"), { status: 503, kind: "unavailable" });
  assert.match(friendlyError(quizFailurePlan(down, none).cause, "ar"), /الذكاء الاصطناعي/);
});

test("placeholders with no value are left visible rather than printed as 'undefined'", () => {
  assert.match(friendlyError({ kind: "quiz_partial" }, "en", {}), /\{n\} of \{total\}/);
});

// --- streamQuiz() against a mocked fetch ---------------------------------------------------

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});
const enc = new TextEncoder();
const line = (o) => JSON.stringify(o) + "\n";

function mockBody(chunks, { failAfter = null, hang = false } = {}) {
  let i = 0;
  globalThis.fetch = async () =>
    new Response(
      new ReadableStream({
        pull(c) {
          if (i < chunks.length) c.enqueue(typeof chunks[i] === "string" ? enc.encode(chunks[i++]) : chunks[i++]);
          else if (hang) return new Promise(() => {});
          else if (failAfter) c.error(failAfter);
          else c.close();
        },
      }),
    );
}

test("posts the right request to the quiz stream route", async () => {
  let seen;
  globalThis.fetch = async (url, init) => {
    seen = { url, init };
    return new Response(new ReadableStream({ start: (c) => c.close() }));
  };
  await streamQuiz(7, 5, "ar", { onEvent() {} });
  assert.match(seen.url, /\/quiz\/start\/stream$/);
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.credentials, "include");
  assert.deepEqual(JSON.parse(seen.init.body), { subject_id: 7, question_count: 5, language: "ar" });
});

test("delivers events in order, however the network chunks them, Arabic included", async () => {
  const arabic = { id: 2, question_text: "ما هي عملية تجميع الكائنات؟", options: ["أ", "ب", "ج", "د"], source_citation: "x" };
  const all = enc.encode(line(start(2)) + line(question(1, 0)) + line({ type: "question", index: 1, question: arabic }) + line(done(2)));
  const slices = [];
  for (let i = 0; i < all.length; i += 7) slices.push(all.slice(i, i + 7));
  mockBody(slices);

  const events = [];
  await streamQuiz(1, 2, "ar", { onEvent: (e) => events.push(e) });
  assert.deepEqual(events.map((e) => e.type), ["start", "question", "question", "done"]);
  assert.equal(events[2].question.question_text, arabic.question_text);
  assert.equal(fold(events).questions.length, 2);
});

test("429 and 503 are refused before any event, tagged and translatable", async () => {
  for (const [status, kind] of [[429, "rate_limited"], [503, "unavailable"]]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: "x" }), { status });
    const events = [];
    await assert.rejects(streamQuiz(1, 5, "en", { onEvent: (e) => events.push(e) }), (e) => e.status === status && e.kind === kind);
    assert.deepEqual(events, []);
  }
});

test("a 204 (the server saw us hang up) is an empty stream, not an error", async () => {
  globalThis.fetch = async () => new Response(null, { status: 204 });
  const events = [];
  await streamQuiz(1, 5, "en", { onEvent: (e) => events.push(e) });
  assert.deepEqual(events, []);
});

test("the connection dropping part-way keeps the questions that arrived", async () => {
  mockBody([line(start(5)), line(question(1, 0)), line(question(2, 1))], { failAfter: new TypeError("network error") });
  const events = [];
  const err = await streamQuiz(1, 5, "en", { onEvent: (e) => events.push(e) }).catch((e) => e);
  assert.equal(err.kind, "unavailable");
  const state = fold(events);
  assert.deepEqual(state.questions.map((x) => x.id), [1, 2]);
  assert.equal(quizFailurePlan(err, state).keep, true);
});

test("aborting mid-stream rejects with AbortError (not a translated network error)", async () => {
  // In a browser, aborting the fetch closes the connection by itself -- that is
  // what tells the server to stop -- so all the client must do is not mislabel it.
  const controller = new AbortController();
  const events = [];
  globalThis.fetch = async (_u, init) =>
    new Response(
      new ReadableStream({
        pull(c) {
          c.enqueue(enc.encode(line(start(5))));
          return new Promise((_r, reject) =>
            init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
          );
        },
      }),
    );
  const pending = streamQuiz(1, 5, "en", { signal: controller.signal, onEvent: (e) => events.push(e) });
  setTimeout(() => controller.abort(), 20);
  await assert.rejects(pending, (e) => e.name === "AbortError" && e.kind === undefined);
  assert.deepEqual(events.map((e) => e.type), ["start"], "what arrived before Stop is kept");
});

test("completeQuiz posts to the complete route", async () => {
  let seen;
  globalThis.fetch = async (url, init) => {
    seen = { url, method: init.method };
    return new Response(JSON.stringify({ attempt_id: 7 }), { status: 200 });
  };
  await completeQuiz(7);
  assert.match(seen.url, /\/quiz\/7\/complete$/);
  assert.equal(seen.method, "POST");
});
