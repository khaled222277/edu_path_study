import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement as h } from "react";

import { MessageText, splitCitations } from "./messageText.js";

const render = (text) => renderToStaticMarkup(h("div", null, h(MessageText, { text })));
const strip = (html) => html.replace(/<[^>]+>/g, "");

test("splitCitations: plain text is one piece, an empty answer none", () => {
  assert.deepEqual(splitCitations("no citations here"), [{ cite: false, text: "no citations here" }]);
  assert.deepEqual(splitCitations(""), []);
  assert.deepEqual(splitCitations(undefined), []);
});

test("splitCitations: English text keeps its order around the citation", () => {
  assert.deepEqual(splitCitations("Support is a ratio [dm.pdf, page 4]. Next."), [
    { cite: false, text: "Support is a ratio " },
    { cite: true, text: "[dm.pdf, page 4]" },
    { cite: false, text: ". Next." },
  ]);
});

test("splitCitations: Arabic text, mixed text and several citations keep reading order", () => {
  const ar = "الدعم نسبة [kdd_lec08.pdf, page 37] وهي مفيدة [kdd_lec01.pdf, page 7].";
  const parts = splitCitations(ar);
  assert.deepEqual(
    parts.filter((p) => p.cite).map((p) => p.text),
    ["[kdd_lec08.pdf, page 37]", "[kdd_lec01.pdf, page 7]"],
  );
  assert.equal(parts[0].text, "الدعم نسبة ");
  assert.equal(parts.map((p) => p.text).join(""), ar, "nothing lost or reordered");
  const mixed = "AdaBoost (Adaptive Boosting) يجمع المصنفات [kdd_lec08.pdf, page 44] ثم Bagging.";
  assert.equal(splitCitations(mixed).map((p) => p.text).join(""), mixed);
});

test("splitCitations: ordinary brackets, formulas and ranges are not citations", () => {
  for (const t of ["the range [0.0, 1.0] is used", "[v in Values(A)]", "see [1] and [2]", "[page 3]"]) {
    assert.ok(!splitCitations(t).some((p) => p.cite), t);
  }
});

test("splitCitations: file-only citations and Arabic comma are isolated too", () => {
  assert.deepEqual(splitCitations("x [kdd_lec01_Introduction.pdf] y").filter((p) => p.cite).map((p) => p.text), ["[kdd_lec01_Introduction.pdf]"]);
  assert.equal(splitCitations("x [dm.pdf، صفحة 4] y").filter((p) => p.cite).length, 1);
});

test("MessageText: every citation is wrapped in a left-to-right isolate, the text is not changed", () => {
  const ar = "الدعم نسبة [kdd_lec08.pdf, page 37] وهي مفيدة.";
  const html = render(ar);
  assert.equal(html, '<div>الدعم نسبة <bdi dir="ltr" class="cite">[kdd_lec08.pdf, page 37]</bdi> وهي مفيدة.</div>');
  assert.equal(strip(html), ar, "the visible text is exactly the stored text");
});

test("MessageText: English and citation-free text render as plain text", () => {
  assert.equal(render("Just English."), "<div>Just English.</div>");
  assert.equal(render("A [dm.pdf, page 4] B"), '<div>A <bdi dir="ltr" class="cite">[dm.pdf, page 4]</bdi> B</div>');
  assert.equal(render(""), "<div></div>");
});

test("MessageText: HTML-looking text is escaped, never injected", () => {
  const html = render("<img src=x onerror=alert(1)> [dm.pdf, page 4]");
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img"));
});

test("MessageText: a half-written citation while streaming stays plain text until it closes", () => {
  assert.ok(!render("see [dm.pdf, pa").includes("<bdi"));
  assert.ok(render("see [dm.pdf, page 4]").includes("<bdi"));
});
