# Context Graph

![Coverage](badges/coverage.svg)

Kai Lo, Dominik Kucz, Kirill Utyashev, Hrid Patel

## Testing

Run the focused `context_graph` submodule suite with coverage locally:

```bash
PYTHONPATH=. uv run --no-project \
  --with pytest \
  --with pytest-cov \
  --with pydantic-settings \
  --with pyyaml \
  python -m pytest --cov=context_graph --cov-report=term-missing tests
```

GitHub Actions runs the same coverage check on pull requests, comments the report on the PR, and refreshes the README badge on pushes to `main`.
