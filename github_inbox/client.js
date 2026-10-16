// github_inbox, Spectra list archetype. One list of everything open
// across the repos you own: issues (accent-3 record glyph), pull
// requests (accent-4 git-pull-request, muted pencil for drafts) and
// discussions (accent-2 chats-circle, accent-1 question mark for an
// unanswered Q&A). Grouped by repo (default), by kind, or flat. Each
// row carries the same age chip as github_pr_queue (muted → ochre →
// terracotta → plum as the thing ages) and a comment-bubble chip.

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

export function repoShort(name) {
  if (typeof name !== "string") return "";
  const slash = name.lastIndexOf("/");
  return slash >= 0 ? name.slice(slash + 1) : name;
}

export function ageDays(iso, now = Date.now()) {
  if (typeof iso !== "string") return NaN;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return NaN;
  return Math.max(0, (now - t) / 86_400_000);
}

export function ageTier(days) {
  if (!Number.isFinite(days)) return { label: "-", color: "var(--text-muted)", tint: 0 };
  if (days < 1) return { label: `${Math.max(1, Math.round(days * 24))}h`, color: "var(--text-secondary)", tint: 0 };
  if (days < 2) return { label: `${Math.round(days)}d`, color: "var(--text-secondary)", tint: 0 };
  if (days < 7) return { label: `${Math.round(days)}d`, color: "var(--accent-2)", tint: 14 };
  if (days < 30) return { label: `${Math.round(days / 7)}w`, color: "var(--accent-1)", tint: 16 };
  if (days < 365) return { label: `${Math.round(days / 30)}mo`, color: "var(--accent-6)", tint: 18 };
  return { label: `${Math.round(days / 365)}y`, color: "var(--accent-6)", tint: 18 };
}

const KIND_LABEL = { issue: "Issues", pr: "Pull requests", discussion: "Discussions" };
const KIND_ORDER = ["issue", "pr", "discussion"];

export function kindGlyph(item) {
  if (item.kind === "pr") {
    return item.draft
      ? { icon: "ph-pencil-circle", color: "var(--text-muted)" }
      : { icon: "ph-git-pull-request", color: "var(--accent-4)" };
  }
  if (item.kind === "discussion") {
    return item.answerable && !item.answered
      ? { icon: "ph-question", color: "var(--accent-1)" }
      : { icon: "ph-chats-circle", color: "var(--accent-2)" };
  }
  return { icon: "ph-record", color: "var(--accent-3)" };
}

// Groups preserve the incoming (server-sorted) order: a repo section
// appears where its first item does, so "latest activity first" puts
// the busiest repo on top.
export function groupItems(items, groupBy) {
  if (groupBy === "none") return [{ key: "", label: "", items: [...items] }];
  const groups = new Map();
  for (const it of items) {
    const key = groupBy === "kind" ? it.kind : it.repo || "";
    if (!groups.has(key)) {
      const label = groupBy === "kind" ? KIND_LABEL[key] || key : repoShort(key);
      groups.set(key, { key, label, items: [] });
    }
    groups.get(key).items.push(it);
  }
  const out = [...groups.values()];
  if (groupBy === "kind") out.sort((a, b) => KIND_ORDER.indexOf(a.key) - KIND_ORDER.indexOf(b.key));
  return out;
}

function ageChip(iso, now) {
  const tier = ageTier(ageDays(iso, now));
  const bg = tier.tint > 0
    ? `background:color-mix(in oklab, ${tier.color} ${tier.tint}%, var(--surface));`
    : "background:color-mix(in oklab, var(--text-primary) 5%, transparent);";
  return `<span class="gi-chip gi-age" style="color:${tier.color};${bg}" title="opened ${escapeHtml(iso || "-")}">
    <i class="ph-bold ph-clock" style="font-size:.9em"></i>${escapeHtml(tier.label)}
  </span>`;
}

function commentChip(n) {
  if (!Number.isFinite(n) || n <= 0) return "";
  return `<span class="gi-chip gi-comments" title="${n} comments">
    <i class="ph-bold ph-chat-circle" style="font-size:.9em"></i>${n}
  </span>`;
}

function labelChips(labels) {
  if (!Array.isArray(labels) || labels.length === 0) return "";
  const shown = labels.slice(0, 2).map((l) => `<span class="gi-chip gi-label">${escapeHtml(l)}</span>`);
  if (labels.length > 2) shown.push(`<span class="gi-chip gi-label">+${labels.length - 2}</span>`);
  return shown.join("");
}

