# prherd redesign

Source for the redesign mockups. The live, clickable version is the
[design canvas](https://claude.ai/artifact/6u4hAsA6WnHNSHVPuXZcrk) (private
until it's shared from its Share menu).

These files are Design Component pages (`.dc.html`) exported from that canvas.
They load the canvas runtime (`support.js`), so they don't render on their own
in a browser. `canvas.json` is the canvas layout.

## Artboards

| File | Screen |
| --- | --- |
| `Main.dc.html` | Main window: toolbar, sidebar views and repos, PR table, detail panel, activity log |
| `Settings.dc.html` | Settings: General, Repositories, Merging, Codex, Notifications |
| `ContextMenu.dc.html` | Right-click menu on selected PRs |
| `MergeNow.dc.html` | Merge now confirmation and its success state |
| `FirstRun.dc.html` | First run: add repos, setup checks, what counts as a go-ahead |
| `MenuBar.dc.html` | Menu bar extra (a new idea, not in the app today) |

Each has a `…Dark.dc.html` copy that opens in dark appearance. The light files
follow the system appearance by default, and each has a `theme` tweak (system,
light, dark).

## Look

- **Type:** Inter for the interface, JetBrains Mono for PR numbers, commit
  hashes, branches and times.
- **Accent:** indigo `#4F46E5`, used for the primary action, the selected row
  and queued merges.
- **Colors:** every color is a CSS variable with a light and a dark value (see
  the `.ph-light` and `.ph-dark` blocks at the top of `Main.dc.html`).
- **Codex statuses:** each has a color, an icon and a word, so no status
  depends on color alone.

| Status | Light text / fill | Dark text / fill |
| --- | --- | --- |
| Thumbs up, No major issues | `#1D6B36` / `#E5F4EA` | `#7BD9A4` / `#15301F` |
| Reviewing | `#7A4F00` / `#FFF1CC` | `#F2C462` / `#33280F` |
| Merge conflict | `#A3191F` / `#FDE7E7` | `#FF9494` / `#3A1A1C` |
| Lookup failed | `#8F3A00` / `#FFE9D9` | `#FFB27A` / `#3A2312` |
| Don't merge | `#3A4150` / `#ECEEF2` | `#C3C7D1` / `#262830` |
| Awaiting Codex | `#4F5563` / `#F3F3F6` | `#A9ADB8` / `#202127` |

The PR titles, authors and times in the mockups are sample data.
