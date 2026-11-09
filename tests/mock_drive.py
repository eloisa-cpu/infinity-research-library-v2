#!/usr/bin/env python3
"""A small stand-in for the Google Drive v3 API, for tests only.

Serves a local directory tree as if it were the Drive archive:
  GET /drive/v3/files?q='<folderId>' in parents and trashed = false   (pageSize/pageToken honoured)
  GET /drive/v3/files/<id>?alt=media                                   file content
Folder/file ids are derived from paths (the top directory is ROOT). md5Checksum and modifiedTime come from the
file contents and mtime, so editing or adding files is picked up like on Drive.

Access: requests need "Authorization: Bearer <token>"; tokens listed in --tokens may read everything, others get 404
(Drive's answer for files you cannot see). Sends CORS headers so the site can call it from a browser.

Usage: python3 tests/mock_drive.py <archive_dir> --port 8767 --tokens good-token
"""
import argparse, hashlib, json, os, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ap = argparse.ArgumentParser()
ap.add_argument("root")
ap.add_argument("--port", type=int, default=8767)
ap.add_argument("--tokens", default="good-token")
ap.add_argument("--page-size", type=int, default=0, help="force a smaller page size to exercise paging")
args = ap.parse_args()
ROOT = os.path.abspath(args.root)
TOKENS = set(args.tokens.split(","))


def fid(path):
    rel = os.path.relpath(path, ROOT)
    return "ROOT" if rel == "." else "id" + hashlib.sha1(rel.encode()).hexdigest()[:24]


def index():
    m = {}
    for d, dirs, files in os.walk(ROOT):
        m[fid(d)] = d
        for f in files:
            m[fid(os.path.join(d, f))] = os.path.join(d, f)
    return m


def entry(path):
    st = os.stat(path)
    if os.path.isdir(path):
        return {"id": fid(path), "name": os.path.basename(path), "mimeType": "application/vnd.google-apps.folder"}
    data = open(path, "rb").read()
    mime = {"md": "text/markdown", "pdf": "application/pdf", "txt": "text/plain", "csv": "text/csv"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
    return {"id": fid(path), "name": os.path.basename(path), "mimeType": mime, "md5Checksum": hashlib.md5(data).hexdigest(),
            "modifiedTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(st.st_mtime)), "size": str(len(data))}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization")

    def reply(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code); self.cors()
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.send_header("Access-Control-Allow-Methods", "GET"); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not tok:
            return self.reply(401, {"error": {"code": 401, "message": "Login Required"}})
        allowed = tok in TOKENS
        m = index()
        if u.path == "/drive/v3/files":
            q = qs.get("q", [""])[0]
            mm = re.match(r"'([^']+)' in parents", q)
            parent = m.get(mm.group(1)) if mm else None
            if not allowed or not parent or not os.path.isdir(parent):
                return self.reply(200, {"files": []})
            kids = sorted(os.listdir(parent))
            nm = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            if nm:
                kids = [k for k in kids if k == nm.group(1).replace("\\'", "'")]
            size = args.page_size or int(qs.get("pageSize", ["100"])[0])
            start = int(qs.get("pageToken", ["0"])[0])
            page = kids[start:start + size]
            out = {"files": [entry(os.path.join(parent, k)) for k in page]}
            if start + size < len(kids):
                out["nextPageToken"] = str(start + size)
            return self.reply(200, out)
        mm = re.match(r"^/drive/v3/files/([^/]+)$", u.path)
        if mm and qs.get("alt", [""])[0] == "media":
            p = m.get(mm.group(1))
            if not allowed or not p or os.path.isdir(p):
                return self.reply(404, {"error": {"code": 404, "message": "File not found"}})
            return self.reply(200, open(p, "rb").read(), entry(p)["mimeType"])
        self.reply(404, {"error": {"code": 404, "message": "Not found"}})


ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
