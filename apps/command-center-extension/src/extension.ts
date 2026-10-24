// Command Center: JQL presets -> tickets -> linked Claude Code conversations and
// pull requests (by repo); GitHub search presets -> repos -> pull requests.
// Opens conversations through the Claude Code extension's primaryEditor.open
// command, which its vscode://anthropic.claude-code/open?session=&prompt= URI
// handler also uses.

import * as fs from "node:fs";
import * as vscode from "vscode";
import * as conversations from "./conversations";
import * as github from "./github";
import * as jira from "./jira";
import * as workspaces from "./workspaces";

type Preset = {kind: "preset"; name: string; jql: string};
type TicketNode = {kind: "ticket"; preset: string; ticket: jira.Ticket};
type ConversationNode = {kind: "conversation"; parent: string /* ticket item id */; conversation: conversations.Conversation};
type ErrorNode = {kind: "error"; message: string};
type PrPresetNode = {kind: "prPreset"; name: string; query: string};
type RepoNode = {kind: "repo"; parent: string /* parent item id */; repo: string; prs: github.PullRequest[]};
type PrNode = {kind: "pr"; parent: string; pr: github.PullRequest};
type Node = Preset | TicketNode | ConversationNode | ErrorNode | PrPresetNode | RepoNode | PrNode;

const OPEN = "claude-vscode.primaryEditor.open";
const PR_EXTENSION = "github.vscode-pull-request-github";

// Pull requests per search preset, shared by both trees. Tickets show the PRs
// found by any preset that mention their key.
class PullRequests {
  private changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;
  private results = new Map<string, Promise<github.PullRequest[]>>();
  private loaded = new Map<string, github.PullRequest[]>();

  presets(): {name: string; query: string}[] {
    return settings().get<{name: string; query: string}[]>("prPresets", []);
  }

  get(preset: {name: string; query: string}): Promise<github.PullRequest[]> {
    let result = this.results.get(preset.name);
    if (!result) {
      result = github.search(preset.query, githubUser());
      this.results.set(preset.name, result);
      result.then(
        (prs) => {
          this.loaded.set(preset.name, prs);
          this.changed.fire();
        },
        () => this.results.delete(preset.name),
      );
    }
    return result;
  }

  load(): void {
    for (const preset of this.presets()) this.get(preset).catch(() => {});
  }

  refresh(): void {
    this.results.clear();
    this.loaded.clear();
    this.load();
  }

  linked(key: string): github.PullRequest[] {
    const unique = new Map([...this.loaded.values()].flat().map((pr) => [pr.url, pr]));
    return [...unique.values()].filter((pr) => pr.tickets.has(key));
  }
}

class Tree implements vscode.TreeDataProvider<Node> {
  private changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private tickets = new Map<string, Promise<jira.Ticket[]>>();
  conversations = conversations.load();

  constructor(private prs: PullRequests) {
    prs.onDidChange(() => this.changed.fire());
  }

  refresh(): void {
    this.tickets.clear();
    this.reloadConversations();
  }

  reloadConversations(): void {
    this.conversations = conversations.load();
    this.changed.fire();
  }

  linked(key: string): conversations.Conversation[] {
    return [...this.conversations.values()]
      .filter((c) => c.tickets.has(key))
      .sort((a, b) => b.updated - a.updated);
  }

  async getChildren(node?: Node): Promise<Node[]> {
    if (!node) {
      return settings().get<{name: string; jql: string}[]>("jqlPresets", []).map((p) => ({kind: "preset", ...p}));
    }
    if (node.kind === "preset") {
      if (!this.tickets.has(node.name)) this.tickets.set(node.name, jira.search(jiraConfig(), node.jql));
      try {
        const tickets = await this.tickets.get(node.name)!;
        return tickets.map((ticket) => ({kind: "ticket", preset: node.name, ticket}));
      } catch (err) {
        this.tickets.delete(node.name);
        return [{kind: "error", message: String((err as Error).message ?? err)}];
      }
    }
    if (node.kind === "ticket") {
      const parent = `${node.preset}:${node.ticket.key}`;
      return [
        ...this.linked(node.ticket.key).map((conversation): Node => ({kind: "conversation", parent, conversation})),
        ...github.byRepo(this.prs.linked(node.ticket.key)).map(([repo, prs]): Node => ({kind: "repo", parent, repo, prs})),
      ];
    }
    return children(node);
  }

