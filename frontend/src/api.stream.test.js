// streamMessage() against a mocked fetch: the real reader loop, no browser needed.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { streamMessage } from "./api.js";
import { friendlyError } from "./errors.js";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const enc = new TextEncoder();
const line = (obj) => JSON.stringify(obj) + "\n";

/** A fetch whose body delivers `chunks` (strings or byte arrays), then optionally fails. */
function mockStream(chunks, { failAfter = null, status = 200, hang = false, onCancel = () => {} } = {}) {
  let i = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (i < chunks.length) {
        const c = chunks[i++];
        controller.enqueue(typeof c === "string" ? enc.encode(c) : c);
      } else if (hang) {
        return new Promise(() => {}); // like a server that is still generating
      } else if (failAfter) {
        controller.error(failAfter);
      } else {
        controller.close();
      }
    },
    cancel: onCancel,
  });
  globalThis.fetch = async () => new Response(body, { status });
}

async function collect(content = "q", opts = {}) {
  const events = [];
  await streamMessage(1, content, { onEvent: (e) => events.push(e), ...opts });
  return events;
}

test("delivers events in order, however the network chunks them", async () => {
  const all = enc.encode(
    line({ type: "citations", citations: [] }) +
      line({ type: "token", text: "ما هو" }) +
      line({ type: "token", text: " التجميع؟" }) +
      line({ type: "done", message: { id: 1, content: "ما هو التجميع؟" }, citations: [] }),
  );
  // 5-byte slices: splits JSON lines and two-byte Arabic letters alike.
  const slices = [];
  for (let i = 0; i < all.length; i += 5) slices.push(all.slice(i, i + 5));
  mockStream(slices);

  const events = await collect();
  assert.deepEqual(events.map((e) => e.type), ["citations", "token", "token", "done"]);
  assert.equal(events[1].text + events[2].text, "ما هو التجميع؟");
});

test("an event with no trailing newline is still delivered", async () => {
  mockStream([line({ type: "token", text: "a" }), '{"type":"done","message":{"id":2,"content":"a"},"citations":[]}']);
  const events = await collect();
  assert.equal(events.at(-1).type, "done");
});

test("posts the question to the stream route with the cookie", async () => {
  let seen;
  globalThis.fetch = async (url, init) => {
    seen = { url, init };
    return new Response(new ReadableStream({ start: (c) => c.close() }));
  };
  await collect("hello?");
  assert.match(seen.url, /\/chat\/sessions\/1\/messages\/stream$/);
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.credentials, "include");
  assert.deepEqual(JSON.parse(seen.init.body), { content: "hello?" });
});

test("429 is refused before any event, tagged and translatable", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "Too many requests." }), { status: 429 });
  const events = [];
  await assert.rejects(
    streamMessage(1, "q", { onEvent: (e) => events.push(e) }),
    (e) => e.status === 429 && e.kind === "rate_limited" && /moment/.test(friendlyError(e, "en")),
  );
  assert.deepEqual(events, []);
});

test("503 is refused before any event, tagged and translatable", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "x" }), { status: 503 });
  await assert.rejects(
    collect(),
    (e) => e.status === 503 && e.kind === "unavailable" && /^[؀-ۿ]/.test(friendlyError(e, "ar")),
  );
});

test("a non-JSON error body still produces a usable error", async () => {
  globalThis.fetch = async () => new Response("<html>bad gateway</html>", { status: 502 });
  await assert.rejects(collect(), (e) => e.status === 502 && /502/.test(e.message));
});

test("backend down ('Failed to fetch') becomes the translated unavailable error", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(collect(), (e) => e.kind === "unavailable" && !/Failed to fetch/.test(friendlyError(e)));
});

test("the connection dropping part-way keeps what arrived and rejects as unavailable", async () => {
  mockStream([line({ type: "token", text: "par" }), line({ type: "token", text: "tial" })], {
    failAfter: new TypeError("network error"),
  });
  const events = [];
  await assert.rejects(
    streamMessage(1, "q", { onEvent: (e) => events.push(e) }),
    (e) => e.kind === "unavailable",
  );
  assert.equal(events.map((e) => e.text).join(""), "partial");
});

test("aborting rejects with AbortError, not a translated network error", async () => {
  const controller = new AbortController();
  globalThis.fetch = async (_url, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () =>
        reject(new DOMException("The operation was aborted.", "AbortError")),
      );
    });
  const pending = streamMessage(1, "q", { signal: controller.signal, onEvent() {} });
  controller.abort();
  await assert.rejects(pending, (e) => e.name === "AbortError" && e.kind === undefined);
});

test("aborting mid-body also surfaces as AbortError", async () => {
  mockStream([line({ type: "token", text: "a" })], {
    failAfter: new DOMException("The operation was aborted.", "AbortError"),
  });
  await assert.rejects(collect(), (e) => e.name === "AbortError");
});

test("a garbled line fails the stream as interrupted", async () => {
  mockStream([line({ type: "token", text: "a" }), "this is not json\n"]);
  await assert.rejects(collect(), (e) => e.kind === "interrupted");
});

test("the body is always cancelled afterwards, so the server stops generating", async () => {
  // The server is still generating when the client gives up (here: an unreadable
  // line). Cancelling the body is what closes the connection and stops it.
  let cancelled = 0;
  mockStream([line({ type: "token", text: "a" }), "garbage\n"], {
    hang: true,
    onCancel: () => cancelled++,
  });
  await assert.rejects(collect());
  assert.equal(cancelled, 1);
});
