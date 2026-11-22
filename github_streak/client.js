// github_streak, hero number for the current contribution streak.
// Picks an accent colour based on threshold tier (0 muted, 7+ moss,
// 30+ ochre, 100+ terracotta) and washes the title bar with the same
// hue so the streak length reads at a glance.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtCount(n) {
  const v = Number(n) || 0;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return String(v);
}

// Threshold tiers: each entry is [min_days, accent_css_var, glyph].
// We pick the highest tier the current streak qualifies for.
const TIERS = [
  [100, "var(--accent-1)", "ph-flame"],
  [30,  "var(--accent-2)", "ph-flame"],
  [7,   "var(--accent-3)", "ph-flame"],
  [1,   "var(--accent-4)", "ph-flame"],
  [0,   "var(--text-muted)", "ph-circle-dashed"],
];

function tierFor(streak) {
  const n = Number(streak) || 0;
  for (const [min, color, glyph] of TIERS) {
    if (n >= min) return { color, glyph };
  }
  return { color: "var(--text-muted)", glyph: "ph-circle-dashed" };
}

const PUBLIC_ONLY_HINT =
  "This account is a GitHub App installation, so private contributions may be missing. " +
  "Authorize a user on this app in GitHub Core to include them.";

function publicOnlyIcon(data) {
  if (!data.public_only) return "";
  return `<i class="ph-bold ph-eye-slash" style="color:var(--text-secondary)" role="img"
    aria-label="${escapeHtml(PUBLIC_ONLY_HINT)}" title="${escapeHtml(PUBLIC_ONLY_HINT)}"></i>`;
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="github_streak">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>Streak</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const streak = Number(data.current_streak) || 0;
  const longest = Number(data.longest_streak) || 0;
  const today = Number(data.today_count) || 0;
  const yearTotal = Number(data.year_total) || 0;
  const tier = tierFor(streak);

  const dayLabel = streak === 1 ? "DAY" : "DAYS";
  const todayChip = today > 0
    ? `<span class="pill" style="background:var(--accent-3)">${escapeHtml(`${today} today`)}</span>`
    : `<span class="pill" style="background:var(--surface-sunken);color:var(--text-secondary)">nothing yet today</span>`;

  shadow.innerHTML = `
    ${css}
    <div class="w" data-widget="github_streak">
      <div class="w-title">
        <i class="ph-bold ${tier.glyph}" style="color:${tier.color}"></i>
        <h3>Streak</h3>
        ${todayChip}
        ${publicOnlyIcon(data)}
      </div>
      <div class="w-body status-body">
        <div class="status-hero">
          <i class="ph-bold ${tier.glyph}" style="color:${tier.color}"></i>
          <div class="lockup">
            <span class="status-state" style="color:${tier.color}">${escapeHtml(String(streak))}</span>
            <span class="status-sub">${escapeHtml(dayLabel)}</span>
          </div>
        </div>
        <div class="status-grid">
          <div class="status-cell">
            <span class="u-label">Longest</span>
            <span class="v" style="color:var(--accent-5)">${escapeHtml(String(longest))}</span>
          </div>
          <div class="status-cell">
            <span class="u-label">Year</span>
            <span class="v" style="color:var(--accent-3)">${escapeHtml(fmtCount(yearTotal))}</span>
          </div>
        </div>
      </div>
    </div>`;
}
