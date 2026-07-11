import sqlite3
import re
import os
import glob

SQL_DIR = "data/raw_sql/mtracer-TraceBench-44b29e5"       # folder where your extracted .sql files live
DB_PATH = "data/tracebench_raw.sqlite"

# MySQL → SQLite syntax patches
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

    sql = re.sub(
        r',\s*(?:CONSTRAINT\s+`?\w+`?\s+)?'
        r'(?:PRIMARY\s+KEY|UNIQUE\s+KEY|UNIQUE\s+INDEX|FOREIGN\s+KEY|KEY|INDEX)\s*'
        r'(?:`?\w+`?\s*)?\([^)]*\)'
        r'(?:\s*REFERENCES\s+`?\w+`?\s*\([^)]*\))?'
        r'(?:\s*USING\s+\w+)?',
        '', sql, flags=re.IGNORECASE
    )
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
                buf.append(ch); buf.append(text[i+1]); i += 2; continue
            if ch == "'":
                if i + 1 < n and text[i+1] == "'":
                    buf.append("''"); i += 2; continue
                in_single = False
                buf.append(ch); i += 1; continue
            buf.append(ch); i += 1; continue

        if in_double:
            if ch == '\\' and i + 1 < n:
                buf.append(ch); buf.append(text[i+1]); i += 2; continue
            if ch == '"':
                if i + 1 < n and text[i+1] == '"':
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


def load_sql_file(conn, filepath):
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
            errors += 1
            if errors <= 3:  # cap noise, but show enough to diagnose
                print(f"\n  WARN [{os.path.basename(filepath)}]: {e}")
                print(f"    stmt preview: {stmt[:150]!r}")
    conn.commit()
    return skipped, errors

if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Removed existing {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")   # speed — safe for a build script

    sql_files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
    print(f"Found {len(sql_files)} .sql files")

    for i, f in enumerate(sql_files):
        print(f"[{i+1}/{len(sql_files)}] {os.path.basename(f)}", end=" ... ")
        skipped, errors = load_sql_file(conn, f)
        status = f"done (skipped {skipped} directives, {errors} errors)"
        print(status)

    # Quick sanity check
    cur = conn.cursor()
    for table in ("Event", "Edge", "Trace", "Operation"):
        try:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            count = cur.fetchone()[0]
            print(f"  {table}: {count:,} rows")
        except sqlite3.OperationalError as e:
            print(f"  {table}: NOT FOUND — {e}")

    conn.close()
    print(f"\nDone. DB at {DB_PATH}")