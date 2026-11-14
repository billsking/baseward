"""Migrations must survive `app/db/schema.py` splitting each file on every semicolon."""

from __future__ import annotations

from app.db.schema import IGNORED_SCHEMA_ERRNOS, MIGRATIONS_DIR


def _migration_files():
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert files, "no migration files found"
    return files


def test_comment_lines_contain_no_semicolon() -> None:
    """A semicolon inside a `--` comment leaves the comment tail as a standalone statement."""
    offenders = [
        f"{path.name}:{number}"
        for path in _migration_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.lstrip().startswith("--") and ";" in line
    ]
    assert offenders == []


def test_every_statement_still_has_sql_once_comments_are_removed() -> None:
    """A comment-only chunk would reach MySQL as an empty query (errno 1065), which is not skipped."""
    empty = []
    for path in _migration_files():
        for index, part in enumerate(path.read_text(encoding="utf-8").split(";"), 1):
            if not part.strip():
                continue
            body = [
                line
                for line in part.splitlines()
                if line.strip() and not line.lstrip().startswith("--")
            ]
            if not body:
                empty.append(f"{path.name}#{index}")
    assert empty == []


def test_actor_migration_covers_policy_columns_and_index() -> None:
    migration = MIGRATIONS_DIR / "006_actor.sql"
    assert migration.exists()
    sql = migration.read_text(encoding="utf-8")
    for fragment in (
        "actor_policy",
        "actor_provider",
        "actor_id",
        "actor_name",
        "actor_trust",
        "CREATE INDEX idx_audit_actor",
    ):
        assert fragment in sql, fragment


def test_duplicate_index_errno_is_ignored_on_replay() -> None:
    """006 replays CREATE INDEX on every startup; MySQL reports 1061 for an existing index."""
    assert 1061 in IGNORED_SCHEMA_ERRNOS

def test_exchange_migration_covers_kind_and_short_credential() -> None:
    migration = MIGRATIONS_DIR / "007_exchange.sql"
    assert migration.exists()
    sql = migration.read_text(encoding="utf-8")
    for fragment in (
        "ADD COLUMN token_kind",
        "CREATE TABLE IF NOT EXISTS short_credential",
        "credential_hash",
        "parent_token_id",
        "actor_provider",
        "actor_id",
        "scope_json",
        "source_ip",
        "expires_at",
        "revoked_at",
        "idx_short_parent",
        "idx_short_expires",
        "idx_short_actor",
        "ON DELETE CASCADE",
    ):
        assert fragment in sql, fragment
