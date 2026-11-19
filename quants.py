"""Split a repo's files into quantization variants (GGUF repos ship one file set per quant).

A GGUF repo such as `unsloth/Qwen3-8B-GGUF` holds 20+ quants of the same model; nobody wants
all of them. `group()` sorts every file into:

- shared:   files every variant needs (config, README, tokenizer, the vision projector ...)
- variants: one file set per quant (`Q4_K_M`, `UD-Q4_K_XL`, `BF16` ...), sharded files kept together

Repos without at least two variants (MLX, Transformers, a single GGUF) return no variants and
are downloaded whole, as before.
"""
import re

GGUF = re.compile(r"\.gguf$", re.I)
WEIGHTS = re.compile(r"\.(safetensors|bin|pt|pth|npz|h5|msgpack|onnx|tflite)$|\.(safetensors|bin)\.index\.json$", re.I)
SHARD = re.compile(r"[-.]\d{3,5}-of-\d{3,5}$", re.I)
QUANT = re.compile(r"(?:^|[-_.])((?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|TQ\d_\d|BF16|FP16|FP32|F16|F32|MXFP4(?:_MOE)?))$", re.I)
MMPROJ = re.compile(r"(^|/)mmproj", re.I)
IMATRIX = re.compile(r"imatrix", re.I)  # calibration data for making quants; not needed to run one
ORIGINAL = "safetensors"   # variant id for a GGUF repo's non-GGUF weights, if it has any

# what most people mean by "the" quant of a model: the 4-bit K-quants llama.cpp, LM Studio and
# Ollama default to, then the closest alternatives
PREFERRED = ["Q4_K_M", "UD-Q4_K_XL", "Q4_K_XL", "Q4_K_S", "UD-Q4_K_M", "IQ4_XS", "IQ4_NL", "Q4_0",
             "MXFP4_MOE", "MXFP4", "Q5_K_M", "UD-Q5_K_XL", "Q6_K", "Q8_0"]
MMPROJ_PREF = ["F16", "BF16", "Q8_0", "F32"]


def parse(path):
    """'Q4/Qwen3-8B-UD-Q4_K_XL-00001-of-00002.gguf' -> ('UD-Q4_K_XL', 'qwen3-8b', 'Qwen3-8B-UD-Q4_K_XL').

    base is the file name without the quant (None if no quant was recognised); the quant falls
    back to the folder name, then the file name."""
    parts = path.split("/")
    stem = SHARD.sub("", GGUF.sub("", parts[-1]))
    m = QUANT.search(stem)
    if m:
        q = m.group(1).upper()
        return {"FP16": "F16", "FP32": "F32"}.get(q, q), stem[:m.start(1)].rstrip("-_.").lower(), stem
    if len(parts) > 1 and (m := QUANT.fullmatch(parts[-2])):
        return m.group(1).upper(), stem.lower(), stem
    return (parts[-2] if len(parts) > 1 else stem), None, stem


def quant_of(path):
    return parse(path)[0]


def bits(q):
    """Rough bits per weight, for picking a sensible default when none of PREFERRED exists."""
    q = q.upper().removeprefix("UD-")
    if q in ("BF16", "F16"):
        return 16
    if q == "F32":
        return 32
    if q.startswith("MXFP4"):
        return 4
    m = re.match(r"T?I?Q(\d)", q)
    return int(m.group(1)) if m else None


def _size(v):
    return v["size"] if isinstance(v, dict) else (v or 0)


def group(files):
    """files: {path: size | {"size": ...}} -> None (download everything) or
    {"shared": [paths], "variants": {id: [paths]}}"""
    shared, ggufs, mmproj, other_weights, imatrix = [], [], {}, [], []
    for p in files:
        if IMATRIX.search(p):  # also imatrix *.gguf files: calibration data, not a model
            imatrix.append(p)
        elif GGUF.search(p):
            if MMPROJ.search(p):
                mmproj.setdefault(quant_of(p), []).append(p)
            else:
                ggufs.append((p, *parse(p)))
        elif WEIGHTS.search(p):
            other_weights.append(p)
        else:
            shared.append(p)
    # some repos also carry a different model (e.g. an eagle3 draft model next to gpt-oss):
    # quants of the main model (most bytes) are named by quant alone, anything else by file name
    by_base = {}
    for p, q, base, stem in ggufs:
        if base is not None:
            by_base[base] = by_base.get(base, 0) + _size(files[p])
    main = max(by_base, key=by_base.get) if by_base else None
    # the same quant can ship twice in different folders (e.g. `Q4_K_XL/` and a re-made `Q4_K_XL-V2/`):
    # those are separate downloads, named by folder
    folders = {}
    for p, q, base, stem in ggufs:
        if base == main:
            folders.setdefault(q, set()).add(p.rpartition("/")[0])
    variants, quant_of_key = {}, {}
    for p, q, base, stem in ggufs:
        if base != main:
            k = stem
        elif len(folders[q]) > 1:
            k = p.rpartition("/")[0].rpartition("/")[2] or q
        else:
            k = q
        variants.setdefault(k, []).append(p)
        if base == main:
            quant_of_key[k] = q
    if other_weights and variants:
        variants[ORIGINAL] = other_weights
    else:
        shared += other_weights
    if len(variants) < 2:
        return None
    if mmproj:  # vision models ship the projector in several precisions; one is enough
        pick = next((k for k in MMPROJ_PREF if k in mmproj), sorted(mmproj)[0])
        shared += mmproj[pick]
    return {"shared": sorted(shared), "variants": {k: sorted(v) for k, v in variants.items()},
            "quants": quant_of_key}  # variant id -> its quant, for quants of the main model only


def default(variants):
    """The variant id most people want: a 4-bit K-quant, else the one closest to 4-5 bits."""
    for q in PREFERRED:
        if q in variants:
            return q
    scored = [(abs(b - 4.5), k) for k in variants if (b := bits(k)) is not None]
    return min(scored)[1] if scored else sorted(variants)[0]


def describe(files):
    """For the browse card: variants with download sizes (shared files included), smallest first."""
    g = group(files)
    if not g:
        return None
    shared_size = sum(_size(files[p]) for p in g["shared"])
    out = []
    for k, paths in g["variants"].items():
        out.append({"id": k, "size": shared_size + sum(_size(files[p]) for p in paths),
                    "files": len(g["shared"]) + len(paths), "weight_files": len(paths),
                    "bits": bits(g["quants"].get(k, k)), "original": k == ORIGINAL,
                    "quant": k in g["quants"]})  # False: another model's file
    out.sort(key=lambda v: (v["size"], v["id"]))
    return {"variants": out, "default_variant": default(g["variants"]), "shared_size": shared_size}


def select(files, variant, strict=True):
    """Subset of a manifest {path: info} for one variant. variant=None keeps everything."""
    if not variant:
        return files
    g = group(files)
    if not g or variant not in g["variants"]:
        if strict:
            raise ValueError(f"quant {variant} is not in this repo"
                             + (f" (has: {', '.join(sorted(g['variants']))})" if g else ""))
        return {}
    keep = set(g["shared"]) | set(g["variants"][variant])
    return {p: i for p, i in files.items() if p in keep}
