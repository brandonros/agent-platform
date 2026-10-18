# code-server and the Claude Code extension on top of claude-base.
ARG BASE_TAG
FROM claude-base:${BASE_TAG}
USER root
# Remove the built-in Copilot Chat extension; Claude Code is the only assistant.
RUN curl -fsSL https://code-server.dev/install.sh | sh -s -- --version 4.141.0 \
    && rm -rf /usr/lib/code-server/lib/vscode/extensions/copilot
USER coder
# Hide VS Code's own chat/AI UI by default.
RUN code-server --install-extension anthropic.claude-code \
    && mkdir -p /home/coder/.local/share/code-server/User \
    && printf '%s\n' '{' \
       '    "chat.disableAIFeatures": true,' \
       '    "claudeCode.preferredLocation": "panel",' \
       '    "workbench.startupEditor": "none"' \
       '}' > /home/coder/.local/share/code-server/User/settings.json
EXPOSE 8080
ENTRYPOINT ["dumb-init", "code-server"]
