# Auto Next Turn In The Same TUI Session

This setup keeps normal Codex usage while adding automatic continuation in the **same running session**.

Flow:

1. Codex completes a turn.
2. `notify_next_turn` hook is invoked.
3. Hook sends turn payload to your local decision service.
4. Service returns JSON decision (`need_next_turn` + `next_turn_input`).
5. Codex core enqueues a **new turn** internally, so you see it directly in the same TUI/CLI session.

If the service returns a non-empty `reason`, Codex emits a warning event
(`Auto next-turn decision: ...`) so the decision basis is visible in TUI/exec output.

## Files

- Decision hook: `scripts/autonext_hook.py`
- Decision service: `scripts/autonext_service.py`

## Configure Codex

In `~/.codex/config.toml`:

```toml
notify_next_turn = ["python3", "/ABSOLUTE/PATH/TO/codex/scripts/autonext_hook.py"]
notify_next_turn_service = ["python3", "/ABSOLUTE/PATH/TO/codex/scripts/autonext_service.py"]
```

Optional (separate one-way notification hook):

```toml
notify = ["python3", "/path/to/another_notify_script.py"]
```

`notify_next_turn_service` means Codex starts a dedicated sidecar service automatically for each
session and stops it on shutdown. No manual service start is required.

Each session gets its own service URL/port, so multiple Codex sessions do not interfere.

By default the service will read your current Codex provider config from `~/.codex/config.toml`
and key from `~/.codex/auth.json`/env, then call the same provider's `/responses`.

## Optional model/provider overrides

```bash
export AUTO_NEXT_MODEL="gpt-4o-mini"
export AUTO_NEXT_PROVIDER="openai"
export AUTO_NEXT_BASE_URL="https://api.openai.com/v1"
export AUTO_NEXT_API_KEY="sk-..."
```

If no key can be resolved, service returns `need_next_turn=false`.

## Hook/Service env vars

- `AUTO_NEXT_SERVICE_URL` (hook-side, default `http://127.0.0.1:8765/decide`)
- `AUTO_NEXT_HOOK_TIMEOUT_SEC` (hook-side timeout, default `20.0`)
- `next_turn_local_log` / `NEXT_TURN_LOCAL_LOG` (hook-side local logging switch; set `1` to write output/decision trace)
- `AUTO_NEXT_OUTPUT_FILE` (hook-side completed-output file name/path, default `out.1`; only used when local logging is enabled)
- `AUTO_NEXT_DECIDER` (`codex_config` default, `openai_chat` optional fallback)
- `AUTO_NEXT_HOST` (service bind host, default `127.0.0.1`)
- `AUTO_NEXT_PORT` (service bind port, default `8765`)
- `AUTO_NEXT_MAX_CHAIN` (max consecutive auto turns per thread, default `3`)
- `AUTO_NEXT_HTTP_TIMEOUT_SEC` (service -> provider timeout, default `15`)
- `AUTO_NEXT_MAX_API_RETRIES` (provider call retries, default `2`)
- `AUTO_NEXT_FALLBACK_CODEX_EXEC` (`1` by default; provider call fails then fallback to `codex exec`)
- `AUTO_NEXT_EXEC_TIMEOUT_SEC` (`codex exec` fallback timeout, default `45`)
- `AUTO_NEXT_ORIGINATOR` (default `codex_cli_rs`, sent as request header)
- `AUTO_NEXT_CODEX_VERSION` (default from `~/.codex/version.json`, sent as request header)
