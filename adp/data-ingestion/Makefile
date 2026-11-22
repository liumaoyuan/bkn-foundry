.PHONY: test test-cover lint ci

PYTHON ?= python3

test:
	$(PYTHON) -m unittest discover -s tests -v

test-cover:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(PYTHON) -m compileall -q app tests

ci: lint test
