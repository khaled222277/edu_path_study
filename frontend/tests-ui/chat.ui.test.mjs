// Drives the REAL Chat.jsx in jsdom against a scripted API (streaming, Stop, errors, sources on
// reopen, thumbs). Needs jsdom + Testing Library (devDependencies) and esbuild (a vite dependency):
//   npm run test:ui
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost/" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
// Chat paints streamed text on animation frames; Node has none.
globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

const React = (await import("react")).default;
const { render, screen, fireEvent, cleanup, waitFor, act, within } = await import("@testing-library/react");
const { default: Chat } = await import("./.build/chat.bundle.mjs");

const enc = new TextEncoder();
const user = { id: 1, name: "Test", email: "t@example.com" };
const SRC = (page) => ({ source_filename: "dm.pdf", page_number: page, content_type: "text", score: 0.9, image_url: null });
const SUBJECTS = [{ id: 7, name: "Data Mining", code: "DM501" }];

/** A scripted API. `sessions` / `detail` describe stored chats; `stream` controls the next answer. */
function server({ sessions = [], detail = null, sendStatus = 200, ratingStatus = 204 } = {}) {
  const calls = { ratings: [], sends: [], signal: null };
  let controller;
  const api = {
    calls,
    push(obj) { controller.enqueue(enc.encode(JSON.stringify(obj) + "\n")); },
    close() { controller.close(); },
    drop() { controller.error(new TypeError("network error")); },
  };
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url);
    const method = init.method ?? "GET";
    const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
    if (u.pathname === "/subjects") return json(SUBJECTS);
    if (u.pathname === "/chat/sessions" && method === "GET") return json(sessions);
    if (u.pathname === "/chat/sessions" && method === "POST") return json({ id: 1, subject_id: 7, created_at: "2026-01-01T00:00:00Z" }, 201);
    if (u.pathname === `/chat/sessions/${detail?.id}`) return json(detail);
    if (u.pathname === "/chat/sessions/1/messages/stream") {
      calls.sends.push(JSON.parse(init.body).content);
      calls.signal = init.signal;
      if (sendStatus !== 200) return json({ detail: "x" }, sendStatus);
      const body = new ReadableStream({
        start(c) { controller = c; },
        cancel() {},
      });
      init.signal?.addEventListener("abort", () => { try { controller.error(new DOMException("aborted", "AbortError")); } catch {} });
      return new Response(body);
    }
    const fb = u.pathname.match(/^\/chat\/messages\/(\d+)\/feedback$/);
    if (fb) {
      calls.ratings.push({ method, id: Number(fb[1]), body: init.body ? JSON.parse(init.body) : null });
      return ratingStatus === 204 ? new Response(null, { status: 204 }) : json({ detail: "x" }, ratingStatus);
    }
    throw new Error("unexpected fetch " + method + " " + u.pathname);
  };
  return api;
}

