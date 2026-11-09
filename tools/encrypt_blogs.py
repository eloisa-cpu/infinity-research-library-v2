#!/usr/bin/env python3
"""Encrypt the blog collection (candidates + documents JSON) into data/blogs.enc with the archive access key.

  python3 tools/encrypt_blogs.py --plain blogs.json --keyfile "Library access key.txt" [--out data/blogs.enc]
  python3 tools/encrypt_blogs.py --rekey --old-keyfile old.txt --keyfile new.txt     (after changing the key file)
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libcore import encrypt, decrypt, key_from_keyfile  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--plain"); ap.add_argument("--keyfile", required=True); ap.add_argument("--old-keyfile")
ap.add_argument("--rekey", action="store_true")
ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "blogs.enc"))
a = ap.parse_args()
key = key_from_keyfile(open(a.keyfile, "rb").read())
if a.rekey:
    data = decrypt(open(a.out, "rb").read(), key_from_keyfile(open(a.old_keyfile, "rb").read()))
else:
    data = open(a.plain, "rb").read()
open(a.out, "wb").write(encrypt(data, key))
print("wrote", a.out, len(data), "bytes of JSON (encrypted)")
