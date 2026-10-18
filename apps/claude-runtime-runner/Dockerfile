FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY apps/claude-runtime-runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/claude-runtime-runner/runner.py ./
COPY apps/claude-runtime-runner/CLAUDE.md /config/CLAUDE.md
COPY agents/ /config/.claude/agents/
COPY skills/ /config/.claude/skills/
RUN useradd --uid 1000 --create-home agent
USER 1000:1000
ENV HOME=/session/home PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/runner.py"]