const tick = () => act(async () => { await new Promise((r) => setTimeout(r, 15)); });
const mount = () => render(React.createElement(Chat, { user }));
const openChat = async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
  return screen.findByPlaceholderText("Ask a question…");
};
const ask = async (input, text) => {
  fireEvent.change(input, { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await tick();
};
const bubbles = () => [...document.querySelectorAll(".msg-assistant .msg-body")];

afterEach(() => cleanup());

test("an answer streams in piece by piece, then shows its Sources and thumbs", async () => {
  const s = server();
  const input = await openChat();
  await ask(input, "what is support?");
  assert.ok(screen.getByRole("button", { name: "Stop" }), "Stop while it streams");
  assert.equal(input.disabled, true);

  await act(async () => { s.push({ type: "citations", citations: [SRC(4)] }); s.push({ type: "token", text: "Support is " }); });
  await tick();
  assert.match(bubbles().at(-1).textContent, /Support is/);
  assert.equal(document.querySelectorAll(".answer-rating").length, 0, "no thumbs on a half-written answer");

  await act(async () => {
    s.push({ type: "token", text: "a ratio [dm.pdf, page 4]." });
    s.push({ type: "done", message: { id: 11, role: "assistant", content: "Support is a ratio [dm.pdf, page 4].", created_at: "2026-01-01T00:00:00Z", citations: [SRC(4)], feedback: null }, citations: [SRC(4)] });
    s.close();
  });
  await tick();
  assert.equal(bubbles().at(-1).textContent, "Support is a ratio [dm.pdf, page 4].");
  const bdi = bubbles().at(-1).querySelector("bdi.msg-cite");
  assert.ok(bdi && bdi.getAttribute("dir") === "ltr" && bdi.textContent === "[dm.pdf, page 4]", "the citation is a left-to-right unit");
  assert.ok(document.querySelector(".citations"), "Sources shown");
  assert.match(document.querySelector(".citations").textContent, /dm\.pdf, page 4/);
  assert.ok(screen.getByRole("button", { name: "Good answer" }) && screen.getByRole("button", { name: "Bad answer" }));
  assert.ok(screen.getByRole("button", { name: "Send" }), "Send is back");
});

test("the final event replaces what streamed (the reconciled answer wins)", async () => {
  const s = server();
  const input = await openChat();
  await ask(input, "q");
  await act(async () => { s.push({ type: "token", text: "draft text [ghost.pdf, page 1]" }); });
  await tick();
  await act(async () => {
    s.push({ type: "done", message: { id: 12, role: "assistant", content: "draft text", created_at: "2026-01-01T00:00:00Z", citations: [] }, citations: [] });
    s.close();
  });
  await tick();
  assert.equal(bubbles().at(-1).textContent, "draft text");
  assert.equal(document.querySelector(".citations"), null);
});

test("Stop aborts the request, keeps the partial text with a note, and gives the box back", async () => {
  const s = server();
  const input = await openChat();
  await ask(input, "explain everything");
  await act(async () => { s.push({ type: "token", text: "Partial answer that was cut" }); });
  await tick();
  fireEvent.click(screen.getByRole("button", { name: "Stop" }));
  await tick();
  assert.equal(s.calls.signal.aborted, true, "the request was aborted");
  assert.match(bubbles().at(-1).textContent, /Partial answer that was cut/);
  assert.ok(document.querySelector(".msg-note"), "a note says it was stopped");
  assert.equal(document.querySelector(".msg-streaming"), null);
  assert.equal(document.querySelectorAll(".answer-rating").length, 0, "an unsaved answer cannot be rated");
  assert.ok(screen.getByRole("button", { name: "Send" }));
});

test("a stream that breaks part-way keeps the partial text with an error note, no thumbs", async () => {
  const s = server();
  const input = await openChat();
  await ask(input, "q");
  await act(async () => { s.push({ type: "token", text: "Half an answ" }); });
  await tick();
  await act(async () => { s.push({ type: "error", detail: "The answer was interrupted. Please try again." }); s.close(); });
  await tick();
  assert.match(bubbles().at(-1).textContent, /Half an answ/);
  const note = document.querySelector(".msg-note");
  assert.ok(note && note.className.includes("error"), "an error-toned note");
  assert.equal(document.querySelectorAll(".answer-rating").length, 0);
});

test("a 429 shows the friendly banner and puts the question back in the box", async () => {
  server({ sendStatus: 429 });
  const input = await openChat();
  await ask(input, "my question");
  assert.match(document.querySelector(".error").textContent, /too many requests/i);
  assert.equal(input.value, "my question");
  assert.equal(document.querySelectorAll(".msg-user").length, 0, "the optimistic echo was taken back");
});

test("a 503 shows the translated unavailable banner", async () => {
  server({ sendStatus: 503 });
  const input = await openChat();
  await ask(input, "ما هو الدعم؟");
  assert.match(document.querySelector(".error").textContent, /خدمة الذكاء الاصطناعي|AI service/);
  assert.equal(input.value, "ما هو الدعم؟");
});

const STORED = {
  id: 1, subject_id: 7, created_at: "2026-01-01T00:00:00Z",
  messages: [
    { id: 1, role: "user", content: "what is support?", created_at: "2026-01-01T00:00:00Z", citations: [], feedback: null },
    { id: 2, role: "assistant", content: "Support is a ratio [dm.pdf, page 4].", created_at: "2026-01-01T00:00:01Z", citations: [SRC(4)], feedback: "down" },
    { id: 3, role: "user", content: "and confidence?", created_at: "2026-01-01T00:00:02Z", citations: [], feedback: null },
    { id: 4, role: "assistant", content: "Not covered.", created_at: "2026-01-01T00:00:03Z", citations: [], feedback: null },
    { id: 5, role: "assistant", content: "An older reply.", created_at: "2026-01-01T00:00:04Z" },
  ],
};

test("reopening a chat shows each reply's stored Sources and its rating", async () => {
  server({ sessions: [{ id: 1, subject_id: 7, created_at: "2026-01-01T00:00:00Z" }], detail: STORED });
  mount();
  fireEvent.click(await screen.findByText(/DM501/, { selector: ".session-subject" }));
  await screen.findByText("Support is a ratio", { exact: false });
  const sources = [...document.querySelectorAll(".citations")];
  assert.equal(sources.length, 1, "only the reply that had sources");
  assert.match(sources[0].textContent, /dm\.pdf, page 4/);
  assert.equal(sources[0].closest(".msg").textContent.includes("Support is a ratio"), true, "under the right reply");
  const pressed = [...document.querySelectorAll(".thumb[aria-pressed=true]")].map((t) => t.getAttribute("aria-label"));
  assert.deepEqual(pressed, ["Bad answer"]);
  assert.equal(document.querySelectorAll(".answer-rating").length, 3, "every stored assistant reply can be rated");
});

test("thumbs: down opens the comment box, the comment is sent, and the same thumb again clears it", async () => {
  const s = server({ sessions: [{ id: 1, subject_id: 7, created_at: "x" }], detail: { ...STORED, messages: [STORED.messages[0], { ...STORED.messages[1], feedback: null }] } });
  mount();
  fireEvent.click(await screen.findByText(/DM501/, { selector: ".session-subject" }));
  await screen.findByText("Support is a ratio", { exact: false });

  fireEvent.click(screen.getByRole("button", { name: "Bad answer" }));
  await tick();
  assert.deepEqual(s.calls.ratings.at(-1), { method: "PUT", id: 2, body: { rating: "down", comment: null } });
  const box = await screen.findByPlaceholderText(/what was wrong/i);
  fireEvent.change(box, { target: { value: "الصفحة غير صحيحة" } });
  fireEvent.click(within(box.closest("form")).getByRole("button", { name: "Send" }));
  await tick();
  assert.deepEqual(s.calls.ratings.at(-1), { method: "PUT", id: 2, body: { rating: "down", comment: "الصفحة غير صحيحة" } });
  assert.equal(screen.queryByPlaceholderText(/what was wrong/i), null, "the box closes");
  assert.equal(screen.getByRole("button", { name: "Bad answer" }).getAttribute("aria-pressed"), "true");

  fireEvent.click(screen.getByRole("button", { name: "Good answer" }));
  await tick();
  assert.deepEqual(s.calls.ratings.at(-1), { method: "PUT", id: 2, body: { rating: "up", comment: null } });
  assert.equal(screen.getByRole("button", { name: "Bad answer" }).getAttribute("aria-pressed"), "false");

  fireEvent.click(screen.getByRole("button", { name: "Good answer" }));
  await tick();
  assert.deepEqual(s.calls.ratings.at(-1), { method: "DELETE", id: 2, body: null });
  assert.equal(screen.getByRole("button", { name: "Good answer" }).getAttribute("aria-pressed"), "false");
});

test("thumbs: if saving fails the thumb goes back and an error shows", async () => {
  server({ sessions: [{ id: 1, subject_id: 7, created_at: "x" }], detail: { ...STORED, messages: [STORED.messages[0], { ...STORED.messages[1], feedback: null }] }, ratingStatus: 503 });
  mount();
  fireEvent.click(await screen.findByText(/DM501/, { selector: ".session-subject" }));
  await screen.findByText("Support is a ratio", { exact: false });
  fireEvent.click(screen.getByRole("button", { name: "Good answer" }));
  await tick();
  assert.equal(screen.getByRole("button", { name: "Good answer" }).getAttribute("aria-pressed"), "false", "rolled back");
  assert.ok(document.querySelector(".error"), "the student is told");
});
