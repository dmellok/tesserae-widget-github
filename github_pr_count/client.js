// github_pr_count, hero PR backlog with bucket split. Tile turns
// ochre when total > 0 and terracotta when anything has been open
// past the stale threshold, so a quick glance distinguishes "things
// to do" from "things going stale".

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="github_pr_count">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>PRs</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const total = Number(data.total) || 0;
  const yours = Number((data.yours || {}).count) || 0;
  const review = Number((data.review || {}).count) || 0;
  const oldest = Number(data.oldest_days) || 0;
  const stale = Number(data.stale_days) || 7;
  const isStale = oldest >= stale && total > 0;
  const heroColor = total === 0
    ? "var(--accent-3)"
    : (isStale ? "var(--accent-1)" : "var(--accent-2)");
  const glyph = total === 0 ? "ph-check-circle" : "ph-git-pull-request";

  const ageChip = total === 0
    ? ""
    : `<span class="pill" style="background:${isStale ? "var(--accent-1)" : "var(--surface-sunken)"};color:${isStale ? "var(--on-accent)" : "var(--text-secondary)"}">${escapeHtml(`oldest ${oldest}d`)}</span>`;

  const heroLabel = total === 0
    ? "INBOX ZERO"
    : (total === 1 ? "PR" : "PRS");

  shadow.innerHTML = `
    ${css}
    <div class="w" data-widget="github_pr_count">
      <div class="w-title">
        <i class="ph-bold ph-git-pull-request" style="color:${heroColor}"></i>
        <h3>Open PRs</h3>
        ${ageChip}
      </div>
      <div class="w-body status-body">
        <div class="status-hero">
          <i class="ph-bold ${glyph}" style="color:${heroColor}"></i>
          <div class="lockup">
            <span class="status-state" style="color:${heroColor}">${escapeHtml(String(total))}</span>
            <span class="status-sub">${escapeHtml(heroLabel)}</span>
          </div>
        </div>
        <div class="status-grid">
          <div class="status-cell">
            <span class="u-label">${escapeHtml(data.yours_label || "Yours")}</span>
            <span class="v" style="color:var(--accent-5)">${escapeHtml(String(yours))}</span>
          </div>
          <div class="status-cell">
            <span class="u-label">${escapeHtml(data.review_label || "Review")}</span>
            <span class="v" style="color:var(--accent-4)">${escapeHtml(String(review))}</span>
          </div>
        </div>
      </div>
    </div>`;
}