// Per-kind counts for a repo section: the repo's full open totals when
// the server sent them (so a header reads "70 issues" even when only
// four rows made the cut), else counted from the rows on show.
export function kindCounts(items, totals) {
  const n = totals && typeof totals === "object" ? totals : { issue: 0, pr: 0, discussion: 0 };
  if (!totals) for (const it of items) n[it.kind] = (n[it.kind] || 0) + 1;
  const parts = [];
  if (n.issue) parts.push(`<i class="ph-bold ph-record"></i>${n.issue}`);
  if (n.pr) parts.push(`<i class="ph-bold ph-git-pull-request"></i>${n.pr}`);
  if (n.discussion) parts.push(`<i class="ph-bold ph-chats-circle"></i>${n.discussion}`);
  return parts.join("");
}

function sectionHeader(group, groupBy, byRepo) {
  const totals = groupBy === "repo" && byRepo && byRepo[group.key] ? byRepo[group.key] : null;
  const right = groupBy === "kind" ? String(group.items.length) : kindCounts(group.items, totals);
  return `
    <div class="gi-section-head">
      <span class="u-label">${escapeHtml(group.label)}</span>
      <span class="u-label gi-section-count">${right}</span>
    </div>`;
}

// Compact rows: one line per item, so a 400px-tall cell lists five or
// six things instead of two. Auto picks compact below ~480px of cell
// height or at the sm size, comfortable otherwise.
export function isCompact(density, cell) {
  if (density === "compact") return true;
  if (density === "comfortable") return false;
  const h = Number(cell?.h);
  return cell?.size === "sm" || (Number.isFinite(h) && h > 0 && h < 480);
}

function compactRow(item, i, { showRepo, now }) {
  const glyph = kindGlyph(item);
  const inline = [];
  if (showRepo) inline.push(`<span class="gi-repo">${escapeHtml(repoShort(item.repo))}</span>`);
  if (item.number) inline.push(`<span class="gi-num">#${escapeHtml(item.number)}</span>`);
  return `
    <div class="gi-row is-compact ${i % 2 ? "is-zebra" : ""} ${item.draft ? "is-draft" : ""}" data-kind="${escapeHtml(item.kind)}">
      <div class="list-lead">
        <i class="ph-bold ${glyph.icon}" style="color:${glyph.color}"></i>
        <span class="list-title">${escapeHtml(item.title)}</span>
        <span class="gi-inline">${inline.join("")}</span>
      </div>
      <div class="gi-tail">${commentChip(item.comments)}${ageChip(item.created_at, now)}</div>
    </div>`;
}

function row(item, i, { showRepo, showLabels, now, compact }) {
  if (compact) return compactRow(item, i, { showRepo, now });
  const glyph = kindGlyph(item);
  const bits = [];
  if (showRepo) bits.push(`<span class="gi-repo">${escapeHtml(repoShort(item.repo))}</span>`);
  if (item.number) bits.push(`<span class="gi-num">#${escapeHtml(item.number)}</span>`);
  if (item.author) bits.push(`<span class="gi-author">${escapeHtml(item.author)}</span>`);
  if (item.kind === "discussion" && item.category) {
    bits.push(`<span class="gi-chip gi-cat">${escapeHtml(item.category)}</span>`);
  }
  if (item.kind === "pr" && item.draft) {
    bits.push(`<span class="gi-chip gi-draft"><i class="ph-bold ph-pencil-simple" style="font-size:.9em"></i>DRAFT</span>`);
  }
  if (showLabels) bits.push(labelChips(item.labels));
  bits.push(commentChip(item.comments));
  return `
    <div class="gi-row ${i % 2 ? "is-zebra" : ""} ${item.draft ? "is-draft" : ""}" data-kind="${escapeHtml(item.kind)}">
      <div class="gi-row-head">
        <div class="list-lead">
          <i class="ph-bold ${glyph.icon}" style="color:${glyph.color}"></i>
          <span class="list-title">${escapeHtml(item.title)}</span>
        </div>
        ${ageChip(item.created_at, now)}
      </div>
      <div class="gi-row-meta">${bits.filter(Boolean).join("")}</div>
    </div>`;
}

