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
import argparse, collections, datetime, gzip, json, math, os, re, shutil, struct, sys, tempfile

import numpy as np
import scipy.sparse as sp
from sklearn.utils.extmath import randomized_svd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libcore import (encrypt, decrypt, key_from_keyfile, index_tokens, semantic_tokens, fnv1a, STOPWORDS)  # noqa: E402

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


def load_papers(cache):
    """Paper records without their text (texts are streamed from the cache later, to keep memory flat)."""
    recs = []
    rdir = os.path.join(cache, "records")
    for f in sorted(os.listdir(rdir)):
        r = read_record(os.path.join(rdir, f))
        r["_snippet"] = snippet_of(r.get("text", "")) if r.get("text", "").strip() else ""
        r["_path"] = os.path.join(rdir, f)
        r.pop("text", None)
        recs.append(r)
    researchers = json.load(open(os.path.join(cache, "researchers.json"))) if os.path.exists(os.path.join(cache, "researchers.json")) else []
    return recs, researchers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.environ.get("CACHE_DIR", ".cache/drive"))
    ap.add_argument("--out", default="site")
    ap.add_argument("--blogs", default=os.path.join(ROOT, "data", "blogs.enc"), help="encrypted blog data (see tools/encrypt_blogs.py)")
    args = ap.parse_args()
    key = key_from_keyfile(open(os.path.join(args.cache, "keyfile.bin"), "rb").read())

    # ------------------------------------------------------------ blogs (stored encrypted in the repository)
    blogs = json.loads(decrypt(open(args.blogs, "rb").read(), key))
    group_src = {g: s["id"] for s in SOURCES for g in s["groups"]}
    candidates = {}
    for c in blogs["candidates"]:
        c = dict(c); c["source"] = group_src.get(c["group"], "companies"); c["docs"] = []
        candidates[c["id"]] = c
    docs, texts = [], []          # docs: metadata; texts: blog text, or the cache path of a paper's record (streamed)
    blog_blocks = {}
    for d in sorted(blogs["docs"], key=lambda d: d["id"]):
        body = "\n\n".join(b[1] for b in d["blocks"])
        m = {k: v for k, v in d.items() if k != "blocks"}
        m["source"] = group_src.get(d["group"], "companies")
        m["availability"] = "Locally Archived" if body.strip() else "External Link Only"
        m["snippet"] = snippet_of(body) if body.strip() else ""
        docs.append(m); texts.append(body)
        blog_blocks[d["id"]] = d["blocks"]

    # ------------------------------------------------------------ papers from the Drive archive
    recs, researchers = load_papers(args.cache)
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
        paper_docs.append(m); texts.append(("path", r["_path"]))
    docs += paper_docs
    for i, d in enumerate(docs):
        d["i"] = i
        if d["candidate"] in candidates:
            candidates[d["candidate"]]["docs"].append(d["id"])
    N = len(docs)
    def text_of(i):
        t = texts[i]
        return read_record(t[1]).get("text", "") if isinstance(t, tuple) else t
    print(f"{N} documents ({len(paper_docs)} papers, {N - len(paper_docs)} blog documents), {len(candidates)} researchers/candidates")

    out = args.out
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "data"))
    def write(name, data):
        with open(os.path.join(out, "data", name), "wb") as fh:
            fh.write(encrypt(data, key))

    # ------------------------------------------------------------ full-text keyword index (two passes via temp files)
    tmp = tempfile.mkdtemp()
    shard_files = [open(os.path.join(tmp, f"{s}.tsv"), "w", encoding="utf-8") for s in range(POST_SHARDS)]
    df = collections.Counter()
    lengths = []
    for i, d in enumerate(docs):
        toks = index_tokens(" ".join([d.get("title", ""), d.get("authors", ""), d.get("name", ""), text_of(i)]))
        lengths.append(len(toks))
        for t, tf in collections.Counter(toks).items():
            shard_files[fnv1a(t) % POST_SHARDS].write(f"{t}\t{i}\t{min(tf, 255)}\n")
            df[t] += 1
    for f in shard_files:
        f.close()
    for i, d in enumerate(docs):
        d["len"] = lengths[i]
    post_bytes = 0
    for s in range(POST_SHARDS):
        groups = collections.OrderedDict()
        with open(os.path.join(tmp, f"{s}.tsv"), encoding="utf-8") as fh:
            for line in fh:
                t, i, tf = line.rstrip("\n").split("\t")
                groups.setdefault(t, []).append((int(i), int(tf)))
        terms = sorted(groups)
        payload, counts = bytearray(), []
        for t in terms:
            prev = -1
            counts.append(len(groups[t]))
            for i, tf in groups[t]:
                varint(i - prev - 1, payload); varint(tf, payload); prev = i
        blob = pack({"terms": terms, "n": counts}, bytes(payload))
        write(f"post-{s:04d}.enc", blob); post_bytes += len(blob)
    shutil.rmtree(tmp)

    # vocabulary by word start, for prefix queries
    vgroups = collections.defaultdict(list)
    for t, n in df.items():
        k = t[:2] if len(t) >= 2 and t[:2].isascii() else t[:1]
        vgroups[k].append((t, n))
    vshards = collections.defaultdict(dict)
    for k, lst in vgroups.items():
        lst.sort(key=lambda x: (-x[1], x[0]))
        vshards[fnv1a(k) % VOCAB_SHARDS][k] = lst[:3000]
    for v in range(VOCAB_SHARDS):
        write(f"vocab-{v:03d}.enc", json.dumps(vshards.get(v, {}), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    print(f"keyword index: {len(df)} terms, {sum(df.values())} postings, {post_bytes / 1e6:.1f} MB before encryption")

    # ------------------------------------------------------------ meaning index (LSA, one vector per document)
    # two streaming passes (vocabulary, then a compact sparse matrix) so memory stays flat for very large collections
    def sem_counts(i):
        d = docs[i]
        return collections.Counter(semantic_tokens(" ".join([d.get("title", ""), d.get("title", ""), d.get("authors", ""), text_of(i)[:SEM_MAX_CHARS]])))
    sdf = collections.Counter()
    for i in range(N):
        sdf.update(sem_counts(i).keys())
    vocab = [t for t, n in sdf.items() if 2 <= n <= 0.5 * N]
    vocab.sort(key=lambda t: (-sdf[t], t)); vocab = sorted(vocab[:60000])
    tid = {t: j for j, t in enumerate(vocab)}
    idf = np.array([math.log((1 + N) / (1 + sdf[t])) + 1 for t in vocab])
    del sdf
    from array import array
    rows, cols, vals = array("i"), array("i"), array("f")
    for i in range(N):
        for t, f in sem_counts(i).items():
            j = tid.get(t)
            if j is not None:
                rows.append(i); cols.append(j); vals.append((1 + math.log(f)) * idf[j])
    rows, cols, vals = np.frombuffer(rows, np.int32), np.frombuffer(cols, np.int32), np.frombuffer(vals, np.float32).astype(np.float64)
    X = sp.csr_matrix((vals, (rows, cols)), shape=(N, len(vocab)))
    nrm = np.sqrt(X.multiply(X).sum(axis=1)).A1; nrm[nrm == 0] = 1
    X = sp.diags(1 / nrm) @ X
    dim = min(DIM, max(2, min(X.shape) - 1))
    _, _, Vt = randomized_svd(X, n_components=dim, n_iter=6, random_state=0)
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
        "stats": {"terms": len(df), "postings": sum(df.values()),
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
