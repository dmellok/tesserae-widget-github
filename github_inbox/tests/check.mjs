// Plain-node self-check, no test framework. Run: node tests/check.mjs
import assert from "node:assert/strict";
import render, { ageTier, groupItems, kindGlyph, repoShort } from "../client.js";

const NOW = Date.parse("2026-10-16T00:00:00Z");

assert.equal(repoShort("dmellok/tesserae"), "tesserae");
assert.equal(ageTier(0.5).label, "12h");
assert.equal(ageTier(3).color, "var(--accent-2)");
assert.equal(ageTier(400).label, "1y");
assert.equal(ageTier(NaN).label, "-");

assert.equal(kindGlyph({ kind: "pr", draft: true }).icon, "ph-pencil-circle");
assert.equal(kindGlyph({ kind: "pr" }).icon, "ph-git-pull-request");
assert.equal(kindGlyph({ kind: "discussion", answerable: true, answered: false }).icon, "ph-question");
assert.equal(kindGlyph({ kind: "discussion", answerable: true, answered: true }).icon, "ph-chats-circle");
assert.equal(kindGlyph({ kind: "issue" }).icon, "ph-record");

const ITEMS = [
  { kind: "pr", title: "Add things", number: 4, repo: "dmellok/tesserae-widgets", created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-15T00:00:00Z", author: "siilky", comments: 0, labels: [] },
  { kind: "discussion", title: "Localization?", number: 134, repo: "dmellok/tesserae", created_at: "2026-07-21T00:00:00Z", updated_at: "2026-10-14T00:00:00Z", author: "docbobo", comments: 8, category: "Q&A", answerable: true, answered: false, labels: [] },
  { kind: "pr", title: "Draft <thing>", number: 2, repo: "dmellok/tesserae", draft: true, created_at: "2026-09-01T00:00:00Z", updated_at: "2026-10-12T00:00:00Z", author: "me", comments: 1, labels: ["a", "b", "c"] },
  { kind: "issue", title: "Broken", number: 1, repo: "dmellok/tesserae", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-10-10T00:00:00Z", author: "x", comments: 2, labels: ["bug"] },
];

// groupItems keeps first-seen order for repos, fixed order for kinds
let g = groupItems(ITEMS, "repo");
assert.deepEqual(g.map((x) => x.label), ["tesserae-widgets", "tesserae"]);
assert.equal(g[1].items.length, 3);
g = groupItems(ITEMS, "kind");
assert.deepEqual(g.map((x) => x.label), ["Issues", "Pull requests", "Discussions"]);
g = groupItems(ITEMS, "none");
assert.equal(g.length, 1);
assert.equal(g[0].items.length, 4);

function stub() {
  return { innerHTML: "", host: null, querySelector: () => null, querySelectorAll: () => [] };
}
const data = (over) => ({
  items: ITEMS, counts: { issue: 1, pr: 2, discussion: 1 }, total: 9, shown: 4, truncated: false,
  group_by: "repo", show_labels: true, note: "", ...over,
});

let s = stub();
await render(s, { data: data(), now: NOW });
assert.match(s.innerHTML, /1 ISSUE · 2 PRS · 1 DISC/);
assert.match(s.innerHTML, /gi-section-head/);
assert.match(s.innerHTML, /Draft &lt;thing&gt;/, "titles escaped");
assert.match(s.innerHTML, /\+5 more/);
assert.match(s.innerHTML, /gi-label">a</);
assert.match(s.innerHTML, /gi-label">\+1</, "label overflow");
assert.match(s.innerHTML, /gi-cat">Q&amp;A/);
assert.match(s.innerHTML, /ph-question/);
assert.match(s.innerHTML, /DRAFT/);
assert.doesNotMatch(s.innerHTML, /class="gi-repo"/, "repo name hidden when grouped by repo");
assert.match(s.innerHTML, /10mo/, "issue age chip");

s = stub();
await render(s, { data: data({ group_by: "kind", show_labels: false, truncated: true }), now: NOW });
assert.match(s.innerHTML, /gi-repo">tesserae</, "repo shown when grouped by kind");
assert.doesNotMatch(s.innerHTML, /class="gi-chip gi-label"/);
assert.match(s.innerHTML, /\+5\+ more/);
assert.match(s.innerHTML, /PULL REQUESTS|Pull requests/);

s = stub();
await render(s, { data: data({ group_by: "none" }), now: NOW });
assert.doesNotMatch(s.innerHTML, /class="gi-section-head"/);

s = stub();
await render(s, { data: data({ items: [], total: 0, note: "Discussions need a token in GitHub Core." }), now: NOW });
assert.match(s.innerHTML, /Inbox zero/);
assert.match(s.innerHTML, /need a token/);

s = stub();
await render(s, { data: { error: "Set a GitHub username" } });
assert.match(s.innerHTML, /ph-warning-circle/);
assert.match(s.innerHTML, /Set a GitHub username/);

console.log("github_inbox client: all checks passed");

// density: compact rows are one line, auto follows the cell height
{
  const { isCompact } = await import("../client.js");
  assert.equal(isCompact("compact", { h: 800 }), true);
  assert.equal(isCompact("comfortable", { h: 200 }), false);
  assert.equal(isCompact("auto", { h: 400, size: "md" }), true);
  assert.equal(isCompact("auto", { h: 800, size: "lg" }), false);
  assert.equal(isCompact("auto", { size: "sm" }), true);
  assert.equal(isCompact("auto", undefined), false);
  const s2 = stub();
  await render(s2, { data: data({ density: "compact", group_by: "kind" }), now: NOW, cell: { h: 800 } });
  assert.match(s2.innerHTML, /class="gi-row is-compact/);
  assert.match(s2.innerHTML, /class="gi-repo">tesserae</);
  assert.doesNotMatch(s2.innerHTML, /class="gi-row-meta"/);
  const s3 = stub();
  await render(s3, { data: data({ density: "auto" }), now: NOW, cell: { h: 400, size: "md" } });
  assert.match(s3.innerHTML, /class="gi-row is-compact/);
  console.log("github_inbox client: density checks passed");
}
