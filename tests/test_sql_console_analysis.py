from __future__ import annotations

import pytest

from ppbase.services.sql_console import analyze_script, split_statements


def _kinds(sql: str) -> list[tuple[str, str]]:
    return [(s.command, s.kind) for s in analyze_script(sql)]


def test_split_ignores_semicolons_in_literals_comments_and_dollar_quotes():
    sql = """
    SELECT 'a;b', "weird;name", E'it\\'s;';  -- trailing; comment
    /* block ; /* nested ; */ comment */
    DO $body$ BEGIN PERFORM 1; END $body$;
    SELECT $$x;y$$;
    ;  -- empty statement
    """
    statements = [text for text, _ in split_statements(sql)]
    assert len(statements) == 3
    assert statements[0].startswith("SELECT 'a;b'")
    assert "DO $body$ BEGIN PERFORM 1; END $body$" in statements[1]
    assert statements[2] == "SELECT $$x;y$$"


def test_comment_only_script_is_empty():
    assert split_statements("-- nothing\n/* here */") == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select * from posts", ("SELECT", "read")),
        ("(SELECT 1) UNION (SELECT 2)", ("SELECT", "read")),
        ("WITH x AS (SELECT 1) SELECT * FROM x", ("SELECT", "read")),
        ("SHOW search_path", ("SHOW", "read")),
        ("EXPLAIN SELECT 1", ("EXPLAIN", "read")),
        ("EXPLAIN ANALYZE DELETE FROM posts", ("EXPLAIN ANALYZE DELETE", "delete")),
        ("EXPLAIN (ANALYZE, BUFFERS) UPDATE posts SET a = 1", ("EXPLAIN ANALYZE UPDATE", "modify")),
        ("INSERT INTO posts (id) VALUES ('x')", ("INSERT", "modify")),
        ("update posts set title = 'delete' where id = 'x'", ("UPDATE", "modify")),
        ("DELETE FROM posts WHERE id = 'x'", ("DELETE", "delete")),
        ("TRUNCATE posts", ("TRUNCATE", "delete")),
        ("DROP TABLE posts", ("DROP TABLE", "delete")),
        ("DROP MATERIALIZED VIEW IF EXISTS v", ("DROP MATERIALIZED VIEW", "delete")),
        ("WITH d AS (DELETE FROM posts RETURNING *) SELECT * FROM d", ("WITH ... DELETE", "delete")),
        ("CREATE OR REPLACE VIEW v AS SELECT 1", ("CREATE VIEW", "schema")),
        ("CREATE UNIQUE INDEX i ON posts (id)", ("CREATE INDEX", "schema")),
        ("ALTER TABLE posts ADD COLUMN x text", ("ALTER TABLE", "schema")),
        ("SELECT * INTO backup_posts FROM posts", ("SELECT INTO", "schema")),
        ("VACUUM posts", ("VACUUM", "maintenance")),
        ("SET search_path TO public", ("SET", "other")),
        ("DO $$ BEGIN DELETE FROM posts; END $$", ("DO", "other")),
    ],
)
def test_classification(sql: str, expected: tuple[str, str]):
    assert _kinds(sql) == [expected]


def test_keywords_inside_strings_and_identifiers_do_not_count():
    assert _kinds("SELECT 'DELETE FROM posts', \"drop\" FROM t") == [("SELECT", "read")]


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("DELETE FROM posts", "without a WHERE"),
        ("UPDATE posts SET a = (SELECT 1 WHERE true)", "without a WHERE"),
        ("TRUNCATE posts", "every row"),
        ("DROP TABLE posts CASCADE", "CASCADE"),
        ("ALTER TABLE posts DROP COLUMN title", "drops a column"),
    ],
)
def test_destructive_warnings(sql: str, fragment: str):
    (stmt,) = analyze_script(sql)
    assert stmt.warning and fragment in stmt.warning


def test_filtered_delete_has_no_warning():
    (stmt,) = analyze_script("DELETE FROM posts WHERE id = 'x'")
    assert stmt.warning is None


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN",
        "commit",
        "ROLLBACK",
        "SAVEPOINT s",
        "PREPARE TRANSACTION 'x'",
        "SET TRANSACTION READ WRITE",
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
        "SET transaction_read_only = off",
        "COPY posts FROM '/tmp/x.csv'",
    ],
)
def test_forbidden_statements(sql: str):
    (stmt,) = analyze_script(sql)
    assert stmt.kind == "forbidden"
    assert stmt.forbidden_reason
