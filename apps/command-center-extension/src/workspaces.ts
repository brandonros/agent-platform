// Ticket workspaces: one folder per ticket under the workspace root, holding the
// repos Claude clones for it. .command-center.json lists the folder's tickets so
// a folder can later hold a group of tickets.

import {execFile} from "node:child_process";
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

export function folderFor(key: string): string {
  return path.join(root(), key);
}

// Creates the ticket's folder (named after the first key) and adds the keys to its manifest.
export function ensure(keys: string[]): string {
  const folder = folderFor(keys[0]);
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
  // The reload resets the sidebar; bring the user back to the tree.
  await vscode.commands.executeCommand("workbench.view.extension.commandCenter");
  if (Date.now() - pending.at < 60_000) open(pending);
}

// Git repos in the folder (itself or one level down) with uncommitted changes or
// commits not on any remote, e.g. "api: 3 uncommitted, 1 unpushed".
export async function unsavedWork(folder: string): Promise<string[]> {
  const candidates = [folder, ...fs.readdirSync(folder).map((d) => path.join(folder, d))];
  const repos = candidates.filter((d) => fs.existsSync(path.join(d, ".git")));
  const report: string[] = [];
  for (const repo of repos) {
    const name = path.relative(folder, repo) || ".";
    const status = await git(repo, "status", "--porcelain");
    const log = await git(repo, "log", "--branches", "--not", "--remotes", "--oneline");
    if (status === undefined || log === undefined) {
      report.push(`${name}: couldn't check with git`);
      continue;
    }
    const changed = lines(status);
    const unpushed = lines(log);
    const parts = [changed && `${changed} uncommitted`, unpushed && `${unpushed} unpushed`].filter(Boolean);
    if (parts.length) report.push(`${name}: ${parts.join(", ")}`);
  }
  return report;
}

// Undefined when git fails, so a failed check never reads as "clean".
function git(cwd: string, ...args: string[]): Promise<string | undefined> {
  return new Promise((resolve) => execFile("git", args, {cwd}, (err, out) => resolve(err ? undefined : out)));
}

function lines(out: string): number {
  return out.split("\n").filter(Boolean).length;
}
