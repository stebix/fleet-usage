# fleet-usage

Track how much you use AI coding agents across all of your machines.

Every machine runs `fleet-usage publish` on a schedule. It calls
[`ccusage`](https://github.com/ryoppippi/ccusage), writes an immutable JSON
snapshot, uploads it to a **private** GitHub data repository and updates its
own per-machine ledger there. There is no central aggregator: any machine with
a read token runs `fleet-usage show`, downloads all ledgers and aggregates
them on read.

## Install

```sh
uv tool install git+https://github.com/stebix/fleet-usage
```

The package is not on PyPI, so `uv tool install fleet-usage` fails. From a
local checkout, `uv tool install .` installs the same stable executable into
`~/.local/bin`.

Then create the local configuration and check it:

```sh
fleet-usage init --label my-laptop --repo OWNER/fleet-usage-data
fleet-usage doctor
```

`init` writes `settings.toml` and a placeholder `.env` into the platform
config directory (`~/.config/fleet-usage` on Linux). Put a fine-grained
GitHub token with *contents: read and write* on the data repository into that
`.env` file. Machines that should only read the fleet are set up with
`fleet-usage init --viewer-only`.

## Commands

| Command | Purpose |
| --- | --- |
| `fleet-usage init` | Create `settings.toml` and `.env`. |
| `fleet-usage doctor` | Check settings, token, paths and the collector. |
| `fleet-usage config show` | Print the effective settings, secrets masked. |
| `fleet-usage repo init` | Scaffold the private data repository. |
| `fleet-usage repo register` | Add this machine to `fleet.toml`. |
| `fleet-usage publish` | Collect, upload a snapshot, merge the ledger. |
| `fleet-usage show` | Aggregate the fleet ledgers and report. |
| `fleet-usage rebuild` | Rebuild a ledger from its snapshots. |
| `fleet-usage schedule` | Install, inspect or remove the hourly job. |

Global options: `--config PATH` selects the settings file (the environment
variable `FLEET_USAGE_CONFIG` does the same), `--version` prints the version.

Exit codes: `0` success, `2` configuration error, `3` collection failure,
`4` spooled but not uploaded, `5` remote conflict, `6` authentication failure.

## Status

Early development. `init`, `doctor` and `config show` work; the remaining
commands are stubs that exit with code 1.

## Development

```sh
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src/fleet_usage
uv run pytest -q
```
