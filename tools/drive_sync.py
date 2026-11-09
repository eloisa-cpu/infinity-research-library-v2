#!/usr/bin/env python3
"""Mirror the text of the Google Drive research archive into a local cache (read-only on Drive).

Walks the archive folder tree
    <root>/<University>/<Researcher Name (ID)>/[<subfolder>/]Publication NNN - <title>.md|.pdf
lists every paper file, and downloads only files that are new or changed since the last run (Drive md5 /
modification time). Markdown files are parsed (metadata + text); PDFs are converted to text with pdftotext.
Files that disappeared from Drive are dropped from the cache. Nothing on Drive is ever modified.

Output (CACHE_DIR, default .cache/drive):
  state.json             fileId -> {name, path, md5, modified, size, mime}
  records/<fileId>.json.gz  parsed record used by tools/build_index.py
  keyfile.bin            the archive's access-key file (used to encrypt the site)

Authentication (no keys):
  GitHub Actions: Workload Identity Federation (google-github-actions/auth sets GOOGLE_APPLICATION_CREDENTIALS);
                  the job acts as the read-only service account that has Viewer access to the archive folder.
  DRIVE_ACCESS_TOKEN   a ready OAuth access token (tests against tests/mock_drive.py, quick manual runs)

Environment:
  DRIVE_ROOT_FOLDER_ID   archive root (default: University Publications folder)
  DRIVE_KEY_FILE_NAME    name of the access-key file in the root (default "Library access key.txt")
  DRIVE_API_BASE         default https://www.googleapis.com (tests point it at a mock server)
"""
import concurrent.futures as cf, gzip, json, os, re, subprocess, sys, tempfile, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libcore import parse_archive_md, meta_get, md_to_blocks, NEEDS_REVIEW_RE  # noqa: E402

ROOT = os.environ.get("DRIVE_ROOT_FOLDER_ID", "1TbckZTHb2sbKMZDyskcgt0uczTfimXnI")
KEY_NAME = os.environ.get("DRIVE_KEY_FILE_NAME", "Library access key.txt")
API = os.environ.get("DRIVE_API_BASE", "https://www.googleapis.com").rstrip("/")
CACHE = os.environ.get("CACHE_DIR", ".cache/drive")
FOLDER = "application/vnd.google-apps.folder"
PUB_RE = re.compile(r"^Publication (?P<n>\d+) - (?P<title>.*)\.(?P<ext>md|pdf)$", re.I)
RESEARCHER_RE = re.compile(r"^(?P<name>.+?) \((?P<id>[A-Za-z]?\d+)\)$")
SKIP_DIRS = {"_pipeline", "Collection Logs and Records"}
WORKERS = int(os.environ.get("DRIVE_WORKERS", "8"))


# ---------------------------------------------------------------- HTTP session with auth + retries
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]   # read-only: the pipeline can never change Drive


def make_session():
    """HTTP session authorised for Drive (read-only).

    In GitHub Actions the credentials come from Workload Identity Federation: the workflow's
    google-github-actions/auth step exchanges GitHub's OIDC token for short-lived credentials of the
    infinity-library-reader service account and points GOOGLE_APPLICATION_CREDENTIALS at a credential
    configuration file (no service-account key). google.auth.default() reads it and refreshes tokens itself.
    """
    import requests
    tok = os.environ.get("DRIVE_ACCESS_TOKEN")
    if tok:
        s = requests.Session(); s.headers["Authorization"] = "Bearer " + tok
        return s
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    import google.auth.exceptions, google.auth.transport.requests
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    try:
        if path:
            creds, _ = google.auth.load_credentials_from_file(path, scopes=SCOPES)
        else:
            creds, _ = google.auth.default(scopes=SCOPES)
        creds.refresh(google.auth.transport.requests.Request())     # fail early, with a clear message
    except google.auth.exceptions.DefaultCredentialsError as e:
        sys.exit("No Google credentials. In GitHub Actions, the 'Authenticate to Google Cloud' step (Workload Identity "
                 "Federation) must run first; locally set DRIVE_ACCESS_TOKEN. Details: " + str(e))
    except google.auth.exceptions.GoogleAuthError as e:
        sys.exit("Google rejected the Workload Identity Federation sign-in. Check GCP_WORKLOAD_IDENTITY_PROVIDER (full provider "
                 "name with the project NUMBER), the provider's attribute condition (this repository), that the pool has the "
                 "'Workload Identity User' role on the service account, and that the IAM Credentials and Security Token "
                 "Service APIs are enabled. Details: " + str(e)[:400])
    return AuthorizedSession(creds)


