# Tesserae GitHub widgets

CI status, PR queue, contributions, release feed, and activity widgets for [Tesserae](https://github.com/dmellok/tesserae), the e-ink dashboard companion.

Bundle of 16 widgets:

- **GitHub Core** (`github_core`), shared multi-account GitHub App & account manager (`/plugins/github_core/`). Supports 1-click GitHub App setup (Manifest flow), personal and multi-organization installations with automatic short-lived token minting, GitHub App User OAuth with automatic token refresh, and optional Personal Access Token fallback. No cell of its own; every other widget lets you pick which account to use.
- **GitHub Inbox** (`github_inbox`), every open issue, pull request and discussion across the repos you own, grouped by repo or by kind, with repos to leave out picked from a list or excluded by pattern.
- **GitHub Issue Queue** (`github_issue_queue`), open issues assigned to you and issues you opened (or unassigned vs assigned issues across an organization).
- **GitHub Actions** (`github_actions`), CI run status for a repo's recent workflows. Leave Repos blank to scan the account's most recently pushed repos (capped by "Max repos to scan", default 10).
- **GitHub Activity** (`github_activity`), commit / PR / issue activity feed.
- **GitHub Activity Heatmap** (`github_activity_heatmap`), a year of contribution squares.
- **GitHub CI Status** (`github_ci_status`), hero pass / fail for one workflow.
- **GitHub Commit Cadence** (`github_commit_cadence`), commits per week as a bar run.
- **GitHub Contributions** (`github_contributions`), contributor's contribution graph.
- **GitHub PR Count** (`github_pr_count`), hero count of open PRs.
- **GitHub PR Queue** (`github_pr_queue`), your open PRs and PRs awaiting your review.
- **GitHub Releases** (`github_releases`), latest releases for tracked repos.
- **GitHub Repo** (`github_repo`), repo summary card (stars, forks, recent activity).
- **GitHub Star Count** (`github_star_count`), hero total stars.
- **GitHub Star Growth** (`github_star_growth`), stars gained over time.
- **GitHub Streak** (`github_streak`), current contribution streak.

### GitHub Accounts (Multiple GitHub Apps & PATs)

Open **Admin pages → GitHub Core** (`/plugins/github_core/`) after installing:

1. **GitHub Apps (1-click or manual)**: Create one or multiple private GitHub Apps (use **Add another GitHub App** to create several in sequence for personal accounts and/or organizations, or register multiple existing apps manually). All credentials (PEM private keys, client secrets, OAuth tokens) are encrypted at rest with Tesserae's `SecretBox`.
2. **Install & authorize**: Click **Install on GitHub** on each app to attach your personal account and/or organizations. GitHub redirects back to Tesserae, syncing installations and authorizing your user OAuth account. Use **Sync all apps** to refresh installations across every registered GitHub App, or **Edit** / **Test** any app directly.
3. **Personal Access Tokens (PATs)**: Add one or multiple classic (`ghp_...`) or fine-grained (`github_pat_...`) PATs as named accounts (use **Add another PAT** to add several at once, with automatic live verification and `@username` detection on save).
4. **Pick per widget or set Default**: Use **Set default** on any account in **Connected GitHub Accounts**, or pick a specific **Account** in any widget's options (blank uses the default account, or public access on widgets that don't require auth). Existing legacy single-token settings continue working automatically.

### GitHub Inbox

Lists what is open across every repo owned by the selected GitHub account (or username override). Issues and pull requests come from the REST search API and work without an account for public repos; discussions come from the GraphQL API and need a connected account in GitHub Core. Options: account, which kinds to show, grouping (repo, kind, flat), row cap up front; username override, extra owners/orgs, repo exclusions (picked from the selected account's repos or by `*` wildcard), order, density and labels under More options. Rows that do not fit the cell are dropped and counted in a "+N more" footer.

### Tests

```bash
python -m pytest github_core/tests github_inbox/tests
node github_inbox/tests/check.mjs
```

## Install

Settings → Widgets → Browse community widgets → Install. After restart, open **GitHub Core** (`/plugins/github_core/`) to connect your GitHub App.

## Why these moved out of the bundle

Developer-focused widgets that need GitHub credentials. The typical Tesserae user, especially a non-dev HA user, never enables them. Bundling 7+ widgets per install was inflating the picker. Marketplace is the right home.

## License

AGPL-3.0-or-later. See [LICENSE](./LICENSE).
