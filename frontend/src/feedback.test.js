import assert from "node:assert/strict";
import { test } from "node:test";

import { COMMENT_MAX, canRate, cleanComment, feedbackByMessage, nextRating } from "./feedback.js";

test("canRate: only stored, finished assistant replies", () => {
  assert.equal(canRate({ id: 5, role: "assistant", content: "x" }), true);
  assert.equal(canRate({ id: 5, role: "user", content: "x" }), false);
  assert.equal(canRate({ id: "stream-123", role: "assistant", streaming: true }), false, "placeholder id");
  assert.equal(canRate({ id: "stream-123", role: "assistant" }), false, "a placeholder id alone is enough to refuse");
  assert.equal(canRate({ id: 5, role: "assistant", streaming: true }), false, "still streaming");
  assert.equal(canRate(undefined), false);
});

test("feedbackByMessage: only rated assistant replies, from a loaded chat", () => {
  assert.deepEqual(
    feedbackByMessage([
      { id: 1, role: "user", content: "q", feedback: null },
      { id: 2, role: "assistant", content: "a", feedback: "down" },
      { id: 3, role: "assistant", content: "b", feedback: null },
      { id: 4, role: "assistant", content: "c", feedback: "up" },
      { id: 5, role: "assistant", content: "d" },
      { id: 6, role: "assistant", content: "e", feedback: "bogus" },
    ]),
    { 2: "down", 4: "up" },
  );
  assert.deepEqual(feedbackByMessage(undefined), {});
});

test("nextRating: choosing the other thumb switches, choosing the same one clears", () => {
  assert.equal(nextRating(undefined, "up"), "up");
  assert.equal(nextRating("up", "down"), "down");
  assert.equal(nextRating("down", "down"), null);
});

test("cleanComment: trimmed, limited, empty becomes null", () => {
  assert.equal(cleanComment("  wrong page  "), "wrong page");
  assert.equal(cleanComment("   "), null);
  assert.equal(cleanComment(undefined), null);
  assert.equal(cleanComment("x".repeat(COMMENT_MAX + 50)).length, COMMENT_MAX);
  assert.equal(cleanComment("  الصفحة غير صحيحة "), "الصفحة غير صحيحة");
});
