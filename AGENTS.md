# Agent notes for prherd

## Releasing

Publish a release **only when asked to** ("release 1.2.0", "cut a release", "ship it").
Never start one on your own: not after a merge, a version bump, or a passing build.

When asked, run the Release workflow by hand with the version:

```bash
gh workflow run release.yml --repo pacnpal/prherd --ref main -f version=1.2.0
```

- The version is `X.Y.Z`, or `X.Y.Z-rc.1` for a prerelease, with no leading `v`. If
  the request doesn't name a version, look at the latest release
  (`gh release view --repo pacnpal/prherd --json tagName`) and ask which one to use.
- The workflow tags `v<version>` on the commit it runs on, builds macOS (arm64, x64),
  Windows (x64, arm64, x86) and Linux (x64, arm64; tar.gz, deb, rpm, pacman, AppImage),
  signs and notarizes the macOS apps, writes the notes and publishes the release. It
  refuses a tag that already exists. It never runs on its own: not on a push, a tag or a
  merge.
- To check the builds without publishing, add `-f dry-run=true`. That's how to test a
  change to the workflow or `packaging/`; run it on the branch with `--ref <branch>`.
- Find the run with `gh run list --repo pacnpal/prherd --workflow release.yml --limit 1`.
  When it finishes, give the release URL from
  `gh release view v<version> --repo pacnpal/prherd --json url`.

See [docs/releasing.md](docs/releasing.md) for the secrets the workflow needs and what it
builds.
