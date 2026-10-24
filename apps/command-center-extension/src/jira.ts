// Jira search: Cloud (REST v3, email + API token), Data Center (REST v2,
// personal access token), or demo tickets when no base URL is configured.

export interface Ticket {
  key: string;
  summary: string;
  status: string;
  url: string;
}

export interface JiraConfig {
  baseUrl: string;
  userEmail: string;
}

const DEMO: Omit<Ticket, "url">[] = [
  {key: "DEMO-101", summary: "Add retry with backoff to payment webhook consumer", status: "To Do"},
  {key: "DEMO-102", summary: "Fix null pointer in invoice PDF export", status: "In Progress"},
  {key: "DEMO-103", summary: "Expose account balance on /v2/accounts", status: "In Review"},
  {key: "DEMO-104", summary: "Upgrade logging library and remove deprecated appenders", status: "To Do"},
];

export async function search(config: JiraConfig, jql: string): Promise<Ticket[]> {
  const base = config.baseUrl.replace(/\/+$/, "");
  if (!base) {
    return DEMO.map((t) => ({...t, url: `https://jira.example.com/browse/${t.key}`}));
  }
  // ${userEmail} is the IDE's user, so a shared service-account token still sees "my" tickets.
  jql = jql.replaceAll("${userEmail}", config.userEmail ? JSON.stringify(config.userEmail) : "currentUser()");
  const fields = ["summary", "status"];
  const cloud = base.includes(".atlassian.net");
  const res = cloud
    ? await fetch(`${base}/rest/api/3/search/jql`, {
        method: "POST",
        headers: {...auth(), "Content-Type": "application/json", Accept: "application/json"},
        body: JSON.stringify({jql, fields, maxResults: 100}),
      })
    : await fetch(`${base}/rest/api/2/search?${new URLSearchParams({jql, fields: fields.join(","), maxResults: "100"})}`, {
        headers: {...auth(), Accept: "application/json"},
      });
  if (!res.ok) {
    throw new Error(`Jira ${res.status}: ${(await res.text()).slice(0, 300)}`);
  }
  const body = (await res.json()) as {issues: {key: string; fields: {summary: string; status?: {name: string}}}[]};
  return body.issues.map((i) => ({
    key: i.key,
    summary: i.fields.summary,
    status: i.fields.status?.name ?? "",
    url: `${base}/browse/${i.key}`,
  }));
}

function auth(): Record<string, string> {
  const {JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PAT} = process.env;
  if (JIRA_PAT) return {Authorization: `Bearer ${JIRA_PAT}`};
  if (JIRA_EMAIL && JIRA_API_TOKEN) {
    return {Authorization: `Basic ${Buffer.from(`${JIRA_EMAIL}:${JIRA_API_TOKEN}`).toString("base64")}`};
  }
  throw new Error("Set JIRA_EMAIL and JIRA_API_TOKEN (Cloud) or JIRA_PAT (Data Center)");
}
