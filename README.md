# openclaw-sessions-wait

An OpenClaw plugin that gives agents a clean way to wait for child sessions to finish. Replaces the ugly `sleep` polling loops that agents fall back to when coordinating builder/verifier lanes.

## The Problem

OpenClaw has `sessions_spawn` to start child sessions (ACP builders, verifiers, probes), but no built-in tool to wait for them to finish. The agent can fire off a builder lane, but then has no blocking primitive to say "wait until that's done, then give me the result."

So agents do this:


```bash
sleep 120; python3 -c "
import json, glob
for p in glob.glob('/path/to/state/sessions/*session-key*'):
    d = json.load(open(p))
    if d.get('closed'): print('done')
"
```



...nested inside bash calls, sometimes sleeping 2+ minutes between polls, burning tokens re-entering the work loop, and making the controller agent look like it's hung when it's really just waiting.

This is what it looked like in practice — a controller agent coordinating a SlicerRebuild project was spending most of its time in sleep loops instead of doing useful work:


```
01:53:52 CALL bash: sleep 45; python3 check_state.py ...
01:54:37 RESULT: still running
01:54:46 CALL bash: sleep 75; python3 check_state.py ...
01:56:01 RESULT: still running
01:56:47 CALL bash: sleep 120; python3 check_state.py ...
01:58:47 RESULT: closed
```



Nearly 5 minutes of wall time, three tool calls, three re-entries into the model's context, all to find out "yeah it's done now."

## The Fix

This plugin adds two tools to OpenClaw:

### `sessions_wait`

Blocks until a child session closes or times out. One tool call, one result.


```
sessions_wait(
  session_key: "agent:codex:acp:8d30ee5d-...",
  timeout_seconds: 900,
  poll_interval: 5
)
```



Returns:

```json
{
  "status": "closed",
  "session_key": "agent:codex:acp:8d30ee5d-...",
  "file": "/path/to/state/file.json",
  "closed_at": "2026-05-22T02:00:00Z",
  "result": "Last assistant message from the child session",
  "message_count": 3,
  "cwd": "/path/to/repo",
  "pid": 12345
}
```



On timeout:

```json
{
  "status": "timeout",
  "session_key": "...",
  "timeout_seconds": 900,
  "last_closed": false,
  "message_count": 0
}
```



If no matching session files are found:

```json
{
  "status": "not_found",
  "session_key": "...",
  "files_found": 0
}
```



### `sessions_check`

Instant non-blocking status check. Use when you want to peek without waiting.


```
sessions_check(session_key: "agent:codex:acp:8d30ee5d-...")
```



Returns `running` or `closed` with message count and result if available.

## The Controller Pattern

This plugin is designed for the controller/builder/verifier pattern used in autonomous OpenClaw work:

1. **Controller** decides the next chunk of work
2. **Controller** calls `sessions_spawn(runtime: "acp", mode: "run", ...)` → gets a session key
3. **Controller** calls `sessions_wait(session_key: "<key>", timeout_seconds: 900)` → blocks until builder finishes
4. **Controller** reads the result, dispatches a verifier
5. **Controller** calls `sessions_wait` on the verifier
6. **Controller** reports receipts and moves to the next chunk

Without this plugin, step 3 becomes a multi-turn sleep loop that wastes tokens and time. With it, it's a single tool call.

## How It Works

The plugin polls OpenClaw session state files at a configurable interval (default: 5 seconds). It checks:

- `$OPENCLAW_HOME/workspace/state/sessions/` — ACP one-shot session state files
- `$OPENCLAW_HOME/agents/*/sessions/` — agent session directories

It matches files by session key fragment (supports both raw and URL-encoded keys), reads the JSON state, and watches the `closed` field. When a session closes, it extracts the last assistant message as the result.

## Install

### As an OpenClaw plugin (recommended)


```bash
openclaw plugins install /path/to/openclaw-sessions-wait
```



Or install from git:


```bash
openclaw plugins install git:https://github.com/peterkatz/openclaw-sessions-wait
```



### As an MCP server (fallback)

If plugin install doesn't work on your setup, you can use the bundled MCP server directly:


```bash
openclaw mcp set sessions-wait '{"command":"python3","args":["/path/to/openclaw-sessions-wait/mcp/server.py"]}'
```



The MCP server (`mcp/server.py`) is a standalone Python implementation of the same tools. No dependencies beyond Python 3.8+.

## Build from Source


```bash
npm install
npm run plugin:build
npm run plugin:validate
```



## Configuration

Optional config in `openclaw.json`:


```json
{
  "plugins": {
    "entries": {
      "sessions-wait": {
        "enabled": true,
        "config": {
          "defaultTimeoutSeconds": 600,
          "defaultPollInterval": 5
        }
      }
    }
  }
}
```


## Background

This plugin was born out of frustration with autonomous agent coordination in OpenClaw. The specific case: a multi-agent setup where a controller agent (`slicer-pm`) was managing a software rebuild project through Telegram. The controller would spawn Codex ACP builder sessions for implementation chunks, then spawn separate verifier sessions to check the work.

Every spawn required the controller to poll for completion. Without a blocking wait primitive, the agent fell back to bash sleep loops — sleeping 45 seconds, checking, sleeping 75 seconds, checking, sleeping 120 seconds, checking. Each poll was a full tool call round trip through the model, burning tokens and making the Telegram thread look dead for minutes at a time.

The fix was obvious: give the agent a tool that blocks until the child session is done. Poll internally at 5-second intervals, return the result in one shot. The controller goes from three tool calls and 5 minutes of wall time to one tool call and immediate results.

## Requirements

- OpenClaw 2026.5.17+
- Node.js 20+ (for plugin mode)
- Python 3.8+ (for MCP server mode)

## License

MIT
