import logging
from collections.abc import Iterable

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

# Where a candidate_id_links pair was observed. Each is a place 104 itself hands over
# both of a person's ids on one piece of data, with its own strength of evidence
# (docs/104-site-facts.md §8.12, §8.14-2, §8.17); the source is stored so one kind of
# link can be cleared on its own if it is later measured to be unreliable.
LINK_SOURCE_RESUME_ROW = "resume_row"        # search_resumes / list_recommended_resumes / list_matched_resumes rows
LINK_SOURCE_APPLICANT_ROW = "applicant_row"  # list_applicants rows
LINK_SOURCE_RESUME_DETAIL = "resume_detail"  # get_resume_detail's resume
LINK_SOURCE_EVENT_BRIDGE = "event_bridge"    # the event tools' reverse bridge (pId -> idNo)
LINK_SOURCES = frozenset({
    LINK_SOURCE_RESUME_ROW,
    LINK_SOURCE_APPLICANT_ROW,
    LINK_SOURCE_RESUME_DETAIL,
    LINK_SOURCE_EVENT_BRIDGE,
})

# How find_candidate_records reached a row.
MATCHED_SAME_ID = "same_id"                          # the id and source the caller gave
MATCHED_UNKNOWN_SOURCE = "same_id_unknown_source"    # same id string, row predates id_source
MATCHED_LINK = "linked"                              # reached through a recorded link

_MATCH_ORDER = {MATCHED_SAME_ID: 0, MATCHED_UNKNOWN_SOURCE: 1, MATCHED_LINK: 2}

