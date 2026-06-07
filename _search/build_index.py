#!/usr/bin/env python3
"""
Semantic-search index over the ENTITY corpus (live + Assam/HP + CPPP — all 209k).

Reads every entity's index shards from docs/<entity>/index, embeds each record with
bge-small-en-v1.5 (ONNX via fastembed — the same model family the app loads in-browser
to embed the query), and writes sharded int8 vectors + meta + manifest to docs/search/.
Meta carries the entity `source` + `provenance`/`originPortal` so results route correctly.

Run after compile. Needs: pip install fastembed numpy  (use the monorepo .venv which has it).
"""
import json, os, glob
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                 # _search/ -> woodpecker-data/
DOCS = os.path.join(REPO, "docs")
OUT = os.path.join(DOCS, "search")

FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
APP_MODEL = "Xenova/bge-small-en-v1.5"        # transformers.js loads this for the query
DIM, SCALE, SHARD_VECS = 384, 127, 40000      # 40k -> ~15 MB int8 vec shard (<20 MB)
POOLING = "cls"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

def gather():
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

def main():
    docs = gather()
    n = len(docs)
    print(f"gathered {n:,} docs; embedding with {FASTEMBED_MODEL} …", flush=True)
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=FASTEMBED_MODEL)
    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(os.path.join(OUT, "vec-*.bin")) + glob.glob(os.path.join(OUT, "meta-*.json")):
        os.remove(f)
    vecs = np.empty((n, DIM), dtype=np.int8)
    i = 0
    for emb in model.embed((d["_text"] for d in docs), batch_size=256):
        vecs[i] = np.clip(np.rint(np.asarray(emb, dtype=np.float32) * SCALE), -127, 127).astype(np.int8)
        i += 1
        if i % 25000 == 0:
            print(f"  embedded {i:,}/{n:,}", flush=True)
    assert i == n
    META_KEYS = ("ocid", "source", "provenance", "originPortal", "title", "buyer", "value", "category", "closing")
    shards = []
    for k, start in enumerate(range(0, n, SHARD_VECS)):
        end = min(start + SHARD_VECS, n)
        vecs[start:end].tofile(os.path.join(OUT, f"vec-{k}.bin"))
        with open(os.path.join(OUT, f"meta-{k}.json"), "w", encoding="utf-8") as f:
            json.dump([{kk: d[kk] for kk in META_KEYS} for d in docs[start:end]],
                      f, ensure_ascii=False, separators=(",", ":"))
        shards.append({"vec": f"vec-{k}.bin", "meta": f"meta-{k}.json"})
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"model": APP_MODEL, "dim": DIM, "scale": SCALE, "pooling": POOLING,
                   "queryPrefix": QUERY_PREFIX, "count": n, "shards": shards}, f, ensure_ascii=False)
    mb = sum(os.path.getsize(os.path.join(OUT, s["vec"])) for s in shards) / 1e6
    biggest = max(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT)) / 1e6
    print(f"wrote {OUT}/  ·  {n:,} vecs · {len(shards)} shard(s) · {mb:.0f} MB int8 · largest file {biggest:.1f} MB")

if __name__ == "__main__":
    main()
