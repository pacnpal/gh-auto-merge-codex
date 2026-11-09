# Port plan: bringing the redesign into prherd

How to get the mockups in this folder into `prherd.py`, in phases that each
ship as their own PR. Every phase leaves the app working, keeps the merge
logic untouched, and keeps `--headless` exactly as it is.

## Ground rules

- **Stay on tkinter/ttk.** No new dependencies, still one file, and the
  existing window test keeps working. The ttk `clam` theme is the base,
  because it's the one whose colors can be fully restyled (`aqua` ignores most
  of `ttk.Style`).
- **Merge logic is off limits.** `verdict`, `merge`, `poll`, `mark` and
  `flag_conflict` keep their behavior. Where the UI needs more data (like
  timestamps), it's returned alongside the existing values, never instead of
  them.
- **Internal values stay as they are.** The verdict strings (`👍`,
  `no major issues`, `👀`, `merge conflict`, `don't merge`, `?`, `None`) stay
  the data. The window maps them to the design's labels, colors and icons in
  one table.
- **Fonts:** the mockups use Inter and JetBrains Mono. The app uses the system
  font (SF Pro on macOS) and the system monospace font instead of bundling
  font files.
- **Each phase:** all tests pass, new pure functions get tests, the README's
  Window section is updated, and the PR has light and dark screenshots.

## What's in the design today vs. new

| In the mockups | Status in prherd today |
| --- | --- |
| Merge mode, interval, Auto-merge, Poll now, add/remove repos | Exists, restyled |
| PR table with Codex verdict, author, state | Exists, restyled, plus Head and Age columns |
| Right-click menus, Merge now, Update branch, Don't merge | Exists, restyled |
| Activity log | Exists, restyled and collapsible |
| Light/dark appearance | New |
| Sidebar views with counts, repo health dots | New (filters over data already polled) |
| Search | New (filters over data already polled) |
| Detail panel: readiness checklist, timeline | New |
| Merge now sheet listing commits and skipped PRs | New (replaces the `messagebox`) |
| Settings window | New window for existing settings, plus appearance |
| First run | New |
| Notifications, Open at login, menu bar extra | New features |

## Phase 1: Theme foundation

Restyle what's there, with no layout changes yet.

- A `PALETTE` dict with the light and dark tokens from `Main.dc.html`
  (`--win`, `--line`, `--text`, `--muted`, the status colors and so on).
- A `theme(root, dark)` function that configures `ttk.Style` on `clam`:
  frames, labels, entries, spinbox, buttons (a `Primary.TButton` in the
  accent and a plain secondary style), radiobuttons as a segmented control
  (`Toolbutton`), checkbutton, Treeview (row height, heading, selection
  colors) and scrollbars. It also sets the `tk.Menu` and `tk.Text` colors.
- A `VERDICTS` table mapping each internal verdict to its label and colors:
  Thumbs up, No major issues, Reviewing, Merge conflict, Lookup failed,
  Don't merge, Awaiting Codex. `row_values` shows the label.
- Appearance setting: `"appearance": "system" | "light" | "dark"` in
  `prherd.json`, validated in `load_settings` like `mode` is.
  - **System detection:** on macOS, `::tk::unsupported::MacWindowStyle isdark`
    (Tk 8.6.10+), falling back to `defaults read -g AppleInterfaceStyle`.
  - **Keeping up with system changes:** re-check on each tick and re-theme
    when it changes.
- **Tests:**
  - **Updates:** `test_row_values` for the new labels. The window test waits
    on the label text instead of the raw verdict.
  - **New:** the appearance setting round-trips, and a bad value falls back to
    `system`.

**Done when:** the current window looks right in light and dark, and nothing
else changed.

## Phase 2: Main window layout

Rebuild the window's structure to match `Main.dc.html`.

- **Toolbar:**
  - **Search:** an entry that filters rows by title, branch, author and
    number.
  - **Merge mode:** a Queue / Force / Admin segmented control.
  - **Interval:** the `Every N s` spinbox.
  - **Auto-merge:** a switch-styled checkbutton.
  - **Poll now:** shows the countdown (`Poll now · 42s`), driven by the
    existing `tick`.
- **Banners** under the toolbar:
  - **Admin mode:** says that branch protection is bypassed.
  - **Watching only:** shown when Auto-merge is off, with a Resume button.
- **Sidebar** (a `ttk.PanedWindow` pane):
  - **Views with counts:** All open, Ready to merge, Codex reviewing, Needs
    attention, Awaiting Codex, Don't merge.
  - **Repo list:** a health dot, PR count, a + button, and the existing repo
    right-click menu.
  - **Account footer:** the signed-in login from `gh api user`.
