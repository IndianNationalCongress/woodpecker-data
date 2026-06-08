#!/usr/bin/env python3
"""
Semantic-search index over the ENTITY corpus (live + Assam/HP + CPPP — all ~209k).

INCREMENTAL by default. Reads the existing docs/search/ index, embeds ONLY tenders
whose ocid isn't already indexed, and APPENDS them — filling the last shard, then
spilling into new shards. Existing full shards are never rewritten, so a daily cron
touches at most one ~15 MB shard (not the whole ~140 MB index) and finishes in seconds
instead of ~1h40m. When nothing new arrived, it doesn't even load the model.

Order is irrelevant to search: the app flattens every shard into one array and scores
each vector independently (app/index.html), so appending at the end is safe as long as
vec[i] stays aligned with meta[i] — which it does.

Pass --full (or set REEMBED_FULL=1) to wipe and rebuild from scratch. Needed on a model
or format change (auto-detected from the manifest), and worth running periodically to
purge any removed ocids and refresh records whose text changed in place.

Reads each entity's docs/<entity>/index shards, embeds `_text` with bge-small-en-v1.5
(fastembed ONNX — the same model family the app loads in-browser to embed the query),
and writes sharded int8 vectors + meta + manifest to docs/search/.

Run after compile. Needs: pip install fastembed numpy  (the monorepo .venv has both).
Set WOODPECKER_DOCS to point at a different docs/ root (used by the tests).
"""
import argparse
import glob
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                                  # _search/ -> woodpecker-data/
DOCS = os.environ.get("WOODPECKER_DOCS") or os.path.join(REPO, "docs")
OUT = os.path.join(DOCS, "search")

FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
APP_MODEL = "Xenova/bge-small-en-v1.5"        # transformers.js loads this for the query
DIM, SCALE, SHARD_VECS = 384, 127, 40000      # 40k -> ~15 MB int8 vec shard (<20 MB)
POOLING = "cls"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
META_KEYS = ("ocid", "source", "provenance", "originPortal",
             "title", "buyer", "value", "category", "closing")


def gather():
    """Every tender across all entity index shards -> embed-ready doc dicts (corpus order)."""
    docs = []
    for s in json.load(open(os.path.join(DOCS, "sources.json"), encoding="utf-8"))["sources"]:
        idir = os.path.join(DOCS, s["id"], "index")
        if not os.path.isdir(idir):
            continue
        for fn in sorted(os.listdir(idir)):
            if fn == "latest.json" or not fn.endswith(".json"):
                continue
            for t in json.load(open(os.path.join(idir, fn), encoding="utf-8")).get("tenders", []):
                title = (t.get("title") or "").strip()
                buyer = (t.get("buyer") or "").strip()
                supplier = (t.get("supplier") or "").strip()
                cat = (t.get("category") or "").strip()
                parts = [p for p in (title,
                         "Buyer: " + buyer if buyer else "",
                         "Supplier: " + supplier if supplier else "", cat) if p]
                docs.append({
                    "ocid": t.get("ocid"), "source": t.get("source") or s["id"],
                    "provenance": t.get("provenance"), "originPortal": t.get("originPortal"),
                    "title": title, "buyer": buyer, "value": t.get("value"), "category": cat,
                    "closing": t.get("closingDate") or t.get("closing"),
                    "_text": ". ".join(parts) or title or buyer or (t.get("ocid") or ""),
                })
    return docs


def _meta_row(d):
    return {k: d[k] for k in META_KEYS}


def embed_array(texts):
    """Embed a list of strings -> int8 ndarray [n, DIM], quantized the one canonical way."""
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=FASTEMBED_MODEL)
    n = len(texts)
    vecs = np.empty((n, DIM), dtype=np.int8)
    i = 0
    for emb in model.embed(texts, batch_size=256):
        vecs[i] = np.clip(np.rint(np.asarray(emb, dtype=np.float32) * SCALE), -127, 127).astype(np.int8)
        i += 1
        if i % 25000 == 0:
            print(f"  embedded {i:,}/{n:,}", flush=True)
    assert i == n, f"embedded {i} != {n}"
    return vecs


def _write_shard(out_dir, k, vecs, metas):
    """Write one shard pair (vec-k.bin + meta-k.json) and return its manifest entry."""
    vecs.tofile(os.path.join(out_dir, f"vec-{k}.bin"))
    with open(os.path.join(out_dir, f"meta-{k}.json"), "w", encoding="utf-8") as f:
        json.dump(metas, f, ensure_ascii=False, separators=(",", ":"))
    return {"vec": f"vec-{k}.bin", "meta": f"meta-{k}.json"}


def _write_manifest(out_dir, count, shards):
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"model": APP_MODEL, "dim": DIM, "scale": SCALE, "pooling": POOLING,
                   "queryPrefix": QUERY_PREFIX, "count": count, "shards": shards},
                  f, ensure_ascii=False)


