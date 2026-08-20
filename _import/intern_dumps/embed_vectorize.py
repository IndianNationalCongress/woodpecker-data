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
_ap.add_argument("--threads", type=int, default=0)  # embed threads (0 = all cores; one embedder)
_ap.add_argument("--pool", type=int, default=8)     # concurrent insert subprocesses (the bottleneck)
ARGS = _ap.parse_args()
os.makedirs(OUT, exist_ok=True)
PROG = os.path.join(OUT, f"done_{ARGS.tag}.txt")
done = set(open(PROG).read().split()) if os.path.exists(PROG) else set()

def band(v):
    if not v: return "none"
    for lim, lab in [(1e6,"<10L"),(1e7,"10L-1Cr"),(1e8,"1-10Cr"),(1e9,"10-100Cr")]:
        if v < lim: return lab
    return "100Cr+"

def launch_insert(path):
    return subprocess.Popen(["npx","wrangler","vectorize","insert","woodpecker","--file",path,
                             "--batch-size","1000"], cwd=API,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

POOL = []  # (Popen, path, key, tries)
def reap(block):
    """Harvest finished insert subprocesses; on success delete shard + mark done; retry failures.
    Producer/consumer: ONE embedder (full cores) feeds up to --pool concurrent inserts (the
    real bottleneck), so embed runs continuously instead of blocking on each insert."""
    global POOL, total
    while True:
        alive = []
        for p, path, key, tries in POOL:
            rc = p.poll()
            if rc is None:
                alive.append((p, path, key, tries))
            elif rc == 0:
                try: os.remove(path)
                except OSError: pass
                open(PROG, "a").write(key + "\n")
                el = time.time() - t0
                print(f"shard {key} inserted (total ~{total:,}, {el:.0f}s, {total/max(el,1):.0f}/s)", flush=True)
            elif tries < 4:
                alive.append((launch_insert(path), path, key, tries + 1))
            else:
                print(f"ABORT: insert failed 4x at shard {key}", flush=True); sys.exit(1)
        POOL = alive
        if not block or len(POOL) < ARGS.pool:
            return
        time.sleep(3)

print("loading bge-small-en-v1.5 ...", flush=True)
model = TextEmbedding("BAAI/bge-small-en-v1.5", threads=ARGS.threads or None)
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
        total += len(texts)
        reap(block=True)                          # wait only if the insert pool is full
        POOL.append((launch_insert(path), path, key, 0))
        print(f"shard {lo} embedded (+{len(texts)}), pool={len(POOL)}", flush=True)
    else:
        open(PROG, "a").write(key + "\n")         # empty shard -> done immediately
    lo = hi
while POOL:                                       # drain remaining inserts
    reap(block=True); time.sleep(2)
print("EMBED DONE", flush=True)
