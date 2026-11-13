import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import aiosqlite
import mcp104.db.database as dbmod
from mcp104.db.database import Database, ID_SOURCE_MESSAGE, ID_SOURCE_RESUME, ID_SOURCE_UNKNOWN

TAIPEI = ZoneInfo("Asia/Taipei")
UTC = ZoneInfo("UTC")

ACCOUNT = "user@104.com"
OTHER_ACCOUNT = "other@104.com"
KNOWN_OLD = "2020-01-01 00:00:00"

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

# The schema this database had right before the status-time column and the
# id-link table were added: id_source present, status_updated_at absent,
# candidate_id_links absent.
PRE_STATUS_TIME_DDL = """
    CREATE TABLE candidates (
        candidate_id    TEXT NOT NULL,
        id_source       TEXT NOT NULL,
        account_email   TEXT NOT NULL,
        name            TEXT,
        status          TEXT,
        created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (candidate_id, id_source, account_email)
    );
    CREATE TABLE sent_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        account_email   TEXT NOT NULL,
        candidate_id    TEXT,
        id_source       TEXT NOT NULL,
        sent_at         DATETIME DEFAULT CURRENT_TIMESTAMP
    );
"""


@asynccontextmanager
async def _opened(path):
    """A Database that is always closed, even when init() itself raises."""
    database = Database(str(path))
    try:
        await database.init()
        yield database
    finally:
        await database.close()


async def _select(path, sql, params=()):
    async with aiosqlite.connect(str(path)) as conn:
        cursor = await conn.execute(sql, params)
        names = [column[0] for column in cursor.description]
        rows = await cursor.fetchall()
    return [dict(zip(names, row)) for row in rows]


async def _execute(path, sql, params=()):
    async with aiosqlite.connect(str(path)) as conn:
        await conn.execute(sql, params)
        await conn.commit()


async def _row(path, candidate_id, id_source, account=ACCOUNT):
    rows = await _select(
        path,
        "SELECT * FROM candidates WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        (candidate_id, id_source, account),
    )
    return rows[0] if rows else None


async def _insert_candidate(path, candidate_id, id_source, account, *, name=None, status=None,
                            created_at="2026-01-01 00:00:00", status_updated_at=None):
    await _execute(
        path,
        "INSERT INTO candidates (candidate_id, id_source, account_email, name, status, created_at, "
        "status_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, id_source, account, name, status, created_at, status_updated_at),
    )


