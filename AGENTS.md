# Repository Guidelines

## Project Structure & Module Organization
- `ppbase/`: Python backend. Key areas are `api/` (routers), `services/` (business logic), `db/` (engine, schema, bootstrap), `models/`, `middleware/`, and `storage/`.
- `admin-ui/`: React + TypeScript admin SPA served at `/_/`; source lives in `admin-ui/src/`, and production assets are built into `ppbase/admin/dist/`.
- `tests/integration/`: backend integration tests (pytest).
- `tests/e2e/`: PocketBase SDK compatibility tests (Vitest).
- `pb_migrations/`: migration files consumed by the backend migration runner.
- `project_docs/` and `pocketbase_markdown_docs/`: architecture and compatibility references.

## Build, Test, and Development Commands
```bash
pip install -e ".[dev]"                    # install backend + dev dependencies
python -m ppbase db start                  # start PostgreSQL (Docker, port 5433)
python -m ppbase serve                     # run API server on :8090
python -m ppbase serve -d --port 8090      # run server as daemon
pytest tests/ -v                           # run Python tests
cd admin-ui && npm install && npm run dev  # run admin UI locally
cd admin-ui && npm run build               # type-check + build admin UI
cd tests/e2e && npm test                   # run SDK e2e compatibility tests
```

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints on non-trivial/public interfaces, and concise docstrings where behavior is not obvious.
- Python naming: modules/files/functions use `snake_case`; classes use `PascalCase`.
- TypeScript/React: match existing style (2-space indentation, single quotes, no semicolons).
- Frontend filenames are mostly `kebab-case` (example: `record-editor.tsx`, `use-collections.ts`).

## Testing Guidelines
- Backend tests use `pytest` with `pytest-asyncio`; async tests should use `@pytest.mark.asyncio`.
- Keep Python tests under `tests/` with `test_*.py` naming.
- E2E tests use Vitest in `tests/e2e/*.test.ts`; these expect a running PPBase server and DB.
- No enforced coverage percentage is configured; add tests for every behavioral change, especially API compatibility paths.

## Commit & Pull Request Guidelines
- Git history favors Conventional Commit style, especially `feat(scope): summary` (for example, `feat(auth,compat): ...`).
- Keep commits focused and avoid `WIP` commits on shared branches.
- PRs should include: concise change summary, linked issue (if available), test evidence (`pytest`, `npm test`), and screenshots for `admin-ui` changes.
- Explicitly mention migration/schema impact when touching `pb_migrations/` or dynamic table logic.

## Security & Configuration Tips
- Configure locally with `PPBASE_` env vars (for example `PPBASE_DATABASE_URL`, `PPBASE_PORT`).
- Never commit real secrets (database credentials, OAuth client secrets); keep them in local environment files ignored by git.
