# Every target runs from the repo root. Tests live in tests/, the package in
# backend/, so `--project backend` points uv at the environment while pytest
# resolves its rootdir from ./pytest.ini.

.PHONY: install test lint types check

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
