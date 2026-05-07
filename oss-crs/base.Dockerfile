# Codex CRS base image (prepare phase)
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git \
    rsync \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js (for Codex CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

ARG CODEX_CLI_VERSION=0.104.0

# Codex CLI (pinned to avoid breaking config schema changes)
RUN npm install -g @openai/codex@${CODEX_CLI_VERSION}

# Git config
RUN git config --global user.email "crs@oss-crs.dev" \
    && git config --global user.name "OSS-CRS Patcher" \
    && git config --global --add safe.directory '*'
