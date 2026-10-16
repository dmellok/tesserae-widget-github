# Tesserae GitHub widgets

CI status, PR queue, contributions, release feed, and activity widgets for [Tesserae](https://github.com/dmellok/tesserae), the e-ink dashboard companion.

Bundle of 15 widgets:

- **GitHub Core** (`github_core`), shared token + username. No cell of its own; the other widgets read from it.
- **GitHub Inbox** (`github_inbox`), every open issue, pull request and discussion across the repos you own, grouped by repo or by kind, with repos to leave out picked from a list or excluded by pattern.
- **GitHub Actions** (`github_actions`), CI run status for a repo's recent workflows.
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

### GitHub Inbox

Lists what is open across every repo the configured user owns. Issues and pull requests come from the REST search API and work without a token (at GitHub's unauthenticated rate limit); discussions come from the GraphQL API and need a token in GitHub Core. Options: extra owners or orgs to include, which kinds to show, repos to leave out (a picker filled from your repos, plus a free-text list that takes `*` wildcards such as `tesserae-widget-*`), grouping (repo, kind, flat), order (latest activity, oldest first, newest first), row cap, row density, labels on or off. Rows that do not fit the cell are dropped and counted in a "+N more" footer.

Tests: `python -m pytest github_inbox/tests` and `node github_inbox/tests/check.mjs`.

## Install

Settings → Widgets → Browse community widgets → Install. After restart, paste a GitHub personal access token (PAT) under Settings → Widgets → GitHub Core. A PAT with `repo` and `read:user` scopes is enough for most widgets; `read:org` lets you read org repos.

## Why these moved out of the bundle

Developer-focused widgets that need a personal access token. The typical Tesserae user, especially a non-dev HA user, never enables them. Bundling 7 widgets per install was inflating the picker. Marketplace is the right home.

## License

AGPL-3.0-or-later. See [LICENSE](./LICENSE).