  getTreeItem(node: Node): vscode.TreeItem {
    switch (node.kind) {
      case "preset": {
        const item = new vscode.TreeItem(node.name, vscode.TreeItemCollapsibleState.Expanded);
        item.id = `preset:${node.name}`;
        item.tooltip = node.jql;
        item.iconPath = new vscode.ThemeIcon("filter");
        return item;
      }
      case "ticket": {
        const {key, summary, status} = node.ticket;
        const linked = this.linked(key);
        const item = new vscode.TreeItem(
          key,
          linked.length || this.prs.linked(key).length
            ? vscode.TreeItemCollapsibleState.Expanded
            : vscode.TreeItemCollapsibleState.None,
        );
        item.id = `ticket:${node.preset}:${key}`;
        item.description = summary;
        item.tooltip = `${key} · ${status}\n${summary}`;
        item.contextValue = "ticket";
        item.iconPath = linked.some((c) => c.status === "needs-you")
          ? new vscode.ThemeIcon("bell-dot", new vscode.ThemeColor("charts.yellow"))
          : new vscode.ThemeIcon("issues");
        return item;
      }
      case "conversation": {
        const c = node.conversation;
        const item = new vscode.TreeItem(c.title || c.sessionId.slice(0, 8));
        item.id = `conversation:${node.parent}:${c.sessionId}`;
        item.description = `${label(c.status)}${c.headless ? " · headless" : ""} · ${age(c.updated)}`;
        item.tooltip = `${c.sessionId}\nTickets: ${[...c.tickets].join(", ")}`;
        item.iconPath = icon(c.status);
        item.contextValue = "conversation";
        item.command = {command: "commandCenter.openConversation", title: "Open", arguments: [node]};
        return item;
      }
      default:
        return item(node);
    }
  }
}

class PrTree implements vscode.TreeDataProvider<Node> {
  private changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;

  constructor(private prs: PullRequests) {
    prs.onDidChange(() => this.changed.fire());
  }

  async getChildren(node?: Node): Promise<Node[]> {
    if (!node) return this.prs.presets().map((p) => ({kind: "prPreset", ...p}));
    if (node.kind === "prPreset") {
      try {
        const prs = await this.prs.get(node);
        return github.byRepo(prs).map(([repo, list]) => ({kind: "repo", parent: `prPreset:${node.name}`, repo, prs: list}));
      } catch (err) {
        return [{kind: "error", message: String((err as Error).message ?? err)}];
      }
    }
    return children(node);
  }

  getTreeItem(node: Node): vscode.TreeItem {
    return item(node);
  }
}

// Children and items shared by both trees.
function children(node: Node): Node[] {
  if (node.kind === "repo") return node.prs.map((pr) => ({kind: "pr", parent: `${node.parent}:${node.repo}`, pr}));
  return [];
}

