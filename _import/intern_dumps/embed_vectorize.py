#!/usr/bin/env python3
"""
Embed the corpus (title+description+org+winner) with bge-small-en-v1.5 (384-dim) and insert
into the Vectorize index `woodpecker` for semantic search. Streams the LOCAL canonical
tenders.sqlite, shards of 100k, inserts each shard via `wrangler vectorize insert`, then
deletes the shard file. Resumable: a done.txt marks completed rowid ranges.

Run: python3 embed_vectorize.py   (multi-hour; safe to re-run — skips finished shards)
"""
import sqlite3, json, os, subprocess, sys, time
from fastembed import TextEmbedding

DB = os.path.expanduser("~/wp_d1_build/tenders.sqlite")
OUT = os.path.expanduser("~/wp_d1_build/vec")
API = "/Users/chiragpatnaik/Code/woodpecker/api"
SHARD = 100000
import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument("--lo", type=int, default=1)       # rowid range start (SHARD-aligned)
_ap.add_argument("--hi", type=int, default=0)       # rowid range end (0 = to max)
_ap.add_argument("--tag", default="all")            # per-worker progress file, for parallel runs
ARGS = _ap.parse_args()
os.makedirs(OUT, exist_ok=True)
PROG = os.path.join(OUT, f"done_{ARGS.tag}.txt")
done = set(open(PROG).read().split()) if os.path.exists(PROG) else set()

def band(v):
    if not v: return "none"
    for lim, lab in [(1e6,"<10L"),(1e7,"10L-1Cr"),(1e8,"1-10Cr"),(1e9,"10-100Cr")]:
        if v < lim: return lab
    return "100Cr+"

def insert(path):
    for t in range(4):
        r = subprocess.run(["npx","wrangler","vectorize","insert","woodpecker","--file",path,
                            "--batch-size","1000"], cwd=API, capture_output=True, text=True)
        if r.returncode == 0:
            return True
        sys.stderr.write(f"  insert retry {t+1}: {r.stderr[-200:]}\n"); time.sleep(6)
    return False

print("loading bge-small-en-v1.5 ...", flush=True)
model = TextEmbedding("BAAI/bge-small-en-v1.5")
conn = sqlite3.connect(DB)
maxrow = conn.execute("SELECT MAX(rowid) FROM tenders").fetchone()[0]
end = min(ARGS.hi, maxrow) if ARGS.hi else maxrow
t0 = time.time(); total = 0
lo = ARGS.lo
while lo <= end:
    hi = lo + SHARD
    key = str(lo)
    if key in done:
        lo = hi; continue
    rows = conn.execute(
        "SELECT ocid,title,description,org_name,winner_name,entity_id,year,has_award,value_amount "
        "FROM tenders WHERE rowid>=? AND rowid<?", (lo, hi)).fetchall()
    texts, metas = [], []
    for ocid, title, desc, org, win, ent, yr, aw, val in rows:
        t = " ".join(x for x in (title, desc, org, win) if x)[:512]
        if not t.strip():
            continue
        texts.append(t)
        metas.append((ocid, {"entity": ent or "", "year": yr or 0,
                             "awarded": int(aw or 0), "band": band(val)}))
    if texts:
        embs = model.embed(texts, batch_size=256)
        path = os.path.join(OUT, f"shard_{lo}.ndjson")
        with open(path, "w") as f:
            for (ocid, meta), emb in zip(metas, embs):
                f.write(json.dumps({"id": ocid, "values": [round(float(x), 6) for x in emb],
                                    "metadata": meta}) + "\n")
        if not insert(path):
            print(f"ABORT: insert failed at shard {lo}", flush=True); sys.exit(1)
        os.remove(path)
        total += len(texts)
    with open(PROG, "a") as f:
        f.write(key + "\n")
    el = time.time() - t0
    print(f"shard {lo}: +{len(texts)} (total {total:,}, {el:.0f}s, {total/max(el,1):.0f}/s)", flush=True)
    lo = hi
print("EMBED DONE", flush=True)
