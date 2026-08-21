# Every target runs from the repo root. Tests live in tests/, the package in
# backend/, so `--project backend` points uv at the environment while pytest
# resolves its rootdir from ./pytest.ini.

.PHONY: install test lint types check api

install:
	uv sync --project backend

test:
	uv run --project backend pytest -q

lint:
	uv run --project backend ruff check backend/src tests

types:
	cd backend && uv run mypy

# What CI runs, and what to run before a commit.
check: lint types test

# The demo backend. http://127.0.0.1:8000/docs lists every route.
api:
	uv run --project backend uvicorn yquo.api.main:app --reload --port 8000