function item(node: Node): vscode.TreeItem {
  switch (node.kind) {
    case "prPreset": {
      const item = new vscode.TreeItem(node.name, vscode.TreeItemCollapsibleState.Expanded);
      item.id = `prPreset:${node.name}`;
      item.tooltip = node.query;
      item.iconPath = new vscode.ThemeIcon("filter");
      return item;
    }
    case "repo": {
      const item = new vscode.TreeItem(node.repo, vscode.TreeItemCollapsibleState.Expanded);
      item.id = `repo:${node.parent}:${node.repo}`;
      item.description = `${node.prs.length} PR${node.prs.length === 1 ? "" : "s"}`;
      item.iconPath = new vscode.ThemeIcon("repo");
      return item;
    }
    case "pr": {
      const {pr} = node;
      const item = new vscode.TreeItem(`#${pr.number} ${pr.title}`);
      item.id = `pr:${node.parent}:${pr.number}`;
      item.description = prStatus(pr);
      item.tooltip = `${pr.repo}#${pr.number}\n${pr.title}\nTickets: ${[...pr.tickets].join(", ") || "none"}`;
      item.iconPath = prIcon(pr);
      item.contextValue = "pr";
      item.command = {command: "commandCenter.openPullRequest", title: "Open", arguments: [node]};
      return item;
    }
    case "error": {
      const item = new vscode.TreeItem(node.message);
      item.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("errorForeground"));
      return item;
    }
    default:
      throw new Error(`No shared item for ${node.kind}`);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const prs = new PullRequests();
  const tree = new Tree(prs);
  const view = vscode.window.createTreeView("commandCenter.tickets", {treeDataProvider: tree, canSelectMany: true});
  const prView = vscode.window.createTreeView("commandCenter.pullRequests", {treeDataProvider: new PrTree(prs)});
  prs.load();
  const updateBadge = () => {
    const n = [...tree.conversations.values()].filter((c) => c.status === "needs-you").length;
    view.badge = n ? {value: n, tooltip: `${n} conversation(s) need you`} : undefined;
  };
  updateBadge();

  // The hook log is outside the workspace, so poll it rather than use a workspace watcher.
  const onLog = () => {
    tree.reloadConversations();
    updateBadge();
  };
  fs.watchFile(conversations.logPath, {interval: 1500}, onLog);

  context.subscriptions.push(
    view,
    prView,
    {dispose: () => fs.unwatchFile(conversations.logPath, onLog)},
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("commandCenter")) {
        tree.refresh();
        prs.refresh();
      }
    }),
    vscode.commands.registerCommand("commandCenter.refresh", () => {
      tree.refresh();
      prs.refresh();
    }),
    // In the GitHub Pull Requests extension when it's installed, otherwise the browser.
    vscode.commands.registerCommand("commandCenter.openPullRequest", (node: PrNode) => {
      if (!vscode.extensions.getExtension(PR_EXTENSION)) return vscode.env.openExternal(vscode.Uri.parse(node.pr.url));
      return vscode.env.openExternal(
        vscode.Uri.from({
          scheme: vscode.env.uriScheme,
          authority: PR_EXTENSION,
          path: "/open-pull-request-webview",
          query: `uri=${encodeURIComponent(node.pr.url)}`,
        }),
      );
    }),
    vscode.commands.registerCommand("commandCenter.openPullRequestInBrowser", (node: PrNode) =>
      vscode.env.openExternal(vscode.Uri.parse(node.pr.url)),
    ),
    vscode.commands.registerCommand("commandCenter.openConversation", (node: ConversationNode) => {
      const c = node.conversation;
      if (c.headless && !useTerminal()) {
        return vscode.window.showWarningMessage(
          "This conversation was started with claude -p, which the Claude panel can't open. Run headless turns with CLAUDE_CODE_ENTRYPOINT=claude-vscode.",
        );
      }
      // Conversations belong to the folder they ran in; switch there first.
      const title = c.title || c.sessionId.slice(0, 8);
      return workspaces.inFolder(context, {folder: c.cwd || workspaces.root(), sessionId: c.sessionId, title}, openClaude);
    }),
    vscode.commands.registerCommand("commandCenter.openInJira", (node: TicketNode) =>
      vscode.env.openExternal(vscode.Uri.parse(node.ticket.url)),
    ),
    // Inline action on one ticket; with several tickets selected, one conversation covers them all.
    vscode.commands.registerCommand("commandCenter.newConversation", (node: TicketNode, selected?: Node[]) => {
      const picked = selected?.includes(node) ? selected.filter((n): n is TicketNode => n.kind === "ticket") : [node];
      const tickets = [...new Map(picked.map((n) => [n.ticket.key, n.ticket])).values()];
      // The first line (minus the tag) becomes the conversation's title in the tree.
      const prompt = [
        `${conversations.ticketTag(tickets.map((t) => t.key))} ${tickets.length === 1 ? tickets[0].summary : `${tickets.length} tickets`}`,
        "",
        ...tickets.map((t) => `- ${t.key}: ${t.summary}`),
        "",
        "Read the full ticket(s) in Jira (description, acceptance criteria, comments) and propose a plan before changing code.",
      ].join("\n");
      const keys = tickets.map((t) => t.key);
      return workspaces.inFolder(context, {folder: workspaces.ensure(keys), prompt, title: keys.join(", ")}, openClaude);
    }),
    vscode.commands.registerCommand("commandCenter.deleteConversation", async (node: ConversationNode) => {
      const c = node.conversation;
      const running = c.status === "running" ? " It is still running." : "";
      const ok = await vscode.window.showWarningMessage(
        `Delete conversation "${c.title || c.sessionId}"?${running}`,
        {modal: true, detail: "Its history is deleted and can't be resumed. Files it changed are kept."},
        "Delete",
      );
      if (ok !== "Delete") return;
      conversations.remove(c.sessionId);
      tree.reloadConversations();
      updateBadge();
    }),
    vscode.commands.registerCommand("commandCenter.deleteWorkspace", async (node: TicketNode) => {
      const folder = workspaces.folderFor(node.ticket.key);
      if (!fs.existsSync(folder)) {
        return vscode.window.showInformationMessage(`${node.ticket.key} has no workspace folder.`);
      }
      const open = workspaces.current();
      if (open && (open === folder || open.startsWith(folder + "/"))) {
        return vscode.window.showWarningMessage(`${folder} is open in this window. Switch to another folder before deleting it.`);
      }
      const unsaved = await workspaces.unsavedWork(folder);
      const ok = await vscode.window.showWarningMessage(
        `Delete ${folder}?`,
        {
          modal: true,
          detail: unsaved.length
            ? `These repos have work that isn't pushed and will be lost:\n${unsaved.join("\n")}`
            : "Every repo in it is committed and pushed. Its conversations are kept.",
        },
        "Delete",
      );
      if (ok !== "Delete") return;
      fs.rmSync(folder, {recursive: true, force: true});
      vscode.window.showInformationMessage(`Deleted ${folder}.`);
    }),
    vscode.commands.registerCommand("commandCenter.toggleTerminal", async () => {
      await settings().update("useTerminal", !useTerminal(), vscode.ConfigurationTarget.Global);
      vscode.window.showInformationMessage(`Claude conversations open in the ${useTerminal() ? "terminal" : "panel"}.`);
    }),
  );

  // Finish an open that switched the window to a ticket's folder.
  workspaces.resume(context, openClaude);
}

