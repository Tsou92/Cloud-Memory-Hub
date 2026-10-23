.PHONY: check format prepare-upstream check-runtime check-upstream check-web check-container

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

check-upstream: prepare-upstream
	uv run --locked python .local/upstream/hy-memory/memhub/scripts/fetch_embedding_model.py
	cd .local/upstream/hy-memory/memhub && uv tool run --from uv==0.12.5 uv run --no-project -- bash scripts/run-tests-isolated.sh core-pr -q

check-container:
	docker build --platform linux/amd64 --tag cloud-memory-hub:development .
	uv run --locked python -m scripts.smoke_container

check-web:
	docker build --platform linux/amd64 --target web-tests .
