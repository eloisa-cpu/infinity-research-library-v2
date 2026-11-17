"""The meaning-search model: an open-source text-embedding model, run on the build server for documents and in each
person's browser for searches (the same model file in both places, so both produce the same "meaning coordinates").

  model    BAAI bge-small-en-v1.5 (MIT licence), ONNX int8 version prepared by Hugging Face (Xenova/bge-small-en-v1.5)
  runtime  transformers.js (Apache-2.0) with ONNX Runtime Web (MIT), from the npm registry

Every file is pinned to an exact version and checked against a SHA-256 fingerprint recorded here; a file that does not
match is refused. The files are pure model data / library code: nothing is ever uploaded, and no paper text or search
leaves the build server or the browser.
"""
import base64, hashlib, io, os, shutil, tarfile, time, urllib.request

MODEL_ID = "Xenova/bge-small-en-v1.5"
MODEL_REVISION = "ea104dacec62c0de699686887e3f920caeb4f3e3"
MODEL_FILES = {   # path in the model repository -> SHA-256
    "config.json": "fa73f90bf92c8cace1fbcb709626306f2bdbc9ea3e5b5f94b440df9b6aa56350",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "tokenizer_config.json": "9261e7d79b44c8195c1cada2b453e55b00aeb81e907a6664974b4d7776172ab3",
    "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    "onnx/model_quantized.onnx": "6c9c6101a956d62dfb5e7190c538226c0c5bb9cb27b651234b6df063ee7dbfe4",
}
RUNTIME_PACKAGE = "@huggingface/transformers"
RUNTIME_VERSION = "3.8.1"
RUNTIME_TARBALL = "https://registry.npmjs.org/@huggingface/transformers/-/transformers-3.8.1.tgz"
RUNTIME_INTEGRITY = "sha512-tsTk4zVjImqdqjS8/AOZg2yNLd1z9S5v+7oUPpXaasDRwEDhB+xnglK1k5cad26lL5/ZIaeREgWWy0bs9y9pPA=="
RUNTIME_FILES = ["dist/transformers.min.js", "dist/ort-wasm-simd-threaded.jsep.mjs", "dist/ort-wasm-simd-threaded.jsep.wasm", "LICENSE"]

DIM = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "   # bge: added to searches, not to documents
MAX_TOKENS = 384                                                            # title + abstract / opening fits


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _get(url, tries=4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "library-build"}), timeout=120) as r:
                return r.read()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))


def ensure(dest):
    """Download (once) and verify the model and the browser runtime into dest/. Returns dest. Raises on any mismatch."""
    mdir = os.path.join(dest, "models", MODEL_ID)
    for rel, sha in MODEL_FILES.items():
        p = os.path.join(mdir, rel)
        if not (os.path.exists(p) and _sha256(p) == sha):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            data = _get(f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{rel}")
            if hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError(f"model file {rel} does not match its recorded fingerprint; refusing it")
            with open(p, "wb") as fh:
                fh.write(data)
    rdir = os.path.join(dest, "runtime")
    stamp = os.path.join(rdir, ".version")
    if not (os.path.exists(stamp) and open(stamp).read() == RUNTIME_INTEGRITY):
        data = _get(RUNTIME_TARBALL)
        algo, b64 = RUNTIME_INTEGRITY.split("-", 1)
        if hashlib.new(algo, data).digest() != base64.b64decode(b64):
            raise RuntimeError("the transformers.js package does not match its recorded fingerprint; refusing it")
        shutil.rmtree(rdir, ignore_errors=True); os.makedirs(rdir)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for rel in RUNTIME_FILES:
                m = tf.getmember("package/" + rel)
                with open(os.path.join(rdir, os.path.basename(rel)), "wb") as fh:
                    fh.write(tf.extractfile(m).read())
        open(stamp, "w").write(RUNTIME_INTEGRITY)
    return dest


def install_into_site(dest, site):
    """Copy the verified model and runtime into the built site (served from the site itself, never from elsewhere)."""
    shutil.copytree(os.path.join(dest, "models"), os.path.join(site, "models"), dirs_exist_ok=True)
    out = os.path.join(site, "vendor", "transformers")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(os.path.join(dest, "runtime")):
        if not f.startswith("."):
            shutil.copy(os.path.join(dest, "runtime", f), out)


class Embedder:
    """bge-small with ONNX Runtime on the CPU: CLS pooling, unit length (same as the browser's transformers.js)."""

    def __init__(self, dest, threads=1):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        mdir = os.path.join(dest, "models", MODEL_ID)
        self.tok = Tokenizer.from_file(os.path.join(mdir, "tokenizer.json"))
        self.tok.enable_truncation(MAX_TOKENS)
        self.tok.no_padding()
        so = ort.SessionOptions(); so.intra_op_num_threads = threads; so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(os.path.join(mdir, "onnx", "model_quantized.onnx"), so, providers=["CPUExecutionProvider"])

    def embed(self, texts, batch=16):
        import numpy as np
        out = []
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))     # similar lengths together: less padding
        res = [None] * len(texts)
        for k in range(0, len(order), batch):
            idx = order[k:k + batch]
            encs = self.tok.encode_batch([texts[i] for i in idx])
            L = max(len(e.ids) for e in encs)
            ids = np.zeros((len(encs), L), np.int64); am = np.zeros_like(ids)
            for r, e in enumerate(encs):
                ids[r, :len(e.ids)] = e.ids; am[r, :len(e.ids)] = 1
            v = self.sess.run(None, {"input_ids": ids, "attention_mask": am, "token_type_ids": np.zeros_like(ids)})[0][:, 0]
            v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
            for r, i in enumerate(idx):
                res[i] = v[r].astype(np.float32)
        return res


def doc_text(title, body, limit=1800):
    """What represents a document in meaning search: its title and its abstract (or opening text)."""
    import re
    lines = body.split("\n")
    start = 0
    for i, l in enumerate(lines[:200]):
        if re.fullmatch(r"\s*(#+\s*)?(\d+\.?\s*)?(abstract|summary)[.:]?\s*", l, re.I):
            start = i + 1
            break
    text = re.sub(r"\s+", " ", " ".join(lines[start:])).strip()[:limit]
    return (title.strip() + ". " + text).strip(". ").strip()
