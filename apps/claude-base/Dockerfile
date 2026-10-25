# Claude Code plus org-wide configuration. Jobs run this image directly;
# code-server builds on it.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates python3 dumb-init gh \
    && rm -rf /var/lib/apt/lists/*
# git push/pull to GitHub authenticate with GH_TOKEN through gh.
RUN git config --system credential.https://github.com.helper '!gh auth git-credential'
# Same user and home as code-server, so session folders match (-home-coder-workspace).
RUN useradd --uid 1000 --create-home --shell /bin/bash coder
# Org-wide Claude Code config, outside ~/.claude: managed settings enable the
# local "org" plugin (agents, skills, MCP); managed CLAUDE.md applies to everyone.
COPY apps/claude-base/managed-settings.json apps/claude-base/CLAUDE.md /etc/claude-code/
COPY apps/claude-base/org/ /opt/claude-org/
COPY agents/ /opt/claude-org/plugins/org/agents/
COPY skills/ /opt/claude-org/plugins/org/skills/
USER coder
WORKDIR /home/coder/workspace
ENV PATH=/home/coder/.local/bin:$PATH
RUN curl -fsSL https://claude.ai/install.sh | bash