# Stay well under SQLite's bound-variable limit when a query takes an id list.
_IN_CHUNK = 500


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
                status_updated_at DATETIME,
                PRIMARY KEY (candidate_id, id_source, account_email)
            );
            CREATE TABLE IF NOT EXISTS candidate_id_links (
                account_email   TEXT NOT NULL,
                id_no           TEXT NOT NULL,
                p_id            TEXT NOT NULL,
                source          TEXT NOT NULL,
                first_seen_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account_email, id_no, p_id)
            );
            CREATE INDEX IF NOT EXISTS candidate_id_links_by_p_id ON candidate_id_links (account_email, p_id);
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
        await self._migrate_status_updated_at_column()

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
            # tagged ID_SOURCE_UNKNOWN. The status readers find such a row by its
            # id string under either source and label it as source-unknown;
            # no writer touches it.
            log.info("DB migration: candidates migrated (%d pre-existing row(s) tagged %r)", row_count, ID_SOURCE_UNKNOWN)

    async def _migrate_status_updated_at_column(self):
        """Add candidates.status_updated_at to a database that predates it. Runs after
        _migrate_id_source_column, so a database older than both ends up with both
        columns. Existing rows keep NULL: when their status was written is not
        recoverable, and filling in created_at would turn "no earlier than" into "exactly".
        The column is added without a default because SQLite's ADD COLUMN rejects a
        non-constant default such as CURRENT_TIMESTAMP; the two status-writing methods
        set it explicitly, so a freshly created table and a migrated one behave alike.
        A single ALTER commits on its own, so an interrupted run is simply repeated:
        whether the column exists is the only state there is."""
        if await self._column_exists("candidates", "status_updated_at"):
            return
        log.warning("DB migration: candidates has no status_updated_at column, adding it")
        await self._conn.execute("ALTER TABLE candidates ADD COLUMN status_updated_at DATETIME")
        await self._conn.commit()
        cursor = await self._conn.execute("SELECT COUNT(*) FROM candidates WHERE status IS NOT NULL")
        with_status = (await cursor.fetchone())[0]
        log.info(
            "DB migration: candidates.status_updated_at added (%d row(s) carry a status whose time is unknown)",
            with_status,
        )

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
        """One statement per write: the primary key guarantees a single row per
        (candidate_id, id_source, account_email), and two concurrent writers cannot
        collide on it the way a read-then-insert would. Only the fields given are
        updated. When `status` is among them, status_updated_at is set to now in the
        same statement — even if the value is unchanged, because the write itself is
        the event the time records."""
        if not fields:
            await self._conn.execute(
                "INSERT OR IGNORE INTO candidates (candidate_id, id_source, account_email) VALUES (?, ?, ?)",
                [candidate_id, id_source, account_label],
            )
            await self._conn.commit()
            return

        columns = list(fields)
        insert_cols = ["candidate_id", "id_source", "account_email", *columns]
        insert_vals = ["?"] * len(insert_cols)
        updates = [f"{c} = excluded.{c}" for c in columns]
        if "status" in fields:
            insert_cols.append("status_updated_at")
            insert_vals.append("CURRENT_TIMESTAMP")
            updates.append("status_updated_at = CURRENT_TIMESTAMP")
        await self._conn.execute(
            f"INSERT INTO candidates ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)}) "
            f"ON CONFLICT(candidate_id, id_source, account_email) DO UPDATE SET {', '.join(updates)}",
            [candidate_id, id_source, account_label, *fields.values()],
        )
        await self._conn.commit()

    async def set_status_if_unset(self, candidate_id: str, id_source: str, account_label: str, status: str):
        """Write `status` only when the row has none. "Has none" is decided by the one
        statement itself (COALESCE on the stored value, which an UPDATE's expressions read
        from the pre-update row), so two near-simultaneous writers cannot both read NULL and
        have the later one overwrite a status the earlier just wrote. status_updated_at moves
        only when the status does. Columns other than status are not touched."""
        await self._conn.execute(
            "INSERT INTO candidates (candidate_id, id_source, account_email, status, status_updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(candidate_id, id_source, account_email) DO UPDATE SET "
            "status = COALESCE(candidates.status, excluded.status), "
            "status_updated_at = CASE WHEN candidates.status IS NULL "
            "THEN CURRENT_TIMESTAMP ELSE candidates.status_updated_at END",
            [candidate_id, id_source, account_label, status],
        )
        await self._conn.commit()

    async def get_candidate(self, candidate_id: str, id_source: str, account_label: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
            [candidate_id, id_source, account_label],
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def record_id_links(
        self, account_label: str, pairs: Iterable[tuple[str, str]], source: str,
    ) -> None:
        """Remember pairs 104 handed over as (idNo, pId) of one person. A pair already
        recorded is left exactly as it was — its first source and first_seen_at are
        kept. `source` is checked here rather than by a CHECK on the table: SQLite cannot
        alter a CHECK on an existing table, so a fifth source added later would be refused
        by every already-created database with an error that only reaches the log."""
        if source not in LINK_SOURCES:
            raise ValueError(f"unknown id-link source {source!r}; expected one of {sorted(LINK_SOURCES)}")
        rows = [(account_label, id_no, p_id, source) for id_no, p_id in pairs]
        if not rows:
            return
        await self._conn.executemany(
            "INSERT OR IGNORE INTO candidate_id_links (account_email, id_no, p_id, source) VALUES (?, ?, ?, ?)",
            rows,
        )
        await self._conn.commit()

    async def _query_in(self, sql: str, args: list, values: Iterable[str]) -> list:
        """Run `sql` once per chunk of `values`; the single `{in}` marker in it becomes
        the chunk's placeholders. Rows of all chunks are concatenated."""
        values = list(values)
        found: list = []
        for start in range(0, len(values), _IN_CHUNK):
            chunk = values[start:start + _IN_CHUNK]
            cursor = await self._conn.execute(
                sql.replace("{in}", ", ".join("?" for _ in chunk)), [*args, *chunk]
            )
            found.extend(await cursor.fetchall())
        return found

    async def _attach_linked_ids(self, rows: list[dict], account_label: str) -> None:
        """Set rows[i]["linked_ids"]: for each row, the ids in the other space that the
        link table pairs with it directly, each with the status and status time recorded
        under that other id (None when it has no row). Directly paired only — the full
        connected set is find_candidate_records' to walk. The one place that decides what
        a linked_ids entry looks like."""
        resume_ids = {r["candidate_id"] for r in rows if r["id_source"] == ID_SOURCE_RESUME}
        message_ids = {r["candidate_id"] for r in rows if r["id_source"] == ID_SOURCE_MESSAGE}

        message_for_resume: dict[str, set[str]] = {}
        for link in await self._query_in(
            "SELECT id_no, p_id FROM candidate_id_links WHERE account_email = ? AND id_no IN ({in})",
            [account_label], resume_ids,
        ):
            message_for_resume.setdefault(link["id_no"], set()).add(link["p_id"])
        resume_for_message: dict[str, set[str]] = {}
        for link in await self._query_in(
            "SELECT id_no, p_id FROM candidate_id_links WHERE account_email = ? AND p_id IN ({in})",
            [account_label], message_ids,
        ):
            resume_for_message.setdefault(link["p_id"], set()).add(link["id_no"])

        wanted_messages = {p for ids in message_for_resume.values() for p in ids}
        wanted_resumes = {i for ids in resume_for_message.values() for i in ids}
        state: dict[tuple[str, str], tuple] = {}
        for source, wanted in ((ID_SOURCE_MESSAGE, wanted_messages), (ID_SOURCE_RESUME, wanted_resumes)):
            for row in await self._query_in(
                "SELECT candidate_id, status, status_updated_at FROM candidates "
                "WHERE account_email = ? AND id_source = ? AND candidate_id IN ({in})",
                [account_label, source], wanted,
            ):
                state[(source, row["candidate_id"])] = (row["status"], row["status_updated_at"])

        def entries(other_source: str, ids: set[str]) -> list[dict]:
            return [
                {
                    "candidate_id": other_id,
                    "id_source": other_source,
                    "status": state.get((other_source, other_id), (None, None))[0],
                    "status_updated_at": state.get((other_source, other_id), (None, None))[1],
                }
                for other_id in sorted(ids)
            ]

        for row in rows:
            if row["id_source"] == ID_SOURCE_RESUME:
                row["linked_ids"] = entries(ID_SOURCE_MESSAGE, message_for_resume.get(row["candidate_id"], set()))
            elif row["id_source"] == ID_SOURCE_MESSAGE:
                row["linked_ids"] = entries(ID_SOURCE_RESUME, resume_for_message.get(row["candidate_id"], set()))
            else:
                row["linked_ids"] = []

    async def find_candidate_records(
        self, candidate_id: str, id_source: str, account_label: str,
    ) -> list[dict]:
        """Every candidates row that belongs to the person (candidate_id, id_source)
        names, each with "matched_by" and "linked_ids". Read-only.

        The person is the set of ids reachable from the starting id through this
        account's candidate_id_links (a resume id pairs with message ids, which pair
        with other resume ids, and so on until nothing new turns up). The rows are the
        (id, space) rows of every id in that set, plus any id_source = "unknown" row
        whose id string is in the set. Rows come back as same_id (the starting row),
        same_id_unknown_source (an unknown row with the starting id string), then
        linked (everything else); within a group by (id_source, candidate_id)."""
        resume_ids: set[str] = set()
        message_ids: set[str] = set()
        frontier_resume: set[str] = set()
        frontier_message: set[str] = set()
        if id_source == ID_SOURCE_RESUME:
            resume_ids.add(candidate_id)
            frontier_resume.add(candidate_id)
        elif id_source == ID_SOURCE_MESSAGE:
            message_ids.add(candidate_id)
            frontier_message.add(candidate_id)

        while frontier_resume or frontier_message:
            next_message = {
                link["p_id"] for link in await self._query_in(
                    "SELECT p_id FROM candidate_id_links WHERE account_email = ? AND id_no IN ({in})",
                    [account_label], frontier_resume,
                )
            } - message_ids
            next_resume = {
                link["id_no"] for link in await self._query_in(
                    "SELECT id_no FROM candidate_id_links WHERE account_email = ? AND p_id IN ({in})",
                    [account_label], frontier_message,
                )
            } - resume_ids
            message_ids |= next_message
            resume_ids |= next_resume
            frontier_message, frontier_resume = next_message, next_resume

        found: list[dict] = []
        for source, ids in (
            (ID_SOURCE_RESUME, resume_ids),
            (ID_SOURCE_MESSAGE, message_ids),
            (ID_SOURCE_UNKNOWN, resume_ids | message_ids | {candidate_id}),
        ):
            for row in await self._query_in(
                "SELECT * FROM candidates WHERE account_email = ? AND id_source = ? AND candidate_id IN ({in})",
                [account_label, source], ids,
            ):
                found.append(dict(row))

        for record in found:
            if (record["id_source"], record["candidate_id"]) == (id_source, candidate_id):
                record["matched_by"] = MATCHED_SAME_ID
            elif record["id_source"] == ID_SOURCE_UNKNOWN and record["candidate_id"] == candidate_id:
                record["matched_by"] = MATCHED_UNKNOWN_SOURCE
            else:
                record["matched_by"] = MATCHED_LINK
        found.sort(key=lambda r: (_MATCH_ORDER[r["matched_by"]], r["id_source"], r["candidate_id"]))
        await self._attach_linked_ids(found, account_label)
        return found

    async def list_candidates(
        self, account_label: str, status: str | None, *, limit: int, offset: int,
    ) -> tuple[list[dict], int]:
        """One page of this account's rows that carry a status — exactly `status` when
        given, any status when None — including id_source = "unknown" rows. Newest status
        time first, rows whose status time is unknown last, ties broken by created_at
        (newest first) then (id_source, candidate_id) so the order is stable between calls.
        Returns (rows with "linked_ids", total count ignoring limit/offset). Read-only."""
        if status is None:
            condition, args = "status IS NOT NULL", [account_label]
        else:
            condition, args = "status = ?", [account_label, status]
        cursor = await self._conn.execute(
            f"SELECT COUNT(*) FROM candidates WHERE account_email = ? AND {condition}", args
        )
        total = (await cursor.fetchone())[0]
        cursor = await self._conn.execute(
            f"SELECT * FROM candidates WHERE account_email = ? AND {condition} "
            "ORDER BY status_updated_at IS NULL, status_updated_at DESC, created_at DESC, id_source, candidate_id "
            "LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await self._attach_linked_ids(rows, account_label)
        return rows, total

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
