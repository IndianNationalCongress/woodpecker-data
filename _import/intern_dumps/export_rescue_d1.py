#!/usr/bin/env python3
"""Generate D1 sync SQL for the stub rescue: delete the collapsed rows, insert the rescued
distinct tenders, FTS-index them. Reads the rescued rows from the patched local canonical."""
import json, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(__file__))
from build_canonical import COLS
from export_d1 import sqlstr

OUT = os.path.expanduser("~/wp_d1_build/rescue")
delta = json.load(open(os.path.join(OUT, "delta.json")))
rescued, stale = delta["rescued"], delta["stale"]
local = sqlite3.connect(os.path.expanduser("~/wp_d1_build/tenders.sqlite"))

def batches(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i+n]

# 1. delete collapsed rows
with open(os.path.join(OUT, "00_del.sql"), "w") as f:
    for b in batches(stale, 200):
        f.write("DELETE FROM tenders WHERE ocid IN (%s);\n" % ",".join(sqlstr(o) for o in b))

# 2. insert rescued rows (40/stmt for the 100KB cap)
collist = ",".join(COLS)
nfiles = 0
fh = None; size = 0; fi = 0
rescued_set = set(rescued)
# pull the rescued rows from local in chunks
def newf():
    global fh, fi, size, nfiles
    if fh: fh.close()
    fh = open(os.path.join(OUT, "01_data_%03d.sql" % fi), "w"); fi += 1; size = 0; nfiles += 1
newf()
buf = []
got = 0
for b in batches(rescued, 500):
    rows = local.execute("SELECT %s FROM tenders WHERE ocid IN (%s)" % (collist, ",".join("?"*len(b))), b).fetchall()
    for r in rows:
        buf.append("(" + ",".join(sqlstr(v) for v in r) + ")")
        got += 1
        if len(buf) >= 40:
            stmt = "INSERT OR IGNORE INTO tenders (%s) VALUES %s;\n" % (collist, ",".join(buf))
            fh.write(stmt); size += len(stmt); buf = []
            if size > 40*1024*1024: newf()
if buf:
    fh.write("INSERT OR IGNORE INTO tenders (%s) VALUES %s;\n" % (collist, ",".join(buf)))
fh.close()

# 3. FTS-index rescued rows (by ocid -> rowid, server-side; 120 ocids/stmt)
with open(os.path.join(OUT, "99_fts.sql"), "w") as f:
    for b in batches(rescued, 120):
        f.write("INSERT INTO tenders_fts(rowid,title,description,org_name,winner_name) "
                "SELECT rowid,title,description,org_name,winner_name FROM tenders WHERE ocid IN (%s);\n"
                % ",".join(sqlstr(o) for o in b))

print("rescued rows pulled from local: %d (expected %d)" % (got, len(rescued)))
print("wrote: 00_del.sql, 01_data_*.sql (%d files), 99_fts.sql" % nfiles)
