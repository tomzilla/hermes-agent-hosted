"""tmux-based client that runs Claude Code as an interactive subprocess.

This adapter lets Hermes treat Claude Code as a chat-style backend by
running it inside a tmux session and communicating via send-keys / capture.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

_TMUX_SESSION_NAME = "hermes-claude"
_DEFAULT_TIMEOUT_SECONDS = 900.0

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_JSON_RE = re.compile(
    r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,\s*\"function\"\s*:\s*\{.*?\}\s*\}",
    re.DOTALL,
)


def _resolve_claude_command() -> str:
    return os.getenv("HERMES_CLAUDE_COMMAND", "").strip() or "claude"


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    sections: list[str] = [
        "You are Claude Code, an autonomous agent running inside a tmux session managed by Hermes.",
        "Your task is to complete the user's request. You have access to tools.",
        "IMPORTANT: If you use a tool, you MUST emit the result using <tool_call>{...}</tool_call> blocks.",
        'Format tool calls exactly as: {"id":"call_xxx","type":"function","function":{"name":"tool_name","arguments":{"arg1":"value1"}}}',
        "If no tool is needed, respond with text only.",
    ]

    if isinstance(tools, list) and tools:
        tool_specs: list[dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            tool_specs.append(
                {
                    "name": name.strip(),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        if tool_specs:
            sections.append(
                "Available tools:\n" + json.dumps(tool_specs, ensure_ascii=False)
            )

    if tool_choice is not None:
        sections.append(f"Tool choice hint: {json.dumps(tool_choice, ensure_ascii=False)}")

    transcript: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            role = "context"

        content = message.get("content")
        rendered = _render_message_content(content)
        if not rendered:
            continue

        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool Result",
            "context": "Context",
        }.get(role, role.title())
        transcript.append(f"{label}:\n{rendered}")

    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))

    sections.append("Respond to the latest user request. Use tools if needed.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if "content" in content and isinstance(content.get("content"), str):
            return str(content.get("content") or "").strip()
        return json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _extract_tool_calls_from_text(text: str) -> tuple[list[SimpleNamespace], str]:
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: list[SimpleNamespace] = []
    consumed_spans: list[tuple[int, int]] = []

    def _try_add_tool_call(raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"claude_call_{len(extracted)+1}"

        extracted.append(
            SimpleNamespace(
                id=call_id,
                call_id=call_id,
                response_item_id=None,
                type="function",
                function=SimpleNamespace(name=fn_name.strip(), arguments=fn_args),
            )
        )

    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        raw = m.group(1)
        _try_add_tool_call(raw)
        consumed_spans.append((m.start(), m.end()))

    if not extracted:
        for m in _TOOL_CALL_JSON_RE.finditer(text):
            raw = m.group(0)
            _try_add_tool_call(raw)
            consumed_spans.append((m.start(), m.end()))

    if not consumed_spans:
        return extracted, text.strip()

    consumed_spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in consumed_spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return extracted, cleaned


class _ClaudeChatCompletions:
    def __init__(self, client: "ClaudeTmuxClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_streaming_response(**kwargs)


class _ClaudeChatNamespace:
    def __init__(self, client: "ClaudeTmuxClient"):
        self.completions = _ClaudeChatCompletions(client)


class ClaudeTmuxClient:
    """OpenAI-client-compatible facade for Claude Code run via tmux."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        acp_cwd: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "claude-tmux"
        self.base_url = base_url or "tmux://claude"
        self._default_headers = dict(default_headers or {})
        self._claude_command = acp_command or command or _resolve_claude_command()
        self._claude_args = list(acp_args or args or [])
        self._cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = _ClaudeChatNamespace(self)
        self.is_closed = False
        self._session_name = _TMUX_SESSION_NAME
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def close(self) -> None:
        with self._lock:
            self.is_closed = True
            self._stop_event.set()
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", self._session_name],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass

    def _tmux_send(self, text: str) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", self._session_name, "-l", text],
            capture_output=True,
            check=True,
        )

    def _tmux_send_enter(self) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", self._session_name, "Enter"],
            capture_output=True,
            check=True,
        )

    def _tmux_capture(self) -> str:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self._session_name, "-p"],
            capture_output=True,
            text=True,
        )
        return result.stdout if result.returncode == 0 else ""

    def _ensure_session(self) -> None:
        """Start a tmux session running claude."""
        check = subprocess.run(
            ["tmux", "has-session", "-t", self._session_name],
            capture_output=True,
        )
        if check.returncode == 0:
            return

        cmd = [self._claude_command] + self._claude_args
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self._session_name, "-c", self._cwd] + cmd,
            capture_output=True,
            check=True,
        )
        time.sleep(2)

    def _clear_pane(self) -> None:
        """Clear the tmux pane buffer by sending Ctrl-L (clear scrollback)."""
        subprocess.run(
            ["tmux", "send-keys", "-t", self._session_name, "C-l"],
            capture_output=True,
        )

    def _create_streaming_response(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = True,
        **_: Any,
    ) -> Iterator[Any]:
        """Yield streaming chunks mimicking the OpenAI chat completions format.

        Each yielded value is a SimpleNamespace mimicking an httpx/OAI streaming
        chunk so that Hermes's _call_chat_completions loop can consume it directly.
        """
        if timeout is None:
            effective_timeout = _DEFAULT_TIMEOUT_SECONDS
        elif isinstance(timeout, (int, float)):
            effective_timeout = float(timeout)
        else:
            candidates = [
                getattr(timeout, attr, None)
                for attr in ("read", "write", "connect", "pool", "timeout")
            ]
            numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
            effective_timeout = max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS

        prompt_text = _format_messages_as_prompt(
            messages or [],
            model=model,
            tools=tools,
            tool_choice=tool_choice,
        )

        self._ensure_session()
        self._stop_event.clear()

        # Clear screen and scrollback so capture is clean
        self._clear_pane()

        # Inject the prompt
        self._tmux_send(prompt_text)
        self._tmux_send_enter()

        deadline = time.time() + effective_timeout
        last_capture = ""
        poll_interval = 0.4
        model_name = model or "claude-tmux"
        chunk_index = [0]
        accumulated = ""

        def make_chunk(
            chunk_type: str,
            content: str = "",
            delta: str = "",
            tool_calls: list | None = None,
            finish_reason: str | None = None,
            index: int = 0,
            chunk_id: str = "",
        ) -> SimpleNamespace:
            return SimpleNamespace(
                id=chunk_id or f"chatcmpl-{chunk_index[0]}",
                object="chat.completion.chunk",
                created=int(time.time()),
                model=model_name,
                choices=[
                    SimpleNamespace(
                        index=index,
                        delta=SimpleNamespace(**({"content": delta} if delta else {})),
                        finish_reason=finish_reason,
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                ),
            )

        yield make_chunk("start", chunk_id=f"chatcmpl-{chunk_index[0]}")

        tool_calls_found: list[SimpleNamespace] = []
        text_parts: list[str] = []
        reasoning_parts: list[str] = []

        while time.time() < deadline:
            if self._stop_event.is_set():
                break

            capture = self._tmux_capture()
            if capture and capture != last_capture:
                new_content = capture[len(last_capture):]
                last_capture = capture
                accumulated += new_content

                # Parse for tool calls
                tc, remaining = _extract_tool_calls_from_text(new_content)
                if tc:
                    for t in tc:
                        chunk_index[0] += 1
                        yield SimpleNamespace(
                            id=f"chatcmpl-{chunk_index[0]}",
                            object="chat.completion.chunk",
                            created=int(time.time()),
                            model=model_name,
                            choices=[
                                SimpleNamespace(
                                    index=0,
                                    delta=SimpleNamespace(
                                        tool_calls=[
                                            SimpleNamespace(
                                                index=0,
                                                id=t.id,
                                                type="function",
                                                function=SimpleNamespace(
                                                    name=t.function.name,
                                                    arguments=t.function.arguments,
                                                ),
                                            )
                                        ]
                                    ),
                                    finish_reason=None,
                                )
                            ],
                        )
                    tool_calls_found.extend(tc)
                    if remaining.strip():
                        text_parts.append(remaining.strip())
                elif new_content.strip() and not tc:
                    # Send text delta
                    delta_text = new_content
                    chunk_index[0] += 1
                    yield SimpleNamespace(
                        id=f"chatcmpl-{chunk_index[0]}",
                        object="chat.completion.chunk",
                        created=int(time.time()),
                        model=model_name,
                        choices=[
                            SimpleNamespace(
                                index=0,
                                delta=SimpleNamespace(content=delta_text),
                                finish_reason=None,
                            )
                        ],
                    )

            time.sleep(poll_interval)

        # Final chunk with usage and finish_reason
        chunk_index[0] += 1
        tool_calls_out = None
        finish_reason = None
        if tool_calls_found:
            finish_reason = "tool_calls"
            tool_calls_out = []
            for i, t in enumerate(tool_calls_found):
                tool_calls_out.append(
                    SimpleNamespace(
                        index=i,
                        id=t.id,
                        type="function",
                        function=SimpleNamespace(
                            name=t.function.name,
                            arguments=t.function.arguments,
                        ),
                    )
                )
        else:
            finish_reason = "stop"

        yield SimpleNamespace(
            id=f"chatcmpl-{chunk_index[0]}",
            object="chat.completion.chunk",
            created=int(time.time()),
            model=model_name,
            choices=[
                SimpleNamespace(
                    index=0,
                    delta=SimpleNamespace(),
                    finish_reason=finish_reason,
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            ),
        )

        # Final full response (used by non-streaming code paths)
        full_output = self._tmux_capture()
        tool_calls_final, cleaned_text = _extract_tool_calls_from_text(full_output)

        yield SimpleNamespace(
            id=f"chatcmpl-{chunk_index[0]}",
            object="chat.completion",
            created=int(time.time()),
            model=model_name,
            choices=[
                SimpleNamespace(
                    index=0,
                    message=SimpleNamespace(
                        content=cleaned_text,
                        tool_calls=tool_calls_final,
                        role="assistant",
                    ),
                    finish_reason="tool_calls" if tool_calls_final else "stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            ),
        )
