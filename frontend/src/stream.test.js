// Run with: npm test   (Node's built-in runner, no extra dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";

import { friendlyError, networkFailure } from "./errors.js";
import {
  applyEvent,
  createNdjsonParser,
  failurePlan,
  initialStreamState,
  streamOutcome,
  upsertAnswer,
} from "./stream.js";

const line = (obj) => JSON.stringify(obj) + "\n";

// --- the NDJSON parser -------------------------------------------------------

test("parser: a complete line is one event", () => {
  const p = createNdjsonParser();
  assert.deepEqual(p.push(line({ type: "token", text: "hi" })), [{ type: "token", text: "hi" }]);
});

test("parser: several lines in one chunk come out in order", () => {
  const p = createNdjsonParser();
  const out = p.push(line({ type: "citations", citations: [] }) + line({ type: "token", text: "a" }) + line({ type: "token", text: "b" }));
  assert.deepEqual(out.map((e) => e.type), ["citations", "token", "token"]);
  assert.equal(out[2].text, "b");
});

test("parser: a line split across chunks waits for its newline", () => {
  const p = createNdjsonParser();
  const whole = line({ type: "token", text: "hello world" });
  assert.deepEqual(p.push(whole.slice(0, 9)), []); // mid-JSON
  assert.deepEqual(p.push(whole.slice(9, -1)), []); // everything but the newline
  assert.deepEqual(p.push("\n"), [{ type: "token", text: "hello world" }]);
});

test("parser: feeding the stream one character at a time reassembles it exactly", () => {
  const events = [
    { type: "citations", citations: [{ source_filename: "a.pdf", page_number: 3 }] },
    { type: "token", text: "التجميع " },
    { type: "token", text: "is \"quoted\" and has a \\ backslash" },
    { type: "done", message: { id: 5, content: "x" }, citations: [] },
  ];
  const p = createNdjsonParser();
  const got = [];
  for (const ch of events.map(line).join("")) got.push(...p.push(ch));
  assert.deepEqual(got, events);
});

test("parser: a final line without a newline is released by flush, once", () => {
  const p = createNdjsonParser();
  assert.deepEqual(p.push(line({ type: "token", text: "a" }) + '{"type":"done"}'), [{ type: "token", text: "a" }]);
  assert.deepEqual(p.flush(), [{ type: "done" }]);
  assert.deepEqual(p.flush(), []);
});

test("parser: blank lines and CRLF are tolerated", () => {
  const p = createNdjsonParser();
  assert.deepEqual(p.push('\n\r\n{"type":"token","text":"a"}\r\n\n'), [{ type: "token", text: "a" }]);
});

test("parser: an unreadable line is an 'interrupted' failure, not silent data loss", () => {
  const p = createNdjsonParser();
  assert.throws(() => p.push("{not json}\n"), (e) => e.kind === "interrupted");
});

test("parser + TextDecoder: Arabic split mid-character across network chunks stays intact", () => {
  const text = "ما هي طرق معالجة البيانات الناقصة؟";
  const bytes = new TextEncoder().encode(line({ type: "token", text }));
  const decoder = new TextDecoder("utf-8");
  const p = createNdjsonParser();
  const got = [];
  // Every chunk boundary, including the middle of two-byte letters.
  for (let i = 0; i < bytes.length; i += 3) {
    got.push(...p.push(decoder.decode(bytes.slice(i, i + 3), { stream: true })));
  }
  assert.deepEqual(got, [{ type: "token", text }]);
  assert.ok(!JSON.stringify(got).includes("�"), "no replacement characters");
});

// --- folding events into state -----------------------------------------------

test("state: tokens accumulate in arrival order", () => {
  let s = initialStreamState();
  assert.equal(s.started, false);
  for (const t of ["Clus", "tering", " groups"]) s = applyEvent(s, { type: "token", text: t });
  assert.equal(s.text, "Clustering groups");
  assert.equal(s.started, true);
});

test("state: citations are available before any text", () => {
  let s = applyEvent(initialStreamState(), { type: "citations", citations: [{ page_number: 1 }] });
  assert.equal(s.started, true);
  assert.equal(s.text, "");
  assert.deepEqual(s.citations, [{ page_number: 1 }]);
});

test("state: done makes the screen match what was stored, and takes its citations", () => {
  let s = initialStreamState();
  s = applyEvent(s, { type: "citations", citations: [{ page_number: 1 }] });
  s = applyEvent(s, { type: "token", text: "  answer  " });
  s = applyEvent(s, { type: "done", message: { id: 9, content: "answer" }, citations: [] });
  assert.equal(s.text, "answer"); // the stored, trimmed text
  assert.deepEqual(s.citations, []); // withdrawn, as for a 'not found' answer
  assert.equal(s.message.id, 9);
  assert.equal(streamOutcome(s), "done");
});

test("state: an error event is recorded and the partial text is kept", () => {
  let s = applyEvent(initialStreamState(), { type: "token", text: "partial" });
  s = applyEvent(s, { type: "error", detail: "generic" });
  assert.equal(s.text, "partial");
  assert.equal(streamOutcome(s), "error");
});

