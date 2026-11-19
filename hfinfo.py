"""Hugging Face metadata for the model browser: search, model stats, benchmarks.

Uses the public Hub HTTP API directly (stdlib only) with a small in-memory cache
so typing in the UI doesn't hammer the Hub.
"""
import json, os, re, threading, time, urllib.error, urllib.parse, urllib.request

import quants

HUB = "https://huggingface.co"
TTL = 900                      # seconds to cache Hub responses
_cache, _clock = {}, threading.Lock()

EXPAND = ["author", "downloads", "downloadsAllTime", "likes", "lastModified", "createdAt",
          "pipeline_tag", "library_name", "tags", "gated", "private", "cardData",
          "safetensors", "config", "evalResults", "trendingScore"]


class HubError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg); self.status = status


def _get(url, raw=False, limit=4_000_000):
    with _clock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < TTL:
            if isinstance(hit[1], HubError):
                raise hit[1]
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "llm-manager/1.0"})
    tok = os.environ.get("HF_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(limit)
        val = body.decode("utf-8", "replace") if raw else json.loads(body)
    except urllib.error.HTTPError as e:
        err = HubError(e.code, {401: "not found, or private/gated (needs HF_TOKEN)", 403: "gated: accept the terms on huggingface.co",
                                404: "not found"}.get(e.code, f"Hub error {e.code}"))
        with _clock:
            _cache[url] = (time.time(), err)
        raise err
    with _clock:
        _cache[url] = (time.time(), val)
        if len(_cache) > 500:
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
                _cache.pop(k, None)
    return val


def clear_cache():
    """Drop cached Hub responses (e.g. after the HF token changes: gated repos look different)."""
    with _clock:
        _cache.clear()


def search(q, lib=None, limit=12):
    params = [("search", q), ("sort", "downloads"), ("direction", "-1"), ("limit", str(limit))]
    if lib:
        params.append(("filter", lib))
    for f in ("downloads", "likes", "library_name", "pipeline_tag", "lastModified", "gated"):
        params.append(("expand[]", f))
    rows = _get(f"{HUB}/api/models?{urllib.parse.urlencode(params)}")
    return [{"id": r["id"], "downloads": r.get("downloads"), "likes": r.get("likes"),
             "library": r.get("library_name"), "pipeline": r.get("pipeline_tag"),
             "lastModified": r.get("lastModified"), "gated": r.get("gated")} for r in rows]


def _info(repo):
    q = "&".join(f"expand[]={e}" for e in EXPAND)
    return _get(f"{HUB}/api/models/{repo}?{q}")


def _tree(repo):
    files = _get(f"{HUB}/api/models/{repo}/tree/main?recursive=true")
    return [f for f in files if f.get("type") == "file"]


def _bases(card):
    b = (card or {}).get("base_model")
    if isinstance(b, str):
        return [b]
    return [x for x in (b or []) if isinstance(x, str)]


def _eval_rows(info):
    rows = []
    for e in info.get("evalResults") or []:
        d = e.get("data") or {}
        ds = d.get("dataset") or {}
        src = d.get("source") or {}
        rows.append({"benchmark": _pretty(ds.get("id"), ds.get("task_id")), "dataset": ds.get("id"),
                     "task": ds.get("task_id"), "value": d.get("value"), "date": d.get("date"),
                     "notes": d.get("notes"), "source": src.get("name"), "source_url": src.get("url"),
                     "verified": bool(e.get("verified"))})
    # legacy model-index in the card metadata
    for mi in ((info.get("cardData") or {}).get("model-index") or []):
        for res in mi.get("results") or []:
            ds = res.get("dataset") or {}
            for m in res.get("metrics") or []:
                rows.append({"benchmark": ds.get("name") or ds.get("type"), "dataset": ds.get("type"),
                             "task": m.get("name") or m.get("type"), "value": m.get("value"),
                             "verified": bool(m.get("verified")), "source": "model-index"})
    # group sub-tasks under their benchmark, keeping the card's order
    groups, seen = {}, set()
    for r in rows:
        k = (r["benchmark"], r.get("task"), str(r["value"]))
        if k in seen:
            continue
        seen.add(k)
        groups.setdefault(r["benchmark"], []).append(r)
    out = []
    for name, rs in groups.items():
        head = next((r for r in rs if str(r.get("task")).lower() in ("overall", "mean", "average", "avg", "main")), rs[0])
        out.append({"benchmark": name, "dataset": head.get("dataset"), "task": head.get("task") if len(rs) > 1 else None,
                    "value": head["value"], "notes": head.get("notes"), "verified": any(r["verified"] for r in rs),
                    "subtasks": [{"task": r.get("task"), "value": r["value"]} for r in rs if r is not head]})
    return out


def _pretty(ds_id, task):
    name = (ds_id or "").split("/")[-1] or task or "?"
    return name.replace("_", " ")


TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _clean_md(s):
    s = re.sub(r"\*\*|__|`", "", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [_clean_md(c) for c in line.split("|")]


def readme_bits(text, max_tables=5, max_rows=25):
    """Description paragraph + markdown pipe tables (with nearest heading) from a model card."""
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    lines = body.splitlines()
    desc, heading, tables, i = None, None, [], 0
    in_code = False
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            in_code = not in_code; i += 1; continue
        if in_code:
            i += 1; continue
        if ln.startswith("#"):
            heading = _clean_md(ln.lstrip("#")); i += 1; continue
        if "|" in ln and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]) and len(tables) < max_tables:
            head = _split_row(ln); rows = []; i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                if len(rows) < max_rows:
                    rows.append(_split_row(lines[i]))
                i += 1
            tables.append({"title": heading, "head": head, "rows": rows})
            continue
        s = ln.strip()
        if desc is None and len(s) > 80 and not s.startswith(("<", ">", "!", "|", "-", "*")):
            desc = _clean_md(s)[:700]
        i += 1
    return desc, tables


