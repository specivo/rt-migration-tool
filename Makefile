# Development tasks for specivo-rt-migration. See README.md.

.PHONY: install test lint fixture-build fixture-build-args fixture-up fixture-seed fixture-files fixture-down fixture-reset fixture-export fixture-test

# The RT release (4.4.9, 5.0.11 or 6.0.4) and the database (pg or mysql).
RT_VERSION ?= 6.0.4
DB ?= pg

# What each release is built from: the commit its tag points at in RT's git
# repository, and Best Practical's image with its dependencies, by digest.
# RT 4.4.9's own Dockerfile names the 5.0.3 image; 5.0.11 needs newer modules
# than that one has, so it uses the image RT 6.0.4 names.
RT_BASE_OLD := bpssysadmin/rt-base-debian:RT-5.0.3-buster-20230130@sha256:1e14342e4e294a8c5530075ea2c6b4d776dd3d69cc4c23383891910c160deef3
RT_BASE_NEW := bpssysadmin/rt-base-debian:RT-6.0.3-trixie-20261029@sha256:c09fa5ab76210df137a2cea5ea7b9838ccce14ccea5ba96058a404df765609af
RT_COMMIT_4.4.9 := e973ba5a09ba811a4ca11ab2ddc945d69968cf59
RT_COMMIT_5.0.11 := 1de3247499616b2373e5cd64c851d2f34ce714d4
RT_COMMIT_6.0.4 := dd5c5ab2b7037173325fb96e9d15aeb897821e2c
RT_BASE_4.4.9 := $(RT_BASE_OLD)
RT_BASE_5.0.11 := $(RT_BASE_NEW)
RT_BASE_6.0.4 := $(RT_BASE_NEW)
RT_COMMIT ?= $(RT_COMMIT_$(RT_VERSION))
RT_BASE_IMAGE ?= $(RT_BASE_$(RT_VERSION))
FIXTURE_IMAGE := specivo-rt-fixture:$(RT_VERSION)
# Extra options for `docker build`, e.g. a proxy: --network host --build-arg https_proxy=...
FIXTURE_BUILD_OPTS ?=

# One fixture per checkout, RT version and database: the compose project is
# named after this directory's path, so two checkouts never share containers.
FIXTURE_PROJECT ?= rt-tool-fixture-$(shell printf '%s' "$(CURDIR)" | shasum | cut -c1-10)-$(subst .,,$(RT_VERSION))-$(DB)
FIXTURE_COMPOSE := RT_VERSION=$(RT_VERSION) docker compose -p $(FIXTURE_PROJECT) -f docker-compose.fixture.yml --profile $(DB)
FIXTURE_APP := rt-$(DB)
FIXTURE_FILES := .fixture-files/$(RT_VERSION)-$(DB)

RT_TOOL_FIXTURE_PG_PORT ?= 5462
RT_TOOL_FIXTURE_MYSQL_PORT ?= 3319
FIXTURE_DB_URL := $(if $(filter mysql,$(DB)),mysql://rt_user:rt_pass@127.0.0.1:$(RT_TOOL_FIXTURE_MYSQL_PORT)/rt,postgresql://rt_user:rt_pass@127.0.0.1:$(RT_TOOL_FIXTURE_PG_PORT)/rt)
FIXTURE_OUT ?= out/fixture-$(RT_VERSION)-$(DB)
FIXTURE_INSTANCE ?= fixture.example.com

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --strict src

fixture-build:  ## Build RT_VERSION's image from its git tag
	@test -n "$(RT_COMMIT)" || { echo "unknown RT_VERSION $(RT_VERSION): use 4.4.9, 5.0.11 or 6.0.4"; exit 2; }
	docker build $(FIXTURE_BUILD_OPTS) --build-arg RT_BASE_IMAGE=$(RT_BASE_IMAGE) --build-arg RT_VERSION=$(RT_VERSION) \
		--build-arg RT_COMMIT=$(RT_COMMIT) -t $(FIXTURE_IMAGE) fixture

fixture-build-args:  ## Print the build arguments of RT_VERSION's image (CI builds it with a cache)
	@echo "RT_BASE_IMAGE=$(RT_BASE_IMAGE)"
	@echo "RT_VERSION=$(RT_VERSION)"
	@echo "RT_COMMIT=$(RT_COMMIT)"

fixture-up:  ## Start RT_VERSION on DB (pg or mysql); builds nothing
	$(FIXTURE_COMPOSE) up -d --wait

fixture-seed:  ## Fill the fixture through RT's Perl API, then move large files to external storage
	$(FIXTURE_COMPOSE) exec -T $(FIXTURE_APP) perl /seed/seed.pl

fixture-files:  ## Copy RT's external storage out to .fixture-files/RT_VERSION-DB
	rm -rf $(FIXTURE_FILES)
	mkdir -p $(dir $(FIXTURE_FILES))
	$(FIXTURE_COMPOSE) cp $(FIXTURE_APP):/opt/rt/var/external-storage $(FIXTURE_FILES)

fixture-down:  ## Stop the fixture and discard its data and files
	$(FIXTURE_COMPOSE) down -v
	rm -rf $(FIXTURE_FILES)

fixture-reset: fixture-down fixture-up fixture-seed fixture-files  ## Recreate, reseed and copy out the files

fixture-export:  ## Write a bundle from the fixture to FIXTURE_OUT (a directory)
	rm -rf $(FIXTURE_OUT)
	uv run specivo-rt-migration --db-url "$(FIXTURE_DB_URL)" --external-storage-path $(FIXTURE_FILES) \
		--out $(FIXTURE_OUT) --instance $(FIXTURE_INSTANCE) --validate $(ARGS)
	uv run specivo-bundle validate $(FIXTURE_OUT)

fixture-test:  ## Run the end-to-end test against the running fixture
	RT_FIXTURE_REQUIRED=1 RT_FIXTURE_VERSION=$(RT_VERSION) RT_FIXTURE_DB=$(DB) RT_FIXTURE_DB_URL="$(FIXTURE_DB_URL)" \
		RT_FIXTURE_FILES=$(FIXTURE_FILES) uv run pytest tests/test_e2e_fixture.py
