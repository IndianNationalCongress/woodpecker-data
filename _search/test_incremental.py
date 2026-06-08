#!/usr/bin/env python3
"""
Deterministic test for the incremental index writer — no model, fake int8 vectors.

Drives build_full + build_incremental (the file-format core that CI exercises every
night) and asserts the three properties the app depends on:
  1. append tops up the last shard, then spills into new shards;
  2. leading full shards stay BYTE-for-byte identical (the git-churn fix);
  3. vec[i] stays aligned with meta[i] across the whole index, through repeated appends.

Run: python3 _search/test_incremental.py   (exits non-zero on any failure)
"""
import json
import os
import tempfile

import numpy as np

import build_index as bi

bi.SHARD_VECS = 4                                            # tiny shards -> exercise spill fast


def mkdoc(i):
    return {k: (f"oc{i}" if k == "ocid" else f"{k}{i}") for k in bi.META_KEYS}


def fake_vecs(idxs):
    """One distinctive int8 row per doc index, so misalignment is detectable on reload."""
    v = np.empty((len(idxs), bi.DIM), dtype=np.int8)
    for r, i in enumerate(idxs):
        v[r] = np.int8((i % 251) - 125)
    return v


def load_all(out):
    man = json.load(open(os.path.join(out, "manifest.json")))
    vecs, metas = [], []
    for sh in man["shards"]:
        vecs.append(np.fromfile(os.path.join(out, sh["vec"]), dtype=np.int8).reshape(-1, bi.DIM))
        metas += json.load(open(os.path.join(out, sh["meta"])))
    return man, (np.concatenate(vecs) if vecs else np.empty((0, bi.DIM), np.int8)), metas


def assert_consistent(out, expected_idxs):
    """Index must reproduce exactly expected_idxs in order, with aligned vecs + sane shards."""
    man, vecs, metas = load_all(out)
    assert man["count"] == len(expected_idxs), (man["count"], len(expected_idxs))
    assert len(metas) == len(expected_idxs) == vecs.shape[0]
    for r, i in enumerate(expected_idxs):
        assert metas[r]["ocid"] == f"oc{i}", (r, metas[r]["ocid"], i)
        assert int(vecs[r, 0]) == (i % 251) - 125, (r, int(vecs[r, 0]), i)
    # every non-final shard is exactly full; every vec file matches its meta length
    for k, sh in enumerate(man["shards"]):
        n_meta = len(json.load(open(os.path.join(out, sh["meta"]))))
        n_vec = os.path.getsize(os.path.join(out, sh["vec"])) // bi.DIM
        assert n_meta == n_vec, (sh, n_meta, n_vec)
        if k < len(man["shards"]) - 1:
            assert n_meta == bi.SHARD_VECS, (k, n_meta)


def main():
    with tempfile.TemporaryDirectory() as out:
        # 1) full build of 10 docs -> shards [4, 4, 2]
        docs0 = [mkdoc(i) for i in range(10)]
        count, shards = bi.build_full(out, docs0, fake_vecs(range(10)))
        assert (count, len(shards)) == (10, 3), (count, len(shards))
        assert_consistent(out, list(range(10)))
        lead0 = open(os.path.join(out, "vec-0.bin"), "rb").read()
        lead1 = open(os.path.join(out, "vec-1.bin"), "rb").read()

        # 2) append 3 -> last shard 2->4 then a new shard of 1: shards [4, 4, 4, 1]
        man, _, _ = load_all(out)
        new = [mkdoc(i) for i in range(10, 13)]
        count, shards, untouched = bi.build_incremental(out, man, new, fake_vecs(range(10, 13)))
        assert (count, len(shards), untouched) == (13, 4, 2), (count, len(shards), untouched)
        assert_consistent(out, list(range(13)))

        # 3) the churn fix: leading full shards are byte-identical (git wouldn't re-commit them)
        assert open(os.path.join(out, "vec-0.bin"), "rb").read() == lead0, "vec-0 churned!"
        assert open(os.path.join(out, "vec-1.bin"), "rb").read() == lead1, "vec-1 churned!"

        # 4) a SECOND append, to prove repeated daily runs keep aligning: +3 -> [4,4,4,4]
        man, _, _ = load_all(out)
        new2 = [mkdoc(i) for i in range(13, 16)]
        count, shards, untouched = bi.build_incremental(out, man, new2, fake_vecs(range(13, 16)))
        assert (count, len(shards), untouched) == (16, 4, 3), (count, len(shards), untouched)
        assert_consistent(out, list(range(16)))
        assert open(os.path.join(out, "vec-0.bin"), "rb").read() == lead0, "vec-0 churned on run 2!"

    print("PASS — incremental writer: spill, churn-free leading shards, vec/meta alignment ✓")


if __name__ == "__main__":
    main()
