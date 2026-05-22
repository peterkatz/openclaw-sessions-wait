import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";

const OPENCLAW_HOME = process.env.OPENCLAW_HOME || join(homedir(), ".openclaw");
const STATE_SESSIONS_DIR = join(OPENCLAW_HOME, "workspace", "state", "sessions");
const AGENTS_DIR = join(OPENCLAW_HOME, "agents");

interface SessionState {
  created_at?: string;
  last_used_at?: string;
  closed?: boolean;
  closed_at?: string;
  cwd?: string;
  pid?: number;
  title?: string | null;
  messages?: Array<Record<string, unknown>>;
}

async function findSessionFiles(sessionKey: string): Promise<string[]> {
  const encoded = sessionKey.replace(/:/g, "%3A");
  const matches: string[] = [];

  try {
    const files = await readdir(STATE_SESSIONS_DIR);
    for (const file of files) {
      if (file.includes(sessionKey) || file.includes(encoded)) {
        matches.push(join(STATE_SESSIONS_DIR, file));
      }
    }
  } catch { /* dir may not exist */ }

  try {
    const agents = await readdir(AGENTS_DIR);
    for (const agent of agents) {
      try {
        const files = await readdir(join(AGENTS_DIR, agent, "sessions"));
        for (const file of files) {
          if ((file.includes(sessionKey) || file.includes(encoded)) && file.endsWith(".jsonl")) {
            matches.push(join(AGENTS_DIR, agent, "sessions", file));
          }
        }
      } catch { /* agent may lack sessions dir */ }
    }
  } catch { /* agents dir may not exist */ }

  return [...new Set(matches)].sort();
}

async function readSessionState(filePath: string): Promise<SessionState | null> {
  try {
    return JSON.parse(await readFile(filePath, "utf-8")) as SessionState;
  } catch {
    return null;
  }
}

function extractResult(state: SessionState): string | null {
  const messages = state.messages || [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i] as Record<string, unknown>;
    const assistant = msg.Assistant || msg.assistant;
    if (assistant && typeof assistant === "object") {
      const content = (assistant as Record<string, unknown>).content;
      if (Array.isArray(content)) {
        const texts: string[] = [];
        for (const part of content) {
          if (typeof part === "object" && part !== null && "Text" in part) {
            texts.push((part as { Text: string }).Text);
          } else if (typeof part === "string") {
            texts.push(part);
          }
        }
        if (texts.length > 0) return texts.join("\n");
      }
    }
  }
  return null;
}

async function checkSession(sessionKey: string) {
  const files = await findSessionFiles(sessionKey);
  const jsonFiles = files.filter((f) => f.endsWith(".json"));

  for (const f of jsonFiles) {
    const state = await readSessionState(f);
    if (state?.closed === true) {
      return { closed: true, state, filePath: f, filesFound: files.length };
    }
  }

  for (const f of jsonFiles) {
    const state = await readSessionState(f);
    if (state) {
      return { closed: false, state, filePath: f, filesFound: files.length };
    }
  }

  return { closed: false, state: null, filePath: null, filesFound: files.length };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export default defineToolPlugin({
  id: "sessions-wait",
  name: "Sessions Wait",
  description:
    "Wait for OpenClaw child sessions (ACP builders, verifiers, probes) to finish without sleep loops.",
  tools: (tool) => [
    tool({
      name: "sessions_wait",
      description:
        "Wait for an OpenClaw child session (ACP builder, verifier, etc.) to finish. " +
        "Polls session state files and returns the result when the session closes, " +
        "or returns a timeout. Use instead of sleep loops after sessions_spawn.",
      parameters: Type.Object({
        session_key: Type.String({
          description:
            "Session key or UUID fragment from sessions_spawn.",
        }),
        timeout_seconds: Type.Optional(
          Type.Number({ description: "Max wait seconds. Default 600.", default: 600 }),
        ),
        poll_interval: Type.Optional(
          Type.Number({ description: "Poll interval seconds. Default 5.", default: 5 }),
        ),
      }),
      execute: async ({ session_key, timeout_seconds, poll_interval }) => {
        const timeout = timeout_seconds ?? 600;
        const interval = poll_interval ?? 5;
        const deadline = Date.now() + timeout * 1000;
        let lastState: SessionState | null = null;
        let lastFile: string | null = null;

        while (Date.now() < deadline) {
          const { closed, state, filePath, filesFound } = await checkSession(session_key);
          lastState = state;
          lastFile = filePath;

          if (closed && state) {
            return {
              status: "closed",
              session_key,
              file: filePath,
              closed_at: state.closed_at,
              result: extractResult(state),
              message_count: (state.messages || []).length,
              cwd: state.cwd,
              pid: state.pid,
            };
          }

          if (filesFound === 0 && Date.now() > deadline - timeout * 500) {
            return { status: "not_found", session_key, files_found: 0 };
          }

          await sleep(interval * 1000);
        }

        return {
          status: "timeout",
          session_key,
          file: lastFile,
          timeout_seconds: timeout,
          last_closed: lastState?.closed,
          last_used_at: lastState?.last_used_at,
          message_count: (lastState?.messages || []).length,
        };
      },
    }),

    tool({
      name: "sessions_check",
      description:
        "Instant status check on an OpenClaw child session. Returns running/closed, " +
        "message count, and result if closed. No waiting.",
      parameters: Type.Object({
        session_key: Type.String({
          description: "Session key or UUID fragment.",
        }),
      }),
      execute: async ({ session_key }) => {
        const { closed, state, filePath, filesFound } = await checkSession(session_key);
        return {
          status: closed ? "closed" : "running",
          session_key,
          file: filePath,
          closed_at: state?.closed_at,
          result: closed && state ? extractResult(state) : null,
          message_count: (state?.messages || []).length,
          files_found: filesFound,
        };
      },
    }),
  ],
});
