#!/usr/bin/env python3
"""
OpenClaw MCP server: sessions_wait

Exposes a single tool that polls child session state files until the session
closes or a timeout is reached. Replaces the ugly sleep-loop pattern agents
use when waiting for ACP builder/verifier lanes to finish.
"""

import glob
import json
import os
import sys
import time
from pathlib import Path

# --- MCP stdio protocol helpers ---

def read_message():
    """Read a JSON-RPC message from stdin."""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("utf-8")
        if line.strip() == "":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", 0))
    if length == 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def send_message(msg):
    """Write a JSON-RPC message to stdout."""
    body = json.dumps(msg)
    encoded = body.encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def respond(id, result):
    send_message({"jsonrpc": "2.0", "id": id, "result": result})


def respond_error(id, code, message):
    send_message({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


# --- Session waiting logic ---

OPENCLAW_HOME = os.environ.get("OPENCLAW_HOME", os.path.expanduser("~/.openclaw"))
STATE_SESSIONS_DIR = os.path.join(OPENCLAW_HOME, "workspace", "state", "sessions")
AGENTS_DIR = os.path.join(OPENCLAW_HOME, "agents")


def find_session_files(session_key):
    """Find all state files matching a session key (or fragment)."""
    # URL-encoded and raw patterns
    encoded = session_key.replace(":", "%3A")
    matches = []
    for pattern in [f"*{session_key}*", f"*{encoded}*"]:
        matches.extend(glob.glob(os.path.join(STATE_SESSIONS_DIR, pattern)))
    # Also check agent session directories
    for agent_dir in glob.glob(os.path.join(AGENTS_DIR, "*/sessions/*.jsonl")):
        if session_key in agent_dir or encoded in agent_dir:
            matches.append(agent_dir)
    return sorted(set(matches))


def check_session_closed(session_key):
    """Check if any state file for this session shows closed=true.
    Returns (closed: bool, state: dict or None, file_path: str or None)."""
    files = find_session_files(session_key)
    if not files:
        return False, None, None

    # Check JSON state files (not .jsonl trajectory files)
    for f in files:
        if not f.endswith(".json"):
            continue
        try:
            data = json.loads(Path(f).read_text())
            if data.get("closed") is True:
                return True, data, f
        except (json.JSONDecodeError, OSError):
            continue

    # Return latest state even if not closed
    for f in files:
        if not f.endswith(".json"):
            continue
        try:
            data = json.loads(Path(f).read_text())
            return False, data, f
        except (json.JSONDecodeError, OSError):
            continue

    return False, None, None


def extract_result(state):
    """Pull the last assistant message text from a closed session state."""
    if not state:
        return None
    messages = state.get("messages", [])
    if not messages:
        return None
    # Messages are dicts like {"Assistant": {...}} or {"User": {...}}
    for msg in reversed(messages):
        assistant = msg.get("Assistant", msg.get("assistant"))
        if assistant:
            content = assistant.get("content", [])
            texts = []
            for part in content:
                if isinstance(part, dict) and "Text" in part:
                    texts.append(part["Text"])
                elif isinstance(part, str):
                    texts.append(part)
            if texts:
                return "\n".join(texts)
    return None


def wait_for_session(session_key, timeout_seconds=600, poll_interval=5):
    """Poll until session closes or timeout. Returns result dict."""
    deadline = time.time() + timeout_seconds
    last_state = None
    last_file = None

    while time.time() < deadline:
        closed, state, fpath = check_session_closed(session_key)
        last_state = state
        last_file = fpath

        if closed:
            result_text = extract_result(state)
            return {
                "status": "closed",
                "session_key": session_key,
                "file": fpath,
                "closed_at": state.get("closed_at"),
                "result": result_text,
                "message_count": len(state.get("messages", [])),
                "cwd": state.get("cwd"),
                "pid": state.get("pid"),
            }

        time.sleep(poll_interval)

    # Timeout
    return {
        "status": "timeout",
        "session_key": session_key,
        "file": last_file,
        "timeout_seconds": timeout_seconds,
        "last_closed": last_state.get("closed") if last_state else None,
        "last_used_at": last_state.get("last_used_at") if last_state else None,
        "message_count": len(last_state.get("messages", [])) if last_state else 0,
    }


# --- MCP server main loop ---

TOOLS = [
    {
        "name": "sessions_wait",
        "description": (
            "Wait for an OpenClaw child session (ACP builder, verifier, etc.) to finish. "
            "Polls the session state files and returns the result when the session closes, "
            "or returns a timeout. Use this instead of sleep loops when waiting for "
            "spawned sessions to complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": (
                        "Session key or fragment to match. Can be a full session key like "
                        "'agent:codex:acp:8d30ee5d-...' or a partial match like a UUID fragment."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to wait before returning a timeout. Default 600 (10 minutes).",
                    "default": 600,
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Seconds between polls. Default 5.",
                    "default": 5,
                },
            },
            "required": ["session_key"],
        },
    },
    {
        "name": "sessions_check",
        "description": (
            "Check the current state of an OpenClaw child session without waiting. "
            "Returns whether it's open or closed, message count, and last result if available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "Session key or fragment to match.",
                },
            },
            "required": ["session_key"],
        },
    },
]


def handle_tool_call(name, arguments):
    if name == "sessions_wait":
        session_key = arguments["session_key"]
        timeout = arguments.get("timeout_seconds", 600)
        interval = arguments.get("poll_interval", 5)
        return wait_for_session(session_key, timeout, interval)

    elif name == "sessions_check":
        session_key = arguments["session_key"]
        closed, state, fpath = check_session_closed(session_key)
        result_text = extract_result(state) if closed else None
        return {
            "status": "closed" if closed else "running",
            "session_key": session_key,
            "file": fpath,
            "closed_at": state.get("closed_at") if state else None,
            "result": result_text,
            "message_count": len(state.get("messages", [])) if state else 0,
            "files_found": len(find_session_files(session_key)),
        }

    return {"error": f"Unknown tool: {name}"}


def main():
    while True:
        msg = read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            respond(id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "openclaw-sessions-wait",
                    "version": "1.0.0",
                },
            })
        elif method == "notifications/initialized":
            pass  # no response needed for notifications
        elif method == "tools/list":
            respond(id, {"tools": TOOLS})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            try:
                result = handle_tool_call(tool_name, arguments)
                respond(id, {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2)}
                    ]
                })
            except Exception as e:
                respond(id, {
                    "content": [
                        {"type": "text", "text": json.dumps({"error": str(e)})}
                    ],
                    "isError": True,
                })
        elif method == "ping":
            respond(id, {})
        elif id is not None:
            respond_error(id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    main()