_local = threading.local()
def session():
    if not hasattr(_local, "s"):
        _local.s = make_session()
    return _local.s


def get(url, params=None, stream=False):
    for attempt in range(8):
        r = session().get(url, params=params, timeout=120, stream=stream)
        if r.status_code in (429, 500, 502, 503, 504) or (r.status_code == 403 and "rate" in r.text.lower()):
            time.sleep(min(60, 2 ** attempt)); continue
        if r.status_code == 401 and attempt < 2:
            _local.__dict__.pop("s", None); continue
        r.raise_for_status()
        return r
    r.raise_for_status()


def list_children(folder_id):
    out, token = [], None
    while True:
        p = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 1000,
             "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime, size)",
             "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if token:
            p["pageToken"] = token
        j = get(f"{API}/drive/v3/files", p).json()
        out += j.get("files", [])
        token = j.get("nextPageToken")
        if not token:
            return out


# ---------------------------------------------------------------- crawl
def crawl():
    """Returns ({fileId: entry}, keyfile entry or None). entry: name, mime, md5, modified, size, univ, researcher, rid, sub, n, ext, title."""
    papers, keyfile = {}, None
    top = list_children(ROOT)
    for f in top:
        if f["mimeType"] != FOLDER and f["name"] == KEY_NAME:
            keyfile = f
    univs = [f for f in top if f["mimeType"] == FOLDER and f["name"] not in SKIP_DIRS]
    researchers = []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for u, kids in zip(univs, ex.map(lambda u: list_children(u["id"]), univs)):
            researchers += [(u["name"], k) for k in kids if k["mimeType"] == FOLDER]
        def walk(item):
            univ, folder = item
            m = RESEARCHER_RE.match(folder["name"])
            if not m:
                return []
            found, stack = [], [(folder["id"], "")]
            while stack:
                fid, sub = stack.pop()
                for c in list_children(fid):
                    if c["mimeType"] == FOLDER:
                        stack.append((c["id"], (sub + "/" if sub else "") + c["name"]))
                        continue
                    pm = PUB_RE.match(c["name"])
                    if not pm:
                        continue
                    found.append({**c, "univ": univ, "researcher": m.group("name"), "rid": m.group("id").upper(),
                                  "sub": sub, "n": int(pm.group("n")), "ext": pm.group("ext").lower(), "title": pm.group("title")})
            return found
        for files in ex.map(walk, researchers):
            for f in files:
                papers[f["id"]] = f
    return papers, keyfile, len(researchers), researchers


# ---------------------------------------------------------------- per-file processing
def pdf_text(data):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as t:
        t.write(data); t.flush()
        r = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", t.name, "-"], capture_output=True, timeout=300)
        return r.stdout.decode("utf-8", "replace")


