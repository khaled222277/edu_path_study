// The plain request() path: a dead backend must not surface the browser's raw error.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { getMe, getUniversities, reportQuestion, unreportQuestion } from "./api.js";
import { friendlyError } from "./errors.js";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("backend down: the error is the translated 'unavailable', never 'Failed to fetch'", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(getUniversities(), (e) => {
    assert.equal(e.kind, "unavailable");
    // Screens that print err.message directly (App, Dashboard) get friendly text too.
    assert.doesNotMatch(e.message, /fetch/i);
    assert.match(e.message, /busy or unavailable/i);
    assert.match(friendlyError(e, "ar"), /الذكاء الاصطناعي/);
    return true;
  });
});

test("other HTTP errors are unchanged", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401 });
  await assert.rejects(getMe(), (e) => e.status === 401 && e.kind === null && e.message === "Not authenticated");

  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "slow down" }), { status: 429 });
  await assert.rejects(getMe(), (e) => e.status === 429 && e.kind === "rate_limited");
});

test("a successful call still returns the body", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify([{ id: 1 }]), { status: 200 });
  assert.deepEqual(await getUniversities(), [{ id: 1 }]);
});

test("reporting a question is a PUT on the item with the reason (or null), withdrawing is a DELETE", async () => {
  const seen = [];
  globalThis.fetch = async (url, init) => {
    seen.push([new URL(url).pathname, init.method, init.body]);
    return new Response(null, { status: 204 });
  };
  assert.equal(await reportQuestion(9, 4, "unclear"), null);
  await reportQuestion(9, 4);
  await unreportQuestion(9, 4);
  assert.deepEqual(seen, [
    ["/quiz/9/items/4/report", "PUT", '{"reason":"unclear"}'],
    ["/quiz/9/items/4/report", "PUT", '{"reason":null}'],
    ["/quiz/9/items/4/report", "DELETE", undefined],
  ]);
});
