Maps are built in the labeling image (`python:3.13-slim` at the digest in the Dockerfile) with
`requirements.lock` installed and sqlglot checked out at the pinned commit:

1. run every test of `tests.json` alone under coverage (`run_tests.sh`),
2. `python tools/build_map.py OUT SQLGLOT_CHECKOUT map tests.tsv`.
