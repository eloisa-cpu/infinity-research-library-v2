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


# Fast equivalents used by the index builder: count each distinct word of a document once and clean it up once
# (cached), instead of handling every occurrence. Results equal Counter(index_tokens(t)) / Counter(semantic_tokens(t)).
_IDX_CACHE, _SEM_CACHE = {}, {}


def _clean_index_word(w):
    if len(w) < 2 or len(w) > 40 or w in STOP:
        return ""
    w = stem(w)
    return "" if w in STOP else w


def _clean_semantic_word(w):
    if len(w) < 2 or w.isdigit() or w in STOP:
        return ""
    w = stem(w)
    return "" if w in STOP else w


def _count(raw, cache, clean):
    from collections import Counter
    if len(cache) > 3_000_000:
        cache.clear()
    out = Counter()
    for w, c in Counter(raw).items():
        s = cache.get(w)
        if s is None:
            s = cache[w] = clean(w)
        if s:
            out[s] += c
    return out


def index_counts(text):
    return _count(WORD_RE.findall(unicodedata.normalize("NFKC", text).lower()), _IDX_CACHE, _clean_index_word)


def semantic_counts(text):
    return _count(ASCII_RE.findall(text.lower()), _SEM_CACHE, _clean_semantic_word)


def fnv1a(s):
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


# ---------------------------------------------------------------- researcher folders
def _slug(s):
    return re.sub(r"[^\w]+", "-", unicodedata.normalize("NFKC", s).lower()).strip("-")


def parse_researcher_folder(folder, univ):
    """Read a researcher folder name from the archive. Folder styles in use:
        Name (C40481)                     Top 50 list            Name (A5014441633)        OpenAlex author
        Name (STA-03544)                  person key             Name (Google Scholar)     matched via Google Scholar
        Name (name match - review)        uncertain name match   _Unassigned (review)      papers without a researcher
        Name (ORCID 0000-0002-1825-0097)  ORCID identifier       Name                      folder name only
    Drive adds " (1)", " (2)" to repeated folder names; those are the same researcher and are merged.
    Returns {name, key, match, review}; key identifies the researcher across folders (stable between runs).
    """
    name = re.sub(r"(?: \(\d{1,3}\))+$", "", folder.strip())
    if name.lower().startswith("_unassigned"):
        return {"name": f"Unassigned papers ({univ})", "key": "UNASSIGNED-" + _slug(univ), "match": "Unassigned (review)", "review": True}
    m = re.match(r"^(?P<name>.+?) \((?P<label>[^()]+)\)$", name)
    if not m:
        return {"name": name, "key": "NAME-" + _slug(univ) + "-" + _slug(name), "match": "Folder name", "review": False}
    nm, label = m.group("name").strip(), m.group("label").strip()
    if re.fullmatch(r"C\d+", label, re.I):
        return {"name": nm, "key": label.upper(), "match": "Top 50 list", "review": False}
    if re.fullmatch(r"A\d+", label, re.I):
        return {"name": nm, "key": label.upper(), "match": "OpenAlex", "review": False}
    mo = re.fullmatch(r"ORCID[:\s]*(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])", label, re.I)
    if mo:
        return {"name": nm, "key": "ORCID-" + mo.group(1).upper(), "match": "ORCID", "review": False}
    if re.fullmatch(r"[A-Za-z]{2,4}-\d+", label):
        return {"name": nm, "key": label.upper(), "match": "Person key", "review": False}
    if label.lower() == "google scholar":
        return {"name": nm, "key": "GS-" + _slug(univ) + "-" + _slug(nm), "match": "Google Scholar", "review": False}
    if "review" in label.lower():
        return {"name": nm, "key": "NM-" + _slug(univ) + "-" + _slug(nm), "match": "Name match (review)", "review": True}
    return {"name": nm, "key": "L-" + _slug(univ) + "-" + _slug(nm + " " + label), "match": label, "review": False}


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


