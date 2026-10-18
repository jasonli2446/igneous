MAILBOXES := lay-k skilling-j allen-p arnold-j delainey-d lavorato-j kaminski-v kean-s dasovich-j beck-s

enron:
	mkdir -p data
	test -f data/enron.tar.gz || curl -L -o data/enron.tar.gz https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz
	cd data && tar -xzf enron.tar.gz $(addprefix maildir/,$(MAILBOXES))

test:
	cd api && uv run pytest -q
