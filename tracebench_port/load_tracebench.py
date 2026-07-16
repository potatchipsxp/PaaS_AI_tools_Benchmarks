import sqlite3
import re
import os
import glob

SQL_DIR = "data/raw_sql/mtracer-TraceBench-44b29e5"  # folder where your extracted .sql files live
DB_PATH = "data/tracebench_raw.sqlite"


def clean_statement(sql):
    sql = re.sub(r'ENGINE\s*=\s*\w+', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'DEFAULT CHARSET\s*=\s*\w+', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'COLLATE\s*=?\s*\w+', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'AUTO_INCREMENT\s*=?\s*\d*', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'CHARACTER SET \w+', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'UNSIGNED', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bINT\(\d+\)', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bBIGINT\(\d+\)', 'INTEGER', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bTINYINT\(\d+\)', 'INTEGER', sql, flags=re.IGNORECASE)

    # Strip KEY/INDEX/CONSTRAINT/FOREIGN KEY clauses (handles backtick-quoted key names)
    sql = re.sub(
        r',\s*(?:CONSTRAINT\s+`?\w+`?\s+)?'
        r'(?:PRIMARY\s+KEY|UNIQUE\s+KEY|UNIQUE\s+INDEX|FOREIGN\s+KEY|KEY|INDEX)\s*'
        r'(?:`?\w+`?\s*)?\([^)]*\)'
        r'(?:\s*REFERENCES\s+`?\w+`?\s*\([^)]*\))?'
        r'(?:\s*USING\s+\w+)?',
        '', sql, flags=re.IGNORECASE
    )
    # Fix dangling trailing comma left before closing paren after stripping a KEY clause
    sql = re.sub(r',(\s*\))', r'\1', sql)

    # Table-name aliases per TraceBench dump: Task=Trace, Report=Event
    sql = re.sub(r'\bTask\b', 'Trace', sql)
    sql = re.sub(r'\bReport\b', 'Event', sql)

    sql = re.sub(r'CREATE TABLE\s+', 'CREATE TABLE IF NOT EXISTS ', sql, flags=re.IGNORECASE)
    sql = re.sub(r'INSERT INTO\s+', 'INSERT OR IGNORE INTO ', sql, flags=re.IGNORECASE)
    return sql.strip()


def split_sql_statements(text):
    """Split on semicolons outside quotes; handles both backslash-escaped
    and doubled-quote-escaped apostrophes ('' and \\')."""
    statements = []
    buf = []
    i = 0
    n = len(text)
    in_single = in_double = in_backtick = False

    while i < n:
        ch = text[i]

        if in_single:
            if ch == '\\' and i + 1 < n:
                nxt = text[i + 1]
                if nxt == "'":
                    # MySQL backslash-escape -> SQLite doubled-quote escape.
                    # Preserving the raw backslash here was the bug: SQLite
                    # doesn't recognize \' as an escape, so it read the
                    # backslash as a literal char and the following ' as the
                    # real closing quote, truncating the string early.
                    buf.append("''"); i += 2; continue
                buf.append(ch); buf.append(nxt); i += 2; continue
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("''"); i += 2; continue
                # Grammar-aware lookahead: in this dump's tuple format, a
                # string field ends only where ',' ')' or ';' follows (after
                # optional whitespace). If not, this is a genuine unescaped
                # apostrophe inside the content (e.g. "doesn't", "can't")
                # left unescaped by the original 2014 mysqldump export —
                # treat it as literal text, not a string terminator.
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j >= n or text[j] in ',);':
                    in_single = False
                    buf.append(ch); i += 1; continue
                else:
                    buf.append("''")  # escape for SQLite, stay inside string
                    i += 1; continue
            buf.append(ch); i += 1; continue

        if in_double:
            if ch == '\\' and i + 1 < n:
                buf.append(ch); buf.append(text[i + 1]); i += 2; continue
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    buf.append('""'); i += 2; continue
                in_double = False
                buf.append(ch); i += 1; continue
            buf.append(ch); i += 1; continue

        if in_backtick:
            if ch == '`':
                in_backtick = False
            buf.append(ch); i += 1; continue

        if ch == "'":
            in_single = True; buf.append(ch); i += 1; continue
        if ch == '"':
            in_double = True; buf.append(ch); i += 1; continue
        if ch == '`':
            in_backtick = True; buf.append(ch); i += 1; continue
        if ch == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch); i += 1

    tail = ''.join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def load_sql_file(conn, filepath, reject_log):
    with open(filepath, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    statements = split_sql_statements(raw)
    cur = conn.cursor()
    skipped = 0
    errors = 0
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt or stmt.startswith("--") or stmt.startswith("/*"):
            continue
        if re.match(r'(SET |LOCK |UNLOCK |\/\*)', stmt, re.IGNORECASE):
            skipped += 1
            continue
        stmt = clean_statement(stmt)
        if not stmt:
            continue
        try:
            cur.execute(stmt)
        except sqlite3.OperationalError as e:
            # Multi-row INSERTs fail atomically — one bad tuple loses every
            # good row in the batch. Fall back to per-tuple execution so we
            # salvage everything except the genuinely malformed rows.
            m = re.match(
                r'^(INSERT OR IGNORE INTO\s+\S+\s+VALUES\s*)(.*)$',
                stmt, re.IGNORECASE | re.DOTALL
            )
            if not m:
                errors += 1
                reject_log.write(f"=== {os.path.basename(filepath)} | ERROR: {e} ===\n")
                reject_log.write(stmt)
                reject_log.write("\n\n")
                continue

            prefix, tuples_blob = m.group(1), m.group(2)
            tuples = split_value_tuples(tuples_blob)
            tuple_errors = 0
            for t in tuples:
                single_stmt = f"{prefix}{t}"
                try:
                    cur.execute(single_stmt)
                except sqlite3.OperationalError as e2:
                    tuple_errors += 1
                    reject_log.write(f"=== {os.path.basename(filepath)} | ROW ERROR: {e2} ===\n")
                    reject_log.write(t)
                    reject_log.write("\n\n")
            errors += tuple_errors
    conn.commit()
    return skipped, errors


def split_value_tuples(tuples_blob):
    """Split '(...),( ...),( ...)' into a list of individual '(...)' tuples,
    respecting quotes/backticks so commas and parens inside strings don't
    break the split."""
    tuples = []
    buf = []
    depth = 0
    in_single = in_backtick = False
    i = 0
    n = len(tuples_blob)
    while i < n:
        ch = tuples_blob[i]
        if in_single:
            if ch == '\\' and i + 1 < n:
                nxt = tuples_blob[i + 1]
                if nxt == "'":
                    buf.append("''"); i += 2; continue
                buf.append(ch); buf.append(nxt); i += 2; continue
            if ch == "'":
                if i + 1 < n and tuples_blob[i + 1] == "'":
                    buf.append("''"); i += 2; continue
                in_single = False
                buf.append(ch); i += 1; continue
            buf.append(ch); i += 1; continue
        if in_backtick:
            buf.append(ch)
            if ch == '`':
                in_backtick = False
            i += 1
            continue
        if ch == "'":
            in_single = True; buf.append(ch); i += 1; continue
        if ch == '`':
            in_backtick = True; buf.append(ch); i += 1; continue
        if ch == '(':
            depth += 1; buf.append(ch); i += 1; continue
        if ch == ')':
            depth -= 1; buf.append(ch); i += 1
            if depth == 0:
                tuples.append(''.join(buf))
                buf = []
            continue
        if ch == ',' and depth == 0:
            i += 1; continue
        buf.append(ch); i += 1
    return tuples


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Removed existing {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")

    # The dump never includes a CREATE TABLE Edge statement (confirmed via
    # find_table_names.py scan of all 364 files). Schema per spec + verified
    # against a real sample row: ('2D955D...','41E97A...',2308781750653409,'79DDC6...')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS `Edge` (
            `TraceID` TEXT NOT NULL,
            `FatherNID` TEXT,
            `FatherStartTime` INTEGER,
            `ChildNID` TEXT
        )
    """)
    conn.commit()
    print("Created Edge table manually (schema absent from source dump).")

    # The trace-set / fault label (e.g. "AN_Data_corruptBlk_r_00FDN_...") only
    # exists as the source filename — Trace.Title holds the HDFS command
    # ('fs -copyToLocal'), not the fault. Without recording it, ground truth
    # for any given TraceID is unrecoverable once rows land in the shared
    # Trace table. IMPORTANT: do NOT ALTER TABLE Trace to add a column —
    # every subsequent file's "INSERT INTO Trace VALUES (...)" is a bare
    # positional statement with no column list, sized to the *original*
    # 8-column schema. Adding a 9th column breaks every later file's insert
    # ("table Trace has 9 columns but 8 values were supplied") and silently
    # drops ~370k rows down to a few hundred — this happened on the first
    # attempt. Keep the mapping in a separate side table instead.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS `Trace_Source` (
            `TraceID` TEXT PRIMARY KEY,
            `trace_set` TEXT
        )
    """)
    # Operation has the same provenance problem as Trace did, for a different
    # reason: clean_statement() strips its `PRIMARY KEY (OpName)` clause along
    # with every other file's dump, so nothing deduplicates across files —
    # each file contributes its own ~19 rows, all equally named 'readBlock'
    # etc., with no way to tell which file/condition a given row came from.
    # Unlike Trace this doesn't drop rows, but it means a future Track B
    # agent querying Operation for one OpName gets back many unattributed,
    # conflicting rows. OpName isn't unique per file (that's the point — we
    # want to keep per-file granularity), so this can't be tagged the same
    # "WHERE col IS NULL/NOT IN" way Trace_Source is. Tag by rowid range
    # instead: SQLite gives every table an implicit rowid, and each file's
    # INSERTs land at the end (OR IGNORE has no PK to trigger on anymore, so
    # they always append), so "everything inserted since last time" is just
    # "rowid > last seen max".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS `Operation_Source` (
            `op_rowid` INTEGER PRIMARY KEY,
            `trace_set` TEXT
        )
    """)
    conn.commit()

    sql_files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
    print(f"Found {len(sql_files)} .sql files")

    last_op_rowid = 0
    reject_path = "data/tracebench_load_rejects.log"
    with open(reject_path, "w", encoding="utf-8") as reject_log:
        for i, f in enumerate(sql_files):
            print(f"[{i+1}/{len(sql_files)}] {os.path.basename(f)}", end=" ... ")
            skipped, errors = load_sql_file(conn, f, reject_log)

            # NOTE: the dump's real PK column is `TaskID`, not `TraceID` — the
            # blanket `\bTask\b` -> `Trace` rename in clean_statement() only
            # renames the bare table-name token, not "TaskID" (no word
            # boundary between "Task" and "ID"). Trace_Source itself still
            # uses the `TraceID` name to match the terminology used
            # everywhere else in the port (spec, manifest, downstream code).
            conn.execute(
                """
                INSERT OR IGNORE INTO `Trace_Source` (`TraceID`, `trace_set`)
                SELECT `TaskID`, ? FROM `Trace`
                WHERE `TaskID` NOT IN (SELECT `TraceID` FROM `Trace_Source`)
                """,
                (os.path.basename(f),),
            )

            cur = conn.cursor()
            cur.execute("SELECT MAX(rowid) FROM `Operation`")
            max_op_rowid = cur.fetchone()[0] or 0
            if max_op_rowid > last_op_rowid:
                conn.execute(
                    "INSERT OR IGNORE INTO `Operation_Source` (`op_rowid`, `trace_set`) "
                    "SELECT rowid, ? FROM `Operation` WHERE rowid > ? AND rowid <= ?",
                    (os.path.basename(f), last_op_rowid, max_op_rowid),
                )
                last_op_rowid = max_op_rowid

            conn.commit()

            print(f"done (skipped {skipped} directives, {errors} errors)")

    cur = conn.cursor()
    for table in ("Event", "Edge", "Trace", "Operation", "Trace_Source", "Operation_Source"):
        try:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            count = cur.fetchone()[0]
            print(f"  {table}: {count:,} rows")
        except sqlite3.OperationalError as e:
            print(f"  {table}: NOT FOUND — {e}")

    cur.execute("SELECT COUNT(*) FROM `Trace` WHERE `TaskID` NOT IN (SELECT `TraceID` FROM `Trace_Source`)")
    untagged = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT `trace_set`) FROM `Trace_Source`")
    distinct_sets = cur.fetchone()[0]
    print(f"  trace_set: {distinct_sets:,} distinct sets, {untagged:,} Trace rows untagged")

    cur.execute("SELECT COUNT(*) FROM `Operation` WHERE rowid NOT IN (SELECT op_rowid FROM `Operation_Source`)")
    op_untagged = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT `trace_set`) FROM `Operation_Source`")
    op_distinct_sets = cur.fetchone()[0]
    print(f"  Operation trace_set: {op_distinct_sets:,} distinct sets, {op_untagged:,} Operation rows untagged")

    conn.close()
    print(f"\nDone. DB at {DB_PATH}")
    print(f"Rejected statements logged to {reject_path}")
