.PHONY: test test-cover lint ci

PYTHON ?= python3
TEST_ENV = APP_ENV=test ALLOW_SQLITE_DATABASE=true DATABASE_URL=sqlite+pysqlite:///./data/test-control.db?check_same_thread=false

test:
	$(TEST_ENV) $(PYTHON) -m unittest discover -s tests -v

test-cover:
	$(TEST_ENV) $(PYTHON) -m unittest discover -s tests -v

lint:
	$(PYTHON) -m compileall -q app tests

ci: lint test
