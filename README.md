# Codex Fork: Auto Next-Turn Closed Loop

This repository is a custom Codex fork focused on one enhancement:

- after a turn completes, run an intelligent next-step decision;
- if the result is safely continuable, inject the next turn automatically;
- extend execution from single-turn completion to a longer closed loop.

## What Changed

- Added `notify_next_turn` hook directive parsing and next-turn queueing in core.
- Added per-session sidecar startup via `notify_next_turn_service`.
- Added in-session warning visibility for decision reasoning in TUI/CLI.
- Added local scripts:
  - `scripts/autonext_hook.py`
  - `scripts/autonext_service.py`
- Added optional local turn logging (`out.1`) controlled by env switch, off by default.

## Runtime Flow

1. Codex finishes one turn.
2. `notify_next_turn` hook is triggered.
3. Hook sends payload to local sidecar decision service.
4. Sidecar calls configured model/provider and returns JSON decision.
5. If `need_next_turn=true`, Codex injects a new user turn in the same session.

## Build And Run

```bash
cd codex-rs
cargo build -p codex-cli --release
./target/release/codex
```

## Config

Set in `~/.codex/config.toml`:

```toml
notify_next_turn = ["python3", "/ABSOLUTE/PATH/TO/codex/scripts/autonext_hook.py"]
notify_next_turn_service = ["python3", "/ABSOLUTE/PATH/TO/codex/scripts/autonext_service.py"]
```

## Local Logging (out.1)

`out.1` writing is disabled by default.

Enable it only when needed:

```bash
export next_turn_local_log=1
# or
export NEXT_TURN_LOCAL_LOG=1
```

Optional output filename/path:

```bash
export AUTO_NEXT_OUTPUT_FILE=out.1
```

When local logging is enabled, hook writes:

- last assistant output
- decision trace line (`[auto-next] ...`)

## Decision Model Source

By default, decision service reads the same provider/model settings from your Codex config and auth.
You can still override provider/model by env vars (see `docs/auto_next_turn.md`).

## Docs

- `docs/auto_next_turn.md`
- `docs/install.md`
- `docs/contributing.md`

This repository is licensed under the [Apache-2.0 License](LICENSE).
