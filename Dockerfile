### ── Stage 1: Build Next.js dashboard ─────────────────────────────────
FROM node:20-alpine AS dashboard-build

WORKDIR /app/dashboard

COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci

COPY dashboard/ .

ENV NEXT_TELEMETRY_DISABLED=1
# Both processes run in the same container, so the dashboard
# uses the source-code defaults (http://localhost:9718/api/v1).
# Do NOT set NEXT_PUBLIC_API_* here — Next.js bakes them at build time.
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

# Set default git identity for commits inside the container
RUN git config --global user.name "wave-bot" \
    && git config --global user.email "wave-bot@pi-legion"

# Supervisor config to run both processes
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Backend: 9718, Dashboard: 9719
EXPOSE 9718 9719

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
