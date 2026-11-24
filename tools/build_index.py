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
  data/docvec-<src>-N.enc  meaning index: one vector per document (LSA; used when the AI model cannot load)
  data/embvec-<src>-N.enc  AI meaning index: one bge-small vector per document (title + abstract), see tools/models.py
  models/, vendor/transformers/   the meaning model and its browser runtime (open-source, fingerprint-checked)
  data/blogtext-N.enc      blog documents' text (blogs are not in the Drive archive)

Full texts of papers are NOT copied: the site fetches each paper from Google Drive when it is opened.
Every data file is gzip + AES-256-GCM encrypted with the key in the Drive access-key file (see libcore).

Usage: python3 tools/build_index.py [--cache .cache/drive] [--out site]
"""
import argparse, collections, datetime, gzip, json, math, multiprocessing as mp, os, re, shutil, struct, sys, time, urllib.parse

import numpy as np
import scipy.sparse as sp
from sklearn.utils.extmath import randomized_svd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libcore import (encrypt, decrypt, key_from_keyfile, index_counts, semantic_counts, fnv1a, STOPWORDS, contactability, linkedin_urls, pick_linkedin)  # noqa: E402
import models  # noqa: E402
from models import doc_text  # noqa: E402

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
    """Stable researcher id: Top 50 list ids keep their old form (saved bookmarks stay valid)."""
    return ("top50-" if re.fullmatch(r"C\d+", rid, re.I) else "uni-") + rid.lower()


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
    return r, ic, sum(ic.values()), sc, doc_text(title, text)


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


GH_FIELDS = ("name", "company", "location", "bio", "email", "twitter_username", "hireable", "blog", "updated_at")


def github_accounts(handles, cache_path):
    """Look up each candidate's GitHub profile. Suspended, deleted and hidden accounts answer 404 ("missing"); existing
    ones return what the person lists publicly (name, company, location, bio, public email, X handle, "available for
    hire"). Needs GITHUB_TOKEN (set automatically in GitHub Actions); results are cached for 7 days.
    Returns {handle (lower case): {"state": "ok"|"missing", "profile": {...}}}."""
    import urllib.request, urllib.error
    token, api = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    now, out, asked, limited = time.time(), {}, 0, False
    for h in sorted({x.strip() for x in handles if x and x.strip()}):
        k = h.lower()
        hit = cache.get(k)
        fresh = hit and len(hit) >= 3 and now - hit[1] < 7 * 86400       # older entries had no profile: refresh them
        if fresh or not token or limited:
            if hit: out[k] = {"state": hit[0], "profile": hit[2] if len(hit) >= 3 else {}}
            continue
        req = urllib.request.Request(f"{api}/users/{urllib.parse.quote(h)}", headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "library-build"})
        state, prof = None, {}
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode("utf-8") or "{}")
            state = "ok"
            prof = {f: j.get(f) for f in GH_FIELDS if j.get(f) not in (None, "")}
            try:                                  # "Social accounts" the person added to their profile (LinkedIn etc.)
                sreq = urllib.request.Request(f"{api}/users/{urllib.parse.quote(h)}/social_accounts", headers=req.headers)
                with urllib.request.urlopen(sreq, timeout=20) as r:
                    socials = json.loads(r.read().decode("utf-8") or "[]")
                li = [x.get("url", "") for x in socials if x.get("provider") == "linkedin" or "linkedin.com" in x.get("url", "")]
                li += linkedin_urls(j.get("blog") or "")
                if li:
                    prof["linkedin"] = li[0]
            except Exception:
                pass
        except urllib.error.HTTPError as e:
            if e.code == 404:
                state = "missing"
            else:                                # rate limit or GitHub trouble: keep what we knew, try next build
                limited = e.code in (403, 429)
        except Exception:
            pass
        asked += 1
        if state:
            cache[k] = [state, now, prof]; out[k] = {"state": state, "profile": prof}
        elif hit:
            out[k] = {"state": hit[0], "profile": hit[2] if len(hit) >= 3 else {}}
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    json.dump(cache, open(cache_path, "w"))
    log(f"GitHub accounts: {len(out)} known, {asked} checked now, {sum(v['state'] == 'missing' for v in out.values())} unavailable, "
        f"{sum(bool(v['profile']) for v in out.values())} with public profile details" + ("" if token else " (no GITHUB_TOKEN: not checked this time)"))
    return out


def page_links(html, base):
    """Links on a web page: [[visible text, absolute URL], ...] in page order, plus mailto addresses."""
    from html.parser import HTMLParser
    from urllib.parse import urljoin

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True); self.out, self.cur, self.skip = [], None, 0
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"): self.skip += 1
            if tag == "a":
                href = dict(attrs).get("href") or ""
                self.cur = [href, []]
        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.skip: self.skip -= 1
            if tag == "a" and self.cur:
                href, parts = self.cur; self.cur = None
                text = re.sub(r"\s+", " ", "".join(parts)).strip()
                if href and text and not href.startswith(("javascript:", "#")) and len(text) <= 200:
                    self.out.append([text, href if href.startswith("mailto:") else urljoin(base, href)])
        def handle_data(self, data):
            if self.cur and not self.skip: self.cur[1].append(data)
    p = P()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.out[:600]


def fetch_pages(urls, cache_path):
    """Read each web page once a week (cached): its links, LinkedIn links and email addresses.
    Returns {url: {"links": [[text, href]...], "linkedin": [...], "mailto": [...]}}. LIBRARY_FETCH_SITES=0 skips reading."""
    import urllib.request, concurrent.futures as cf
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    now, todo, out = time.time(), [], {}
    for u in sorted({u for u in urls if u and u.startswith("http")}):
        hit = cache.get(u)
        if hit and isinstance(hit[0], dict) and now - hit[1] < 7 * 86400:
            out[u] = hit[0]
        elif os.environ.get("LIBRARY_FETCH_SITES", "1") != "0":
            todo.append(u)
        elif hit and isinstance(hit[0], dict):
            out[u] = hit[0]

    def fetch(u):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (research-library link check)", "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if "html" not in (r.headers.get("Content-Type") or "html"):
                    return u, {"links": [], "linkedin": [], "mailto": []}
                html = r.read(3_000_000).decode("utf-8", "replace")
            links = page_links(html, r.geturl() if hasattr(r, "geturl") else u)
            mail = list(dict.fromkeys(h[7:].split("?")[0].strip() for _, h in links if h.startswith("mailto:") and "@" in h))
            return u, {"links": links, "linkedin": linkedin_urls(html), "mailto": mail[:5]}
        except Exception:
            return u, None                        # unreachable now: try again next build

    with cf.ThreadPoolExecutor(8) as ex:
        for u, res in ex.map(fetch, todo):
            if res is not None:
                cache[u] = [res, now]; out[u] = res
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    json.dump(cache, open(cache_path, "w"))
    log(f"web pages: {len(todo)} read now, {len(out)} known, {sum(len(v['links']) for v in out.values()):,} links")
    return out


def doc_links(page, blocks):
    """Links from the live page whose text appears in the saved text, in page order (to restore them when displayed)."""
    if not page:
        return []
    text = " ".join(b[1] for b in blocks)
    return [[t, h] for t, h in page["links"] if len(t) >= 2 and t in text][:400]


def enrich_from_github(c, prof):
    """Fill gaps in a candidate from their public GitHub profile, without overwriting what the research team recorded.
    Everything added is listed in c["filled"] so the site can say where it came from."""
    from libcore import looks_like_real_name
    filled = []
    name = (prof.get("name") or "").strip()
    if name and not looks_like_real_name(c.get("name")) and looks_like_real_name(name):
        c["handle"] = c.get("name"); c["name"] = name; filled.append("name")
    comp = re.sub(r"\s+", " ", (prof.get("company") or "").replace("@", " ")).strip(" ,")
    if comp and not (c.get("company") or "").strip():
        c["company"] = comp; filled.append("company")
    for src, dst in (("location", "location"), ("email", "email"), ("bio", "bio")):
        v = (prof.get(src) or "").strip()
        if v and not (c.get(dst) or "").strip():
            c[dst] = v[:240]; filled.append(dst)
    tw = (prof.get("twitter_username") or "").strip()
    if tw and not (c.get("x") or "").strip():
        c["x"] = "https://x.com/" + tw; filled.append("x")
    if prof.get("hireable") is True:
        c["hireable"] = True; filled.append("hireable")
    if filled:
        c["filled"] = {"from": "GitHub profile", "fields": filled}
    return c


_EMB = None


def _emb_init(dest):
    global _EMB
    _EMB = models.Embedder(dest, threads=1)


def _emb_run(texts):
    return _EMB.embed(texts)


def build_embeddings(args, docs, texts, write):
    """One AI meaning vector per document. Vectors are cached by text, so a build only computes new or changed ones."""
    import hashlib
    models.ensure(args.models)
    os.makedirs(args.emb_cache, exist_ok=True)
    kp, vp = os.path.join(args.emb_cache, "keys.json"), os.path.join(args.emb_cache, "vecs.npy")
    cache = {}
    if os.path.exists(kp) and os.path.exists(vp):
        ks, vs = json.load(open(kp)), np.load(vp)
        if len(ks) == len(vs):
            cache = dict(zip(ks, vs))
    keys = [hashlib.sha1((models.MODEL_REVISION + "\n" + t).encode("utf-8")).hexdigest() for t in texts]
    todo = sorted({k: t for k, t in zip(keys, texts) if k not in cache}.items())
    log(f"AI meaning index: {len(texts) - len([k for k in keys if k not in cache]):,} vectors reused, {len(todo):,} to compute")
    def save(keep):
        tmpk, tmpv = kp + ".tmp", vp + ".tmp.npy"
        json.dump(keep, open(tmpk, "w"))
        np.save(tmpv, np.stack([cache[k] for k in keep]).astype(np.float16) if keep else np.zeros((0, models.DIM), np.float16))
        os.replace(tmpv, vp); os.replace(tmpk, kp)
    if todo:
        chunks = [todo[i:i + 64] for i in range(0, len(todo), 64)]
        done = 0
        with mp.Pool(args.workers, initializer=_emb_init, initargs=(args.models,)) as pool:
            for chunk, vecs in zip(chunks, pool.imap(_emb_run, [[t for _, t in c] for c in chunks])):
                for (k, _), v in zip(chunk, vecs):
                    cache[k] = v.astype(np.float16)
                done += len(chunk)
                if done % 2048 < 64 or done == len(todo):
                    log(f"  computed {done:,}/{len(todo):,} vectors")
                if done % 4096 < 64:
                    save(list(cache))           # progress survives an interrupted build
    V = np.stack([cache[k].astype(np.float32) for k in keys]) if keys else np.zeros((0, models.DIM), np.float32)
    save(list(dict.fromkeys(keys)))     # keep only vectors of documents that still exist
    shards = []
    for s in SOURCES:
        idx = [d["i"] for d in docs if d["source"] == s["id"]]
        for p in range(0, len(idx), VEC_CHUNK):
            part = idx[p:p + VEC_CHUNK]
            name = f"embvec-{s['id']}-{p // VEC_CHUNK + 1}"
            write(name + ".enc", pack({"docs": part}, quantize(V[part])))
            shards.append({"name": name, "source": s["id"], "rows": len(part)})
    return {"model": models.MODEL_ID, "revision": models.MODEL_REVISION, "dim": models.DIM, "queryPrefix": models.QUERY_PREFIX,
            "dtype": "q8", "shards": shards, "modelBase": "models/", "runtime": "vendor/transformers/"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.environ.get("CACHE_DIR", ".cache/drive"))
    ap.add_argument("--out", default="site")
    ap.add_argument("--blogs", default=os.path.join(ROOT, "data", "blogs.enc"), help="encrypted blog data (see tools/encrypt_blogs.py)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--models", default=os.environ.get("MODELS_DIR", ".cache/models"), help="where the meaning model is kept between builds")
    ap.add_argument("--emb-cache", default=os.environ.get("EMB_CACHE_DIR", ".cache/embeddings"), help="document vectors kept between builds")
    args = ap.parse_args()
    key = key_from_keyfile(open(os.path.join(args.cache, "keyfile.bin"), "rb").read())

    # ------------------------------------------------------------ blogs (stored encrypted in the repository)
    blogs = json.loads(decrypt(open(args.blogs, "rb").read(), key))
    group_src = {g: s["id"] for s in SOURCES for g in s["groups"]}
    candidates = {}
    for c in blogs["candidates"]:
        c = dict(c); c["source"] = group_src.get(c["group"], "companies"); c["docs"] = []
        candidates[c["id"]] = c
    # public GitHub profiles: does the account still exist, and what does the person list (real name, employer...)?
    # done first, so a real name found on GitHub is part of the search index
    gh = github_accounts([c.get("github", "") for c in candidates.values()], os.path.join(args.emb_cache, "github_accounts.json"))
    nfilled = collections.Counter()
    for c in candidates.values():
        acct = gh.get((c.get("github") or "").strip().lower()) or {}
        c["_gh_state"] = acct.get("state")
        if acct.get("state") == "ok" and acct.get("profile"):
            enrich_from_github(c, acct["profile"])
            nfilled.update(c.get("filled", {}).get("fields", []))
    log(f"filled from GitHub profiles: {dict(nfilled)}")
    # LinkedIn: only links the person published. Our own records first, then their website, then their GitHub profile.
    in_records = collections.defaultdict(list)
    for c in candidates.values():
        in_records[c["id"]] += linkedin_urls(" ".join(str(v) for v in c.values() if isinstance(v, str)))
    for d in blogs["docs"]:
        in_records[d.get("candidate")] += linkedin_urls(json.dumps(d.get("blocks", []), ensure_ascii=False) + " " + (d.get("url") or ""))
    pages = fetch_pages([c.get("site", "") for c in candidates.values() if not (c.get("status") or "").startswith("Skipped")]
                        + [d.get("url", "") for d in blogs["docs"] if d.get("blocks")], os.path.join(args.emb_cache, "web_pages.json"))
    from_sites = {}
    for c in candidates.values():
        home = pages.get((c.get("site") or "").strip()) if not (c.get("status") or "").startswith("Skipped") else None
        if not home:
            continue
        li = pick_linkedin(home["linkedin"], c.get("name"))
        if li:
            from_sites[c["id"]] = li
        if not (c.get("email") or "").strip() and len(home["mailto"]) == 1:     # one address on their own homepage
            c["email"], c["emailFrom"] = home["mailto"][0], "their website"
    nli = collections.Counter()
    for c in candidates.values():
        rec = pick_linkedin(list(dict.fromkeys(in_records.get(c["id"], []))), c.get("name"))
        prof_li = (gh.get((c.get("github") or "").strip().lower()) or {}).get("profile", {}).get("linkedin")
        gh_li = linkedin_urls(prof_li or "")
        for url, src in ((rec, "our records"), (from_sites.get(c["id"]), "their website"), (gh_li[0] if gh_li else None, "their GitHub profile")):
            if url:
                c["linkedin"], c["linkedinFrom"] = url, src; nli[src] += 1
                break
    log(f"LinkedIn links: {dict(nli)}")
    blog_docs, blog_blocks, jobs = [], {}, []
    for d in sorted(blogs["docs"], key=lambda d: d["id"]):
        body = "\n\n".join(b[1] for b in d["blocks"])
        m = {k: v for k, v in d.items() if k != "blocks"}
        bc = candidates.get(d.get("candidate"))
        if bc and bc.get("handle") and m.get("name") == bc["handle"]:
            m["name"] = bc["name"]
        m["source"] = group_src.get(d["group"], "companies")
        m["availability"] = "Locally Archived" if body.strip() else "External Link Only"
        m["snippet"] = snippet_of(body) if body.strip() else ""
        lk = doc_links(pages.get(d.get("url", "")), d["blocks"])
        blog_docs.append(m); blog_blocks[d["id"]] = {"blocks": d["blocks"], "links": lk} if lk else d["blocks"]
        jobs.append(("blog", (m.get("title", ""), m.get("authors", ""), m.get("name", "") + (" " + bc["handle"] if bc and bc.get("handle") else ""), body)))
    rdir = os.path.join(args.cache, "records")
    paths = [os.path.join(rdir, f) for f in sorted(os.listdir(rdir))]
    jobs += [("paper", p) for p in paths]
    log(f"analysing {len(jobs)} documents ({len(paths)} papers, {len(blog_docs)} blog documents) with {args.workers} workers")

    # ------------------------------------------------------------ one pass over every document (each is read once)
    idx_terms, sem_terms = {}, {}
    idx_ids, idx_tf, sem_ids, sem_cnt, lengths, recs, emb_texts = [], [], [], [], [], [], []
    with mp.Pool(args.workers) as pool:
        for j, (r, ic, n, sc, et) in enumerate(pool.imap(analyse, jobs, chunksize=8)):
            emb_texts.append(et)
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
        if "key" not in r:          # cache written by an older version of drive_sync
            from libcore import parse_researcher_folder
            r.update(parse_researcher_folder(r["folder"], r["univ"]))
        rid = r["key"]; cid = cand_id(rid)
        c = candidates.setdefault(cid, {
            "id": cid, "name": r["name"], "group": "Top 50 Publications" if r["match"] == "Top 50 list" else "University Research Papers",
            "university": r["univ"], "institution": r["univ"], "source": "university", "x": "", "github": "", "site": "",
            "status": "", "fit": "", "track": "", "fitReason": "", "relevance": "", "note": "", "match": r["match"],
            "openalex": f"https://openalex.org/{rid}" if r["match"] == "OpenAlex" else "",
            "orcid": "https://orcid.org/" + rid[6:] if r["match"] == "ORCID" else "", "driveFolder": r["id"], "researcherKey": rid, "docs": []})
        c.setdefault("driveFolders", [])
        if r["id"] not in c["driveFolders"]:
            c["driveFolders"].append(r["id"])          # the same researcher can have several folders ("Name (1)")
    recs.sort(key=lambda r: (cand_id(r["rid"]), r["n"], r["ext"] != "md", r["sub"], r["id"]))
    used_ids = set()
    paper_docs = []
    order = list(range(len(blog_docs)))          # job index of each document, in document order
    for r in recs:
        cid = cand_id(r["rid"])
        c = candidates.setdefault(cid, {"id": cid, "name": r["researcher"], "group": "Top 50 Publications" if r.get("match") == "Top 50 list" or re.fullmatch(r"C\d+", r["rid"]) else "University Research Papers", "match": r.get("match", ""),
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
    # can the hiring team actually reach each candidate? (real name, working links, real person; see libcore.contactability)
    tiers = collections.Counter()
    for c in candidates.values():
        c["contact"] = contactability(c, c.pop("_gh_state", None))
        tiers[c["contact"]["tier"]] += 1
    log(f"contactability: {dict(tiers)}")
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
    D = np.repeat(np.arange(N, dtype=np.int32), [len(idx_ids[j]) for j in order])
    del idx_ids, idx_tf
    df = np.bincount(T, minlength=len(terms))
    shard_of = np.fromiter((fnv1a(t) % POST_SHARDS for t in terms), np.int16, len(terms))
    log(f"keyword index: {len(terms):,} words, {len(T):,} postings; writing {POST_SHARDS} shards")
    # Shards are written in groups, so only one group's postings are sorted at a time (memory stays low for very
    # large collections). Postings are already in document order, so a stable sort by word keeps documents ordered.
    post_bytes, GROUPS = 0, 16
    TS = shard_of[T]
    for g in range(GROUPS):
        lo, hi = g * POST_SHARDS // GROUPS, (g + 1) * POST_SHARDS // GROUPS
        sel = np.flatnonzero((TS >= lo) & (TS < hi))
        gt, gd, gf, gs = T[sel], D[sel], F[sel], TS[sel]
        o = np.lexsort((gt, gs))                 # by shard, then word; stable, so documents stay in order
        gt, gd, gf, gs = gt[o], gd[o].astype(np.int64), gf[o], gs[o]
        bounds = np.searchsorted(gs, np.arange(lo, hi + 1))
        for s in range(lo, hi):
            a, b = bounds[s - lo], bounds[s - lo + 1]
            t, d, f = gt[a:b], gd[a:b], gf[a:b]
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
        del sel, gt, gd, gf, gs, o
    del T, D, F, TS

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

    # ------------------------------------------------------------ AI meaning index (bge-small; only new/changed texts are computed)
    embed = None
    if os.environ.get("LIBRARY_EMBEDDINGS", "1") != "0":
        try:
            embed = build_embeddings(args, docs, [emb_texts[order[i]] for i in range(N)], write)
        except Exception as e:      # the site still works: meaning search then uses the LSA index
            log(f"AI meaning index skipped: {e}")

    manifest = {
        "version": 2, "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "docCount": N, "paperCount": len(paper_docs), "candidateCount": len(candidates), "sources": SOURCES,
        "metaFiles": meta_files, "postShards": POST_SHARDS, "vocabShards": VOCAB_SHARDS,
        "avgLen": sum(lengths) / max(1, N), "blogText": bf,
        "semantic": {"dim": dim, "termShards": TERM_SHARDS, "docShards": vec_shards, "stopwords": STOPWORDS},
        **({"embed": embed} if embed else {}),
        "stats": {"terms": n_terms, "postings": n_postings,
                  "papersFullText": sum(d["availability"] == "Locally Archived" for d in paper_docs),
                  "papersExternal": sum(d["availability"] != "Locally Archived" for d in paper_docs),
                  "needsReview": sum(d["attribution"] == "Needs Review" for d in paper_docs),
                  "pdf": sum(d.get("fmt") == "pdf" for d in paper_docs)},
    }
    write("manifest.enc", json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    for f in ("index.html", "config.json"):
        shutil.copy(os.path.join(ROOT, f), os.path.join(out, f))
    if embed:
        models.install_into_site(args.models, out)
    if os.path.isdir(os.path.join(ROOT, "vendor")):          # pdf.js: reads PDF text in the browser for search-result passages
        shutil.copytree(os.path.join(ROOT, "vendor"), os.path.join(out, "vendor"), dirs_exist_ok=True)
    # pdf.js uploaded to the top level (pdfjs/) instead of vendor/pdfjs/: use it from there
    if not os.path.exists(os.path.join(out, "vendor", "pdfjs", "pdf.min.mjs")) and os.path.exists(os.path.join(ROOT, "pdfjs", "pdf.min.mjs")):
        shutil.copytree(os.path.join(ROOT, "pdfjs"), os.path.join(out, "vendor", "pdfjs"), dirs_exist_ok=True)
    if not os.path.exists(os.path.join(out, "vendor", "pdfjs", "pdf.min.mjs")):
        log("warning: pdf.js not found (vendor/pdfjs/); PDF results will not show their relevant passage")
    open(os.path.join(out, ".nojekyll"), "w").close()
    total = sum(os.path.getsize(os.path.join(out, "data", f)) for f in os.listdir(os.path.join(out, "data")))
    print(f"site written to {out}/: {len(os.listdir(os.path.join(out, 'data')))} data files, {total / 1e6:.1f} MB")
    print(json.dumps(manifest["stats"]))


if __name__ == "__main__":
    main()
