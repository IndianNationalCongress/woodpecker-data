// Offline semantic-search index builder.
// Embeds every compiled tender with bge-small-en-v1.5 (the SAME model the browser
// uses for the query, so vectors are comparable), quantizes to int8, and writes a
// SHARDED index under docs/search/ — each shard kept well under 20 MB:
//   vec-NNN.i8    raw int8 vectors (count * dim bytes)
//   meta-NNN.json aligned [{ocid, source, title, buyer, value, closing, status}]
//   manifest.json {model, dim, scale, pooling, queryPrefix, count, shards[]}
// The app fetches the manifest + shards, embeds the query, and cosine-ranks in-browser.
import { pipeline } from '@huggingface/transformers';
import fs from 'node:fs';
import path from 'node:path';

const MODEL = 'Xenova/bge-small-en-v1.5';
const DIM = 384;
const SCALE = 127;                       // int8 quantization scale (unit vectors -> [-127,127])
const POOLING = 'cls';                   // bge-v1.5 uses CLS pooling; app reads this from manifest
const QUERY_PREFIX = 'Represent this sentence for searching relevant passages: ';
const SHARD_TENDERS = 40000;             // ~15 MB of int8 vectors/shard (40000*384B) — well under 20 MB
const ROOT = process.cwd();
const DOCS = path.join(ROOT, 'docs');
const OUT = path.join(DOCS, 'search');

const cr = r => (r.compiledRelease || {});
function passage(rec) {
  const t = cr(rec).tender || {}, b = cr(rec).buyer || {};
  return [t.title, b.name, t.mainProcurementCategory].filter(Boolean).join(' · ');
}
function meta(rec, source) {
  const t = cr(rec).tender || {}, b = cr(rec).buyer || {};
  return {
    ocid: rec.ocid, source,
    title: t.title || '', buyer: b.name || '',
    value: t.value || null, status: t.status || '',
    closing: (t.tenderPeriod || {}).endDate || '',
    category: t.mainProcurementCategory || '',
  };
}

// gather every compiled record across sources
const sources = fs.readdirSync(DOCS).filter(d => fs.existsSync(path.join(DOCS, d, 'records')));
const items = [];
for (const s of sources) {
  for (const f of fs.readdirSync(path.join(DOCS, s, 'records'))) {
    if (!f.endsWith('.json')) continue;
    items.push({ rec: JSON.parse(fs.readFileSync(path.join(DOCS, s, 'records', f))), source: s });
  }
}
console.log(`embedding ${items.length} tenders from ${sources.length} sources with ${MODEL} …`);

const extract = await pipeline('feature-extraction', MODEL, { dtype: 'q8' });  // q8 (~33MB) — matches the app's in-browser query model exactly; downloads once
const vecs = [];
for (let i = 0; i < items.length; i++) {
  const out = await extract(passage(items[i].rec), { pooling: POOLING, normalize: true });
  const f = out.data;                              // Float32Array(DIM), unit length
  const q = new Int8Array(DIM);
  for (let k = 0; k < DIM; k++) q[k] = Math.max(-127, Math.min(127, Math.round(f[k] * SCALE)));
  vecs.push(q);
  if (i % 50 === 0) console.log(`  ${i}/${items.length}`);
}

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });
const shards = [];
for (let off = 0, idx = 0; off < items.length; off += SHARD_TENDERS, idx++) {
  const vslice = vecs.slice(off, off + SHARD_TENDERS);
  const mslice = items.slice(off, off + SHARD_TENDERS).map(it => meta(it.rec, it.source));
  const buf = Buffer.alloc(vslice.length * DIM);
  vslice.forEach((q, j) => Buffer.from(q.buffer, q.byteOffset, DIM).copy(buf, j * DIM));
  const tag = String(idx).padStart(3, '0');
  fs.writeFileSync(path.join(OUT, `vec-${tag}.i8`), buf);
  fs.writeFileSync(path.join(OUT, `meta-${tag}.json`), JSON.stringify(mslice));
  shards.push({ vec: `vec-${tag}.i8`, meta: `meta-${tag}.json`, n: vslice.length });
}
fs.writeFileSync(path.join(OUT, 'manifest.json'), JSON.stringify({
  model: MODEL, dim: DIM, scale: SCALE, pooling: POOLING, queryPrefix: QUERY_PREFIX,
  count: items.length, shards,
}, null, 2));
console.log(`done: ${items.length} vectors -> ${shards.length} shard(s) in docs/search/`);
