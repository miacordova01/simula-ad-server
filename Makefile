.PHONY: help up down logs test lint typecheck check demo seed clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

up:         ## build and start the full stack
	docker compose up -d --build
	@echo "api:  http://localhost:8080/docs"

down:       ## stop everything
	docker compose down

clean:      ## stop and remove volumes
	docker compose down -v

logs:       ## tail api + worker
	docker compose logs -f api worker

test:       ## run the test suite (no containers needed)
	.venv/bin/python -m pytest -q

lint:       ## ruff
	.venv/bin/ruff check app tests scripts

typecheck:  ## mypy
	.venv/bin/mypy app

check: lint typecheck test  ## everything

demo:       ## walk the API end to end and write samples/
	.venv/bin/python scripts/demo.py

escaping:   ## render hostile payloads and prove the JS still parses
	.venv/bin/python scripts/adversarial_render.py
	node scripts/validate_rendered_ad.js /tmp/adv/*.html
