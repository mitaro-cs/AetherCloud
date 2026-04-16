PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: help venv install dev test run docker-up docker-down clean

help:
	@printf "Targets:\n"
	@printf "  make venv        Create virtualenv\n"
	@printf "  make install     Install dependencies\n"
	@printf "  make dev         Run local dev server\n"
	@printf "  make test        Run unit tests\n"
	@printf "  make run         Run production server with gunicorn\n"
	@printf "  make docker-up   Start docker compose stack\n"
	@printf "  make docker-down Stop docker compose stack\n"
	@printf "  make clean       Remove local caches and venv\n"

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install -r requirements.txt

dev: install
	$(PY) AetherCloud.py

test: install
	$(PY) -m unittest discover -s tests -p "test_*.py" -v

run: install
	$(VENV)/bin/gunicorn --workers=2 --threads=4 --timeout=120 --bind=0.0.0.0:5000 wsgi:app

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

clean:
	rm -rf $(VENV) __pycache__ aethercloud/__pycache__ tests/__pycache__
