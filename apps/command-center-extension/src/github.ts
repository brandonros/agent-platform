// GitHub pull requests from search presets (GraphQL, GH_TOKEN). A PR links to
// every ticket key in its title, branch, or description, so one ticket can have
// PRs in several repos and one PR can cover several tickets.

export interface PullRequest {
  repo: string; // owner/name
  number: number;
  title: string;
  url: string;
  state: "OPEN" | "CLOSED" | "MERGED";
  draft: boolean;
  ci?: "SUCCESS" | "FAILURE" | "ERROR" | "PENDING" | "EXPECTED";
  review?: "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED";
  tickets: Set<string>;
}

const KEY = /\b([A-Z][A-Z0-9_]+-\d+)\b/g;

const QUERY = `query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        number title url state isDraft headRefName body reviewDecision
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}`;

// ${githubUser} is the IDE's user, so a shared service-account token still finds "my" PRs.
export async function search(query: string, githubUser: string): Promise<PullRequest[]> {
  const token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
  if (!token) throw new Error("Set GH_TOKEN to list pull requests");
  const q = query.replaceAll("${githubUser}", githubUser || "@me");
  const res = await fetch("https://api.github.com/graphql", {
    method: "POST",
    headers: {Authorization: `bearer ${token}`, "Content-Type": "application/json"},
    body: JSON.stringify({query: QUERY, variables: {q}}),
  });
  const body = (await res.json()) as {data?: {search: {nodes: any[]}}; errors?: {message: string}[]; message?: string};
  if (!res.ok || body.errors) {
    throw new Error(`GitHub ${res.status}: ${body.errors?.map((e) => e.message).join("; ") ?? body.message}`);
  }
  return body.data!.search.nodes
    .filter((n) => n.number)
    .map((n) => ({
      repo: n.repository.nameWithOwner,
      number: n.number,
      title: n.title,
      url: n.url,
      state: n.state,
      draft: n.isDraft,
      ci: n.commits.nodes[0]?.commit.statusCheckRollup?.state,
      review: n.reviewDecision ?? undefined,
      tickets: new Set([n.title, n.headRefName, n.body ?? ""].flatMap((s: string) => [...s.matchAll(KEY)].map((m) => m[1]))),
    }));
}

// Groups PRs by repo, repos and PRs in a stable order.
export function byRepo(prs: PullRequest[]): [string, PullRequest[]][] {
  const groups = new Map<string, PullRequest[]>();
  for (const pr of prs) groups.set(pr.repo, [...(groups.get(pr.repo) ?? []), pr]);
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([repo, list]) => [repo, list.sort((a, b) => b.number - a.number)]);
}
