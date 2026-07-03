## Summary

<!-- What does this PR change and why? -->

## Related issues

<!-- e.g. Closes #123 -->

## Checklist

- [ ] PR targets the `dev` branch (not `main`) — see [CONTRIBUTING.md](../CONTRIBUTING.md)
- [ ] Changes are scoped to a single topic
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `python -m pytest -q` is green
- [ ] Schema changes ship an Alembic migration (no manual `ALTER TABLE`; Timescale-only DDL guarded)
- [ ] Documentation updated where relevant (README / CLAUDE.md / docs/ARCHITECTURE.md / docs/workout-analysis.md)
- [ ] No secrets, credentials, or personal health data committed