- **PR table:**
  - **Columns:** PR (number, title and branch), Codex, Author, State, Head
    (short SHA) and Age, grouped under repo rows as today.
  - **Data:** add `headRefName` and `updatedAt` to the `gh pr list --json`
    fields.
  - **Colors:** Treeview can't color one cell, so status shows as icon plus
    label text, and rows that need attention get a tinted row tag. Real pills
    are Phase 5.
- **Selection bar:** shown when two or more PRs are selected, with Request
  Codex review, Don't merge, Update branch and Merge now.
- **Activity log:**
  - **Look:** monospace times and colored event kinds via `Text` tags.
  - **Collapsing:** a disclosure header, with a Live badge and the next poll
    time.
  - **Event kinds:** log lines carry a kind (merge, codex, conflict, review,
    error, poll) set where they're logged.
- **Tests:**
  - **Search and views:** a pure `matches(pr, verdict, view, query)` that the
    search and the views both use.
  - **Window test:** keep `test_window_marks_and_clears_without_merging`
    passing. It finds the `ttk.Treeview` and the "Don't merge" / "Allow
    merge" buttons, so those must still exist (the selection bar can host the
    buttons).

**Done when:** everything the old window could do is reachable in the new
layout, in both appearances.

## Phase 3: Detail panel and Merge now sheet

- **Detail panel** (right pane) for the selected PR:
  - **Header:** repo, number, title, branch → base, and an Open on GitHub
    link.
  - **Status card:** a headline ("Cleared — queued to merge", "Blocked by a
    merge conflict" and so on).
  - **Merge readiness:** from a new pure `readiness(pr, verdict)`. It covers
    the Codex go-ahead on the current head, conflicts, the `do-not-merge`
    label, and draft or checks state.
  - **Timeline:**
    - **Events:** head pushed, check suite, Codex's go-ahead or 👀, queued or
      merging.
    - **Data:** this needs the times `verdict` already looks up. Add
      `verdict_detail(repo, pr)`, returning the verdict plus `pushed` and
      `cleared_at`. `verdict` becomes a thin wrapper, so `poll` and its tests
      are unchanged.
  - **Actions:** Merge now, Request review, Don't merge / Allow merge, Update
    branch, Copy link.
- **Merge now sheet:** a `Toplevel` replacing the `messagebox`.
  - **Lists:** each PR with the commit it will merge, plus the skipped PRs
    and why.
  - **Mode line:** states how the current mode treats branch protection.
  - **Keyboard:** Cancel is the default. Return confirms only when the Merge
    button is focused.
- **Tests:** `readiness` for every verdict, and `verdict_detail` against the
  existing fakes.

## Phase 4: Settings window and first run

- **Settings** (`Settings.dc.html`): a `Toplevel` with a `ttk.Notebook`,
  opened from the sidebar and `⌘,`.
  - **General:** appearance, poll interval, and where settings are saved,
    with a Show in Finder button.
  - **Repositories:** the watched list, add and remove, and a merge-rights
    badge from `gh repo view --json viewerPermission`.
  - **Merging:** Auto-merge, mode cards, and the fixed rules: squash pinned to
    the head, the hold label, conflict comments.
  - **Codex:** the bot id from `BOT_ID`, what counts as a go-ahead, and the
    review comment text. All read-only.
- **First run** (`FirstRun.dc.html`): shown in place of the table when no
  repos are saved.
  - **Repo entry:** add repos, with Return to add.
  - **Setup checks:** `gh --version` ≥ 2.48 and `gh auth status`.
  - **Explainer:** a short three-point "what counts as a go-ahead".
- **Tests:** the version check parsing, and the first-run screen appearing
  only with an empty repo list.

## Phase 5: New features (one PR each, any order)

- **Notifications:** merged, started conflicting, lookup failed (after two
  failed polls in a row), Codex cleared. Sent with `osascript -e 'display
  notification …'`, so no dependency. Each one is a setting.
- **Open at login:** writes or removes a LaunchAgent plist in
  `~/Library/LaunchAgents`.
- **Pills in the PR list:** replace the Treeview with a `Canvas`-drawn list if
  the per-cell colors from the mockup matter. It's a large change, so do it
  only if Phase 2's version falls short. The window test would then need a
  small accessor in place of finding the Treeview.
- **Merged today count:** a stat chip and menu-bar section. Count merges this
  session, or query merged PRs since midnight.
- **Menu bar extra** (`MenuBar.dc.html`): Tk can't create a macOS status item.
  It needs PyObjC or `rumps`, so it's a dependency decision before it's a
  task.

## Order and sizing

| Phase | Depends on | Size |
| --- | --- | --- |
| 1 Theme foundation | — | Small |
| 2 Main window layout | 1 | Large |
| 3 Detail panel, Merge now sheet | 2 | Medium |
| 4 Settings, first run | 1 (better after 2) | Medium |
| 5 New features | 1–4 as noted | Small each, except pills and the menu bar extra |
