# code-server and the Claude Code extension on top of claude-base.
# BASE_IMAGE defaults to the local image; CI points it at GHCR.
ARG BASE_IMAGE=claude-base
ARG BASE_TAG
# Command Center extension (apps/command-center-extension), packaged as a .vsix.
FROM node:22-bookworm-slim AS command-center
WORKDIR /src
COPY apps/command-center-extension/package.json apps/command-center-extension/package-lock.json ./
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY apps/command-center-extension/ ./
RUN npm run package

FROM ${BASE_IMAGE}:${BASE_TAG}
USER root
# Remove the built-in Copilot Chat extension; Claude Code is the only assistant.
RUN curl -fsSL https://code-server.dev/install.sh | sh -s -- --version 4.141.0 \
    && rm -rf /usr/lib/code-server/lib/vscode/extensions/copilot
USER coder
COPY --from=command-center /src/command-center.vsix /tmp/command-center.vsix
# Defaults go in Machine settings: User settings are on the user's PVC and override
# them. Hide VS Code's own chat/AI UI by default.
RUN code-server --install-extension anthropic.claude-code \
    && code-server --install-extension /tmp/command-center.vsix \
    && code-server --install-extension GitHub.vscode-pull-request-github \
    && mkdir -p /home/coder/.local/share/code-server/Machine \
    && printf '%s\n' '{' \
       '    "chat.disableAIFeatures": true,' \
       '    "claudeCode.preferredLocation": "panel",' \
       '    "workbench.startupEditor": "none"' \
       '}' > /home/coder/.local/share/code-server/Machine/settings.json \
    # The browser encrypts sign-ins (e.g. GitHub) with this server key half; keep it on
    # the user's PVC so sign-ins survive redeploys. code-server creates it if missing.
    && ln -s /home/coder/.local/share/code-server/User/serve-web-key-half \
       /home/coder/.local/share/code-server/serve-web-key-half
EXPOSE 8080
ENTRYPOINT ["dumb-init", "code-server"]
