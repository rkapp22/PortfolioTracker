# =============================================================================
# Convenience commands. Run `make help` to list them.
# (Windows users without `make`: the raw docker commands are in the README.)
# =============================================================================
.PHONY: help up down logs build ingest transform pipeline psql shell reset

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	 awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up:             ## Build (if needed) and start db + app in the background
	docker compose up -d --build

down:           ## Stop containers (DB data is kept)
	docker compose down

reset:          ## Stop AND wipe the database volume (full clean slate)
	docker compose down -v

logs:           ## Tail logs from both services
	docker compose logs -f

build:          ## Rebuild the app image (after changing requirements.txt)
	docker compose build app

ingest:         ## Run only the ingest step (APIs + Excel -> staging)
	docker compose exec app python src/ingest.py

transform:      ## Run only the transform step (staging -> dwh)
	docker compose exec app python src/transform.py

pipeline:       ## Run the FULL pipeline (ingest then transform)
	docker compose exec app python src/run_pipeline.py

psql:           ## Open a psql shell inside the database
	docker compose exec db psql -U $${POSTGRES_USER:-portfolio} -d $${POSTGRES_DB:-portfolio}

shell:          ## Open a bash shell inside the app container
	docker compose exec app bash
