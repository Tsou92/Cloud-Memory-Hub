.PHONY: check format prepare-upstream check-runtime check-container

check:
	uv run --locked ruff format --check scripts tests apps/hub/src apps/hub/tests
	uv run --locked ruff check scripts tests apps/hub/src apps/hub/tests
	uv run --locked python -m unittest discover -s tests -v
	uv run --locked python -m scripts.check_repository
	uv run --locked python -m scripts.check_secrets
	git diff --check
	git diff --cached --check

format:
	uv run --locked ruff format scripts tests apps/hub/src apps/hub/tests

prepare-upstream:
	uv run --locked python -m scripts.prepare_upstream

check-runtime: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub pytest -q

check-container:
	docker build --platform linux/amd64 --tag cloud-memory-hub:development .
	uv run --locked python -m scripts.smoke_container
