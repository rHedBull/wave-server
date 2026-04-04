### ── Stage 1: Build Next.js dashboard ─────────────────────────────────
FROM node:20-alpine AS dashboard-build

WORKDIR /app/dashboard

COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci

COPY dashboard/ .

ENV NEXT_TELEMETRY_DISABLED=1

# Configure dashboard paths for reverse proxy deployment.
# basePath: Next.js serves from /wave/dashboard
# API URLs: relative so browser uses same origin (reverse proxy routes /wave/api/*)
ARG NEXT_PUBLIC_BASE_PATH=/wave/dashboard
ARG NEXT_PUBLIC_API_URL=/wave/api/v1
ARG NEXT_PUBLIC_API_BASE=/wave/api
ENV NEXT_PUBLIC_BASE_PATH=$NEXT_PUBLIC_BASE_PATH
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE

RUN npm run build

### ── Stage 2: Combined backend + dashboard ───────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies + Node.js (for pi CLI + dashboard) + gh CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc git curl supervisor \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install pi CLI globally
RUN npm install -g @mariozechner/pi-coding-agent

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy backend source and install
COPY pyproject.toml uv.lock main.py README.md ./
COPY wave_server/ ./wave_server/
RUN uv pip install --system .

# Copy dashboard build
COPY --from=dashboard-build /app/dashboard/.next ./dashboard/.next
COPY --from=dashboard-build /app/dashboard/node_modules ./dashboard/node_modules
COPY --from=dashboard-build /app/dashboard/package.json ./dashboard/package.json
COPY --from=dashboard-build /app/dashboard/public ./dashboard/public

# Copy agent prompt files
COPY agents/ ./agents/

# Create data directory
RUN mkdir -p /app/data

# Version: set at build time, read by wave_server.__init__
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Set default git identity for commits inside the container
RUN git config --global user.name "wave-bot" \
    && git config --global user.email "wave-bot@pi-legion"

# Supervisor config to run both processes
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Backend: 9718, Dashboard: 9719
EXPOSE 9718 9719

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
