.PHONY: help install check lint typecheck test api ui smoke-data publish image chart-lint chart-template tf-validate rules clean

PY ?= python
IMAGE ?= ghcr.io/silverkai13/football-availability-model
TAG ?= local
CHART := deploy/chart/availabilityiq
VERSION := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## Everything, including dev tools
	$(PY) -m pip install -e ".[dev]"

check: lint typecheck test  ## The quality gate CI runs

lint:  ## ruff check + format check
	$(PY) -m ruff check src tests app
	$(PY) -m ruff format --check src tests app

typecheck:  ## mypy, strict
	$(PY) -m mypy src tests

test:  ## The suite; no network, no corpus
	$(PY) -m pytest

api:  ## Serve the published predictions on :8000
	AIQ_ARTIFACT_DIR=artifacts/published AIQ_TEAMS_PATH=data/raw/teams.parquet aiq-api

ui:  ## The Streamlit squad page on :8501
	AIQ_ARTIFACT_DIR=artifacts/published streamlit run app/streamlit_app.py

smoke-data:  ## Synthetic artefacts that pass the contract (no real players)
	$(PY) -m aiq.cli smoke-data --out artifacts/smoke

publish:  ## Validate artifacts/predictions.parquet and install it for the API
	AIQ_VERSION=$(VERSION) $(PY) -m aiq.cli publish

image:  ## Build the container image
	docker build --build-arg AIQ_VERSION=$(VERSION) -t $(IMAGE):$(TAG) .

chart-lint:  ## helm lint + kubeconform over the rendered chart
	helm lint $(CHART) -f $(CHART)/values-ci.yaml
	helm template aiq $(CHART) -f $(CHART)/values-ci.yaml | kubeconform -strict -summary \
	  -schema-location default \
	  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

chart-template:  ## Render the chart with the CI values
	helm template aiq $(CHART) -f $(CHART)/values-ci.yaml

tf-validate:  ## terraform fmt/validate and tflint over the AWS footprint
	cd deploy/terraform/aws && terraform fmt -check -recursive && terraform init -backend=false -input=false > /dev/null && terraform validate && tflint --init > /dev/null && tflint

rules:  ## Validate the Prometheus alert rules
	promtool check rules deploy/monitoring/alerts.yaml
	promtool test rules deploy/monitoring/alerts_test.yaml

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist src/*.egg-info artifacts/smoke
