FROM node:22-bookworm-slim
ARG PI_VERSION=1.4.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 git ca-certificates ripgrep \
    && npm install -g --ignore-scripts @earendil-works/pi-coding-agent@${PI_VERSION} \
    && mkdir -p /home/agent && chmod 1777 /home/agent
WORKDIR /app
COPY bin/ bin/
COPY prompts/ prompts/
COPY migrations/ migrations/
ENV HOME=/home/agent PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1000:1000
CMD ["python3", "bin/worker.py"]
