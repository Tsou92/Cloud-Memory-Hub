.PHONY: check format prepare-upstream check-runtime check-upstream check-web check-container check-claude check-claude-recall probe-sdk-governance probe-sdk-governance-links probe-sdk-reconciliation probe-productivity-read probe-original-archive probe-output-lineage

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

check-claude: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_claude_native.py

check-claude-recall: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_claude_recall.py

probe-sdk-governance: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_sdk_governance.py

probe-sdk-governance-links: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_sdk_governance_links.py

probe-sdk-reconciliation: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_sdk_reconciliation.py

probe-productivity-read: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_productivity_read.py

probe-original-archive: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_original_archive.py

probe-output-lineage: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_output_lineage.py

.PHONY: probe-input-context
probe-input-context: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_input_context.py

.PHONY: probe-reconcile-inputs
probe-reconcile-inputs: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_reconcile_inputs.py

.PHONY: probe-record-origins
probe-record-origins: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_record_origins.py

.PHONY: probe-reconcile-graph
probe-reconcile-graph: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_reconcile_graph.py

.PHONY: probe-chat-history
probe-chat-history: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_chat_history.py

.PHONY: probe-chat-graph
probe-chat-graph: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_chat_graph.py

.PHONY: probe-chat-reconcile
probe-chat-reconcile: prepare-upstream
	uv tool run --from uv==0.12.5 uv run --locked --directory apps/hub python ../../scripts/probe_chat_reconcile.py

check-upstream: prepare-upstream
	uv run --locked python .local/upstream/hy-memory/memhub/scripts/fetch_embedding_model.py
	cd .local/upstream/hy-memory/memhub && uv tool run --from uv==0.12.5 uv run --no-project -- bash scripts/run-tests-isolated.sh core-pr -q

check-container:
	docker build --platform linux/amd64 --tag cloud-memory-hub:development .
	uv run --locked python -m scripts.smoke_container

check-web:
	docker build --platform linux/amd64 --target web-tests .
