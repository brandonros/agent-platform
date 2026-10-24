// Claude Code conversations, folded from the hook event log written by the org
// plugin (apps/claude-base/org/plugins/org/hooks/record-event.py). A prompt that
// mentions [ABC-123] links its conversation to that ticket.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export type Status = "running" | "your-turn" | "needs-you";

export interface Conversation {
  sessionId: string;
  tickets: Set<string>;
  title: string;
  status: Status;
  updated: number; // epoch seconds
  cwd: string;
  // Started by claude -p without CLAUDE_CODE_ENTRYPOINT=claude-vscode: the IDE panel can't open these.
  headless: boolean;
}

interface HookEvent {
  ts: number;
  event: "UserPromptSubmit" | "Stop" | "Notification";
  session_id: string;
  cwd?: string;
  entrypoint?: string;
  prompt?: string;
}

const TAG = /\[([A-Z][A-Z0-9_]+-\d+)\]/g;

export const logPath = path.join(
  process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"),
  "command-center",
  "events.jsonl",
);

export function ticketTag(keys: string[]): string {
  return keys.map((k) => `[${k}]`).join("");
}

export function load(): Map<string, Conversation> {
  const conversations = new Map<string, Conversation>();
  let text: string;
  try {
    text = fs.readFileSync(logPath, "utf8");
  } catch {
    return conversations;
  }
  for (const line of text.split("\n")) {
    let e: HookEvent;
    try {
      e = JSON.parse(line);
    } catch {
      continue;
    }
    if (!e.session_id) continue;
    let c = conversations.get(e.session_id);
    if (!c) {
      c = {
        sessionId: e.session_id,
        tickets: new Set(),
        title: "",
        status: "your-turn",
        updated: e.ts,
        cwd: e.cwd ?? "",
        headless: e.entrypoint?.startsWith("sdk-") ?? false,
      };
      conversations.set(e.session_id, c);
    }
    c.updated = e.ts;
    if (e.event === "UserPromptSubmit") {
      c.status = "running";
      const prompt = e.prompt ?? "";
      for (const m of prompt.matchAll(TAG)) c.tickets.add(m[1]);
      if (!c.title) c.title = prompt.replace(TAG, "").trim().split("\n")[0].slice(0, 80);
    } else if (e.event === "Stop") {
      c.status = "your-turn";
    } else if (e.event === "Notification") {
      c.status = "needs-you";
    }
  }
  return conversations;
}