# ---------------------------------------------------------------- contactability (can the hiring team actually reach this person?)
# Every candidate gets a tier, shown in search results and used to rank: "ready" first, then "limited", then
# "unavailable" (kept in the library, hidden at the end of results). Reasons are shown on the candidate's page.
_HAN = re.compile(r"[㐀-鿿]")
_KANA_HANGUL = re.compile(r"[぀-ヿ가-힯]")
_FANCY = re.compile(r"[\U0001D400-\U0001D7FF]")          # mathematical "fancy" letters used in nicknames
_BOT = re.compile(r"(^|[-_ ])bot($|[-_ ])|github-bot|\bteam$|-team\b|\bofficial$", re.I)
_PSEUDO_NOTE = re.compile(r"pseudonym|real name not|full name not|name not (stated|given|shown|found)|only a (handle|username)", re.I)
_ORG_NOTE = re.compile(r"(organi[sz]ation|company|team|project|group|bot) account|not an individual|not a (real )?person\b(?!al)", re.I)
_DEAD_NOTE = re.compile(r"\b404\b|site not found|not found|no longer exists|rendered empty|no content|access ?denied|unreachable|parked|domain for sale", re.I)
_INACTIVE_NOTE = re.compile(r"inactive|dormant|outdated \(last update|last update (19|20[01]\d|202[01])|since (19|20[01]\d|202[01])\b|latest visible post (19|20[01]\d|202[01])", re.I)
_OPEN_NOTE = re.compile(r"\b(open to|seeking|looking for)\b[^.;]{0,60}\b(roles?|opportunit\w*|positions?|jobs?|work)", re.I)
_NOT_OPEN_NOTE = re.compile(r"not open to|not looking", re.I)


def looks_like_real_name(name):
    """True for full personal names ("Ada Kernelton", "Meng, Hengyu", "赵晨阳"); False for handles and nicknames."""
    n = (name or "").strip()
    if not n or _FANCY.search(n) or _BOT.search(n):
        return False
    if _HAN.search(n):
        han = "".join(_HAN.findall(n))
        return 2 <= len(han) <= 4                       # Chinese names are 2-4 characters; longer runs are nicknames
    if _KANA_HANGUL.search(n) and not re.search(r"[A-Za-z]", n):
        return bool(re.search(r"[가-힯]{2,4}", n))  # Korean names; kana-only strings are usually nicknames
    if re.search(r"\d|_|@", n):
        return False
    words = [w for w in re.split(r"[\s,]+", n.replace("(", " ").replace(")", " ")) if w]
    alpha = [w for w in words if re.match(r"[^\W\d_]", w)]
    if len(alpha) < 2:
        return False                                    # one word: a handle or a first name only
    return True


def contactability(c, github_status=None):
    """c: candidate dict (name, status, note, github, group...). github_status: "ok" / "missing" / None (not checked).
    Returns {"tier": "ready" | "limited" | "unavailable", "reasons": [...], "open": True/False/None}."""
    note, status = c.get("note") or "", c.get("status") or ""
    reasons, tier = [], "ready"

    def worse(t, why):
        nonlocal tier
        order = {"ready": 0, "limited": 1, "unavailable": 2}
        if order[t] > order[tier]:
            tier = t
        reasons.append(why)

    if (c.get("key") or c.get("researcherKey") or "").startswith("UNASSIGNED") or (c.get("match") or "").startswith("Unassigned"):
        worse("unavailable", "Not a person: papers without an identified researcher")
    if _BOT.search(c.get("name") or "") or _BOT.search(c.get("github") or "") or _ORG_NOTE.search(note):
        worse("unavailable", "Organization, team or bot account, not an individual")
    if github_status == "missing":
        worse("unavailable", "GitHub account unavailable (suspended, deleted or hidden)")
    named_on_github = "name" in (c.get("filled") or {}).get("fields", [])
    if not looks_like_real_name(c.get("name")) or (_PSEUDO_NOTE.search(note) and not named_on_github):
        worse("limited", "No full real name (handle or nickname only)")
    if status.startswith("No data") or _DEAD_NOTE.search(note):
        worse("limited", "Website unreachable, empty or gone")
    elif status.startswith("Skipped"):
        worse("limited", "Listed link is not their own site")
    if _INACTIVE_NOTE.search(note):
        worse("limited", "No recent public activity")
    is_open = False if _NOT_OPEN_NOTE.search(note) else (True if _OPEN_NOTE.search(note) or c.get("hireable") else None)
    return {"tier": tier, "reasons": reasons, "open": is_open}


# ---------------------------------------------------------------- LinkedIn profile links (only ones the person published)
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub)/([A-Za-z0-9\-_%.]{3,100})", re.I)


def linkedin_urls(text):
    """All LinkedIn personal-profile links in a text or HTML page, normalized to https://www.linkedin.com/in/<id>/."""
    out = []
    for m in LINKEDIN_RE.finditer(text or ""):
        u = "https://www.linkedin.com/in/" + m.group(1).strip("/.").split("?")[0] + "/"
        if u.lower() not in [x.lower() for x in out]:
            out.append(u)
    return out


def pick_linkedin(urls, name=""):
    """One profile link, or None when a page links several different people and none matches the name."""
    if len(urls) == 1:
        return urls[0]
    parts = [p for p in re.split(r"[^a-z]+", (name or "").lower()) if len(p) > 2]
    hits = [u for u in urls if any(p in u.lower() for p in parts)]
    return hits[0] if len(hits) == 1 else None
