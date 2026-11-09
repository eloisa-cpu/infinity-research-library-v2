#!/usr/bin/env python3
"""Build the encrypted search site from the Drive cache (tools/drive_sync.py) and the blog data.

Output directory (default site/) is what GitHub Pages serves:
  index.html, config.json, .nojekyll
  data/manifest.enc        what exists: counts, sources, shard lists, settings
  data/candidates.enc      researchers / candidates
  data/meta-N.enc          per-document metadata + short snippet (no full text)
  data/post-NNNN.enc       full-text keyword index: postings for the terms hashed to this shard
  data/vocab-NNN.enc       vocabulary grouped by word start (prefix search: quantiz*)
  data/terms-NN.enc        meaning index: word vectors (LSA)
  data/docvec-<src>-N.enc  meaning index: one vector per document
  data/blogtext-N.enc      blog documents' text (blogs are not in the Drive archive)

Full texts of papers are NOT copied: the site fetches each paper from Google Drive when it is opened.
Every data file is gzip + AES-256-GCM encrypted with the key in the Drive access-key file (see libcore).

Usage: python3 tools/build_index.py [--cache .cache/drive] [--out site]
"""
import argparse, collections, datetime, gzip, json, math, multiprocessing as mp, os, re, shutil, struct, sys, time

import numpy as np
import scipy.sparse as sp
from sklearn.utils.extmath import randomized_svd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libcore import (encrypt, decrypt, key_from_keyfile, index_counts, semantic_counts, fnv1a, STOPWORDS)  # noqa: E402

T0 = time.time()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POST_SHARDS = 1024
VOCAB_SHARDS = 256
TERM_SHARDS = 16
DIM = 192
META_CHUNK = 2500
VEC_CHUNK = 4000
SEM_MAX_CHARS = 200_000
SOURCES = [
    {"id": "university", "label": "University Data", "lazy": True,
     "description": "Research papers by university researchers (Top 50 archive and University Research Papers), full text from the Google Drive archive.",
     "groups": ["Top 50 Publications", "University Research Papers"]},
    {"id": "companies", "label": "Personal Blogs from Priority Companies",
     "description": "Personal blogs and technical writing: OpenAI, Anthropic, Eigen AI, Jane Street, ML Sys and Kernel GitHub DB.",
     "groups": ["OpenAI", "Anthropic", "Eigen AI", "Jane Street", "ML Sys", "Kernel GitHub DB"]},
]


def varint(n, out):
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80); n >>= 7
    out.append(n)


def pack(header, payload=b""):
    h = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(h)) + h + payload


def quantize(m):
    scale = np.abs(m).max(axis=1) / 127.0 if len(m) else np.zeros(0)
    scale[scale == 0] = 1.0
    q = np.clip(np.rint(m / scale[:, None]), -127, 127).astype(np.int8) if len(m) else np.zeros((0, m.shape[1]), np.int8)
    return scale.astype("<f4").tobytes() + q.tobytes()


def snippet_of(text, n=320):
    paras = [p.strip() for p in text.split("\n") if len(p.strip()) > 120]
    for i, p in enumerate(text.split("\n")):
        if p.strip().lower() in ("abstract", "abstract.", "summary"):
            rest = [q for q in text.split("\n")[i + 1:] if len(q.strip()) > 80]
            if rest:
                paras = rest
            break
    s = (paras[0] if paras else text.strip())[:n]
    return s + ("…" if len(s) == n else "")


def year_of(date):
    m = re.match(r"(19|20)\d\d", date or "")
    return int(m.group(0)) if m else None


def cand_id(rid):
    rid = rid.upper()
    return ("top50-" if rid.startswith("C") else "uni-") + rid.lower()


