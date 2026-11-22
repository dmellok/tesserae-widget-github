// github_issue_queue, Spectra list archetype with two sub-sections.
// Mirrors github_pr_queue for issues: "Assigned" (or "Unassigned" in
// org mode) leads with an accent-1 record glyph; "Opened" (or
// "Assigned" in org mode) leads with an accent-3 record glyph. Each row
// carries an issue-age chip (muted → ochre → terracotta → plum as the
// issue ages), compact labels, and a comment-bubble chip showing
// discussion activity.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function repoShort(name) {
  if (typeof name !== "string") return "";
  const slash = name.lastIndexOf("/");
  return slash >= 0 ? name.slice(slash + 1) : name;
}

function ageDays(iso) {
  if (typeof iso !== "string") return NaN;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return NaN;
  return Math.max(0, (Date.now() - t) / 86_400_000);
}

function ageTier(days) {
  if (!Number.isFinite(days)) return { label: "-", color: "var(--text-muted)", tint: 0 };
  if (days < 1) return { label: `${Math.max(1, Math.round(days * 24))}h`, color: "var(--text-secondary)", tint: 0 };
  if (days < 2) return { label: `${Math.round(days)}d`, color: "var(--text-secondary)", tint: 0 };
  if (days < 7) return { label: `${Math.round(days)}d`, color: "var(--accent-2)", tint: 14 };
  if (days < 30) return { label: `${Math.round(days / 7)}w`, color: "var(--accent-1)", tint: 16 };
  if (days < 365) return { label: `${Math.round(days / 30)}mo`, color: "var(--accent-6)", tint: 18 };
  return { label: `${Math.round(days / 365)}y`, color: "var(--accent-6)", tint: 18 };
}

function ageChip(iso) {
  const d = ageDays(iso);
  const tier = ageTier(d);
  const bg = tier.tint > 0
    ? `background:color-mix(in oklab, ${tier.color} ${tier.tint}%, var(--surface));`
    : "background:color-mix(in oklab, var(--text-primary) 5%, transparent);";
  return `<span class="iq-chip iq-age" style="color:${tier.color};${bg}" title="opened ${escapeHtml(iso || "-")}">
    <i class="ph-bold ph-clock" style="font-size:.9em"></i>${escapeHtml(tier.label)}
  </span>`;
}

function commentChip(n) {
  if (!Number.isFinite(n) || n <= 0) return "";
  return `<span class="iq-chip iq-comments" title="${n} comments">
    <i class="ph-bold ph-chat-circle" style="font-size:.9em"></i>${n}
  </span>`;
}

function labelChips(labels) {
  if (!Array.isArray(labels) || labels.length === 0) return "";
  const shown = labels.slice(0, 2).map((l) => `<span class="iq-chip iq-label">${escapeHtml(l)}</span>`);
  if (labels.length > 2) shown.push(`<span class="iq-chip iq-label">+${labels.length - 2}</span>`);
  return shown.join("");
}

function sectionHeader(label, count) {
  return `
    <div class="iq-section-head">
      <span class="u-label">${escapeHtml(label)}</span>
      <span class="u-label iq-section-count">${count}</span>
    </div>`;
}

