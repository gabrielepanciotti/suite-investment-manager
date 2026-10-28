# InvestmentManager

InvestmentManager is a FastAPI service for recording portfolio snapshots and evaluating how a new budget fits investment directives. It ingests manually read broker screenshots, retains position history, and produces allocation proposals; it never places trades or connects to a broker to execute an order.

## Features

- Append-only position snapshots from supported broker payloads.
- Data-driven instrument classifications and allocation targets.
- Portfolio allocation gaps and budget proposals.
- Explicit handling of unknown data rather than inferred values.

## Requirements

- Python 3.13
- PostgreSQL 16 for local development (SQLite is used by the test suite)
- The [Life framework](https://github.com/suite-life) checked out as a sibling directory, because `suite_core` and `suite_backend_core` are local framework packages
- Docker Compose is optional for the example container setup

## Quickstart

```bash
git clone <your-fork-url> investment-manager
git clone <life-framework-url> Life
cd investment-manager
cp .env.example .env
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ../Life/packages/shared ../Life/packages/backend-core
python -m pip install -r backend/requirements-dev.txt
docker compose -f deploy/docker-compose.example.yml up database -d
cd backend && alembic upgrade head
PYTHONPATH=. uvicorn investment_manager_backend.app:app --reload --port 8000
```

Alternatively, after setting `DB_PASSWORD` in `.env`, vendor the local framework package and start the generic local stack:

```bash
LIFE_FRAMEWORK_DIR=../Life bash deploy/prebuild.sh
docker compose -f deploy/docker-compose.example.yml up --build
```

## Tests

```bash
cd backend
python -m pytest
```

The tests use SQLite and do not require PostgreSQL. CI checks out the Life framework next to this repository and installs its shared Python packages before running pytest.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Backend database connection URL |
| `DB_PASSWORD` | Password used by the Compose database example |
| `DB_HOST_PORT` | Host port exposed by the Compose database example |
| `BACKEND_PORT` | Host port exposed by the Compose backend example |
| `LIFE_FRAMEWORK_DIR` | Optional path to the local Life framework for deployment helpers |

## License

MIT. See [LICENSE](LICENSE).
