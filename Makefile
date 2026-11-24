.PHONY: help install sample-data train-local predict-local serve-local test lint test-apps-script docker-build docker-run

PY ?= python
IMAGE ?= sales-forecast-api

help:  ## show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install:  ## install runtime + dev dependencies
	$(PY) -m pip install -r requirements-dev.txt

sample-data:  ## generate 2 years of synthetic sheet-format data in data/sample_sales.csv
	$(PY) scripts/make_sample_data.py --days 730 --out data/sample_sales.csv

train-local: sample-data  ## train + evaluate on the CSV, no Supabase
	DATA_SOURCE=csv $(PY) -m ml.train --no-upload

predict-local:  ## batch forecast from the local model to forecasts.csv (dry run)
	DATA_SOURCE=csv MODEL_SOURCE=local $(PY) -m ml.predict_batch --dry-run --out forecasts.csv

serve-local:  ## run the API on :8000 with the local model + CSV history
	DATA_SOURCE=csv MODEL_SOURCE=local uvicorn api.main:app --reload --port 8000

test:  ## python test suite
	$(PY) -m pytest

test-apps-script:  ## Apps Script tests (needs node >= 18)
	node tests/apps_script/sync.test.js

lint:  ## ruff
	ruff check .

docker-build:  ## build the API image
	docker build -t $(IMAGE) .

docker-run:  ## run the image against Supabase using .env
	docker run --rm -p 8000:8000 --env-file .env -e MODEL_SOURCE=supabase $(IMAGE)
