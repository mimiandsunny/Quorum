from data import storage


class _Result:
    rowcount = 0


class _FakeConnection:
    def __init__(self):
        self.statements = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.statements.append(statement)
        return _Result()

    def commit(self):
        self.committed = True


def test_strategy_index_is_created_after_strategy_column_migration(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(storage, "get_connection", lambda: conn)
    monkeypatch.setattr(
        storage,
        "_column_exists",
        lambda _conn, table, column: not (table == "paper_trades" and column == "strategy"),
    )

    storage.init_db()

    schema_idx = conn.statements.index(storage.SCHEMA_SQL)
    alter_idx = next(
        i for i, statement in enumerate(conn.statements)
        if statement.startswith("ALTER TABLE paper_trades ADD COLUMN strategy")
    )
    post_idx = conn.statements.index(storage.POST_MIGRATION_SQL)

    assert schema_idx < alter_idx < post_idx
    assert "idx_paper_trades_strategy" not in storage.SCHEMA_SQL
    assert "idx_paper_trades_strategy" in storage.POST_MIGRATION_SQL
    assert conn.committed is True


def test_sector_and_industry_columns_added_when_missing(monkeypatch):
    """Wave-1 signals tables predate the sector/industry columns. The guard
    migration must add both so wave-2 reads don't fail."""
    conn = _FakeConnection()
    monkeypatch.setattr(storage, "get_connection", lambda: conn)
    monkeypatch.setattr(
        storage,
        "_column_exists",
        lambda _conn, table, column: not (
            table == "signals" and column in {"sector", "industry"}
        ),
    )

    storage.init_db()

    sector_alter = next(
        i for i, s in enumerate(conn.statements)
        if s.startswith("ALTER TABLE signals ADD COLUMN sector")
    )
    industry_alter = next(
        i for i, s in enumerate(conn.statements)
        if s.startswith("ALTER TABLE signals ADD COLUMN industry")
    )
    schema_idx = conn.statements.index(storage.SCHEMA_SQL)
    assert schema_idx < sector_alter
    assert schema_idx < industry_alter


def test_idempotency_migration_only_backfills_legacy_ticker_date_keys(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(storage, "get_connection", lambda: conn)
    monkeypatch.setattr(storage, "_column_exists", lambda *_args: True)

    storage.init_db()

    statements = "\n".join(conn.statements)
    assert "ticker || ':' || signal_date::text || ':' || strategy AS target_key" in statements
    assert "recommendation_id || ':' || strategy || ':balanced'" in statements
    assert "idempotency_key ~ '^[A-Z][A-Z0-9.-]*:[0-9]{4}-[0-9]{2}-[0-9]{2}$'" in statements
    assert "idempotency_key !~ ':[^:]+:[^:]+$'" not in statements


def test_llm_calls_table_in_schema():
    """Wave 2 prep: llm_calls table is part of the base schema (no guard
    migration needed since it didn't exist in any prior wave)."""
    assert "CREATE TABLE IF NOT EXISTS llm_calls" in storage.SCHEMA_SQL
    assert "idx_llm_calls_created" in storage.SCHEMA_SQL
    assert "idx_llm_calls_model_stage" in storage.SCHEMA_SQL


def test_recommendation_v2_tables_in_schema():
    """Recommendation v2 starts with immutable snapshots and recommendations."""
    assert "CREATE TABLE IF NOT EXISTS data_snapshots" in storage.SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS recommendations" in storage.SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS recommendation_scores" in storage.SCHEMA_SQL
    assert "snapshot_id TEXT PRIMARY KEY" in storage.SCHEMA_SQL
    assert "recommendation_id TEXT PRIMARY KEY" in storage.SCHEMA_SQL
    assert "idx_recommendations_ticker_created" in storage.SCHEMA_SQL


def test_option_chain_snapshots_table_in_schema():
    assert "CREATE TABLE IF NOT EXISTS option_chain_snapshots" in storage.SCHEMA_SQL
    assert "contracts JSONB NOT NULL DEFAULT '[]'" in storage.SCHEMA_SQL
    assert "idx_option_chain_snapshots_ticker_captured" in storage.SCHEMA_SQL


def test_paper_trades_recommendation_id_guard_and_index():
    """Existing paper_trades tables get a nullable recommendation_id."""
    assert ("signals", "recommendation_id", "TEXT") in storage.GUARD_MIGRATIONS
    assert "recommendation_id TEXT" in storage.SCHEMA_SQL
    assert ("paper_trades", "recommendation_id", "TEXT") in storage.GUARD_MIGRATIONS
    assert "idx_paper_trades_recommendation" in storage.POST_MIGRATION_SQL


def test_recommendation_scores_execution_columns_are_guarded():
    assert "execution_status TEXT" in storage.SCHEMA_SQL
    assert ("recommendation_scores", "execution_status", "TEXT") in storage.GUARD_MIGRATIONS
    assert ("recommendation_scores", "execution_return_pct", "REAL") in storage.GUARD_MIGRATIONS
    assert ("recommendation_scores", "execution_slippage_pct", "REAL") in storage.GUARD_MIGRATIONS
