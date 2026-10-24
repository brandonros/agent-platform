// Ticket workspaces: one folder per ticket under the workspace root, holding the
// repos Claude clones for it. .command-center.json lists the folder's tickets so
// a folder can later hold a group of tickets.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";

const MANIFEST = ".command-center.json";
const PENDING = "commandCenter.pending";

// Done after the window has switched to `folder`.
export type Pending = {folder: string; sessionId?: string; prompt?: string; title: string};

export function root(): string {
  const configured = vscode.workspace.getConfiguration("commandCenter").get("workspaceRoot", "");
  return configured.replace(/^~(?=$|\/)/, os.homedir()) || path.join(os.homedir(), "workspace");
}

export function current(): string | undefined {
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  return folder && path.resolve(folder);
}

// Creates the ticket's folder (named after the first key) and adds the keys to its manifest.
export function ensure(keys: string[]): string {
  const folder = path.join(root(), keys[0]);
  fs.mkdirSync(folder, {recursive: true});
  const file = path.join(folder, MANIFEST);
  let tickets: string[] = [];
  try {
    tickets = JSON.parse(fs.readFileSync(file, "utf8")).tickets ?? [];
  } catch {}
  const merged = [...new Set([...tickets, ...keys])];
  if (merged.length !== tickets.length) fs.writeFileSync(file, JSON.stringify({tickets: merged}, null, 2) + "\n");
  return folder;
}

// Runs `open` now if the window already shows the folder; otherwise switches the
// window to it (a reload) and leaves the action for resume() after activation.
export async function inFolder(context: vscode.ExtensionContext, pending: Pending, open: (p: Pending) => unknown) {
  if (!fs.existsSync(pending.folder) || current() === path.resolve(pending.folder)) return open(pending);
  await context.globalState.update(PENDING, {...pending, at: Date.now()});
  await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(pending.folder));
}

export async function resume(context: vscode.ExtensionContext, open: (p: Pending) => unknown) {
  const pending = context.globalState.get<Pending & {at: number}>(PENDING);
  if (!pending || current() !== path.resolve(pending.folder)) return;
  await context.globalState.update(PENDING, undefined);
  if (Date.now() - pending.at < 60_000) open(pending);
}
