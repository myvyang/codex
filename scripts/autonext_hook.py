#!/usr/bin/env python3
"""Codex hook script for notify_next_turn.

Reads the Codex payload from argv, calls local decision service, and prints JSON decision to stdout.
Printed stdout is consumed by Codex core.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _resolve_output_path(payload: dict) -> Path:
    output_name = os.getenv("AUTO_NEXT_OUTPUT_FILE", "out.1").strip() or "out.1"
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        cwd = os.getcwd()

    out_path = Path(output_name)
    if not out_path.is_absolute():
        out_path = Path(cwd) / out_path
    return out_path


def _write_turn_output(payload: dict) -> Path:
    text = payload.get("last-assistant-message")
    if not isinstance(text, str):
        text = json.dumps(payload, ensure_ascii=False)

    out_path = _resolve_output_path(payload)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except Exception:
        # Hook must be best-effort and not block Codex flow.
        pass
    return out_path


def _append_decision_trace(out_path: Path, trace_line: str) -> None:
    try:
        with out_path.open("a", encoding="utf-8") as f:
            f.write("\n\n[auto-next] ")
            f.write(trace_line)
            f.write("\n")
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        return 0

    raw_payload = sys.argv[-1]
    try:
        payload = json.loads(raw_payload)
    except Exception:
        return 0

    out_path = None
    if isinstance(payload, dict):
        out_path = _write_turn_output(payload)

    service_url = os.getenv("AUTO_NEXT_SERVICE_URL", "http://127.0.0.1:8765/decide")
    timeout_sec = float(os.getenv("AUTO_NEXT_HOOK_TIMEOUT_SEC", "20.0"))

    req = urllib.request.Request(
        service_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            response_text = resp.read().decode("utf-8").strip()
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
        if out_path is not None:
            _append_decision_trace(out_path, f"request_error={type(e).__name__}")
        return 0

    if out_path is not None and response_text:
        _append_decision_trace(out_path, response_text)

    # Stdout is parsed by Codex next-turn hook parser.
    if response_text:
        print(response_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
