# Contributing

## Local setup

```bash
make install
make test
```

Or run directly:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p "test_*.py" -v
```

## Development rules

- Keep the self-hosted workflow simple: SQLite + local disk are part of the product direction.
- Do not commit runtime files such as `users.db`, `data/` or uploaded blobs.
- Prefer small, reviewable changes with clear deployment impact.
- If you touch routes or upload logic, run the test suite before opening a PR.

## Pull requests

- Explain user-facing changes.
- Mention deployment or migration impact.
- Include screenshots when the landing page or dashboard UI changes.
