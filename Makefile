# Kith — developer tasks. Everything the container does is reproducible from here.
#
#   make venv     local dev/test environment (throwaway; the app never needs it)
#   make lock     regenerate the hash-locked requirements.txt
#   make test     run the test suite
#   make run      run the app locally without a container
#   make build    build the image with Podman
#   make health   check a running instance
IMAGE ?= localhost/kith:0.11.0
VENV  ?= .venv
PY    ?= $(VENV)/bin/python

.PHONY: help venv lock lock-dev test run build health shell clean

help:
	@sed -n 's/^#   //p' Makefile

venv:
	uv venv --python 3.13 $(VENV)
	uv pip install --python $(PY) -r requirements-dev.txt

lock:
	uv pip compile --python-version 3.13 --generate-hashes --no-header \
		-o requirements.txt requirements.in
	uv pip compile --python-version 3.13 --generate-hashes --no-header \
		-o requirements-dev.txt requirements-dev.in

test:
	$(PY) -m pytest -q

run:
	KITH_DATA_DIR=$(CURDIR)/data KITH_HOST=127.0.0.1 $(PY) -m uvicorn app.main:app \
		--host 127.0.0.1 --port 8080 --reload

# --format docker matters: Podman 5 defaults to OCI image format, in which the
# Containerfile's HEALTHCHECK is silently ignored (found on the host, 2026-10-01).
build:
	podman build --format docker -t $(IMAGE) -f Containerfile .

health:
	curl -fsS http://127.0.0.1:8080/api/health | python3 -m json.tool

shell:
	podman exec -it kith /bin/sh

clean:
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
