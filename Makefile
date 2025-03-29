format: .venv
	uv run ruff format src/
	uv run ruff check --select I --fix src/

lint: .venv
	uv run ruff check
	uv run ruff format --check

.venv: pyproject.toml Makefile
	test -e .venv || uv venv --system-site-packages .venv
	uv pip install --editable .[dev]
	touch .venv

.PHONY: lint format