def _config_stats(repo):
    try:
        cfg = _get(f"{HUB}/{repo}/resolve/main/config.json", limit=3_000_000)
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    tc = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else {}

    def pick(*keys):
        for src in (cfg, tc):
            for k in keys:
                v = src.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return v
        return None
    q = cfg.get("quantization_config") or cfg.get("quantization") or {}
    return {"context": pick("max_position_embeddings", "max_seq_len", "seq_length", "n_ctx", "model_max_length"),
            "layers": pick("num_hidden_layers", "n_layer", "num_layers"),
            "hidden": pick("hidden_size", "d_model", "n_embd"),
            "experts": pick("num_experts", "num_local_experts", "n_routed_experts"),
            "experts_active": pick("num_experts_per_tok", "num_experts_per_token", "moe_top_k"),
            "vocab": pick("vocab_size"),
            "quant_bits": q.get("bits") if isinstance(q, dict) else None,
            "model_type": cfg.get("model_type"),
            "architecture": (cfg.get("architectures") or [None])[0]}


def model(repo):
    info = _info(repo)
    card = info.get("cardData") or {}
    files = _tree(repo)
    size = sum(f.get("size") or 0 for f in files)
    weights = [f for f in files if re.search(r"\.(safetensors|gguf|bin|pt|pth|npz|h5|msgpack|onnx|tflite)$", f["path"])]
    try:
        desc, tables = readme_bits(_get(f"{HUB}/{repo}/raw/main/README.md", raw=True, limit=400_000))
    except HubError:
        desc, tables = None, []

    st = info.get("safetensors") or {}
    params = st.get("total")
    quantized = card.get("base_model_relation") == "quantized" or bool((info.get("config") or {}).get("quantization_config"))

    # benchmarks: this repo, else walk base_model chain (quant -> finetune -> base)
    bench, bench_from = _eval_rows(info), repo
    base_chain, cur, base_params = [], info, None
    for _ in range(3):
        bases = _bases(cur.get("cardData"))
        if not bases:
            break
        try:
            cur = _info(bases[0])
        except HubError:
            base_chain.append(bases[0]); break
        base_chain.append(cur["id"])
        if base_params is None and (cur.get("safetensors") or {}).get("total"):
            base_params = cur["safetensors"]["total"]
        if not bench:
            rows = _eval_rows(cur)
            if rows:
                bench, bench_from = rows, cur["id"]
        if bench and base_params:
            break

    return {"kind": "model", "id": info.get("id", repo), "author": info.get("author"),
            "downloads": info.get("downloads"), "downloads_all": info.get("downloadsAllTime"),
            "likes": info.get("likes"), "trending": info.get("trendingScore"),
            "created": info.get("createdAt"), "modified": info.get("lastModified"),
            "pipeline": info.get("pipeline_tag"), "library": info.get("library_name"),
            "license": card.get("license_name") or card.get("license"), "gated": info.get("gated"),
            "tags": [t for t in info.get("tags") or [] if ":" not in t][:16],
            "size": size, "file_count": len(files), "weight_files": len(weights),
            **(quants.describe({f["path"]: f.get("size") or 0 for f in files}) or {"variants": None}),
            "params": None if quantized else params, "base_params": base_params if quantized else None,
            "quantized": quantized, "base_chain": base_chain,
            "config": _config_stats(repo), "description": desc, "card_tables": tables,
            "benchmarks": bench, "benchmarks_from": bench_from if bench else None}