function useTerminal(): boolean {
  return settings().get("useTerminal", false);
}

// Opens a conversation (sessionId) or starts one (prompt) in the panel or terminal.
function openClaude({sessionId, prompt, title}: workspaces.Pending): unknown {
  if (!useTerminal()) return vscode.commands.executeCommand(OPEN, sessionId, prompt);
  const terminal = vscode.window.createTerminal({name: title});
  terminal.sendText(sessionId ? `claude --resume ${sessionId}` : `claude '${prompt!.replaceAll("'", `'\\''`)}'`);
  terminal.show();
}

export function deactivate(): void {}

function settings() {
  return vscode.workspace.getConfiguration("commandCenter");
}

function jiraConfig(): jira.JiraConfig {
  return {
    baseUrl: settings().get("jira.baseUrl", "") || process.env.JIRA_BASE_URL || "",
    userEmail: settings().get("userEmail", "") || process.env.COMMAND_CENTER_USER_EMAIL || "",
  };
}

function githubUser(): string {
  return settings().get("githubUser", "") || process.env.COMMAND_CENTER_GITHUB_USER || "";
}

function prStatus(pr: github.PullRequest): string {
  const ci = {SUCCESS: "✓ CI", FAILURE: "✗ CI", ERROR: "✗ CI", PENDING: "⋯ CI", EXPECTED: "⋯ CI"};
  const review = {APPROVED: "approved", CHANGES_REQUESTED: "changes requested", REVIEW_REQUIRED: "needs review"};
  const state = pr.state === "OPEN" ? (pr.draft ? "draft" : "open") : pr.state.toLowerCase();
  return [state, pr.ci && ci[pr.ci], pr.review && review[pr.review]].filter(Boolean).join(" · ");
}

function prIcon(pr: github.PullRequest): vscode.ThemeIcon {
  if (pr.state === "MERGED") return new vscode.ThemeIcon("git-merge", new vscode.ThemeColor("charts.purple"));
  if (pr.state === "CLOSED") return new vscode.ThemeIcon("git-pull-request-closed");
  const colors: Record<string, string> = {SUCCESS: "testing.iconPassed", FAILURE: "testing.iconFailed", ERROR: "testing.iconFailed"};
  const color = colors[pr.ci ?? ""];
  return new vscode.ThemeIcon(pr.draft ? "git-pull-request-draft" : "git-pull-request", color ? new vscode.ThemeColor(color) : undefined);
}

function label(s: conversations.Status): string {
  return {running: "running", "your-turn": "your turn", "needs-you": "needs you"}[s];
}

function icon(s: conversations.Status): vscode.ThemeIcon {
  switch (s) {
    case "running":
      return new vscode.ThemeIcon("sync~spin");
    case "needs-you":
      return new vscode.ThemeIcon("bell-dot", new vscode.ThemeColor("charts.yellow"));
    case "your-turn":
      return new vscode.ThemeIcon("comment-discussion");
  }
}

function age(epochSeconds: number): string {
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
