#!/usr/bin/env python3
"""Check that the pipeline can read the Drive archive, without building anything.

Run by the "Verify Drive access" workflow (or locally with DRIVE_ACCESS_TOKEN). It checks, in order:
  1. Google credentials work (Workload Identity Federation in GitHub Actions) and which account is used
  2. the token is read-only (drive.readonly scope only)
  3. the archive folder can be listed (it is shared with the service account)
  4. the access-key file is visible (its contents are never printed)
  5. one paper can be downloaded (content access, not just listing)
Exit code 0 only if every check passes. Prints a hint for each failure.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drive_sync as D  # noqa: E402

ok = True
def report(passed, msg, hint=""):
    global ok
    print(("PASS  " if passed else "FAIL  ") + msg + ("" if passed or not hint else f"\n      → {hint}"))
    ok = ok and passed
    return passed


def main():
    print(f"Archive folder: {D.ROOT}\n")
    # 1-2. credentials and scope
    try:
        s = D.session()
    except SystemExit as e:
        report(False, "Google credentials", str(e)); return
    if not os.environ.get("DRIVE_ACCESS_TOKEN"):
        try:
            import google.auth.transport.requests
            creds = s.credentials
            info = __import__("requests").get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": creds.token}, timeout=30).json()
            report(True, f"Google credentials work (Workload Identity Federation) — acting as {info.get('email') or getattr(creds, 'service_account_email', '?')}")
            scopes = set((info.get("scope") or "").split())
            report(scopes == {"https://www.googleapis.com/auth/drive.readonly"}, f"token is read-only (scopes: {' '.join(sorted(scopes)) or '?'})",
                   "the pipeline should only ever request drive.readonly")
        except Exception as e:
            report(False, "Google credentials", f"{e}\n      → Check the GitHub variables GCP_WORKLOAD_IDENTITY_PROVIDER and GCP_SERVICE_ACCOUNT, "
                   "the provider's attribute condition (repository name), and that the pool has the Workload Identity User role on the service account.")
            return
    # 3. list the archive
    try:
        top = D.list_children(D.ROOT)
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", "")
        report(False, f"list the archive folder (HTTP {code})",
               "403: enable the Google Drive API in the service account's project. 404: share the folder with the service account's e-mail as Viewer.")
        return
    folders = [f["name"] for f in top if f["mimeType"] == D.FOLDER and f["name"] not in D.SKIP_DIRS]
    if not report(bool(top), f"list the archive folder: {len(top)} items, university folders: {', '.join(sorted(folders)) or 'none'}",
                  "the folder looks empty to this account: share the University Publications folder (not a copy) with the service account as Viewer"):
        return
    # 4. key file
    key = next((f for f in top if f["name"] == D.KEY_NAME and f["mimeType"] != D.FOLDER), None)
    if report(bool(key), f'access-key file "{D.KEY_NAME}" is visible', "upload the access-key file into the University Publications folder (not a sub-folder)"):
        try:
            n = len(D.get(f"{D.API}/drive/v3/files/{key['id']}", {"alt": "media", "supportsAllDrives": "true"}).content)
            report(n > 20, f"access-key file can be read ({n} bytes; contents not shown)", "the file looks empty or truncated")
        except Exception as e:
            report(False, f"read the access-key file ({e})")
    # 5. one paper
    sample = None
    for u in (f for f in top if f["mimeType"] == D.FOLDER and f["name"] not in D.SKIP_DIRS):
        for r in D.list_children(u["id"])[:20]:
            if r["mimeType"] == D.FOLDER and D.RESEARCHER_RE.match(r["name"]):
                sample = next((p for p in D.list_children(r["id"]) if D.PUB_RE.match(p["name"])), None)
                if sample:
                    sample["where"] = f"{u['name']} / {r['name']}"
                    break
        if sample:
            break
    if report(bool(sample), "found a paper file in a researcher folder", "no 'Publication NNN - …' files found in the university/researcher folders"):
        try:
            n = len(D.get(f"{D.API}/drive/v3/files/{sample['id']}", {"alt": "media", "supportsAllDrives": "true"}).content)
            report(n > 0, f"downloaded one paper ({n:,} bytes) from {sample['where']}")
        except Exception as e:
            report(False, f"download a paper ({e})", "the service account can list but not read files: check the folder sharing")


if __name__ == "__main__":
    main()
    print("\nAll checks passed: authentication is configured. You can enable the full build." if ok else "\nSome checks failed; fix them and run this check again.")
    sys.exit(0 if ok else 1)
