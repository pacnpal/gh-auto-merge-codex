# Codex Automerge

Watches open pull requests and requests a squash merge when
`chatgpt-codex-connector` has added a 👍 reaction to the pull request body.

## Usage

```sh
./codex-automerge.sh [--admin] [-R|--repo <directory|owner/repo>] [interval_seconds]
```

| Option | Description |
| --- | --- |
| `-R`, `--repo <directory\|owner/repo>` | Monitor a local Git checkout (relative paths work) or a GitHub repository reference. This overrides `REPO`. |
| `--admin` | Use GitHub administrator privileges for the squash merge. This bypasses merge requirements and does not enable auto-merge. |
| `-h`, `--help` | Print the command usage. |
| `interval_seconds` | Polling interval in seconds. Defaults to `60`. |

Without `--repo` or `REPO`, the script detects the repository from the current directory.

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `REPO` | Current directory's repository | GitHub repository to monitor when `--repo` is not set. |
| `BOT` | `chatgpt-codex-connector` | GitHub login whose 👍 reaction authorizes a merge. |

## Hivemind

The included `Procfile` is for [Hivemind](https://github.com/darthsim/hivemind), a process manager for Procfile-based applications. Replace `owner/repo` with the repository to monitor, then run:

```sh
hivemind
```

## Check

```sh
bash test_cli_target.sh
```
