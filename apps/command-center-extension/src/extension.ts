// Command Center: JQL presets -> tickets -> linked Claude Code conversations.
// Opens conversations through the Claude Code extension's primaryEditor.open
// command, which its vscode://anthropic.claude-code/open?session=&prompt= URI
// handler also uses.

import * as fs from "node:fs";
import * as vscode from "vscode";
import * as conversations from "./conversations";
import * as jira from "./jira";
import * as workspaces from "./workspaces";

type Preset = {kind: "preset"; name: string; jql: string};
type TicketNode = {kind: "ticket"; preset: string; ticket: jira.Ticket};
type ConversationNode = {kind: "conversation"; parent: string /* ticket item id */; conversation: conversations.Conversation};
type ErrorNode = {kind: "error"; message: string};
type Node = Preset | TicketNode | ConversationNode | ErrorNode;

const OPEN = "claude-vscode.primaryEditor.open";

class Tree implements vscode.TreeDataProvider<Node> {
  private changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private tickets = new Map<string, Promise<jira.Ticket[]>>();
  conversations = conversations.load();

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
      return this.linked(node.ticket.key).map((conversation) => ({kind: "conversation", parent: `${node.preset}:${node.ticket.key}`, conversation}));
    }
    return [];
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
          linked.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None,
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
        item.command = {command: "commandCenter.openConversation", title: "Open", arguments: [node]};
        return item;
      }
      case "error": {
        const item = new vscode.TreeItem(node.message);
        item.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("errorForeground"));
        return item;
      }
    }
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const tree = new Tree();
  const view = vscode.window.createTreeView("commandCenter.tickets", {treeDataProvider: tree, canSelectMany: true});
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
    {dispose: () => fs.unwatchFile(conversations.logPath, onLog)},
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("commandCenter")) tree.refresh();
    }),
    vscode.commands.registerCommand("commandCenter.refresh", () => tree.refresh()),
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
