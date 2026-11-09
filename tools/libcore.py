"""Shared pieces of the library pipeline: tokenizers, encryption, Markdown parsing.

The browser re-implements index_tokens(), stem() and the encryption format in index.html;
keep them in sync (tests/e2e.js checks parity).
"""
import gzip, hashlib, os, re, unicodedata

# ---------------------------------------------------------------- encryption
# Every data file the site serves is gzip-compressed, then encrypted with AES-256-GCM:
#   file = 12-byte IV || ciphertext+tag
# The key is SHA-256 of the access-key file kept in the Google Drive archive, so only people who can read
# that Drive file (i.e. people the archive is shared with) can decrypt the library.

def key_from_keyfile(raw: bytes) -> bytes:
    return hashlib.sha256(raw.strip()).digest()


def encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    return iv + AESGCM(key).encrypt(iv, gzip.compress(data, compresslevel=6, mtime=0), None)


def decrypt(blob: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return gzip.decompress(AESGCM(key).decrypt(blob[:12], blob[12:], None))


# ---------------------------------------------------------------- tokenizers
STOPWORDS = sorted(set("""
a about above after again against all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each else etc even ever every few for from further get gets got had has have having he
her here hers herself him himself his how however i if in into is it its itself just let like made make makes many may me might
more most much must my myself no nor not now of off on once one only or other our ours ourselves out over own per really same
see she should since so some still such than that the their theirs them themselves then there these they this those through thus
to too under until up upon us use used using very via was way we well were what when where which while who whom why will with
within without would yet you your yours yourself yourselves www http https com org html htm pdf
""".split()))
STOP = set(STOPWORDS)
WORD_RE = re.compile(r"[^\W_]+")          # letters and digits in any script (JS: /[\p{L}\p{N}]+/gu)
ASCII_RE = re.compile(r"[a-z0-9]+")


def stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def index_tokens(text):
    """Full-text keyword index tokens: Unicode words, lower-cased, light plural stemming, stopwords dropped."""
    out = []
    for w in WORD_RE.findall(unicodedata.normalize("NFKC", text).lower()):
        if len(w) < 2 or len(w) > 40 or w in STOP:
            continue
        w = stem(w)
        if w not in STOP:
            out.append(w)
    return out


def semantic_tokens(text):
    """Tokens for the meaning (LSA) index: ASCII words, numbers dropped (as before)."""
    out = []
    for w in ASCII_RE.findall(text.lower()):
        if len(w) < 2 or w.isdigit() or w in STOP:
            continue
        w = stem(w)
        if w not in STOP:
            out.append(w)
    return out


def fnv1a(s):
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


# ---------------------------------------------------------------- Markdown archive files
META_RE = re.compile(r"\*\*(?P<k>[^:*]{1,60}):\*\*\s*(?P<v>[^|\n]*?)\s*(?=\s\|\s|\s{2}\|\s|$)")
NEEDS_REVIEW_RE = re.compile(r"needs[- ]review|name pending|possible misattribution", re.I)


def clean_inline(s):
    s = s.replace("**", "")
    s = re.sub(r"(?<![\w*\\])\*(\S[^*\n]*?)\*(?![\w*])", r"\1", s)
    s = re.sub(r"<!--.*?-->", "", s)
    return s.strip()


def md_to_blocks(body):
    """Markdown -> [[kind, text]] with kind h (heading), p (paragraph) or pre (table / code / display math)."""
    blocks, para = [], []

    def flush():
        if para:
            t = clean_inline(" ".join(x.strip() for x in para))
            if t:
                blocks.append(["p", t])
            para.clear()
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            flush()
            j, code = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j]); j += 1
            if any(c.strip() for c in code):
                blocks.append(["pre", "\n".join(code).rstrip()])
            i = j + 1
            continue
        if s == "$$":
            flush()
            j, math = i + 1, []
            while j < len(lines) and lines[j].strip() != "$$":
                math.append(lines[j]); j += 1
            if any(m.strip() for m in math):
                blocks.append(["pre", "\n".join(math).strip()])
            i = j + 1
            continue
        if s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                r = lines[i].strip()
                if not re.match(r"^\|[\s:|-]+\|$", r):
                    rows.append(r)
                i += 1
            if rows:
                blocks.append(["pre", "\n".join(rows)])
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            t = clean_inline(m.group(2))
            if t:
                blocks.append(["h", t])
        elif not s or re.fullmatch(r"<!--.*-->", s):
            flush()
        elif s.startswith("- ") or re.match(r"^\d+[.)]\s", s):
            flush(); para.append(s); flush()
        else:
            para.append(s.lstrip("> ") if s.startswith(">") else s)
        i += 1
    flush()
    return blocks


def parse_archive_md(text):
    """Split an archive Markdown file into (metadata dict, body Markdown).

    Handles both layouts found in the Drive archive:
      Top 50:   # Title / ## Archive metadata / - **Key:** value ... / --- / ## Full publication text / > note / text
      Run2/3:   # Title / - **Key:** value ... (some lines hold several "**Key:** value" separated by |) / --- / text
    """
    head, sep, rest = text.partition("\n---\n")
    if not sep:
        head, rest = "", text
    meta = {}
    for line in head.split("\n"):
        line = line.strip()
        if line.startswith("# ") and "title" not in meta:
            meta["_h1"] = line[2:].strip()
            continue
        for m in META_RE.finditer(line):
            meta.setdefault(m.group("k").strip(), m.group("v").strip())
    m = re.search(r"^## Full publication text\s*$", rest, re.M)
    body = rest[m.end():] if m else rest
    body = re.sub(r"^\s*> Extracted verbatim[^\n]*\n", "", body, count=1)
    return meta, body


def meta_get(meta, *keys):
    for k in keys:
        v = meta.get(k)
        if v and v not in ("-", "—", "None"):
            return v
    return ""
