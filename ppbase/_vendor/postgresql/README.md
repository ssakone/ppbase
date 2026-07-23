# Private PostgreSQL wheel payload

Published PPBase wheels replace this source-tree placeholder with verified
`pg_dump`, `pg_restore`, and `psql` executables for the wheel platform.

The exact PostgreSQL source URL and SHA-256 are locked in
`scripts/postgresql-source.json`. `scripts/vendor_postgresql.py` builds the
payload, then `scripts/repair_wheel.py` bundles its shared libraries and seals
the per-file hashes in `PROVENANCE.json`.

This directory is private implementation data. Users install only `ppbase`;
they do not select or install a platform package.
