# Contributing

Keep changes focused on this standalone plugin. Do not modify Hermes core files.

## Development

Install the development dependencies, then run:

```bash
uv sync --group dev
tox
```

Add or update tests for behavior changes. Keep MAX tokens, webhook secrets, user data, and deployment configuration out of commits and logs.

## Pull Requests

- Keep each change small and directly tied to a user-visible behavior.
- Preserve MAX API limits and webhook security checks.
- Update `README.md` or `docs/design.md` when public behavior changes.
