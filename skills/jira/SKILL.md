---
name: jira
description: Read Jira tickets (description, acceptance criteria, comments, links) and comment on them. Use when a prompt mentions a ticket key like [ABC-123] or asks to read, check, or update a Jira ticket.
---

# Jira

Credentials are in the environment: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`.
Never print them. If they are unset, say Jira is not configured and stop.

Read a ticket (REST v2 returns the description and comments as plain text):

```bash
curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "$JIRA_BASE_URL/rest/api/2/issue/ABC-123?fields=summary,status,issuetype,priority,assignee,description,comment,issuelinks,subtasks,parent"
```

Search:

```bash
curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" -H 'Content-Type: application/json' \
  -X POST "$JIRA_BASE_URL/rest/api/3/search/jql" \
  -d '{"jql": "project = ABC AND statusCategory != Done", "fields": ["summary", "status"], "maxResults": 50}'
```

Comment, only when the user asks you to:

```bash
curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" -H 'Content-Type: application/json' \
  -X POST "$JIRA_BASE_URL/rest/api/2/issue/ABC-123/comment" \
  -d '{"body": "..."}'
```

Do not transition, assign, or edit tickets unless the user asks.
