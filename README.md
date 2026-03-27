# Context Graph

![Coverage](badges/coverage.svg)

Kai Lo, Dominik Kucz, Kirill Utyashev, Hrid Patel

## Testing

Install the test dependencies:

```bash
uv sync --group dev
```

Run the project test suite with coverage locally:

```bash
uv run python -m pytest --cov=context_graph --cov-report=term-missing tests
```

GitHub Actions also runs the same coverage command on pull requests and updates the README badge on pushes to `main`.
