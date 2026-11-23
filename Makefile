# The demo mailbox is the one `igneous sync email` reads by default (DEMO_MAILBOXES in api/igneous/sources.py).
BASELINE_BOXES := lay-k skilling-j allen-p arnold-j delainey-d lavorato-j
ALL_BOXES := $(BASELINE_BOXES) kaminski-v kean-s dasovich-j beck-s

IGN := cd api && uv run igneous
# Messages to LLM-extract per run (needs AWS_BEARER_TOKEN_BEDROCK and IGNEOUS_LLM_SOURCES=email). EXTRACT=0 skips model calls.
EXTRACT ?= 100

.PHONY: enron synthetic-corpus db migrate demo baseline api web test check audit

# curl -f fails on an HTTP error instead of saving the error page, and the archive gets its name only once the
# download is complete, so neither passes for the corpus on the next run.
enron:  ## download the corpus (~450 MB) and extract the mailboxes we use
	mkdir -p data
	test -f data/enron.tar.gz || { \
		curl -fL -o data/enron.tar.gz.part https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz \
		&& mv data/enron.tar.gz.part data/enron.tar.gz; }
	cd data && tar -xzf enron.tar.gz $(addprefix maildir/,$(ALL_BOXES))

synthetic-corpus:  ## generate small synthetic files and calendar data (no import)
	$(IGN) synthetic-corpus ../data/synthetic-cross-source
	$(IGN) verify-synthetic-corpus ../data/synthetic-cross-source

db:  ## Postgres in Docker; returns once it accepts connections
	docker compose up -d --wait db

migrate:  ## migrate the database IGNEOUS_DATABASE_URL names (Postgres must be running: make db, or your own)
	cd api && uv sync && uv run alembic upgrade head

demo: migrate  ## one mailbox (~3k emails), end to end in a few minutes
	$(IGN) sync email
	$(IGN) pipeline --extract $(EXTRACT)

baseline: migrate  ## six mailboxes (~26k emails); embedding takes ~45 min on CPU
	$(IGN) sync email $(addprefix --mailbox ,$(BASELINE_BOXES))
	$(IGN) pipeline --extract $(EXTRACT)

api:
	cd api && uv run uvicorn igneous.main:app --reload

web:
	cd web && npm install && npm run dev

test:
	cd api && uv run pytest -q

check:  ## the checks CLAUDE.md requires before any PR (CI runs the same)
	cd api && uv run ruff check . && uv run pytest -q
	cd web && npx next typegen && npx tsc --noEmit && npm run lint && npm run build

audit:  ## fail on known advisories in the locked Python and npm dependencies, as CI does
	cd api && bash -o pipefail -c 'uv export --frozen --no-hashes --no-emit-project | uvx pip-audit --strict --disable-pip --no-deps -r /dev/stdin'
	cd web && npm audit --audit-level=moderate