test("state: a stream that just ends is 'interrupted'; unknown events change nothing", () => {
  const s = applyEvent(initialStreamState(), { type: "token", text: "x" });
  assert.equal(streamOutcome(s), "interrupted");
  assert.equal(applyEvent(s, { type: "mystery" }), s);
  assert.equal(applyEvent(s, undefined), s);
  assert.equal(applyEvent(s, { type: "token" }).text, "x"); // missing text is ignored
});

// --- the answer bubble in the message list ------------------------------------

test("list: the answer is inserted once, then updated in place without moving", () => {
  const user = { id: "pending-1", role: "user", content: "q" };
  const other = { id: 3, role: "assistant", content: "older" };
  let s = applyEvent(initialStreamState(), { type: "token", text: "a" });
  let list = upsertAnswer([other, user], "stream-1", s);
  assert.deepEqual(list.map((m) => m.id), [3, "pending-1", "stream-1"]);
  assert.equal(list[2].streaming, true);

  s = applyEvent(s, { type: "token", text: "b" });
  list = upsertAnswer(list, "stream-1", s);
  assert.equal(list.length, 3);
  assert.equal(list[2].content, "ab");
});

test("list: on done the placeholder becomes the stored message, same position", () => {
  let s = applyEvent(initialStreamState(), { type: "token", text: "answer" });
  let list = upsertAnswer([{ id: "pending-1", role: "user", content: "q" }], "stream-1", s);
  s = applyEvent(s, { type: "done", message: { id: 42, created_at: "t", content: "answer" }, citations: [] });
  list = upsertAnswer(list, "stream-1", s);
  assert.deepEqual(list.map((m) => m.id), ["pending-1", 42]);
  assert.equal(list[1].streaming, false);
});

test("list: a note ends the streaming state and rides on the message", () => {
  const s = applyEvent(initialStreamState(), { type: "token", text: "part" });
  const [m] = upsertAnswer([], "stream-1", s, { note: "Stopped.", noteTone: "muted" });
  assert.equal(m.streaming, false);
  assert.equal(m.note, "Stopped.");
  assert.equal(m.noteTone, "muted");
});

// --- what to do when an answer does not complete --------------------------------

const withText = applyEvent(initialStreamState(), { type: "token", text: "partial" });
const noText = applyEvent(initialStreamState(), { type: "citations", citations: [] });
const abort = Object.assign(new Error("aborted"), { name: "AbortError" });

test("failure: Stop with text keeps it, muted, no error banner", () => {
  const plan = failurePlan(abort, withText);
  assert.deepEqual([plan.keepPartial, plan.tone, plan.banner, plan.cause.kind], [true, "muted", false, "stopped"]);
});

test("failure: Stop before any text just undoes the attempt, silently", () => {
  const plan = failurePlan(abort, noText);
  assert.deepEqual([plan.keepPartial, plan.banner], [false, false]);
});

test("failure: a break part-way keeps the partial text with an error note, not a banner", () => {
  const err = Object.assign(new Error("x"), { kind: "unavailable" });
  const plan = failurePlan(err, withText);
  assert.deepEqual([plan.keepPartial, plan.tone, plan.banner, plan.cause.kind], [true, "error", false, "unavailable"]);
});

test("failure: refused before any text undoes the attempt and shows the banner", () => {
  const err = Object.assign(new Error("x"), { status: 429, kind: "rate_limited" });
  const plan = failurePlan(err, noText);
  assert.deepEqual([plan.keepPartial, plan.banner, plan.cause.kind], [false, true, "rate_limited"]);
});

test("failure: a stream that just closed counts as interrupted", () => {
  assert.equal(failurePlan(null, withText).cause.kind, "interrupted");
  assert.equal(failurePlan(null, withText).keepPartial, true);
  assert.equal(failurePlan(null, noText).banner, true);
});

// --- messages ---------------------------------------------------------------------

test("messages: interrupted and stopped are translated", () => {
  assert.match(friendlyError({ kind: "interrupted" }, "en"), /interrupted/i);
  assert.match(friendlyError({ kind: "interrupted" }, "ar"), /^[؀-ۿ]/);
  assert.match(friendlyError({ kind: "stopped" }, "en"), /not saved/i);
  assert.match(friendlyError({ kind: "stopped" }, "ar"), /^[؀-ۿ]/);
});

// --- network failures (item 4) -----------------------------------------------------

test("network failure: 'Failed to fetch' becomes the translated 'unavailable' error", () => {
  const err = networkFailure(new TypeError("Failed to fetch"));
  assert.equal(err.kind, "unavailable");
  assert.match(friendlyError(err, "en"), /busy or unavailable/i);
  assert.match(friendlyError(err, "ar"), /الذكاء الاصطناعي/);
  assert.ok(!friendlyError(err, "en").includes("Failed to fetch"));
});

test("network failure: an abort is the caller's choice and passes through untouched", () => {
  assert.equal(networkFailure(abort), abort);
});

// --- the reconciled answer: citations the server checked ------------------------------
//
// The server sends only text whose citations it has already checked, and the "done"
// event repeats the stored text. These run whole streams, byte-chunked, through the
// parser, the state and the message list, as Chat.jsx does.