async def _snapshot_all(path):
    tables = await _select(
        path, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    snapshot = {}
    for table in tables:
        snapshot[table["name"]] = await _select(path, f"SELECT * FROM {table['name']} ORDER BY rowid")
    return snapshot


async def _columns(path, table):
    return [row["name"] for row in await _select(path, f"PRAGMA table_info({table})")]


async def _has_table(path, table):
    rows = await _select(path, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
    return bool(rows)


async def _make_pre_status_time_db(path, rows):
    async with aiosqlite.connect(str(path)) as conn:
        await conn.executescript(PRE_STATUS_TIME_DDL)
        await conn.executemany(
            "INSERT INTO candidates (candidate_id, id_source, account_email, name, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        await conn.commit()


_PRE_STATUS_TIME_ROWS = [
    ("1001", "resume", ACCOUNT, "Synthetic A", "interested", "2026-01-01 00:00:00"),
    ("2001", "message", ACCOUNT, None, "contacted", "2026-01-02 00:00:00"),
    ("3001", "resume", ACCOUNT, "Synthetic C", None, "2026-01-03 00:00:00"),
]


@pytest_asyncio.fixture
async def db(tmp_path):
    async with _opened(tmp_path / "test.db") as database:
        yield database


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
    await db.upsert_candidate("0939712", ID_SOURCE_RESUME, "user@104.com", name="FromResume")
    await db.upsert_candidate("0939712", ID_SOURCE_MESSAGE, "user@104.com", name="FromMessage")
    resume_row = await db.get_candidate("0939712", ID_SOURCE_RESUME, "user@104.com")
    message_row = await db.get_candidate("0939712", ID_SOURCE_MESSAGE, "user@104.com")
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

    # Must migrate forward, not just no-op.
    async with _opened(db_path) as database:
        # Full round-trip on both tables — exactly what raised
        # OperationalError: no such column: id_source before the fix.
        await database.upsert_candidate("c1", ID_SOURCE_RESUME, "user@104.com", name="Alice")
        candidate = await database.get_candidate("c1", ID_SOURCE_RESUME, "user@104.com")
        assert candidate["name"] == "Alice"

        await database.log_sent("user@104.com", "c1", ID_SOURCE_MESSAGE)
        count = await database.get_daily_sent_count("user@104.com")
        assert count == 1


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

    # Must recover, not raise "table already exists".
    async with _opened(db_path) as database:
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

    async with _opened(db_path) as database:
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
    async with _opened(loc_a / "104.db") as db_a, _opened(loc_b / "104.db") as db_b:
        await db_a.upsert_candidate("c1", ID_SOURCE_RESUME, same_label, name="Alice", status="contacted")
        await db_a.log_sent(same_label, "c1", ID_SOURCE_MESSAGE)

        assert await db_b.get_candidate("c1", ID_SOURCE_RESUME, same_label) is None
        assert await db_b.get_daily_sent_count(same_label) == 0

    # (b) SAME data location, two DIFFERENT identity values — records stay
    # apart, and the daily send cap only counts the caller's own identity.
    loc_c = tmp_path / "loc_c"
    loc_c.mkdir()
    async with _opened(loc_c / "104.db") as db_c:
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


# ── Status-time column: written by the status writers, migrated in place ─────

@pytest.mark.asyncio
async def test_status_writes_record_the_status_time_in_the_same_call(db):
    path = db.path

    # New row: one call sets the status and its time.
    await db.upsert_candidate("5001", ID_SOURCE_RESUME, ACCOUNT, status="interested")
    row = await _row(path, "5001", ID_SOURCE_RESUME)
    assert row["status"] == "interested"
    assert row["status_updated_at"] is not None

    # Existing row, same status written again: the time is set again even
    # though the value did not change. A known past value proves the rewrite.
    await _execute(
        path,
        "UPDATE candidates SET status_updated_at = ? WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        (KNOWN_OLD, "5001", ID_SOURCE_RESUME, ACCOUNT),
    )
    await db.upsert_candidate("5001", ID_SOURCE_RESUME, ACCOUNT, status="interested")
    row = await _row(path, "5001", ID_SOURCE_RESUME)
    assert row["status"] == "interested"
    assert row["status_updated_at"] is not None
    assert row["status_updated_at"] != KNOWN_OLD

    # Name and status together: both are written and the time is set.
    await db.upsert_candidate("5002", ID_SOURCE_RESUME, ACCOUNT, name="Synthetic", status="contacted")
    row = await _row(path, "5002", ID_SOURCE_RESUME)
    assert row["name"] == "Synthetic"
    assert row["status"] == "contacted"
    assert row["status_updated_at"] is not None


@pytest.mark.asyncio
async def test_upsert_without_status_leaves_status_and_its_time_alone(db):
    path = db.path
    await db.upsert_candidate("4001", ID_SOURCE_RESUME, ACCOUNT, status="interested")
    await _execute(
        path,
        "UPDATE candidates SET status_updated_at = ? WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        (KNOWN_OLD, "4001", ID_SOURCE_RESUME, ACCOUNT),
    )

    # Name only: the status and its time are not touched.
    await db.upsert_candidate("4001", ID_SOURCE_RESUME, ACCOUNT, name="Synthetic")
    row = await _row(path, "4001", ID_SOURCE_RESUME)
    assert row["name"] == "Synthetic"
    assert row["status"] == "interested"
    assert row["status_updated_at"] == KNOWN_OLD

    # No fields on an existing row: nothing changes at all.
    before = await _row(path, "4001", ID_SOURCE_RESUME)
    await db.upsert_candidate("4001", ID_SOURCE_RESUME, ACCOUNT)
    assert await _row(path, "4001", ID_SOURCE_RESUME) == before

    # No fields on an absent row: an empty shell row appears.
    await db.upsert_candidate("4002", ID_SOURCE_RESUME, ACCOUNT)
    shell = await _row(path, "4002", ID_SOURCE_RESUME)
    assert shell is not None
    assert shell["name"] is None
    assert shell["status"] is None


@pytest.mark.asyncio
async def test_concurrent_upserts_of_one_new_row_neither_raise_nor_duplicate(db):
    path = db.path
    await asyncio.gather(
        db.upsert_candidate("4101", ID_SOURCE_MESSAGE, ACCOUNT, status="contacted"),
        db.upsert_candidate("4101", ID_SOURCE_MESSAGE, ACCOUNT, status="interested"),
    )
    rows = await _select(
        path,
        "SELECT status FROM candidates WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        ("4101", ID_SOURCE_MESSAGE, ACCOUNT),
    )
    assert len(rows) == 1
    assert rows[0]["status"] in {"contacted", "interested"}


@pytest.mark.asyncio
async def test_set_status_if_unset_writes_only_when_no_status_is_present(db):
    path = db.path

    # Absent row: created with the status and its time.
    await db.set_status_if_unset("9101", ID_SOURCE_MESSAGE, ACCOUNT, "contacted")
    row = await _row(path, "9101", ID_SOURCE_MESSAGE)
    assert row["status"] == "contacted"
    assert row["status_updated_at"] is not None

    # Existing name-only row: status and time are written, name is kept.
    await db.upsert_candidate("9102", ID_SOURCE_MESSAGE, ACCOUNT, name="Synthetic")
    await db.set_status_if_unset("9102", ID_SOURCE_MESSAGE, ACCOUNT, "contacted")
    row = await _row(path, "9102", ID_SOURCE_MESSAGE)
    assert row["name"] == "Synthetic"
    assert row["status"] == "contacted"
    assert row["status_updated_at"] is not None

    # Existing row that already has a status: neither the status nor its time moves.
    await db.upsert_candidate("9103", ID_SOURCE_MESSAGE, ACCOUNT, status="interested")
    await _execute(
        path,
        "UPDATE candidates SET status_updated_at = ? WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        (KNOWN_OLD, "9103", ID_SOURCE_MESSAGE, ACCOUNT),
    )
    await db.set_status_if_unset("9103", ID_SOURCE_MESSAGE, ACCOUNT, "contacted")
    row = await _row(path, "9103", ID_SOURCE_MESSAGE)
    assert row["status"] == "interested"
    assert row["status_updated_at"] == KNOWN_OLD

    # Two calls at once on one absent row: neither raises, exactly one row.
    await asyncio.gather(
        db.set_status_if_unset("9104", ID_SOURCE_MESSAGE, ACCOUNT, "contacted"),
        db.set_status_if_unset("9104", ID_SOURCE_MESSAGE, ACCOUNT, "contacted"),
    )
    rows = await _select(
        path,
        "SELECT status FROM candidates WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        ("9104", ID_SOURCE_MESSAGE, ACCOUNT),
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "contacted"

    # Racing an explicit interested write, many times: the explicit status
    # always wins, whichever statement lands first. The gather order alternates
    # so both interleavings are exercised.
    for i in range(20):
        candidate_id = f"92{i:02d}"
        explicit = db.upsert_candidate(candidate_id, ID_SOURCE_MESSAGE, ACCOUNT, status="interested")
        guarded = db.set_status_if_unset(candidate_id, ID_SOURCE_MESSAGE, ACCOUNT, "contacted")
        if i % 2 == 0:
            await asyncio.gather(explicit, guarded)
        else:
            await asyncio.gather(guarded, explicit)
        row = await _row(path, candidate_id, ID_SOURCE_MESSAGE)
        assert row["status"] == "interested", f"iteration {i}"


# ── Migration: the status-time column and the id-link table ─────────────────

@pytest.mark.asyncio
async def test_init_adds_status_time_column_and_keeps_rows_with_unknown_times(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    db_path = tmp_path / "pre_status_time.db"
    await _make_pre_status_time_db(db_path, _PRE_STATUS_TIME_ROWS)
    before = await _select(
        db_path,
        "SELECT candidate_id, id_source, account_email, name, status, created_at FROM candidates "
        "ORDER BY candidate_id",
    )
    assert len(before) == 3

    caplog.clear()
    async with _opened(db_path):
        pass

    after = await _select(
        db_path,
        "SELECT candidate_id, id_source, account_email, name, status, created_at FROM candidates "
        "ORDER BY candidate_id",
    )
    assert after == before
    times = await _select(db_path, "SELECT status_updated_at FROM candidates")
    assert len(times) == 3
    assert all(row["status_updated_at"] is None for row in times)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings, "the migration must write a warning log before it alters the table"
    infos = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    status_infos = [message for message in infos if "status_updated_at" in message]
    assert len(status_infos) == 1, "the migration must write exactly one info log that mentions status_updated_at"
    assert re.search(r"(?<!\d)2(?!\d)", status_infos[0]), (
        "that info log must report the number of status-bearing rows (2)"
    )


@pytest.mark.asyncio
async def test_init_migrates_oldest_schema_to_unknown_source_and_null_times(tmp_path):
    db_path = tmp_path / "oldest.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(OLD_SCHEMA_DDL)
        await conn.executemany(
            "INSERT INTO candidates (candidate_id, account_email, name, status) VALUES (?, ?, ?, ?)",
            [("1001", ACCOUNT, "Synthetic A", "contacted"), ("1002", ACCOUNT, "Synthetic B", None)],
        )
        await conn.commit()

    async with _opened(db_path):
        pass

    rows = await _select(
        db_path,
        "SELECT candidate_id, id_source, status, status_updated_at FROM candidates ORDER BY candidate_id",
    )
    assert len(rows) == 2
    assert {row["id_source"] for row in rows} == {ID_SOURCE_UNKNOWN}
    assert all(row["status_updated_at"] is None for row in rows)
    assert rows[0]["status"] == "contacted"


@pytest.mark.asyncio
async def test_second_init_migrates_nothing_and_fresh_schema_matches_migrated(tmp_path, caplog):
    db_path = tmp_path / "twice.db"
    await _make_pre_status_time_db(db_path, _PRE_STATUS_TIME_ROWS)

    async with _opened(db_path):
        pass
    first = await _snapshot_all(db_path)

    caplog.set_level(logging.INFO)
    caplog.clear()
    async with _opened(db_path):
        pass
    second = await _snapshot_all(db_path)

    assert second == first
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert not [record for record in caplog.records if "status_updated_at" in record.getMessage()]

    fresh_path = tmp_path / "fresh.db"
    async with _opened(fresh_path):
        pass
    assert set(await _columns(fresh_path, "candidates")) == set(await _columns(db_path, "candidates"))


@pytest.mark.asyncio
async def test_id_link_table_exists_on_fresh_and_previously_migrated_databases(tmp_path):
    fresh_path = tmp_path / "fresh.db"
    async with _opened(fresh_path):
        pass
    assert await _has_table(fresh_path, "candidate_id_links")

    old_path = tmp_path / "old.db"
    await _make_pre_status_time_db(old_path, _PRE_STATUS_TIME_ROWS)
    async with _opened(old_path) as database:
        await database.record_id_links(ACCOUNT, [("9001", "9002")], dbmod.LINK_SOURCE_RESUME_ROW)
    assert await _has_table(old_path, "candidate_id_links")
    links = await _select(old_path, "SELECT id_no, p_id FROM candidate_id_links")
    assert [(row["id_no"], row["p_id"]) for row in links] == [("9001", "9002")]


# ── record_id_links ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_id_links_keeps_one_row_per_pair_and_rejects_bad_input(db):
    path = db.path

    # A repeated pair within one call produces one row.
    await db.record_id_links(
        ACCOUNT,
        [("1001", "2001"), ("1001", "2001"), ("1002", "2002")],
        dbmod.LINK_SOURCE_RESUME_ROW,
    )
    rows = await _select(
        path,
        "SELECT id_no, p_id FROM candidate_id_links WHERE account_email = ? ORDER BY id_no",
        (ACCOUNT,),
    )
    assert [(row["id_no"], row["p_id"]) for row in rows] == [("1001", "2001"), ("1002", "2002")]

    # An empty list leaves the database exactly as it was.
    before = await _snapshot_all(path)
    await db.record_id_links(ACCOUNT, [], dbmod.LINK_SOURCE_RESUME_ROW)
    assert await _snapshot_all(path) == before

    # A source outside the known set is a programming error.
    with pytest.raises(ValueError):
        await db.record_id_links(ACCOUNT, [("1003", "2003")], "other")
    assert await _select(path, "SELECT * FROM candidate_id_links WHERE id_no = ?", ("1003",)) == []


# ── find_candidate_records ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_candidate_records_shape_and_linked_id_keys(db):
    await db.upsert_candidate("6001", ID_SOURCE_RESUME, ACCOUNT, status="interested")
    await db.record_id_links(ACCOUNT, [("6001", "7001")], dbmod.LINK_SOURCE_RESUME_ROW)
    await db.upsert_candidate("7001", ID_SOURCE_MESSAGE, ACCOUNT, status="contacted")

    rows = await db.find_candidate_records("6001", ID_SOURCE_RESUME, ACCOUNT)
    assert rows
    for row in rows:
        assert "matched_by" in row
        assert "linked_ids" in row
        for item in row["linked_ids"]:
            assert set(item) == {"candidate_id", "id_source", "status", "status_updated_at"}

    # Links without any candidates row: nothing to return, regardless of the link table.
    await db.record_id_links(ACCOUNT, [("6100", "7100")], dbmod.LINK_SOURCE_RESUME_ROW)
    assert await db.find_candidate_records("6100", ID_SOURCE_RESUME, ACCOUNT) == []


@pytest.mark.asyncio
async def test_find_candidate_records_orders_same_id_then_unknown_then_linked(db):
    path = db.path
    await db.record_id_links(ACCOUNT, [("1001", "2001"), ("1001", "2002"), ("1002", "2001")],
                             dbmod.LINK_SOURCE_RESUME_ROW)
    # Written deliberately out of the order the result must come back in.
    await _insert_candidate(path, "1001", ID_SOURCE_RESUME, ACCOUNT, status="contacted")
    await _insert_candidate(path, "1001", ID_SOURCE_UNKNOWN, ACCOUNT, status="contacted")
    await _insert_candidate(path, "2001", ID_SOURCE_MESSAGE, ACCOUNT, status="contacted")
    await _insert_candidate(path, "2002", ID_SOURCE_MESSAGE, ACCOUNT, status="contacted")
    await _insert_candidate(path, "1002", ID_SOURCE_RESUME, ACCOUNT, status="contacted")
    await _insert_candidate(path, "2002", ID_SOURCE_UNKNOWN, ACCOUNT, status="contacted")

    rows = await db.find_candidate_records("1001", ID_SOURCE_RESUME, ACCOUNT)
    got = [(row["id_source"], row["candidate_id"], row["matched_by"]) for row in rows]
    assert got == [
        (ID_SOURCE_RESUME, "1001", "same_id"),
        (ID_SOURCE_UNKNOWN, "1001", "same_id_unknown_source"),
        (ID_SOURCE_MESSAGE, "2001", "linked"),
        (ID_SOURCE_MESSAGE, "2002", "linked"),
        (ID_SOURCE_RESUME, "1002", "linked"),
        # An unknown-source row whose id string equals a linked id is "linked",
        # not "same id": only the start string earns the same-id-unknown label.
        (ID_SOURCE_UNKNOWN, "2002", "linked"),
    ]


@pytest.mark.asyncio
async def test_find_candidate_records_terminates_on_cycles_and_stays_in_one_account(db):
    path = db.path
    for id_no, p_id in [("1001", "2001"), ("1002", "2001"), ("1002", "2002"), ("1001", "2002")]:
        await db.record_id_links(ACCOUNT, [(id_no, p_id)], dbmod.LINK_SOURCE_RESUME_ROW)
    await db.record_id_links(OTHER_ACCOUNT, [("1003", "2002")], dbmod.LINK_SOURCE_RESUME_ROW)

    await _insert_candidate(path, "2002", ID_SOURCE_MESSAGE, ACCOUNT, status="interested")
    await _insert_candidate(path, "1003", ID_SOURCE_RESUME, ACCOUNT, status="contacted")
    await _insert_candidate(path, "2999", ID_SOURCE_MESSAGE, ACCOUNT, status="contacted")

    # The timeout is only a safety net: a walk that never terminates must
    # fail this test instead of hanging the run.
    from_resume = await asyncio.wait_for(
        db.find_candidate_records("1001", ID_SOURCE_RESUME, ACCOUNT), timeout=10
    )
    from_message = await asyncio.wait_for(
        db.find_candidate_records("2002", ID_SOURCE_MESSAGE, ACCOUNT), timeout=10
    )

    resume_ids = {(row["id_source"], row["candidate_id"]) for row in from_resume}
    assert (ID_SOURCE_MESSAGE, "2002") in resume_ids
    assert (ID_SOURCE_RESUME, "1003") not in resume_ids

    message_ids = {(row["id_source"], row["candidate_id"]) for row in from_message}
    assert (ID_SOURCE_RESUME, "1003") not in message_ids

    assert all(candidate_id != "2999" for _, candidate_id in resume_ids | message_ids)


@pytest.mark.asyncio
async def test_linked_ids_carry_the_other_side_status_and_time_and_direct_pairs_only(db):
    path = db.path
    u1 = "2026-11-12 03:00:00"
    u2 = "2026-11-11 04:00:00"
    await db.record_id_links(ACCOUNT, [("1001", "2001"), ("1001", "2002"), ("1002", "2001")],
                             dbmod.LINK_SOURCE_RESUME_ROW)
    await _insert_candidate(path, "2001", ID_SOURCE_MESSAGE, ACCOUNT, status="scheduled", status_updated_at=u1)
    await _insert_candidate(path, "1001", ID_SOURCE_RESUME, ACCOUNT, status="interested", status_updated_at=u2)
    await _insert_candidate(path, "1001", ID_SOURCE_UNKNOWN, ACCOUNT)

    rows = await db.find_candidate_records("1001", ID_SOURCE_RESUME, ACCOUNT)
    by_key = {(row["id_source"], row["candidate_id"]): row for row in rows}

    assert by_key[(ID_SOURCE_RESUME, "1001")]["linked_ids"] == [
        {"candidate_id": "2001", "id_source": ID_SOURCE_MESSAGE, "status": "scheduled", "status_updated_at": u1},
        {"candidate_id": "2002", "id_source": ID_SOURCE_MESSAGE, "status": None, "status_updated_at": None},
    ]
    assert by_key[(ID_SOURCE_MESSAGE, "2001")]["linked_ids"] == [
        {"candidate_id": "1001", "id_source": ID_SOURCE_RESUME, "status": "interested", "status_updated_at": u2},
        {"candidate_id": "1002", "id_source": ID_SOURCE_RESUME, "status": None, "status_updated_at": None},
    ]
    assert by_key[(ID_SOURCE_UNKNOWN, "1001")]["linked_ids"] == []

    listed, _total = await db.list_candidates(ACCOUNT, None, limit=50, offset=0)
    listed_by_key = {(row["id_source"], row["candidate_id"]): row for row in listed}
    assert listed_by_key[(ID_SOURCE_RESUME, "1001")]["linked_ids"] == by_key[(ID_SOURCE_RESUME, "1001")]["linked_ids"]
    assert listed_by_key[(ID_SOURCE_MESSAGE, "2001")]["linked_ids"] == by_key[(ID_SOURCE_MESSAGE, "2001")]["linked_ids"]


# ── list_candidates ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_candidates_applies_limit_and_offset_and_total_ignores_them(db):
    path = db.path
    times = ["2026-11-10 01:00:00", "2026-11-11 01:00:00", "2026-11-12 01:00:00", "2026-11-13 01:00:00"]
    for i, stamp in enumerate(times):
        await _insert_candidate(path, f"80{i}1", ID_SOURCE_RESUME, ACCOUNT, status="interested",
                                status_updated_at=stamp)

    # Newest status time first: 8031, 8021, 8011, 8001. Offset 1 skips the first.
    rows, total = await db.list_candidates(ACCOUNT, "interested", limit=2, offset=1)
    assert [row["candidate_id"] for row in rows] == ["8021", "8011"]
    assert total == 4

    all_rows, all_total = await db.list_candidates(ACCOUNT, "interested", limit=50, offset=0)
    assert [row["candidate_id"] for row in all_rows] == ["8031", "8021", "8011", "8001"]
    assert all_total == 4


@pytest.mark.asyncio
async def test_list_candidates_filters_by_status_and_account_and_includes_unknown_rows(db):
    path = db.path
    for candidate_id in ["6401", "6402", "6403"]:
        await _insert_candidate(path, candidate_id, ID_SOURCE_RESUME, ACCOUNT, status="interested")
    for candidate_id in ["6404", "6405"]:
        await _insert_candidate(path, candidate_id, ID_SOURCE_MESSAGE, ACCOUNT, status="contacted")
    await _insert_candidate(path, "6406", ID_SOURCE_RESUME, ACCOUNT, name="Synthetic name only")
    await _insert_candidate(path, "6407", ID_SOURCE_UNKNOWN, ACCOUNT, status="contacted")
    await _insert_candidate(path, "6408", ID_SOURCE_RESUME, OTHER_ACCOUNT, status="interested")

    interested, interested_total = await db.list_candidates(ACCOUNT, "interested", limit=50, offset=0)
    assert len(interested) == 3
    assert interested_total == 3

    every, every_total = await db.list_candidates(ACCOUNT, None, limit=50, offset=0)
    assert len(every) == 6
    assert every_total == 6
    keys = {(row["id_source"], row["candidate_id"]) for row in every}
    assert (ID_SOURCE_UNKNOWN, "6407") in keys
    assert (ID_SOURCE_RESUME, "6406") not in keys
    assert (ID_SOURCE_RESUME, "6408") not in keys


@pytest.mark.asyncio
async def test_list_candidates_orders_by_status_time_desc_nulls_last_then_tiebreakers(db):
    path = db.path
    t1 = "2026-11-11 03:00:00"
    t2 = "2026-11-12 03:00:00"
    t3 = "2026-11-13 03:00:00"
    c1 = "2026-01-01 00:00:00"
    c2 = "2026-01-02 00:00:00"
    c3 = "2026-01-03 00:00:00"
    await _insert_candidate(path, "3001", ID_SOURCE_RESUME, ACCOUNT, status="interested",
                            created_at=c1, status_updated_at=t3)                      # a
    await _insert_candidate(path, "3002", ID_SOURCE_RESUME, ACCOUNT, status="interested",
                            created_at=c1, status_updated_at=t2)                      # b
    await _insert_candidate(path, "3003", ID_SOURCE_RESUME, ACCOUNT, status="interested",
                            created_at=c2, status_updated_at=t2)                      # c
    await _insert_candidate(path, "3004", ID_SOURCE_RESUME, ACCOUNT, status="interested",
                            created_at=c1, status_updated_at=t1)                      # d
    await _insert_candidate(path, "3004", ID_SOURCE_MESSAGE, ACCOUNT, status="interested",
                            created_at=c1, status_updated_at=t1)                      # e
    await _insert_candidate(path, "3005", ID_SOURCE_MESSAGE, ACCOUNT, status="interested",
                            created_at=c1, status_updated_at=t1)                      # f
    await _insert_candidate(path, "3006", ID_SOURCE_RESUME, ACCOUNT, status="interested",
                            created_at=c3, status_updated_at=None)                    # g

    expected = [
        (ID_SOURCE_RESUME, "3001"),   # a
        (ID_SOURCE_RESUME, "3003"),   # c: same time as b, newer created_at
        (ID_SOURCE_RESUME, "3002"),   # b
        (ID_SOURCE_MESSAGE, "3004"),  # e: same time and created_at as d and f; message before resume
        (ID_SOURCE_MESSAGE, "3005"),  # f
        (ID_SOURCE_RESUME, "3004"),   # d
        (ID_SOURCE_RESUME, "3006"),   # g: NULL status time sorts last even though created_at is newest
    ]

    first, _ = await db.list_candidates(ACCOUNT, None, limit=50, offset=0)
    assert [(row["id_source"], row["candidate_id"]) for row in first] == expected
    second, _ = await db.list_candidates(ACCOUNT, None, limit=50, offset=0)
    assert [(row["id_source"], row["candidate_id"]) for row in second] == expected

