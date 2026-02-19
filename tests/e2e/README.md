# PPBase E2E Tests (PocketBase SDK)

End-to-end tests using the official PocketBase JavaScript SDK to validate API compatibility.

## Test Coverage

- **Collections API** (~10 tests): CRUD, rules, truncate, system protections
- **Records API** (~13 tests): CRUD, pagination, filter, sort, expand, fields, modifiers
- **Auth Flows** (~14 tests): registration, login, refresh, token claims, field filtering
- **Rules Enforcement** (~8 tests): null/empty/expression rules on list/view/create/update/delete
- **Field Validation** (~12 tests): all 14 field types with their constraints
- **Relations & Expand** (~6 tests): single/multi/nested expand, cascade delete

**Total: ~63 SDK-based tests**

## Prerequisites

1. **PostgreSQL** running via Docker:
   ```bash
   python -m ppbase db start
   ```

2. **PPBase server** on port 8090
3. **Admin user** created

## Running Tests

### Quick Start

```bash
cd tests/e2e

# Install dependencies (first time only)
npm install

# Run tests
npm test
```

### Full Test Lifecycle

```bash
# 1. Reset database
docker exec ppbase-pg psql -U ppbase -d ppbase -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# 2. Start server
python -m ppbase serve -d --port 8090

# 3. Create admin
python -m ppbase create-admin --email admin@test.com --password adminpass123

# 4. Run SDK tests
cd tests/e2e && npm test

# 5. Stop server
python -m ppbase stop
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PPBASE_URL` | `http://localhost:8090` | PPBase server URL |
| `PPBASE_ADMIN_EMAIL` | `admin@test.com` | Admin email |
| `PPBASE_ADMIN_PASSWORD` | `adminpass123` | Admin password |

### Watch Mode

```bash
npm run test:watch
```

## Test Structure

```
tests/e2e/
├── helpers.ts              # Shared utilities (admin auth, collection factory)
├── collections.test.ts     # Collection CRUD + rules
├── records.test.ts         # Record CRUD + query features
├── auth.test.ts            # Auth collection flows
├── rules.test.ts           # API rules enforcement
├── fields.test.ts          # Field type validation
└── relations.test.ts       # Relation expand + cascade
```

## Notes

- Tests run **sequentially** to avoid DB conflicts (single fork mode)
- Each test creates its own collections with unique names
- Cleanup happens automatically after each test
- Admin auth uses custom `/api/admins/auth-with-password` endpoint (not SDK's collection auth)
- Tests require a **running server** (not mocked)

## Troubleshooting

### Tests fail with connection errors

Ensure the server is running:
```bash
python -m ppbase status
```

If not running:
```bash
python -m ppbase serve -d --port 8090
```

### Tests fail with auth errors

Ensure admin user exists:
```bash
python -m ppbase create-admin --email admin@test.com --password adminpass123
```

### Database state issues

Reset the database:
```bash
docker exec ppbase-pg psql -U ppbase -d ppbase -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
python -m ppbase restart
```
