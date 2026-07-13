import re
import glob
import os
from load_tracebench import split_sql_statements

SQL_DIR = "data/raw_sql/mtracer-TraceBench-44b29e5"

sql_files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))

for f in sql_files:
    with open(f, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    if "INSERT INTO `Edge`" in raw or "INSERT INTO Edge" in raw:
        stmts = split_sql_statements(raw)
        for stmt in stmts:
            if re.match(r'INSERT INTO\s+`?Edge`?\s+VALUES', stmt.strip(), re.IGNORECASE):
                print(f"Found in: {os.path.basename(f)}\n")
                # Extract just the first tuple after VALUES
                m = re.search(r'VALUES\s*(\(.*)', stmt, re.IGNORECASE | re.DOTALL)
                if m:
                    body = m.group(1)
                    # crude first-tuple grab: up to first "),(" or end
                    end = body.find("),(")
                    first_tuple = body[:end+1] if end != -1 else body[:500]
                    print("First tuple:")
                    print(first_tuple)
                    # count fields (naive split by comma outside quotes not needed for eyeballing)
                exit()

print("No INSERT INTO Edge found (unexpected).")
