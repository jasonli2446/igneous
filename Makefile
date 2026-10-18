DEMO_BOXES := allen-p
BASELINE_BOXES := lay-k skilling-j allen-p arnold-j delainey-d lavorato-j
ALL_BOXES := $(BASELINE_BOXES) kaminski-v kean-s dasovich-j beck-s

IGN := cd api && uv run igneous

.PHONY: enron db migrate demo baseline api web test

enron:  ## download the corpus (~450 MB) and extract the mailboxes we use
	mkdir -p data
	test -f data/enron.tar.gz || curl -L -o data/enron.tar.gz https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz
	cd data && tar -xzf enron.tar.gz $(addprefix maildir/,$(ALL_BOXES))

db:
	docker compose up -d db

migrate: db
	cd api && uv sync && uv run alembic upgrade head

demo: migrate  ## one mailbox (~3k emails), end to end in a few minutes
	$(IGN) sync email $(addprefix --mailbox ,$(DEMO_BOXES))
	$(IGN) pipeline

baseline: migrate  ## six mailboxes (~26k emails); embedding takes ~45 min on CPU
	$(IGN) sync email $(addprefix --mailbox ,$(BASELINE_BOXES))
	$(IGN) pipeline

api:
	cd api && uv run uvicorn igneous.main:app --reload

web:
	cd web && npm install && npm run dev

test:
	cd api && uv run pytest -q