function titleMeta(counts) {
  const parts = [];
  if (counts.issue) parts.push(`${counts.issue} ${counts.issue === 1 ? "ISSUE" : "ISSUES"}`);
  if (counts.pr) parts.push(`${counts.pr} ${counts.pr === 1 ? "PR" : "PRS"}`);
  if (counts.discussion) parts.push(`${counts.discussion} DISC`);
  return parts.join(" · ");
}

const LAYOUT = `
  .gi-section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-2) var(--space-3) var(--space-1);
    color: var(--text-muted);
    letter-spacing: var(--ls-label);
    text-transform: uppercase;
    font-weight: var(--fw-bold);
  }
  .gi-section-head:first-child { padding-top: 0; }
  .gi-section-count {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }
  .gi-section-count .ph-bold { font-size: .95em; margin-right: 2px; color: var(--text-muted); }
  .gi-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-1);
  }
  .gi-row.is-zebra { background: color-mix(in oklab, var(--text-primary) 3%, transparent); }
  .gi-row.is-draft { opacity: 0.78; }
  .gi-row.is-compact {
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding-top: calc(var(--space-1) + 1px);
    padding-bottom: calc(var(--space-1) + 1px);
  }
  .gi-row.is-compact .list-lead { min-width: 0; flex: 1 1 auto; }
  .gi-row.is-compact .list-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gi-inline {
    display: inline-flex;
    gap: var(--space-2);
    flex: 0 0 auto;
    font-size: var(--fs-caption);
    color: var(--text-muted);
  }
  .gi-tail { display: inline-flex; align-items: center; gap: var(--space-2); flex: 0 0 auto; }
  .gi-row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    min-width: 0;
  }
  .gi-row-head .list-lead { min-width: 0; flex: 1 1 auto; }
  .gi-row-head .list-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gi-row-meta {
    display: flex;
    align-items: center;
    flex-wrap: nowrap;
    overflow: hidden;
    gap: var(--space-2);
    margin-left: calc(1.2em + var(--space-2));
    font-size: var(--fs-caption);
    color: var(--text-muted);
    min-height: 1.3em;
  }
  .gi-repo, .gi-num, .gi-author {
    font-weight: var(--fw-semi);
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .gi-repo { color: var(--text-secondary); }
  .gi-author::before { content: "@"; opacity: .7; }
  .gi-chip {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding: 1px var(--space-1);
    border-radius: 999px;
    font-size: var(--fs-caption);
    font-weight: var(--fw-bold);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0;
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .gi-comments, .gi-label, .gi-cat {
    color: var(--text-secondary);
    background: color-mix(in oklab, var(--text-primary) 5%, transparent);
  }
  .gi-label { max-width: 9em; overflow: hidden; text-overflow: ellipsis; display: inline-block; line-height: 1.3; }
  .gi-draft {
    color: var(--text-muted);
    background: color-mix(in oklab, var(--text-primary) 6%, transparent);
    letter-spacing: var(--ls-label);
  }
  .gi-foot {
    padding: var(--space-2) var(--space-3) 0;
    display: flex;
    justify-content: space-between;
    gap: var(--space-2);
    font-size: var(--fs-caption);
    color: var(--text-muted);
  }
  .gi-foot .gi-more { font-weight: var(--fw-bold); letter-spacing: var(--ls-label); text-transform: uppercase; flex: 0 0 auto; }
  .gi-foot .gi-note { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @container (max-width: 320px) {
    .gi-label, .gi-cat, .gi-author, .gi-inline { display: none; }
  }
  @container (max-width: 240px) {
    .gi-comments { display: none; }
    .w-title-meta { display: none; }
  }
`;

// The shadow's stylesheet loads async; until it lands the card has no
// fixed height, so measuring rows against the body is meaningless.
async function settled(shadow) {
  const link = shadow.querySelector && shadow.querySelector('link[rel="stylesheet"]');
  if (link && !link.sheet && typeof link.addEventListener === "function") {
    await new Promise((resolve) => {
      link.addEventListener("load", resolve, { once: true });
      link.addEventListener("error", resolve, { once: true });
      setTimeout(resolve, 3000);
    });
  }
  if (typeof document !== "undefined" && document.fonts && document.fonts.ready) {
    try { await document.fonts.ready; } catch { /* measure anyway */ }
  }
}

