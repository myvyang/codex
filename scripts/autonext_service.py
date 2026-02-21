#!/usr/bin/env python3
"""Decision service for Codex auto-next-turn hooks.

This service receives Codex turn-complete payloads and returns a JSON decision:
{
  "need_next_turn": bool,
  "next_turn_input": str,
  "reason": str
}

Default behavior uses your current Codex provider settings from:
- ~/.codex/config.toml
- ~/.codex/auth.json
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Optional

try:
    import tomllib
except Exception:  # pragma: no cover - Python <3.11 fallback path
    tomllib = None

try:  # pragma: no cover - optional dependency
    import tomli  # type: ignore
except Exception:  # pragma: no cover
    tomli = None

@dataclass
class Decision:
    need_next_turn: bool
    next_turn_input: str
    reason: str = ""


@dataclass
class ProviderRuntime:
    provider_id: str
    model: str
    base_url: str
    api_key: str
    headers: dict[str, str]
    query_params: dict[str, str]


class AutoNextService:
    def __init__(self) -> None:
        self.host = os.getenv("AUTO_NEXT_HOST", "127.0.0.1")
        self.port = int(os.getenv("AUTO_NEXT_PORT", "8765"))

        self.max_chain_per_thread = int(os.getenv("AUTO_NEXT_MAX_CHAIN", "3"))
        self.http_timeout_sec = float(os.getenv("AUTO_NEXT_HTTP_TIMEOUT_SEC", "15"))
        self.max_api_retries = int(os.getenv("AUTO_NEXT_MAX_API_RETRIES", "2"))
        self.exec_timeout_sec = float(os.getenv("AUTO_NEXT_EXEC_TIMEOUT_SEC", "45"))
        self.fallback_codex_exec = os.getenv("AUTO_NEXT_FALLBACK_CODEX_EXEC", "1").lower() not in (
            "0",
            "false",
            "no",
        )

        self.decider_mode = os.getenv("AUTO_NEXT_DECIDER", "codex_config")
        self.runtime = self._resolve_provider_runtime()
        self.llm_base_url = self.runtime.base_url
        self.llm_model = self.runtime.model

        self._lock = threading.Lock()
        self._chain_count_by_thread: dict[str, int] = {}

    def make_handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path not in ("/decide", "/hook", "/notify"):
                    self.send_response(404)
                    self.end_headers()
                    return

                content_length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"invalid json")
                    return

                decision = service.decide(payload if isinstance(payload, dict) else {})
                response = json.dumps(
                    {
                        "need_next_turn": decision.need_next_turn,
                        "next_turn_input": decision.next_turn_input,
                        "reason": decision.reason,
                    }
                ).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, fmt: str, *args: Any) -> None:
                _ = (fmt, args)

        return Handler

    def run(self) -> None:
        server = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        print(f"[auto-next] listening on http://{self.host}:{self.port}")
        print(
            "[auto-next] "
            f"decider={self.decider_mode} provider={self.runtime.provider_id} "
            f"base_url={self.runtime.base_url} model={self.llm_model}"
        )
        server.serve_forever()

    def decide(self, ev: dict[str, Any]) -> Decision:
        if ev.get("type") != "agent-turn-complete":
            return Decision(False, "", "unsupported_event")

        thread_id = str(ev.get("thread-id", "")).strip()
        input_messages = ev.get("input-messages") or []
        assistant_text = str(ev.get("last-assistant-message", "") or "").strip()

        if not thread_id:
            return Decision(False, "", "missing_thread_id")

        with self._lock:
            chain = self._chain_count_by_thread.get(thread_id, 0)
        if chain >= self.max_chain_per_thread:
            return Decision(False, "", "max_chain_reached")

        if self.decider_mode == "codex_config" and self.runtime.api_key:
            try:
                decision = self._decide_with_responses_api(ev, assistant_text, input_messages)
            except Exception as e:
                if self.fallback_codex_exec:
                    try:
                        decision = self._decide_with_codex_exec(ev, assistant_text, input_messages)
                    except Exception as exec_e:
                        return Decision(False, "", f"llm_error:{e}; codex_exec_error:{exec_e}")
                else:
                    return Decision(False, "", f"llm_error:{e}")
        elif self.decider_mode == "openai_chat" and self.runtime.api_key:
            try:
                decision = self._decide_with_openai_chat(ev, assistant_text, input_messages)
            except Exception as e:
                return Decision(False, "", f"llm_error:{e}")
        else:
            return Decision(False, "", "fallback_no_llm_or_key")

        # Chain limiter: only count when we keep auto-continuing.
        with self._lock:
            if decision.need_next_turn:
                self._chain_count_by_thread[thread_id] = chain + 1
            else:
                self._chain_count_by_thread[thread_id] = 0

        return decision

    def _decide_with_openai_chat(
        self,
        ev: dict[str, Any],
        assistant_text: str,
        input_messages: list[Any],
    ) -> Decision:
        url = self.llm_base_url.rstrip("/") + "/chat/completions"

        system_prompt = (
            "You are a strict controller deciding whether Codex should continue with exactly one additional turn. "
            "Only continue when there is a simple, concrete, low-risk next action that improves quality. "
            "Return strict JSON only with fields: need_next_turn (boolean), next_turn_input (string), reason (string). "
            "If no continuation is needed, set need_next_turn=false and next_turn_input to empty string."
        )

        user_payload = {
            "thread_id": ev.get("thread-id"),
            "turn_id": ev.get("turn-id"),
            "cwd": ev.get("cwd"),
            "input_messages": input_messages,
            "assistant_output": assistant_text,
        }

        body = {
            "model": self.llm_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.llm_api_key}",
            },
            method="POST",
        )

        raw = self._execute_http_request(req)

        outer = json.loads(raw)
        content = (
            outer.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            return Decision(False, "", "empty_llm_output")

        parsed = json.loads(content)
        need = bool(parsed.get("need_next_turn", False))
        next_input = str(parsed.get("next_turn_input", "") or "").strip()
        reason = str(parsed.get("reason", "") or "")

        if not need:
            return Decision(False, "", reason)
        if not next_input:
            return Decision(False, "", "need_true_but_empty_input")
        return Decision(True, next_input, reason)

    def _decide_with_responses_api(
        self,
        ev: dict[str, Any],
        assistant_text: str,
        input_messages: list[Any],
    ) -> Decision:
        system_prompt = (
            "You are a strict controller deciding whether Codex should continue with exactly one additional turn. "
            "Only continue when there is a simple, concrete, low-risk next action that improves quality. "
            "Return strict JSON only with fields: need_next_turn (boolean), next_turn_input (string), reason (string). "
            "If no continuation is needed, set need_next_turn=false and next_turn_input to empty string."
        )

        user_payload = {
            "thread_id": ev.get("thread-id"),
            "turn_id": ev.get("turn-id"),
            "cwd": ev.get("cwd"),
            "input_messages": input_messages,
            "assistant_output": assistant_text,
        }

        body = {
            "model": self.runtime.model,
            "input": [
                {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(user_payload, ensure_ascii=False),
                        }
                    ],
                },
            ],
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "auto_next_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "need_next_turn": {"type": "boolean"},
                            "next_turn_input": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["need_next_turn", "next_turn_input", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
        }

        url = self._build_responses_url(self.runtime.base_url, self.runtime.query_params)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.runtime.api_key}"}
        headers.update(self.runtime.headers)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=self.http_timeout_sec) as resp:
            raw = resp.read().decode("utf-8")

        outer = json.loads(raw)
        content = self._extract_responses_text(outer)
        if not content:
            return Decision(False, "", "empty_llm_output")

        parsed = json.loads(content)
        need = bool(parsed.get("need_next_turn", False))
        next_input = str(parsed.get("next_turn_input", "") or "").strip()
        reason = str(parsed.get("reason", "") or "")
        if not need:
            return Decision(False, "", reason)
        if not next_input:
            return Decision(False, "", "need_true_but_empty_input")
        return Decision(True, next_input, reason)

    def _decide_with_codex_exec(
        self,
        ev: dict[str, Any],
        assistant_text: str,
        input_messages: list[Any],
    ) -> Decision:
        schema = {
            "type": "object",
            "properties": {
                "need_next_turn": {"type": "boolean"},
                "next_turn_input": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["need_next_turn", "next_turn_input", "reason"],
            "additionalProperties": False,
        }
        payload = {
            "thread_id": ev.get("thread-id"),
            "turn_id": ev.get("turn-id"),
            "cwd": ev.get("cwd"),
            "input_messages": input_messages,
            "assistant_output": assistant_text,
        }
        prompt = (
            "你是自动续跑判断器。"
            "只返回 JSON（不要 markdown），字段必须是 need_next_turn(bool), next_turn_input(string), reason(string)。"
            "仅当存在明确、低风险、可立即执行的下一步时 need_next_turn=true；否则 false 且 next_turn_input 置空。\n"
            f"输入上下文:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as schema_file:
            schema_file.write(json.dumps(schema, ensure_ascii=False))
            schema_path = schema_file.name
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as out_file:
            out_path = out_file.name

        cwd = str(ev.get("cwd") or "").strip() or os.getcwd()
        command = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--cd",
            cwd,
            "-c",
            "notify=[]",
            "-c",
            "notify_next_turn=[]",
            "--output-schema",
            schema_path,
            "--output-last-message",
            out_path,
            "-",
        ]
        try:
            proc = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.exec_timeout_sec,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"exit_code={proc.returncode}")
            text = Path(out_path).read_text().strip()
            if not text:
                raise RuntimeError("empty codex exec output")
            parsed = json.loads(text)
        finally:
            try:
                os.remove(schema_path)
            except Exception:
                pass
            try:
                os.remove(out_path)
            except Exception:
                pass

        need = bool(parsed.get("need_next_turn", False))
        next_input = str(parsed.get("next_turn_input", "") or "").strip()
        reason = str(parsed.get("reason", "") or "")
        if not need:
            return Decision(False, "", reason or "codex_exec_no_next")
        if not next_input:
            return Decision(False, "", "codex_exec_need_true_but_empty_input")
        return Decision(True, next_input, reason or "codex_exec_next")

    def _extract_responses_text(self, response_obj: dict[str, Any]) -> str:
        output_text = response_obj.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output = response_obj.get("output")
        if not isinstance(output, list):
            return ""

        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for span in content:
                if not isinstance(span, dict):
                    continue
                span_type = span.get("type")
                text = span.get("text")
                if span_type in ("output_text", "text") and isinstance(text, str):
                    parts.append(text)
        return "\n".join(p.strip() for p in parts if p and p.strip()).strip()

    def _build_responses_url(self, base_url: str, query_params: dict[str, str]) -> str:
        base = base_url.rstrip("/")
        url = f"{base}/responses"
        if not query_params:
            return url
        return f"{url}?{urllib.parse.urlencode(query_params)}"

    def _execute_http_request(self, req: urllib.request.Request) -> str:
        last_error: Optional[Exception] = None
        attempts = max(1, self.max_api_retries)
        for idx in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.http_timeout_sec) as resp:
                    return resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                last_error = e
                # Retry on 5xx gateway errors only.
                if 500 <= e.code <= 599 and idx < attempts - 1:
                    continue
                raise
            except Exception as e:
                last_error = e
                if idx < attempts - 1:
                    continue
                raise
        if last_error is None:
            raise RuntimeError("request_failed_without_error")
        raise last_error

    def _resolve_provider_runtime(self) -> ProviderRuntime:
        cfg = self._read_codex_config()
        provider_id = str(
            os.getenv("AUTO_NEXT_PROVIDER")
            or cfg.get("model_provider")
            or "openai"
        ).strip()
        model = str(
            os.getenv("AUTO_NEXT_MODEL")
            or os.getenv("AUTO_NEXT_LLM_MODEL")
            or cfg.get("model")
            or "gpt-4o-mini"
        ).strip()

        providers = cfg.get("model_providers")
        if not isinstance(providers, dict):
            providers = {}
        provider_cfg = providers.get(provider_id)
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}

        base_url = str(
            os.getenv("AUTO_NEXT_BASE_URL")
            or provider_cfg.get("base_url")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip()

        headers: dict[str, str] = {}
        headers.update(self._default_codex_headers())

        raw_headers = provider_cfg.get("http_headers")
        if isinstance(raw_headers, dict):
            for k, v in raw_headers.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    headers[k.strip()] = v.strip()

        raw_env_headers = provider_cfg.get("env_http_headers")
        if isinstance(raw_env_headers, dict):
            for k, env_name in raw_env_headers.items():
                if not isinstance(k, str) or not isinstance(env_name, str):
                    continue
                env_val = os.getenv(env_name)
                if env_val and env_val.strip():
                    headers[k.strip()] = env_val.strip()

        query_params: dict[str, str] = {}
        raw_query = provider_cfg.get("query_params")
        if isinstance(raw_query, dict):
            for k, v in raw_query.items():
                if isinstance(k, str) and isinstance(v, str):
                    query_params[k] = v

        api_key = self._resolve_api_key(provider_cfg)
        return ProviderRuntime(
            provider_id=provider_id,
            model=model,
            base_url=base_url,
            api_key=api_key,
            headers=headers,
            query_params=query_params,
        )

    def _resolve_api_key(self, provider_cfg: dict[str, Any]) -> str:
        explicit = os.getenv("AUTO_NEXT_API_KEY")
        if explicit and explicit.strip():
            return explicit.strip()

        bearer = provider_cfg.get("experimental_bearer_token")
        if isinstance(bearer, str) and bearer.strip():
            return bearer.strip()

        env_key = provider_cfg.get("env_key")
        if isinstance(env_key, str) and env_key.strip():
            key = os.getenv(env_key)
            if key and key.strip():
                return key.strip()
            return ""

        requires_openai_auth = bool(provider_cfg.get("requires_openai_auth", False))
        if requires_openai_auth:
            env_key_val = os.getenv("OPENAI_API_KEY")
            if env_key_val and env_key_val.strip():
                return env_key_val.strip()
            auth_key = self._read_openai_api_key_from_auth_json()
            if auth_key:
                return auth_key
            return ""

        # Soft fallback for custom providers that still rely on OPENAI_API_KEY.
        env_key_val = os.getenv("OPENAI_API_KEY")
        if env_key_val and env_key_val.strip():
            return env_key_val.strip()
        return ""

    def _read_codex_config(self) -> dict[str, Any]:
        codex_home = Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()
        config_path = Path(
            os.getenv("AUTO_NEXT_CODEX_CONFIG", str(codex_home / "config.toml"))
        ).expanduser()
        if not config_path.is_file():
            return {}
        try:
            if tomllib is not None:
                with config_path.open("rb") as f:
                    parsed = tomllib.load(f)
                if isinstance(parsed, dict):
                    return parsed
            elif tomli is not None:
                with config_path.open("rb") as f:
                    parsed = tomli.load(f)
                if isinstance(parsed, dict):
                    return parsed
            else:
                parsed = self._parse_minimal_toml(config_path.read_text())
                if isinstance(parsed, dict):
                    return parsed
        except Exception:
            return {}
        return {}

    def _read_openai_api_key_from_auth_json(self) -> str:
        codex_home = Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()
        auth_path = Path(
            os.getenv("AUTO_NEXT_CODEX_AUTH", str(codex_home / "auth.json"))
        ).expanduser()
        if not auth_path.is_file():
            return ""
        try:
            parsed = json.loads(auth_path.read_text())
        except Exception:
            return ""
        value = parsed.get("OPENAI_API_KEY")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    def _default_codex_headers(self) -> dict[str, str]:
        originator = os.getenv("AUTO_NEXT_ORIGINATOR", "codex_cli_rs").strip() or "codex_cli_rs"
        version = (
            os.getenv("AUTO_NEXT_CODEX_VERSION")
            or self._read_codex_version()
            or "0.0.0"
        ).strip()
        user_agent = (
            f"{originator}/{version} "
            f"({platform.system()} {platform.release()}; {platform.machine()})"
        )
        return {
            "originator": originator,
            "version": version,
            "User-Agent": user_agent,
        }

    def _read_codex_version(self) -> str:
        codex_home = Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()
        version_path = codex_home / "version.json"
        if not version_path.is_file():
            return ""
        try:
            parsed = json.loads(version_path.read_text())
        except Exception:
            return ""
        value = parsed.get("version")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    def _parse_minimal_toml(self, text: str) -> dict[str, Any]:
        root: dict[str, Any] = {}
        section: list[str] = []
        for raw_line in text.splitlines():
            line = self._strip_toml_comment(raw_line).strip()
            if not line:
                continue

            if line.startswith("[") and line.endswith("]"):
                section_name = line[1:-1].strip()
                if section_name:
                    section = [seg.strip().strip('"').strip("'") for seg in section_name.split(".")]
                    self._ensure_path(root, section)
                continue

            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            parsed_value = self._parse_minimal_toml_value(value.strip())
            node = self._ensure_path(root, section)
            node[key] = parsed_value
        return root

    def _strip_toml_comment(self, line: str) -> str:
        out: list[str] = []
        in_single = False
        in_double = False
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'" and not in_double:
                in_single = not in_single
                out.append(ch)
            elif ch == '"' and not in_single:
                escaped = i > 0 and line[i - 1] == "\\"
                if not escaped:
                    in_double = not in_double
                out.append(ch)
            elif ch == "#" and not in_single and not in_double:
                break
            else:
                out.append(ch)
            i += 1
        return "".join(out)

    def _ensure_path(self, root: dict[str, Any], path: list[str]) -> dict[str, Any]:
        node = root
        for seg in path:
            next_node = node.get(seg)
            if not isinstance(next_node, dict):
                next_node = {}
                node[seg] = next_node
            node = next_node
        return node

    def _parse_minimal_toml_value(self, raw: str) -> Any:
        raw = raw.strip()
        if not raw:
            return ""
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            return bytes(raw[1:-1], "utf-8").decode("unicode_escape")
        if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
            return raw[1:-1]
        if raw.lower() == "true":
            return True
        if raw.lower() == "false":
            return False
        if raw.startswith("[") and raw.endswith("]"):
            body = raw[1:-1].strip()
            if not body:
                return []
            parts = self._split_toml_array(body)
            return [self._parse_minimal_toml_value(part) for part in parts]
        try:
            return int(raw)
        except Exception:
            pass
        try:
            return float(raw)
        except Exception:
            pass
        return raw

    def _split_toml_array(self, body: str) -> list[str]:
        parts: list[str] = []
        current: list[str] = []
        in_single = False
        in_double = False
        depth = 0
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "'" and not in_double:
                in_single = not in_single
                current.append(ch)
            elif ch == '"' and not in_single:
                escaped = i > 0 and body[i - 1] == "\\"
                if not escaped:
                    in_double = not in_double
                current.append(ch)
            elif ch == "[" and not in_single and not in_double:
                depth += 1
                current.append(ch)
            elif ch == "]" and not in_single and not in_double and depth > 0:
                depth -= 1
                current.append(ch)
            elif ch == "," and not in_single and not in_double and depth == 0:
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
            else:
                current.append(ch)
            i += 1
        part = "".join(current).strip()
        if part:
            parts.append(part)
        return parts


if __name__ == "__main__":
    AutoNextService().run()
