FROM python:3.12-slim
WORKDIR /app
COPY apps/claude-runtime-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/claude-runtime-server/server.py apps/claude-runtime-server/jobs.py ./
RUN useradd --uid 1000 --create-home agent && mkdir /data && chown agent /data
USER 1000:1000
ENV HOME=/tmp PYTHONUNBUFFERED=1 AGENT_CONFIG_DIR=/config
EXPOSE 8000
CMD ["uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