def read_record(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def log(msg):
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def analyse(job):
    """Process one document in a worker: read it once, return its metadata, snippet and word counts.

    Returns (record or None, index word counts, number of index words, meaning-index word counts).
    """
    kind, payload = job
    if kind == "paper":
        r = read_record(payload)
        text = r.pop("text", "") or ""
        title, authors, name = r.get("title") or r.get("fileTitle") or "", (r.get("authors") or "")[:400], r.get("researcher", "")
        r["_snippet"] = snippet_of(text) if text.strip() else ""
    else:
        r = None
        title, authors, name, text = payload
    ic = index_counts(" ".join([title, authors, name, text]))
    sc = semantic_counts(" ".join([title, title, authors, text[:SEM_MAX_CHARS]]))
    return r, ic, sum(ic.values()), sc


def varint_np(vals):
    """LEB128 varint encoding of a uint64 array (same bytes as varint() applied to each value)."""
    vals = vals.astype(np.uint64)
    n = np.ones(len(vals), np.int64)
    for t in (1 << 7, 1 << 14, 1 << 21, 1 << 28, 1 << 35):
        n += vals >= t
    out = np.empty(int(n.sum()), np.uint8)
    starts = np.cumsum(n) - n
    v = vals.copy()
    for k in range(int(n.max()) if len(n) else 0):
        m = n > k
        out[starts[m] + k] = ((v[m] & np.uint64(0x7F)) | (np.uint64(0x80) * (n[m] > k + 1))).astype(np.uint8)
        v[m] >>= np.uint64(7)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.environ.get("CACHE_DIR", ".cache/drive"))
    ap.add_argument("--out", default="site")
    ap.add_argument("--blogs", default=os.path.join(ROOT, "data", "blogs.enc"), help="encrypted blog data (see tools/encrypt_blogs.py)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    key = key_from_keyfile(open(os.path.join(args.cache, "keyfile.bin"), "rb").read())

    # ------------------------------------------------------------ blogs (stored encrypted in the repository)
    blogs = json.loads(decrypt(open(args.blogs, "rb").read(), key))
    group_src = {g: s["id"] for s in SOURCES for g in s["groups"]}
    candidates = {}
    for c in blogs["candidates"]:
        c = dict(c); c["source"] = group_src.get(c["group"], "companies"); c["docs"] = []
        candidates[c["id"]] = c
    blog_docs, blog_blocks, jobs = [], {}, []
    for d in sorted(blogs["docs"], key=lambda d: d["id"]):
        body = "\n\n".join(b[1] for b in d["blocks"])
        m = {k: v for k, v in d.items() if k != "blocks"}
        m["source"] = group_src.get(d["group"], "companies")
        m["availability"] = "Locally Archived" if body.strip() else "External Link Only"
        m["snippet"] = snippet_of(body) if body.strip() else ""
        blog_docs.append(m); blog_blocks[d["id"]] = d["blocks"]
        jobs.append(("blog", (m.get("title", ""), m.get("authors", ""), m.get("name", ""), body)))
    rdir = os.path.join(args.cache, "records")
    paths = [os.path.join(rdir, f) for f in sorted(os.listdir(rdir))]
    jobs += [("paper", p) for p in paths]
    log(f"analysing {len(jobs)} documents ({len(paths)} papers, {len(blog_docs)} blog documents) with {args.workers} workers")

    # ------------------------------------------------------------ one pass over every document (each is read once)
    idx_terms, sem_terms = {}, {}
    idx_ids, idx_tf, sem_ids, sem_cnt, lengths, recs = [], [], [], [], [], []
    with mp.Pool(args.workers) as pool:
        for j, (r, ic, n, sc) in enumerate(pool.imap(analyse, jobs, chunksize=8)):
            idx_ids.append(np.fromiter((idx_terms.setdefault(t, len(idx_terms)) for t in ic), np.int32, len(ic)))
            idx_tf.append(np.fromiter((min(c, 255) for c in ic.values()), np.uint8, len(ic)))
            sem_ids.append(np.fromiter((sem_terms.setdefault(t, len(sem_terms)) for t in sc), np.int32, len(sc)))
            sem_cnt.append(np.fromiter(sc.values(), np.float32, len(sc)))
            lengths.append(n)
            if r is not None:
                r["_job"] = j; recs.append(r)
            if (j + 1) % 1000 == 0 or j + 1 == len(jobs):
                log(f"  analysed {j + 1}/{len(jobs)} documents, {len(idx_terms):,} distinct words")

    # ------------------------------------------------------------ papers from the Drive archive
    researchers = json.load(open(os.path.join(args.cache, "researchers.json"))) if os.path.exists(os.path.join(args.cache, "researchers.json")) else []
    for r in researchers:
        rm = re.match(r"^(?P<name>.+?) \((?P<id>[A-Za-z]?\d+)\)$", r["folder"])
        if not rm:
            continue
        rid = rm.group("id").upper(); cid = cand_id(rid)
        candidates.setdefault(cid, {
            "id": cid, "name": rm.group("name"), "group": "Top 50 Publications" if rid.startswith("C") else "University Research Papers",
            "university": r["univ"], "institution": r["univ"], "source": "university", "x": "", "github": "", "site": "",
            "status": "", "fit": "", "track": "", "fitReason": "", "relevance": "", "note": "",
            "openalex": f"https://openalex.org/{rid}" if rid.startswith("A") else "", "driveFolder": r["id"], "researcherKey": rid, "docs": []})
    recs.sort(key=lambda r: (cand_id(r["rid"]), r["n"], r["ext"] != "md", r["sub"], r["id"]))
    used_ids = set()
    paper_docs = []
    order = list(range(len(blog_docs)))          # job index of each document, in document order
    for r in recs:
        cid = cand_id(r["rid"])
        c = candidates.setdefault(cid, {"id": cid, "name": r["researcher"], "group": "Top 50 Publications" if r["rid"].startswith("C") else "University Research Papers",
                                        "university": r["univ"], "institution": r["univ"], "source": "university", "x": "", "github": "", "site": "",
                                        "status": "", "fit": "", "track": "", "fitReason": "", "relevance": "", "note": "", "docs": []})
        did = f"{cid}--p{r['n']:03d}"
        if r["ext"] == "pdf" and did in used_ids:
            did += "-pdf"
        base, k = did, 2
        while did in used_ids:
            did = f"{base}-{k}"; k += 1
        used_ids.add(did)
        m = {
            "id": did, "candidate": cid, "name": c["name"], "university": r["univ"], "group": c["group"], "source": "university",
            "title": r.get("title") or r.get("fileTitle"), "type": "Research paper", "platform": r.get("venue") or "Not recorded",
            "date": r.get("date", ""), "year": year_of(r.get("date")), "url": r.get("url") or (("https://doi.org/" + r["doi"]) if r.get("doi") and not r["doi"].startswith("http") else r.get("doi", "")),
            "doi": r.get("doi", ""), "authors": (r.get("authors") or "")[:400],
            "availability": "Locally Archived" if r.get("available") else "External Link Only",
            "attribution": "Needs Review" if r.get("needsReview") else "Matched",
            "attributionNote": r.get("attribution", ""), "archiveStatus": r.get("archiveStatus", ""), "unavailableNote": r.get("notes", "") if not r.get("available") else "",
            "drive": r["id"], "fmt": r["ext"], "fullTextSource": r.get("fullTextSource", ""), "conversion": r.get("conversion", ""),
            "snippet": r["_snippet"],
        }
        paper_docs.append(m); order.append(r["_job"])
    docs = blog_docs + paper_docs
    for i, d in enumerate(docs):
        d["i"] = i
        d["len"] = lengths[order[i]]
        if d["candidate"] in candidates:
            candidates[d["candidate"]]["docs"].append(d["id"])
    N = len(docs)
    log(f"{N} documents ({len(paper_docs)} papers, {N - len(paper_docs)} blog documents), {len(candidates)} researchers/candidates")

    out = args.out
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "data"))
    def write(name, data):
        with open(os.path.join(out, "data", name), "wb") as fh:
            fh.write(encrypt(data, key))

    # ------------------------------------------------------------ full-text keyword index
    terms = [None] * len(idx_terms)
    for t, j in idx_terms.items():
        terms[j] = t
    del idx_terms
    T = np.concatenate([idx_ids[j] for j in order]) if N else np.zeros(0, np.int32)
    F = np.concatenate([idx_tf[j] for j in order]) if N else np.zeros(0, np.uint8)
    D = np.repeat(np.arange(N, dtype=np.int64), [len(idx_ids[j]) for j in order])
    del idx_ids, idx_tf
    df = np.bincount(T, minlength=len(terms))
    shard_of = np.fromiter((fnv1a(t) % POST_SHARDS for t in terms), np.int32, len(terms))
    o = np.lexsort((D, T, shard_of[T]))           # by shard, then word, then document
    T, D, F = T[o], D[o], F[o]
    del o
    bounds = np.searchsorted(shard_of[T], np.arange(POST_SHARDS + 1))
    log(f"keyword index: {len(terms):,} words, {len(T):,} postings; writing {POST_SHARDS} shards")
    post_bytes = 0
    for s in range(POST_SHARDS):
        a, b = bounds[s], bounds[s + 1]
        t, d, f = T[a:b], D[a:b], F[a:b]
        if b > a:
            starts = np.flatnonzero(np.r_[True, t[1:] != t[:-1]])
            uniq, counts = t[starts], np.diff(np.r_[starts, len(t)])
            prev = np.r_[-1, d[:-1]]; prev[starts] = -1
            vals = np.empty(2 * len(t), np.uint64)
            vals[0::2] = d - prev - 1; vals[1::2] = f
            payload = varint_np(vals).tobytes()
            header = {"terms": [terms[u] for u in uniq], "n": counts.tolist()}
        else:
            payload, header = b"", {"terms": [], "n": []}
        blob = pack(header, payload)
        write(f"post-{s:04d}.enc", blob); post_bytes += len(blob)
    del T, D, F

    # vocabulary by word start, for prefix queries
    vgroups = collections.defaultdict(list)
    for j, t in enumerate(terms):
        k = t[:2] if len(t) >= 2 and t[:2].isascii() else t[:1]
        vgroups[k].append((t, int(df[j])))
    vshards = collections.defaultdict(dict)
    for k, lst in vgroups.items():
        lst.sort(key=lambda x: (-x[1], x[0]))
        vshards[fnv1a(k) % VOCAB_SHARDS][k] = lst[:3000]
    for v in range(VOCAB_SHARDS):
        write(f"vocab-{v:03d}.enc", json.dumps(vshards.get(v, {}), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    n_terms, n_postings = len(terms), int(df.sum())
    del vgroups, vshards, terms, df
    log(f"keyword index written: {post_bytes / 1e6:.1f} MB before encryption")

    # ------------------------------------------------------------ meaning index (LSA, one vector per document)
    sterms = [None] * len(sem_terms)
    for t, j in sem_terms.items():
        sterms[j] = t
    del sem_terms
    SI = np.concatenate([sem_ids[j] for j in order]) if N else np.zeros(0, np.int32)
    SC = np.concatenate([sem_cnt[j] for j in order]) if N else np.zeros(0, np.float32)
    SD = np.repeat(np.arange(N, dtype=np.int32), [len(sem_ids[j]) for j in order])
    del sem_ids, sem_cnt
    sdf = np.bincount(SI, minlength=len(sterms))
    keep = np.flatnonzero((sdf >= 2) & (sdf <= 0.5 * N))
    top = sorted(keep.tolist(), key=lambda j: (-sdf[j], sterms[j]))[:60000]
    vocab = sorted(sterms[j] for j in top)
    col_of = np.full(len(sterms), -1, np.int64)
    sid = {sterms[j]: j for j in top}
    for c, t in enumerate(vocab):
        col_of[sid[t]] = c
    idf = np.array([math.log((1 + N) / (1 + sdf[sid[t]])) + 1 for t in vocab])
    cols = col_of[SI]; m = cols >= 0
    rows, cols, vals = SD[m], cols[m], (1 + np.log(SC[m].astype(np.float64))) * idf[cols[m]]
    del SI, SC, SD, sdf, sterms
    log(f"meaning index: {len(vocab):,} words; computing document vectors")
    X = sp.csr_matrix((vals, (rows, cols)), shape=(N, len(vocab)))
    nrm = np.sqrt(X.multiply(X).sum(axis=1)).A1; nrm[nrm == 0] = 1
    X = sp.diags(1 / nrm) @ X
    dim = min(DIM, max(2, min(X.shape) - 1))
    _, _, Vt = randomized_svd(X, n_components=dim, n_iter=6, random_state=0)
    log("meaning index: vectors computed; writing files")
    V = Vt.T
    dv = X @ V; dn = np.linalg.norm(dv, axis=1); dn[dn == 0] = 1; dv = dv / dn[:, None]
    term_buckets = collections.defaultdict(list)
    for j, t in enumerate(vocab):
        term_buckets[fnv1a(t) % TERM_SHARDS].append(j)
    for k in range(TERM_SHARDS):
        js = term_buckets.get(k, [])
        write(f"terms-{k:02d}.enc", pack({"terms": [vocab[j] for j in js], "idf": [round(float(idf[j]), 4) for j in js]}, quantize(V[js] if js else np.zeros((0, dim)))))
    vec_shards = []
    for s in SOURCES:
        idx = [d["i"] for d in docs if d["source"] == s["id"]]
        for p in range(0, len(idx), VEC_CHUNK):
            part = idx[p:p + VEC_CHUNK]
            name = f"docvec-{s['id']}-{p // VEC_CHUNK + 1}"
            write(name + ".enc", pack({"docs": part}, quantize(dv[part])))
            vec_shards.append({"name": name, "source": s["id"], "rows": len(part)})

    # ------------------------------------------------------------ metadata, candidates, blog text
    meta_files = []
    for p in range(0, N, META_CHUNK):
        name = f"meta-{p // META_CHUNK + 1}"
        write(name + ".enc", json.dumps([{k: v for k, v in d.items() if v not in ("", None) or k in ("title",)} for d in docs[p:p + META_CHUNK]],
                                        ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        meta_files.append(name)
    write("candidates.enc", json.dumps(sorted(candidates.values(), key=lambda c: c["id"]), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    blog_files, cur, size, bf = [], {}, 0, {}
    for did, blocks in blog_blocks.items():
        n = len(json.dumps(blocks, ensure_ascii=False))
        if cur and size + n > 2_000_000:
            blog_files.append(cur); cur, size = {}, 0
        cur[did] = blocks; size += n
    if cur:
        blog_files.append(cur)
    for k, chunk in enumerate(blog_files):
        write(f"blogtext-{k + 1}.enc", json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for did in chunk:
            bf[did] = f"blogtext-{k + 1}"

    manifest = {
        "version": 2, "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "docCount": N, "paperCount": len(paper_docs), "candidateCount": len(candidates), "sources": SOURCES,
        "metaFiles": meta_files, "postShards": POST_SHARDS, "vocabShards": VOCAB_SHARDS,
        "avgLen": sum(lengths) / max(1, N), "blogText": bf,
        "semantic": {"dim": dim, "termShards": TERM_SHARDS, "docShards": vec_shards, "stopwords": STOPWORDS},
        "stats": {"terms": n_terms, "postings": n_postings,
                  "papersFullText": sum(d["availability"] == "Locally Archived" for d in paper_docs),
                  "papersExternal": sum(d["availability"] != "Locally Archived" for d in paper_docs),
                  "needsReview": sum(d["attribution"] == "Needs Review" for d in paper_docs),
                  "pdf": sum(d.get("fmt") == "pdf" for d in paper_docs)},
    }
    write("manifest.enc", json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    for f in ("index.html", "config.json"):
        shutil.copy(os.path.join(ROOT, f), os.path.join(out, f))
    open(os.path.join(out, ".nojekyll"), "w").close()
    total = sum(os.path.getsize(os.path.join(out, "data", f)) for f in os.listdir(os.path.join(out, "data")))
    print(f"site written to {out}/: {len(os.listdir(os.path.join(out, 'data')))} data files, {total / 1e6:.1f} MB")
    print(json.dumps(manifest["stats"]))


if __name__ == "__main__":
    main()
