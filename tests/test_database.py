from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import aiosqlite
from mcp104.db.database import Database, ID_SOURCE_MESSAGE, ID_SOURCE_RESUME, ID_SOURCE_UNKNOWN

TAIPEI = ZoneInfo("Asia/Taipei")
UTC = ZoneInfo("UTC")

# The exact DDL Database.init() used to run, before id_source existed —
# verified against the real data/104.db on disk. Migration tests build a
# database with THIS schema, not a fresh one, since every other fixture in
# this file uses a brand-new tmp_path DB and could never see the bug this
# guards against (CREATE TABLE IF NOT EXISTS is a no-op on an existing
# table with the old column set).
OLD_SCHEMA_DDL = """
    CREATE TABLE candidates (
        candidate_id    TEXT NOT NULL,
        account_email   TEXT NOT NULL,
        name            TEXT,
        status          TEXT,
        created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (candidate_id, account_email)
    );
    CREATE TABLE sent_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        account_email   TEXT NOT NULL,
        candidate_id    TEXT,
        sent_at         DATETIME DEFAULT CURRENT_TIMESTAMP
    );
"""


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    # Database.init() now requires the caller's identity value (§C2) — this
    # fixture's store starts empty, so any label is accepted; it's unrelated
    # to the per-call account_email arguments the rest of this file's tests
    # pass to upsert_candidate/log_sent/etc.
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_init_creates_tables(db):
    async with aiosqlite.connect(db.path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    assert "candidates" in tables
    assert "sent_log" in tables


@pytest.mark.asyncio
async def test_upsert_and_get_candidate(db):
    await db.upsert_candidate("c1", ID_SOURCE_RESUME, "user@104.com", name="Alice", status="contacted")
    candidate = await db.get_candidate("c1", ID_SOURCE_RESUME, "user@104.com")
    assert candidate["name"] == "Alice"
    assert candidate["status"] == "contacted"


@pytest.mark.asyncio
async def test_candidate_isolation_by_account(db):
    await db.upsert_candidate("c1", ID_SOURCE_RESUME, "a@104.com", name="Alice")
    await db.upsert_candidate("c1", ID_SOURCE_RESUME, "b@104.com", name="Bob")
    a = await db.get_candidate("c1", ID_SOURCE_RESUME, "a@104.com")
    b = await db.get_candidate("c1", ID_SOURCE_RESUME, "b@104.com")
    assert a["name"] == "Alice"
    assert b["name"] == "Bob"


@pytest.mark.asyncio
async def test_candidate_isolation_by_id_source(db):
    # The same literal id string can legitimately appear in both key spaces
    # (resume card id vs. messaging thread id) — id_source keeps them from
    # silently colliding into one row.
    await db.upsert_candidate("4311229", ID_SOURCE_RESUME, "user@104.com", name="FromResume")
    await db.upsert_candidate("4311229", ID_SOURCE_MESSAGE, "user@104.com", name="FromMessage")
    resume_row = await db.get_candidate("4311229", ID_SOURCE_RESUME, "user@104.com")
    message_row = await db.get_candidate("4311229", ID_SOURCE_MESSAGE, "user@104.com")
    assert resume_row["name"] == "FromResume"
    assert message_row["name"] == "FromMessage"


@pytest.mark.asyncio
async def test_log_sent_and_daily_count(db):
    await db.log_sent("user@104.com", "c1", ID_SOURCE_MESSAGE)
    await db.log_sent("user@104.com", "c2", ID_SOURCE_MESSAGE)
    count = await db.get_daily_sent_count("user@104.com")
    assert count == 2


@pytest.mark.asyncio
async def test_daily_count_isolation(db):
    await db.log_sent("a@104.com", "c1", ID_SOURCE_MESSAGE)
    await db.log_sent("b@104.com", "c1", ID_SOURCE_MESSAGE)
    assert await db.get_daily_sent_count("a@104.com") == 1
    assert await db.get_daily_sent_count("b@104.com") == 1


@pytest.mark.asyncio
async def test_daily_count_taipei_boundary(db):
    # "Now" is pinned at 2026-08-07 00:30 Taipei — 30 minutes into the local
    # day, but process-local date.today() (which the old implementation
    # used) would still call this "2026-08-06" in a UTC-configured
    # container, since 00:30 Taipei is 2026-08-06 16:30 UTC.
    fixed_now_taipei = datetime(2026, 8, 7, 0, 30, tzinfo=TAIPEI)

    # 2026-08-06 23:30 Taipei — 1 hour before local midnight. Must NOT count.
    yesterday_utc = datetime(2026, 8, 6, 23, 30, tzinfo=TAIPEI).astimezone(UTC)
    # 2026-08-07 00:15 Taipei — 15 minutes into the local day. Must count.
    today_early_utc = datetime(2026, 8, 7, 0, 15, tzinfo=TAIPEI).astimezone(UTC)
    # 2026-08-07 23:59 Taipei — near the end of the same local day. Must
    # also count, proving the whole local day is covered, not just the
    # instant "now" sits at.
    today_late_utc = datetime(2026, 8, 7, 23, 59, tzinfo=TAIPEI).astimezone(UTC)

    async with aiosqlite.connect(db.path) as conn:
        for candidate_id, ts in [
            ("c1", yesterday_utc),
            ("c2", today_early_utc),
            ("c3", today_late_utc),
        ]:
            await conn.execute(
                "INSERT INTO sent_log (account_email, candidate_id, id_source, sent_at) VALUES (?, ?, ?, ?)",
                ["user@104.com", candidate_id, ID_SOURCE_MESSAGE, ts.strftime("%Y-%m-%d %H:%M:%S")],
            )
        await conn.commit()

    with patch("mcp104.db.database.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now_taipei
        count = await db.get_daily_sent_count("user@104.com")

    # A MagicMock's .now() ignores its arguments by default, so this would
    # still pass even if the implementation silently dropped TAIPEI_TZ and
    # fell back to a naive/UTC "now" — assert the call shape, not just the
    # return value, to actually pin down which zone was requested.
    mock_dt.now.assert_called_once_with(TAIPEI)
    assert count == 2


# ── Migrating a database created before id_source existed ──────────────
# init()'s CREATE TABLE IF NOT EXISTS is a no-op against an existing table,
# so a database built with the OLD DDL would otherwise silently "succeed"
# init() and then every read/write raises OperationalError: no such
# column: id_source. Manually verified (outside pytest) that this test
# fails with exactly that OperationalError without the migration call in
# init() — that failure is what makes this test meaningful, not just green.

@pytest.mark.asyncio
async def test_init_migrates_old_schema_without_id_source(tmp_path):
    db_path = str(tmp_path / "old_schema.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(OLD_SCHEMA_DDL)
        await conn.commit()

    database = Database(db_path)
    await database.init()  # must migrate forward, not just no-op

    try:
        # Full round-trip on both tables — exactly what raised
        # OperationalError: no such column: id_source before the fix.
        await database.upsert_candidate("c1", ID_SOURCE_RESUME, "user@104.com", name="Alice")
        candidate = await database.get_candidate("c1", ID_SOURCE_RESUME, "user@104.com")
        assert candidate["name"] == "Alice"

        await database.log_sent("user@104.com", "c1", ID_SOURCE_MESSAGE)
        count = await database.get_daily_sent_count("user@104.com")
        assert count == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_init_recovers_from_interrupted_migration(tmp_path):
    # Reproduces the crash-before-commit state: CREATE TABLE candidates_new
    # is DDL and commits on its own the instant it runs, but the INSERT/
    # DROP/RENAME that follow it in the migration share one implicit
    # transaction that only becomes durable at the trailing commit() — a
    # crash anywhere in that window leaves candidates_new behind, empty,
    # sitting next to the still-fully-intact old candidates table
    # (empirically confirmed outside pytest; this is not a partially-copied
    # or missing candidates — DROP/RENAME do not auto-commit the pending
    # transaction the way CREATE does). Without a DROP TABLE IF EXISTS
    # candidates_new before the CREATE, re-running init() against this
    # state raises OperationalError: table candidates_new already exists.
    db_path = str(tmp_path / "interrupted_migration.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(OLD_SCHEMA_DDL)
        await conn.execute(
            "INSERT INTO candidates (candidate_id, account_email, name) VALUES (?, ?, ?)",
            ["c1", "user@104.com", "Alice"],
        )
        # The leftover artifact a crash produces: empty, id_source already
        # present (it's the NEW schema), old candidates untouched.
        await conn.execute("""
            CREATE TABLE candidates_new (
                candidate_id    TEXT NOT NULL,
                id_source       TEXT NOT NULL,
                account_email   TEXT NOT NULL,
                name            TEXT,
                status          TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (candidate_id, id_source, account_email)
            )
        """)
        await conn.commit()

    database = Database(db_path)
    await database.init()  # must recover, not raise "table already exists"

    try:
        # The pre-crash row must have survived the recovery (it lived in
        # the untouched old `candidates`, migrated on this successful run).
        row = await database.get_candidate("c1", ID_SOURCE_UNKNOWN, "user@104.com")
        assert row is not None
        assert row["name"] == "Alice"

        # And a fresh round-trip write must work — this is the state a
        # crash-looping container would need to self-heal into without a
        # human opening the SQLite file.
        await database.upsert_candidate("c2", ID_SOURCE_RESUME, "user@104.com", name="Bob")
        assert (await database.get_candidate("c2", ID_SOURCE_RESUME, "user@104.com"))["name"] == "Bob"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_init_migration_preserves_existing_rows(tmp_path):
    # The non-empty case: migration must not assume the tables are empty
    # (the real data/104.db happens to be, but the migration code must not
    # rely on that).
    db_path = str(tmp_path / "old_schema_with_data.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(OLD_SCHEMA_DDL)
        await conn.execute(
            "INSERT INTO candidates (candidate_id, account_email, name, status) VALUES (?, ?, ?, ?)",
            ["old1", "user@104.com", "PreMigration", "contacted"],
        )
        await conn.execute(
            "INSERT INTO sent_log (account_email, candidate_id) VALUES (?, ?)",
            ["user@104.com", "old1"],
        )
        await conn.commit()

    database = Database(db_path)
    await database.init()

    try:
        # candidates was written from both id spaces before id_source
        # existed, so provenance is genuinely unrecoverable — the migrated
        # row must land under ID_SOURCE_UNKNOWN, not a guessed space.
        row = await database.get_candidate("old1", ID_SOURCE_UNKNOWN, "user@104.com")
        assert row is not None
        assert row["name"] == "PreMigration"
        assert row["status"] == "contacted"
        assert await database.get_candidate("old1", ID_SOURCE_RESUME, "user@104.com") is None

        # sent_log, in contrast, was ONLY ever written by send_message
        # before id_source existed — that IS a known fact, so the migrated
        # row must count toward today's cap.
        count = await database.get_daily_sent_count("user@104.com")
        assert count == 1
    finally:
        await database.close()


# ── upsert_candidate's empty-fields branch ──────────────────────────────

@pytest.mark.asyncio
async def test_upsert_candidate_no_fields_on_existing_row_is_noop(db):
    await db.upsert_candidate("c1", ID_SOURCE_RESUME, "user@104.com", name="Alice", status="contacted")
    await db.upsert_candidate("c1", ID_SOURCE_RESUME, "user@104.com")  # no fields — must not raise or clobber
    candidate = await db.get_candidate("c1", ID_SOURCE_RESUME, "user@104.com")
    assert candidate["name"] == "Alice"
    assert candidate["status"] == "contacted"


@pytest.mark.asyncio
async def test_upsert_candidate_no_fields_creates_bare_row_when_absent(db):
    await db.upsert_candidate("c2", ID_SOURCE_RESUME, "user@104.com")  # no fields, row doesn't exist yet
    candidate = await db.get_candidate("c2", ID_SOURCE_RESUME, "user@104.com")
    assert candidate is not None
    assert candidate["name"] is None


# ── T-28 (R5.1, R5.5): two independent lines of defense ────────────────────
# design.md §C2 is explicit that these are meant to be independently true:
# different keys, AND different directories, each on their own must keep
# records apart. Testing only one half would prove only one line of defense.

@pytest.mark.asyncio
async def test_t028_two_independent_lines_of_defense(tmp_path):
    same_label = "shared@104.com"

    # (a) SAME identity value, two DIFFERENT data locations.
    loc_a = tmp_path / "loc_a"
    loc_b = tmp_path / "loc_b"
    loc_a.mkdir()
    loc_b.mkdir()
    db_a = Database(str(loc_a / "104.db"))
    db_b = Database(str(loc_b / "104.db"))
    await db_a.init()
    await db_b.init()
    try:
        await db_a.upsert_candidate("c1", ID_SOURCE_RESUME, same_label, name="Alice", status="contacted")
        await db_a.log_sent(same_label, "c1", ID_SOURCE_MESSAGE)

        assert await db_b.get_candidate("c1", ID_SOURCE_RESUME, same_label) is None
        assert await db_b.get_daily_sent_count(same_label) == 0
    finally:
        await db_a.close()
        await db_b.close()

    # (b) SAME data location, two DIFFERENT identity values — records stay
    # apart, and the daily send cap only counts the caller's own identity.
    loc_c = tmp_path / "loc_c"
    loc_c.mkdir()
    db_c = Database(str(loc_c / "104.db"))
    await db_c.init()
    try:
        await db_c.upsert_candidate("c2", ID_SOURCE_RESUME, "alice@104.com", name="AliceRow", status="contacted")
        await db_c.upsert_candidate("c2", ID_SOURCE_RESUME, "bob@104.com", name="BobRow", status="interested")
        await db_c.log_sent("alice@104.com", "c3", ID_SOURCE_MESSAGE)
        await db_c.log_sent("bob@104.com", "c4", ID_SOURCE_MESSAGE)
        await db_c.log_sent("bob@104.com", "c5", ID_SOURCE_MESSAGE)

        alice_row = await db_c.get_candidate("c2", ID_SOURCE_RESUME, "alice@104.com")
        bob_row = await db_c.get_candidate("c2", ID_SOURCE_RESUME, "bob@104.com")
        assert alice_row["name"] == "AliceRow"
        assert bob_row["name"] == "BobRow"

        assert await db_c.get_daily_sent_count("alice@104.com") == 1
        assert await db_c.get_daily_sent_count("bob@104.com") == 2
    finally:
        await db_c.close()
