import sqlite3

conn = sqlite3.connect('receptionist.db')
cur = conn.cursor()
tables = [t[0] for t in cur.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall() if not t[0].startswith('sqlite_')]
for table in tables:
    cols = [c[1] for c in cur.execute(f'PRAGMA table_info("{table}")').fetchall()]
    clauses = [f'CAST("{col}" AS TEXT) LIKE "%7935484931%"' for col in cols]
    if clauses:
        q = f'SELECT COUNT(*) FROM "{table}" WHERE ' + ' OR '.join(clauses)
        try:
            cnt = cur.execute(q).fetchone()[0]
            if cnt > 0:
                print(f'{table}: {cnt}')
                # print first match
                for col in cols:
                    row = cur.execute(f'SELECT "{col}" FROM "{table}" WHERE CAST("{col}" AS TEXT) LIKE "%7935484931%" LIMIT 1').fetchone()
                    if row and row[0]:
                        print(f'   col {col} = {row[0]}')
        except Exception as e:
            pass
conn.close()
