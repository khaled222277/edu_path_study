// Renders an answer so that its citations keep their own left-to-right order inside
// Arabic text.
//
// A citation such as "[kdd_lec08.pdf, page 37]" is Latin, digits and punctuation. Inside a
// right-to-left sentence the browser's bidi algorithm lets the neutral punctuation ("[",
// ",", "]") take the surrounding direction, so it shows scrambled ("]page 37 ,kdd.pdf["). Wrapping each
// citation in <bdi dir="ltr"> isolates it: it is laid out as its own left-to-right run and
// placed as one unit in the sentence. Only the markup changes; the stored text is untouched.
import { createElement as h, Fragment } from "react";

// File name ending in .pdf, optionally followed by a page part, all inside one bracket pair.
const CITATION = /\[[^\[\]\n]{1,200}?\.pdf(?:[,،][^\[\]\n]{0,40})?\]/gi;

/** `text` as [{ cite: boolean, text }] in reading order; joining the texts gives `text` back. */
export function splitCitations(text) {
  const out = [];
  if (!text) return out;
  let last = 0;
  for (const m of text.matchAll(CITATION)) {
    if (m.index > last) out.push({ cite: false, text: text.slice(last, m.index) });
    out.push({ cite: true, text: m[0] });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ cite: false, text: text.slice(last) });
  return out;
}

/** The answer text with each citation in a left-to-right isolate. */
export function MessageText({ text }) {
  const parts = splitCitations(text);
  if (!parts.some((p) => p.cite)) return text ?? null;
  return h(
    Fragment,
    null,
    parts.map((p, i) =>
      p.cite ? h("bdi", { key: i, dir: "ltr", className: "msg-cite" }, p.text) : h(Fragment, { key: i }, p.text),
    ),
  );
}