const playStream = (events, chunk = 7) => {
  const bytes = new TextEncoder().encode(events.map(line).join(""));
  const decoder = new TextDecoder("utf-8");
  const parser = createNdjsonParser();
  let state = initialStreamState();
  let messages = [{ id: "u", role: "user", content: "q" }];
  for (let i = 0; i < bytes.length; i += chunk) {
    for (const ev of parser.push(decoder.decode(bytes.slice(i, i + chunk), { stream: true }))) {
      state = applyEvent(state, ev);
      messages = upsertAnswer(messages, "a", state);
    }
  }
  return { state, messages, answer: messages.find((m) => m.role === "assistant") };
};

const SRC = [{ source_filename: "dm.pdf", page_number: 4 }];

test("reconciled: English answer ends as the stored text, with only the retrieved sources", () => {
  const { answer, state } = playStream([
    { type: "citations", citations: SRC },
    { type: "token", text: "Support is a ratio " },
    { type: "token", text: "[dm.pdf, page 4]." },
    { type: "done", message: { id: 5, content: "Support is a ratio [dm.pdf, page 4]." }, citations: SRC },
  ]);
  assert.equal(answer.content, "Support is a ratio [dm.pdf, page 4].");
  assert.equal(answer.id, 5);
  assert.equal(answer.streaming, false);
  assert.deepEqual(state.citations, SRC);
});

test("reconciled: Arabic answer, Arabic-Indic digits already canonical, survives byte-splitting intact", () => {
  const text = "الدعم نسبة [dm.pdf, page 4] وهي مفيدة.";
  const { answer } = playStream(
    [
      { type: "citations", citations: SRC },
      { type: "token", text: "الدعم نسبة " },
      { type: "token", text: "[dm.pdf, page 4] وهي مفيدة." },
      { type: "done", message: { id: 6, content: text }, citations: SRC },
    ],
    5, // splits inside two-byte letters
  );
  assert.equal(answer.content, text);
  assert.ok(!answer.content.includes("�"));
  assert.ok(!/[٠-٩]/.test(answer.content), "no Arabic-Indic digits left in a citation");
});

test("reconciled: if the final text differs from what streamed, the screen ends on the final text", () => {
  const { answer, state } = playStream([
    { type: "citations", citations: SRC },
    { type: "token", text: "A [dm.pdf, page 4] and more" },
    { type: "done", message: { id: 7, content: "A [dm.pdf, page 4]." }, citations: SRC },
  ]);
  assert.equal(answer.content, "A [dm.pdf, page 4].");
  assert.equal(state.text, answer.content);
});

test("reconciled: a withdrawn answer (apology) shows the apology and no sources", () => {
  const apology = "عذرًا، لا أستطيع تقديم إجابة.";
  const { answer, state } = playStream([
    { type: "citations", citations: SRC },
    { type: "token", text: apology },
    { type: "done", message: { id: 8, content: apology }, citations: [] },
  ]);
  assert.equal(answer.content, apology);
  assert.deepEqual(state.citations, []);
});

test("reconciled: the sources shown are the done event's, never an earlier or longer list", () => {
  const { state } = playStream([
    { type: "citations", citations: [...SRC, { source_filename: "dm.pdf", page_number: 9 }] },
    { type: "token", text: "x" },
    { type: "done", message: { id: 9, content: "x" }, citations: SRC },
  ]);
  assert.deepEqual(state.citations, SRC);
});

// --- sources of a reopened chat ---------------------------------------------------------

import { citationsByMessage } from "./stream.js";

test("reopened chat: each assistant reply gets back the sources stored with it", () => {
  const a = [{ source_filename: "dm.pdf", page_number: 4 }];
  const b = [{ source_filename: "dm.pdf", page_number: 9 }];
  const out = citationsByMessage([
    { id: 1, role: "user", content: "q", citations: [] },
    { id: 2, role: "assistant", content: "a", citations: a },
    { id: 3, role: "user", content: "q2", citations: [] },
    { id: 4, role: "assistant", content: "b", citations: b },
  ]);
  assert.deepEqual(out, { 2: a, 4: b });
});

test("reopened chat: refusals, old replies and missing fields have no Sources", () => {
  const out = citationsByMessage([
    { id: 1, role: "assistant", content: "refusal", citations: [] },
    { id: 2, role: "assistant", content: "old reply" }, // stored before sources existed
    { id: 3, role: "assistant", content: "x", citations: null },
  ]);
  assert.deepEqual(out, {});
  assert.deepEqual(citationsByMessage(undefined), {});
});

test("reopened chat: sources shown after a live answer equal those shown after reopening", () => {
  const SRC = [{ source_filename: "dm.pdf", page_number: 4 }];
  const { state } = playStream([
    { type: "citations", citations: SRC },
    { type: "token", text: "x" },
    { type: "done", message: { id: 5, role: "assistant", content: "x", citations: SRC }, citations: SRC },
  ]);
  const reopened = citationsByMessage([{ id: 5, role: "assistant", content: "x", citations: SRC }]);
  assert.deepEqual(reopened[5], state.citations);
});
