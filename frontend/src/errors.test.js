// Run with: npm test   (Node's built-in runner, no extra dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";

import { friendlyError, guessLanguage, kindForStatus } from "./errors.js";

test("429 and 503 map to friendly kinds, everything else does not", () => {
  assert.equal(kindForStatus(429), "rate_limited");
  assert.equal(kindForStatus(503), "unavailable");
  for (const status of [200, 400, 401, 403, 404, 409, 422, 500, 502]) {
    assert.equal(kindForStatus(status), null, String(status));
  }
});

test("rate-limit message is translated and ignores the server text", () => {
  const err = { kind: "rate_limited", message: "Too many requests. Please slow down" };
  assert.match(friendlyError(err, "en"), /too many requests/i);
  assert.match(friendlyError(err, "ar"), /^[؀-ۿ]/); // starts in Arabic script
  assert.notEqual(friendlyError(err, "en"), friendlyError(err, "ar"));
});

test("unavailable message is translated", () => {
  const err = { kind: "unavailable", message: "Could not generate a quiz right now." };
  assert.match(friendlyError(err, "en"), /busy or unavailable/i);
  assert.match(friendlyError(err, "ar"), /الذكاء الاصطناعي/);
});

test("an unknown language falls back to English", () => {
  const err = { kind: "unavailable" };
  assert.equal(friendlyError(err, "fr"), friendlyError(err, "en"));
});

test("other errors keep the server's own message", () => {
  assert.equal(friendlyError({ kind: null, message: "Quiz attempt not found" }), "Quiz attempt not found");
  assert.equal(friendlyError({ message: "Not authenticated" }, "ar"), "Not authenticated");
});

test("an error with no message still produces something to show", () => {
  assert.ok(friendlyError({}).length > 0);
  assert.ok(friendlyError(undefined).length > 0);
  assert.ok(friendlyError(null, "ar").length > 0);
});

test("language is guessed from the script the student typed", () => {
  assert.equal(guessLanguage("ما هي طرق معالجة البيانات الناقصة؟"), "ar");
  assert.equal(guessLanguage("What is clustering?"), "en");
  assert.equal(guessLanguage("ما هو الـ clustering في data mining"), "ar"); // opens in Arabic
  assert.equal(guessLanguage("Explain الانحدار briefly please"), "en"); // English frame
  assert.equal(guessLanguage(""), "en");
  assert.equal(guessLanguage("12345 ???"), "en");
  assert.equal(guessLanguage(undefined), "en");
});

import { signInNotice } from "./errors.js";

test("signInNotice: the not-allowed redirect gets one generic message, anything else none", () => {
  const msg = signInNotice("?signin=not_allowed");
  assert.match(msg, /cannot be used to sign in/);
  assert.ok(!/@|allowlist|list/i.test(msg), "reveals nothing about who is listed");
  assert.equal(signInNotice(""), null);
  assert.equal(signInNotice(undefined), null);
  assert.equal(signInNotice("?signin=other"), null);
  assert.equal(signInNotice("?x=1&signin=not_allowed"), msg);
});