export default async function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;
  const now = ctx?.now ?? Date.now();

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="github_inbox">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>Inbox</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const items = Array.isArray(data.items) ? data.items : [];
  const counts = data.counts || { issue: 0, pr: 0, discussion: 0 };
  const groupBy = data.group_by || "repo";
  const showLabels = data.show_labels !== false;
  const note = data.note ? `<span class="gi-note" title="${escapeHtml(data.note)}">${escapeHtml(data.note)}</span>` : "";

  if (items.length === 0) {
    shadow.innerHTML = `
      ${css}
      <style>${LAYOUT}</style>
      <div class="w" data-widget="github_inbox">
        <div class="w-title">
          <i class="ph-bold ph-tray" style="color:var(--accent-3)"></i>
          <h3>Inbox</h3>
        </div>
        <div class="w-body" style="justify-content:center;align-items:center">
          <i class="ph-bold ph-check-circle" style="color:var(--accent-3);font-size:3em"></i>
          <p class="u-muted">Inbox zero.</p>
          ${note ? `<div class="gi-foot" style="margin-top:0">${note}</div>` : ""}
        </div>
      </div>`;
    return;
  }

  const groups = groupItems(items, groupBy);
  const showRepo = groupBy !== "repo";
  const compact = isCompact(data.density || "auto", ctx?.cell);
  let i = 0;
  const body = groups
    .map((g) => {
      const head = groupBy === "none" ? "" : sectionHeader(g, groupBy, data.by_repo);
      const rows = g.items.map((it) => row(it, i++, { showRepo, showLabels, now, compact })).join("");
      return head + rows;
    })
    .join("");

  const total = Number.isFinite(data.total) ? data.total : items.length;
  const hidden = Math.max(0, total - items.length);
  const more = hidden > 0 ? `<span class="gi-more">+${hidden}${data.truncated ? "+" : ""} more</span>` : "";
  const foot = more || note ? `<div class="gi-foot">${note}${more}</div>` : "";

  shadow.innerHTML = `
    ${css}
    <style>${LAYOUT}</style>
    <div class="w" data-widget="github_inbox">
      <div class="w-title">
        <i class="ph-bold ph-tray" style="color:var(--accent-1)"></i>
        <h3>Inbox</h3>
        <span class="w-title-meta">${titleMeta(counts)}</span>
      </div>
      <div class="w-body list-body" style="gap:0">
        ${body}
        ${foot}
      </div>
    </div>`;

  await settled(shadow);
  fitRows(shadow, { hidden, truncated: !!data.truncated, note });
}

// Drop trailing rows that fall below the body so the list ends on a
// whole row and the "+N more" footer stays in view. The rows are laid
// out before this runs (innerHTML on an attached shadow root), so the
// rects are real; a stub root without layout is left alone.
export function fitRows(shadow, { hidden, truncated, note }) {
  const body = shadow.querySelector && shadow.querySelector(".w-body");
  if (!body || typeof body.getBoundingClientRect !== "function") return;
  let dropped = 0;
  let foot = body.querySelector(".gi-foot");
  const ensureFoot = () => {
    if (!foot) {
      foot = shadow.ownerDocument
        ? shadow.ownerDocument.createElement("div")
        : document.createElement("div");
      foot.className = "gi-foot";
      body.appendChild(foot);
    }
    const total = hidden + dropped;
    foot.innerHTML = `${note || ""}${total > 0 ? `<span class="gi-more">+${total}${truncated ? "+" : ""} more</span>` : ""}`;
  };
  for (let guard = 0; guard < 400; guard += 1) {
    const rows = body.querySelectorAll(".gi-row");
    if (rows.length <= 1) break;
    const limit = body.getBoundingClientRect().bottom + 0.5;
    const last = rows[rows.length - 1];
    const footBottom = foot ? foot.getBoundingClientRect().bottom : last.getBoundingClientRect().bottom;
    if (last.getBoundingClientRect().bottom <= limit && footBottom <= limit) break;
    const prev = last.previousElementSibling;
    last.remove();
    dropped += 1;
    if (prev && prev.classList.contains("gi-section-head")) prev.remove();
    ensureFoot();
  }
  if (dropped > 0) {
    // Zebra striping is by row index; a removed section head does not
    // change it, so nothing to redo there. Clear the empty-footer case.
    if (foot && !foot.innerHTML.trim()) foot.remove();
  }
}