function row(item, i, leadColor) {
  const repoMeta = `<span class="iq-repo">${escapeHtml(repoShort(item.repo))}${
    item.number ? `<small class="iq-num">#${item.number}</small>` : ""
  }</span>`;
  return `
    <div class="iq-row ${i % 2 ? "is-zebra" : ""}">
      <div class="iq-row-head">
        <div class="list-lead">
          <i class="ph-bold ph-record" style="color:${leadColor}"></i>
          <span class="list-title">${escapeHtml(item.title)}</span>
        </div>
        ${ageChip(item.created_at)}
      </div>
      <div class="iq-row-meta">
        ${repoMeta}
        ${labelChips(item.labels)}
        ${commentChip(item.comments)}
      </div>
    </div>`;
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="github_issue_queue">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>Issue Queue</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const assigned = Array.isArray(data.assigned) ? [...data.assigned] : [];
  const opened = Array.isArray(data.opened) ? [...data.opened] : [];

  if (assigned.length === 0 && opened.length === 0) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="github_issue_queue">
        <div class="w-title">
          <i class="ph-bold ph-record" style="color:var(--accent-3)"></i>
          <h3>Issue Queue</h3>
        </div>
        <div class="w-body" style="justify-content:center;align-items:center">
          <i class="ph-bold ph-check-circle" style="color:var(--accent-3);font-size:3em"></i>
          <p class="u-muted">Inbox zero.</p>
        </div>
      </div>`;
    return;
  }

  // Sort each section oldest-first so the stalest issue sits at the top.
  assigned.sort((a, b) => Date.parse(a.created_at || 0) - Date.parse(b.created_at || 0));
  opened.sort((a, b) => Date.parse(a.created_at || 0) - Date.parse(b.created_at || 0));

  const assignedRows = assigned.map((it, i) => row(it, i, "var(--accent-1)")).join("");
  const openedRows = opened.map((it, i) => row(it, i, "var(--accent-3)")).join("");

  const assignedLabel = data.assigned_label || "Assigned";
  const openedLabel = data.opened_label || "Opened";
  const assignedMeta = data.assigned_meta || "ASSIGNED";
  const openedMeta = data.opened_meta || "OPENED";

  const layout = `
    .iq-section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: var(--space-1) var(--space-3) var(--space-1);
      color: var(--text-muted);
      letter-spacing: var(--ls-label);
      text-transform: uppercase;
      font-weight: var(--fw-bold);
    }
    .iq-section-count {
      color: var(--text-secondary);
      font-variant-numeric: tabular-nums;
    }
    .iq-row {
      display: flex;
      flex-direction: column;
      gap: var(--space-1);
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-1);
    }
    .iq-row.is-zebra {
      background: color-mix(in oklab, var(--text-primary) 3%, transparent);
    }
    .iq-row-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-3);
      min-width: 0;
    }
    .iq-row-head .list-lead {
      min-width: 0;
      flex: 1 1 auto;
    }
    .iq-row-head .list-title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .iq-row-meta {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      margin-left: calc(1.2em + var(--space-2));
      font-size: var(--fs-caption);
      flex-wrap: nowrap;
      min-width: 0;
      overflow: hidden;
    }
    .iq-repo {
      color: var(--text-muted);
      font-weight: var(--fw-semi);
      font-size: var(--fs-caption);
      flex: 0 0 auto;
    }
    .iq-num {
      color: var(--text-muted);
      font-weight: var(--fw-semi);
      font-size: .85em;
      margin-left: .2em;
    }
    .iq-chip {
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
    }
    .iq-comments {
      color: var(--text-secondary);
      background: color-mix(in oklab, var(--text-primary) 5%, transparent);
    }
    .iq-label {
      color: var(--text-muted);
      background: color-mix(in oklab, var(--text-primary) 6%, transparent);
      font-weight: var(--fw-semi);
      max-width: 96px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    @container (max-width: 280px) {
      .iq-comments, .iq-label { display: none; }
    }
  `;

  shadow.innerHTML = `
    ${css}
    <style>${layout}</style>
    <div class="w" data-widget="github_issue_queue">
      <div class="w-title">
        <i class="ph-bold ph-record" style="color:var(--accent-3)"></i>
        <h3>Issue Queue</h3>
        <span class="w-title-meta">${assigned.length} ${escapeHtml(assignedMeta)} · ${opened.length} ${escapeHtml(openedMeta)}</span>
      </div>
      <div class="w-body list-body" style="gap:0">
        ${assigned.length ? sectionHeader(assignedLabel, assigned.length) + assignedRows : ""}
        ${opened.length ? sectionHeader(openedLabel, opened.length) + openedRows : ""}
      </div>
    </div>`;
}
