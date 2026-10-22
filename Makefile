.PHONY: check format

check:
	uv run --locked ruff format --check scripts tests
	uv run --locked ruff check scripts tests
	uv run --locked python -m unittest discover -s tests -v
	uv run --locked python -m scripts.check_repository
	uv run --locked python -m scripts.check_secrets
	git diff --check
	git diff --cached --check

format:
	uv run --locked ruff format scripts tests