def build_full(out_dir, docs, vecs):
    """Wipe docs/search/ and write fresh SHARD_VECS-sized shards. Returns (count, shards)."""
    os.makedirs(out_dir, exist_ok=True)
    for f in glob.glob(os.path.join(out_dir, "vec-*.bin")) + glob.glob(os.path.join(out_dir, "meta-*.json")):
        os.remove(f)
    n = len(docs)
    shards = []
    for k, start in enumerate(range(0, n, SHARD_VECS)):
        end = min(start + SHARD_VECS, n)
        shards.append(_write_shard(out_dir, k, vecs[start:end], [_meta_row(d) for d in docs[start:end]]))
    _write_manifest(out_dir, n, shards)
    return n, shards


def build_incremental(out_dir, man, new_docs, new_vecs):
    """Append new_docs/new_vecs: top up the last shard, then spill into new ones. Leading
    full shards are left byte-for-byte untouched. Returns (count, shards, untouched)."""
    shards = list(man["shards"])
    last = shards.pop()                                       # the only (maybe) partial shard
    tail_vecs = np.fromfile(os.path.join(out_dir, last["vec"]), dtype=np.int8).reshape(-1, DIM)
    tail_metas = json.load(open(os.path.join(out_dir, last["meta"]), encoding="utf-8"))
    tail_vecs = np.concatenate([tail_vecs, new_vecs], axis=0)
    tail_metas = tail_metas + [_meta_row(d) for d in new_docs]

    untouched = len(shards)                                   # leading shards kept as-is
    for j, start in enumerate(range(0, len(tail_metas), SHARD_VECS)):
        end = min(start + SHARD_VECS, len(tail_metas))
        shards.append(_write_shard(out_dir, untouched + j, tail_vecs[start:end], tail_metas[start:end]))
    count = man.get("count", untouched * SHARD_VECS) + len(new_docs)
    _write_manifest(out_dir, count, shards)
    return count, shards, untouched


def _load_manifest(out_dir):
    mpath = os.path.join(out_dir, "manifest.json")
    return json.load(open(mpath, encoding="utf-8")) if os.path.exists(mpath) else None


def _format_changed(man):
    return (man.get("model") != APP_MODEL or man.get("dim") != DIM or man.get("scale") != SCALE
            or man.get("pooling") != POOLING or man.get("queryPrefix") != QUERY_PREFIX)


def main():
    ap = argparse.ArgumentParser(description="Build/append the semantic search index.")
    ap.add_argument("--full", action="store_true",
                    help="wipe + re-embed the whole corpus (default: incremental append)")
    args = ap.parse_args()
    full = args.full or os.environ.get("REEMBED_FULL") == "1"

    docs = gather()
    os.makedirs(OUT, exist_ok=True)
    man = _load_manifest(OUT)

    if man and not man.get("shards"):
        man = None                                           # empty index -> rebuild
    if man and _format_changed(man) and not full:
        print("index model/format differs from this build -> forcing a full rebuild", flush=True)
        full = True
    if man is None:
        full = True

    if full:
        n = len(docs)
        print(f"FULL rebuild: embedding all {n:,} docs with {FASTEMBED_MODEL} …", flush=True)
        vecs = embed_array([d["_text"] for d in docs])
        count, shards = build_full(OUT, docs, vecs)
        untouched = 0
    else:
        indexed = set()
        for sh in man["shards"]:
            for m in json.load(open(os.path.join(OUT, sh["meta"]), encoding="utf-8")):
                indexed.add(m["ocid"])
        new_docs, seen = [], set()
        for d in docs:
            oc = d["ocid"]
            if oc in indexed or oc in seen:                  # already embedded, or a dup this run
                continue
            seen.add(oc)
            new_docs.append(d)
        removed = indexed - {d["ocid"] for d in docs}
        if removed:
            print(f"note: {len(removed):,} indexed ocid(s) no longer in the corpus — "
                  f"stale until a --full rebuild", flush=True)
        if not new_docs:
            print(f"index already current: {man.get('count', len(indexed)):,} vecs, "
                  f"0 new tenders — nothing to embed.", flush=True)
            return
        print(f"INCREMENTAL: {len(new_docs):,} new tender(s) "
              f"(index has {man.get('count', len(indexed)):,}); embedding + appending …", flush=True)
        new_vecs = embed_array([d["_text"] for d in new_docs])
        count, shards, untouched = build_incremental(OUT, man, new_docs, new_vecs)

    mb = sum(os.path.getsize(os.path.join(OUT, s["vec"])) for s in shards) / 1e6
    biggest = max(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT)) / 1e6
    print(f"wrote {OUT}/  ·  {count:,} vecs · {len(shards)} shard(s) "
          f"({len(shards) - untouched} rewritten, {untouched} untouched) · "
          f"{mb:.0f} MB int8 · largest file {biggest:.1f} MB")


if __name__ == "__main__":
    main()
