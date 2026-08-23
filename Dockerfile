# One image, one process, one URL.
#
# The backend serves the built frontend, so a deployment is a single container
# rather than two services that have to discover each other, and nothing is
# cross-origin.
#
# Nothing here needs a secret. Every external dependency is still mocked, so the
# running container makes no outbound calls at all.

# --- build the page ----------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build
# Copy manifests first so the dependency layer is cached across source edits.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# PUBLIC_API_BASE is deliberately unset: the production build defaults to the
# same origin, which is the whole point of serving both halves from one app.
RUN npm run build


# --- run it ------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# uv resolves and installs far faster than pip, and the lockfile is already uv's.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies before source, again for layer caching.
COPY backend/pyproject.toml backend/uv.lock backend/README.md* ./backend/
RUN uv sync --project backend --no-dev --frozen --no-install-project

COPY backend/ ./backend/
RUN uv sync --project backend --no-dev --frozen

COPY --from=frontend /build/dist ./frontend/dist
ENV YQUO_FRONTEND_DIST=/app/frontend/dist

# Demo pacing, matching api.ps1 and api.sh. One acceleration factor cannot serve
# both halves of the flow: canvassing wants to be quick enough to watch, the
# confirmation window wants to be slow enough to talk over. At 60x the real
# 30-minute window would be 30 seconds, expiring the order mid-explanation.
ENV YQUO_POLICY__CONFIRMATION_WINDOW_S=7200 \
    YQUO_POLICY__REMINDER_AFTER_S=3600

# Hosts inject the port they want listened on; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

# Unbuffered so container logs appear immediately rather than in bursts.
ENV PYTHONUNBUFFERED=1

# A single worker on purpose: every demo world lives in this process's memory,
# so a second worker would serve some requests from a world that does not have
# the visitor's order in it. Horizontal scaling waits for the Supabase swap
# (MIGRATION_PLAN.md §2), which is where that state stops being in-process.
CMD ["sh", "-c", "uv run --project backend --no-dev uvicorn yquo.api.main:app --host 0.0.0.0 --port ${PORT}"]