def make_record(f, data):
    rec = {"id": f["id"], "univ": f["univ"], "researcher": f["researcher"], "rid": f["rid"], "sub": f["sub"],
           "n": f["n"], "ext": f["ext"], "fileTitle": f["title"], "size": int(f.get("size") or len(data)), "modified": f.get("modifiedTime", "")}
    if f["ext"] == "md":
        text = data.decode("utf-8", "replace")
        meta, body = parse_archive_md(text)
        blocks = md_to_blocks(body)
        plain = "\n".join(b[1] for b in blocks)
        status = meta_get(meta, "Archive status").lower()
        unavailable = status.startswith("unavailable") or (not plain.strip()) or plain.strip().lower().startswith("unavailable.")
        rec.update({
            "title": meta_get(meta, "Publication title", "Title", "_h1") or f["title"],
            "authors": meta_get(meta, "Authors"),
            "date": meta_get(meta, "Publication date", "Date"),
            "venue": meta_get(meta, "Journal / conference", "Venue"),
            "url": meta_get(meta, "Original publication URL", "Original URL"),
            "doi": meta_get(meta, "DOI"),
            "paperKey": meta_get(meta, "Paper key"),
            "openalexWork": meta_get(meta, "OpenAlex work"),
            "fullTextSource": meta_get(meta, "Full-text source used", "Full-text source"),
            "conversion": meta_get(meta, "Conversion", "Conversion method"),
            "archiveStatus": meta_get(meta, "Archive status") or ("unavailable" if unavailable else "full text"),
            "attribution": meta_get(meta, "Attribution (source spreadsheet)", "Attribution"),
            "notes": meta_get(meta, "Archive notes"),
            "available": not unavailable,
            "text": "" if unavailable else plain,
        })
        review_hay = " ".join([text[:3000], f["sub"], rec["attribution"], rec["paperKey"]])
    else:
        plain = pdf_text(data)
        rec.update({"title": f["title"], "authors": "", "date": "", "venue": "", "url": "", "doi": "", "paperKey": "",
                    "archiveStatus": "full text (PDF)", "attribution": "", "notes": "", "available": bool(plain.strip()),
                    "text": plain, "fullTextSource": "", "conversion": "PDF text layer (pdftotext)"})
        review_hay = f["sub"]
    rec["needsReview"] = bool(NEEDS_REVIEW_RE.search(review_hay)) or rec.get("paperKey", "").upper().startswith("UNK-")
    return rec


def main():
    os.makedirs(os.path.join(CACHE, "records"), exist_ok=True)
    state_path = os.path.join(CACHE, "state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    t0 = time.time()
    papers, keyfile, n_researchers, researchers = crawl()
    json.dump([{"univ": u, "folder": f["name"], "id": f["id"]} for u, f in researchers],
              open(os.path.join(CACHE, "researchers.json"), "w"), ensure_ascii=False)
    print(f"crawl: {len(papers)} paper files in {n_researchers} researcher folders ({time.time() - t0:.0f}s)")
    if not keyfile:
        sys.exit(f'The access-key file "{KEY_NAME}" was not found in the archive root folder.')
    key_raw = get(f"{API}/drive/v3/files/{keyfile['id']}", {"alt": "media", "supportsAllDrives": "true"}).content
    open(os.path.join(CACHE, "keyfile.bin"), "wb").write(key_raw)

    todo = [f for fid, f in papers.items()
            if state.get(fid, {}).get("md5") != f.get("md5Checksum") or state.get(fid, {}).get("modified") != f.get("modifiedTime")
            or not os.path.exists(os.path.join(CACHE, "records", fid + ".json.gz"))]
    print(f"to download: {len(todo)} new/changed, {len(papers) - len(todo)} unchanged")
    done, failed, lock = 0, [], threading.Lock()

    def work(f):
        data = get(f"{API}/drive/v3/files/{f['id']}", {"alt": "media", "supportsAllDrives": "true"}).content
        rec = make_record(f, data)
        with gzip.open(os.path.join(CACHE, "records", f["id"] + ".json.gz"), "wt", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        return f

    with cf.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(work, f): f for f in todo}
        for fu in cf.as_completed(futs):
            f = futs[fu]
            try:
                fu.result()
                with lock:
                    state[f["id"]] = {"name": f["name"], "md5": f.get("md5Checksum"), "modified": f.get("modifiedTime"),
                                      "univ": f["univ"], "researcher": f["researcher"], "rid": f["rid"], "sub": f["sub"]}
                    done += 1
                    if done % 500 == 0:
                        print(f"  {done}/{len(todo)} downloaded ({time.time() - t0:.0f}s)")
                        json.dump(state, open(state_path, "w"))
            except Exception as e:
                failed.append((f["id"], f["name"], str(e)[:200]))
    # forget files that are no longer on Drive
    for fid in list(state):
        if fid not in papers:
            state.pop(fid)
            p = os.path.join(CACHE, "records", fid + ".json.gz")
            if os.path.exists(p):
                os.remove(p)
    json.dump(state, open(state_path, "w"))
    print(f"sync done: {done} downloaded, {len(failed)} failed, {len(state)} papers cached ({time.time() - t0:.0f}s)")
    for x in failed[:20]:
        print("  FAILED", *x)
    if failed and len(failed) > max(20, len(todo) // 20):
        sys.exit("Too many download failures; not publishing a partial index.")


if __name__ == "__main__":
    main()
