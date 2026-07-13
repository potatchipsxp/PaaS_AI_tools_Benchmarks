import re
import glob
import os

SQL_DIR = "data/raw_sql/mtracer-TraceBench-44b29e5"

from load_tracebench import clean_statement, split_sql_statements

sql_files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
print(f"Scanning {len(sql_files)} files for the first CREATE TABLE `Edge` statement...\n")

TABLE_NAME_RE = re.compile(r'CREATE TABLE\s+`?Edge`?\s*\(', re.IGNORECASE)

found = False
for f in sql_files:
    with open(f, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if TABLE_NAME_RE.search(raw):
        stmts = split_sql_statements(raw)
        for stmt in stmts:
            if TABLE_NAME_RE.match(stmt.strip()):
                print(f"=== FOUND in {os.path.basename(f)} ===")
                print("--- RAW ---")
                print(stmt)
                print("\n--- AFTER clean_statement() ---")
                cleaned = clean_statement(stmt)
                print(cleaned if cleaned else "(EMPTY STRING — this is the bug)")
                found = True
                break
    if found:
        break

if not found:
    print("No CREATE TABLE Edge statement found in any file at all.")
    print("This would mean the Edge schema was never in the dump as a standalone")
    print("CREATE TABLE statement — check the .sql files manually for how Edge rows")
    print("are structured (maybe combined into another table's file).")
