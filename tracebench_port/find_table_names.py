import re
import glob
import os
from load_tracebench import split_sql_statements

SQL_DIR = "data/raw_sql/mtracer-TraceBench-44b29e5"

CREATE_RE = re.compile(r'CREATE TABLE\s+`?(\w+)`?\s*\(', re.IGNORECASE)
INSERT_RE = re.compile(r'INSERT INTO\s+`?(\w+)`?\s+VALUES', re.IGNORECASE)

sql_files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
print(f"Scanning {len(sql_files)} files...\n")

created_tables = set()
inserted_tables = set()

for f in sql_files:
    with open(f, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    stmts = split_sql_statements(raw)
    for stmt in stmts:
        stmt = stmt.strip()
        m = CREATE_RE.match(stmt)
        if m:
            created_tables.add(m.group(1))
        m = INSERT_RE.match(stmt)
        if m:
            inserted_tables.add(m.group(1))

print("Tables with CREATE TABLE statements:")
for t in sorted(created_tables):
    print(f"  - {t}")

print("\nTables with INSERT INTO statements:")
for t in sorted(inserted_tables):
    print(f"  - {t}")

print("\nInserted but never created (the mismatch):")
for t in sorted(inserted_tables - created_tables):
    print(f"  - {t}")

print("\nCreated but never inserted into:")
for t in sorted(created_tables - inserted_tables):
    print(f"  - {t}")
