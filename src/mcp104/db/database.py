import logging

import aiosqlite
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("104-mcp.database")

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
UTC_TZ = ZoneInfo("UTC")

# The storage layer's column is named account_email; since 2026-09-04 the
# value really is the 104 login e-mail (SessionInfo.account_label, fetched from
# 104 after login). Every public method below takes it as `account_label`.


ID_SOURCE_RESUME = "resume"      # from a search_resumes/get_resume_detail card id
ID_SOURCE_MESSAGE = "message"    # from messaging's own candidate id (104's pId)
ID_SOURCE_UNKNOWN = "unknown"    # migrated row predating id_source — provenance genuinely unrecoverable


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def init(self):
        """Open the database and create/migrate its tables. Rows are keyed on
        the 104 login e-mail 104 itself reports for the session (the
        account_email column), so one store can hold several accounts' rows
        without any operator-supplied label — the former account-label
        isolation guard was removed with the label on 2026-09-04."""
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # sent_log's id_source carries the same DEFAULT here as the
        # migration path's ALTER TABLE gives it (both ID_SOURCE_MESSAGE) —
        # log_sent always passes the value explicitly today, so this
        # doesn't change behavior, but a fresh-create and a migrated
        # deployment must not silently diverge in what the column allows.
        await self._conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id    TEXT NOT NULL,
                id_source       TEXT NOT NULL,
                account_email   TEXT NOT NULL,
                name            TEXT,
                status          TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (candidate_id, id_source, account_email)
            );
            CREATE TABLE IF NOT EXISTS sent_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                account_email   TEXT NOT NULL,
                candidate_id    TEXT,
                id_source       TEXT NOT NULL DEFAULT '{ID_SOURCE_MESSAGE}',
                sent_at         DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await self._conn.commit()
        # CREATE TABLE IF NOT EXISTS is a no-op against a database that
        # already has candidates/sent_log from before id_source existed —
        # init() would otherwise report success and every subsequent read/
        # write raises OperationalError: no such column: id_source. Bring
        # such a database forward explicitly.
        await self._migrate_id_source_column()

    async def _column_exists(self, table: str, column: str) -> bool:
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in await cursor.fetchall()]
        return column in columns

    async def _migrate_id_source_column(self):
        """Add the id_source column to a pre-id_source database. Always
        logged — never a silent no-op when a migration actually runs."""
        if not await self._column_exists("sent_log", "id_source"):
            log.warning("DB migration: sent_log has no id_source column, adding it")
            # Before id_source existed, sent_log was written ONLY by
            # send_message — every pre-migration row genuinely IS from the
            # message id space, not a guessed default.
            await self._conn.execute(
                f"ALTER TABLE sent_log ADD COLUMN id_source TEXT NOT NULL DEFAULT '{ID_SOURCE_MESSAGE}'"
            )
            await self._conn.commit()
            log.info("DB migration: sent_log migrated")

        if not await self._column_exists("candidates", "id_source"):
            log.warning("DB migration: candidates has no id_source column, migrating (create/copy/drop/rename)")
            # SQLite cannot ADD COLUMN into an existing PRIMARY KEY, so this
            # needs the create-new/copy/drop/rename dance regardless of row
            # count — it is correct whether candidates is empty or not.
            # Unlike sent_log, candidates was written from BOTH the resume
            # and message id spaces (get_resume_detail, and
            # tools/status.py's Agent-supplied candidate_id) before
            # id_source existed, so provenance for existing rows is
            # genuinely unrecoverable — ID_SOURCE_UNKNOWN records that
            # honestly instead of guessing "resume" or "message".
            #
            # Re-runnability after a crash: CREATE TABLE (DDL) commits on
            # its own the instant it runs, but the INSERT/DROP/RENAME that
            # follow share ONE implicit transaction that only becomes
            # durable at the commit() below (empirically confirmed: DROP/
            # RENAME do not auto-commit a pending DML transaction the way
            # CREATE does). So the only state a crash can ever leave behind
            # is "candidates_new exists and is empty, candidates (old) is
            # still fully intact" — never a partially-copied or missing
            # candidates. Dropping any such leftover before recreating it
            # is therefore always safe, and makes this method idempotent
            # across restarts instead of dying on "table already exists".
            await self._conn.execute("DROP TABLE IF EXISTS candidates_new")
            await self._conn.execute("""
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
            cursor = await self._conn.execute("SELECT COUNT(*) FROM candidates")
            row_count = (await cursor.fetchone())[0]
            await self._conn.execute(
                "INSERT INTO candidates_new "
                "(candidate_id, id_source, account_email, name, status, created_at) "
                "SELECT candidate_id, ?, account_email, name, status, created_at FROM candidates",
                [ID_SOURCE_UNKNOWN],
            )
            await self._conn.execute("DROP TABLE candidates")
            await self._conn.execute("ALTER TABLE candidates_new RENAME TO candidates")
            await self._conn.commit()
            # Row count matters operationally: every migrated row is
            # tagged ID_SOURCE_UNKNOWN, which VALID_ID_SOURCES deliberately
            # excludes from what the Agent can query — so any row counted
            # here is invisible to check_already_contacted /
            # update_candidate_status from this point on (see CLAUDE.md).
            log.info("DB migration: candidates migrated (%d pre-existing row(s) tagged %r)", row_count, ID_SOURCE_UNKNOWN)

    async def close(self):
        """Idempotent: safe to call more than once, and safe to call after a
        failed init() (e.g. Database(path) constructed, then await
        aiosqlite.connect() succeeded but a later init() step raised before
        this Database was ever handed off) — self._conn is set to None once
        closed so a second close() is a no-op rather than an error on an
        already-closed connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def upsert_candidate(self, candidate_id: str, id_source: str, account_label: str, **fields):
        existing = await self.get_candidate(candidate_id, id_source, account_label)
        if existing:
            if not fields:
                return  # nothing to update
            sets = ", ".join(f"{k} = ?" for k in fields)
            await self._conn.execute(
                f"UPDATE candidates SET {sets} WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
                [*fields.values(), candidate_id, id_source, account_label],
            )
        else:
            cols = "candidate_id, id_source, account_email" + (", " + ", ".join(fields) if fields else "")
            placeholders = "?, ?, ?" + (", " + ", ".join("?" for _ in fields) if fields else "")
            await self._conn.execute(
                f"INSERT INTO candidates ({cols}) VALUES ({placeholders})",
                [candidate_id, id_source, account_label, *fields.values()],
            )
        await self._conn.commit()

    async def get_candidate(self, candidate_id: str, id_source: str, account_label: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
            [candidate_id, id_source, account_label],
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def log_sent(self, account_label: str, candidate_id: str, id_source: str):
        await self._conn.execute(
            "INSERT INTO sent_log (account_email, candidate_id, id_source) VALUES (?, ?, ?)",
            [account_label, candidate_id, id_source],
        )
        await self._conn.commit()

    async def get_daily_sent_count(self, account_label: str) -> int:
        # sent_at is stored as SQLite's CURRENT_TIMESTAMP default, which is
        # UTC ("YYYY-MM-DD HH:MM:SS", no offset). date.today() is process-
        # local and no TZ is set in the deployment, so comparing it directly
        # against sent_at silently used UTC on both sides — the cap reset
        # at 08:00 Taipei instead of local midnight. Compute the Taipei day
        # start explicitly and convert to UTC for the comparison, keeping
        # the column itself UTC for consistency with candidates.created_at.
        taipei_day_start = datetime.now(TAIPEI_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        utc_day_start = taipei_day_start.astimezone(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM sent_log WHERE account_email = ? AND sent_at >= ?",
            [account_label, utc_day_start],
        )
        row = await cursor.fetchone()
        return row[0]
