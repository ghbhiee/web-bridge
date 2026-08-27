"""Find the right tool by intent, not by which URL happens to be open.

Three things were wrong with looking tools up by `match` alone:

  * A tool only surfaced when the current URL matched it. Asking "查电影在哪些
    国家能看" from google.com found nothing, though the tool existed — the user's
    intent was ignored in favour of where they happened to be standing.
  * Retrieval was a substring test, so 表格 and "table" were different questions.
  * Nothing accounted for whether a tool actually WORKS. A capability that fails
    every time ranked exactly like one that has succeeded fifty times.

So: rank by relevance × track record, with the URL as a *boost* rather than a
filter, and hand back only the few best. Context length is a cost — the answer
to "which tool" must be short, not a catalogue.

Relevance has two backends. The built-in lexical scorer is the default: the
corpus is a dozen short descriptions, it needs no dependencies, and it works on
Windows. `qmd` (a local hybrid/vector search) is used instead when it is
installed and WEB_BRIDGE_QMD=1, for semantic matches lexical scoring misses.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from typing import Optional

import capabilities
import journal

# Words that mean the same thing to a user but share no characters. The corpus is
# bilingual, and "table" vs "表格" is the single most common miss.
SYNONYMS = {
    "表格": ["table", "tables", "grid"],
    "表": ["table"],
    "列表": ["list", "listing", "items"],
    "抽取": ["extract", "scrape", "抓取", "提取"],
    "提取": ["extract", "scrape", "抓取", "抽取"],
    "抓取": ["extract", "scrape", "提取", "抽取", "collect"],
    "搜索": ["search", "query", "find", "查询", "查"],
    "查询": ["search", "query", "find", "搜索", "查"],
    "翻页": ["paginate", "pagination", "next page", "分页"],
    "正文": ["article", "content", "body", "文章"],
    "文章": ["article", "content", "正文"],
    "字幕": ["subtitle", "transcript", "caption", "文字稿"],
    "美化": ["restyle", "beautify", "style", "排版"],
    "发帖": ["post", "publish", "tweet", "发布"],
    "电影": ["movie", "film", "title", "剧集", "netflix"],
    "国家": ["country", "countries", "region"],
    "邮件": ["mail", "email", "message"],
    "table": ["表格", "表"],
    "list": ["列表"],
    "extract": ["提取", "抽取", "抓取"],
    "search": ["搜索", "查询", "查"],
    "article": ["正文", "文章"],
    "subtitle": ["字幕", "文字稿"],
    "transcript": ["字幕", "文字稿"],
    "post": ["发帖", "发布"],
    "movie": ["电影", "剧集"],
    "country": ["国家"],
}

TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")


def tokens(text: str) -> list[str]:
    """Words for latin text, single characters for CJK (which has no spaces)."""
    return TOKEN_RE.findall((text or "").lower())


def expand(terms: list[str]) -> set[str]:
    out = set(terms)
    for t in terms:
        for syn in SYNONYMS.get(t, []):
            out.update(tokens(syn))
    # CJK bigrams: 电影 as a unit matters more than 电 and 影 apart
    for a, b in zip(terms, terms[1:]):
        if len(a) == 1 and len(b) == 1 and "一" <= a <= "鿿":
            joined = a + b
            out.add(joined)
            out.update(tokens(" ".join(SYNONYMS.get(joined, []))))
    return out


def _field_text(cap: dict) -> dict:
    return {
        "title": cap.get("title") or "",
        "description": cap.get("description") or "",
        "id": cap.get("id", "").replace("-", " "),
        "match": " ".join(cap.get("match") or []),
        "params": " ".join((cap.get("params") or {}).keys()),
    }


# Title and description are what the author wrote to be found by; the id is
# incidental and match is where it applies, not what it does.
FIELD_WEIGHT = {"title": 3.0, "description": 2.0, "id": 1.0, "params": 0.6, "match": 0.4}


def lexical_score(cap: dict, query_terms: set[str]) -> float:
    if not query_terms:
        return 0.0
    total = 0.0
    for field, text in _field_text(cap).items():
        field_tokens = set(tokens(text))
        if not field_tokens:
            continue
        hits = len(query_terms & field_tokens)
        if hits:
            total += FIELD_WEIGHT[field] * hits / math.sqrt(len(query_terms))
    return total


def cache_dir() -> "pathlib.Path":
    """Where derived, rebuildable data goes — never the config directory.

    The tool index is generated from capabilities/*.js and can be thrown away at
    any time, so it belongs in a cache: XDG_CACHE_HOME (or ~/.cache) on
    macOS/Linux, LOCALAPPDATA on Windows, matching where the Windows port
    already puts its log.
    """
    import pathlib as _pl
    if os.name == "nt":
        base = _pl.Path(os.environ.get("LOCALAPPDATA") or (_pl.Path.home() / "AppData/Local"))
        return base / "web-bridge" / "cache"
    base = os.environ.get("XDG_CACHE_HOME")
    return (_pl.Path(base) if base else _pl.Path.home() / ".cache") / "web-bridge"


def qmd_available() -> bool:
    """qmd is used when installed — it is on Windows too, so it is not mac-only.

    Opt out with WEB_BRIDGE_QMD=0. Only the BM25 path (`qmd search`) runs here:
    `vsearch`/`query` go through a local embedding model, which on this machine
    hangs outright (node-llama-cpp fails to build its Metal shaders) and even
    healthy costs seconds. Tool lookup sits in the hot path — briefing
    construction, exec hints, every agent question — so it must stay in
    milliseconds. Set WEB_BRIDGE_QMD_VECTOR=1 to try the vector path anyway.
    """
    if os.environ.get("WEB_BRIDGE_QMD") == "0":
        return False
    return bool(shutil.which("qmd"))


def qmd_scores(query: str, caps: list[dict]) -> dict[str, float]:
    """Semantic ranking through the local qmd index, when it is enabled.

    Optional on purpose: qmd is a native binary that is not on every machine
    (notably not the Windows install), and the corpus here is small enough that
    lexical scoring is a reasonable default rather than a fallback of last
    resort.
    """
    try:
        index_dir = _qmd_index(caps)
        # Vector is on by default now that it runs (CPU mode). It costs ~5s, but
        # only on an explicit query: the hot path (briefing, exec hints) uses the
        # catalogue and never shells out to qmd at all.
        vector = os.environ.get("WEB_BRIDGE_QMD_VECTOR", "1") != "0"
        cmd = ["qmd", "vsearch" if vector else "search", query, "--json"]
        if vector:
            # reranking is a second model load for a fourteen-document corpus
            cmd.append("--no-rerank")
        # Hard timeout: a hanging search binary must never hold up a tool lookup.
        env = _qmd_env(cache_dir() / "qmd")
        timeout = 30 if vector else 6
        out = subprocess.run(cmd, cwd=index_dir, env=env,
                             capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        data = json.loads(out.stdout)
        hits = data if isinstance(data, list) else (data.get("results") or data.get("hits") or [])
        # Only hits that ARE capabilities count. qmd searches every registered
        # collection, and one rooted at the repo was returning HANDOFF.md and
        # ROADMAP.md — harmless in the merge, but they skewed the normalisation
        # and drowned the real hits.
        known = {c["id"] for c in caps}
        scores = {}
        for h in hits:
            path = str(h.get("file") or h.get("path") or "")
            cap_id = os.path.splitext(os.path.basename(path))[0]
            if cap_id in known:
                scores[cap_id] = max(scores.get(cap_id, 0.0), float(h.get("score") or 0) or 1.0)
        top = max(scores.values(), default=0) or 1.0
        return {k: v / top for k, v in scores.items()}
    except Exception:  # noqa: BLE001
        return {}


# The models that work on this machine, copied from the llm-wiki setup rather
# than guessed. Without a models block qmd falls back to a default the local
# runtime cannot build (node-llama-cpp fails to compile its Metal shaders) and
# vsearch hangs — which is exactly why the vector path looked broken while
# llm-wiki's worked fine.
QMD_MODELS = {
    "embed": "hf:Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf",
    "rerank": "hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf",
}


def _qmd_env(index_home: "pathlib.Path") -> dict:
    """Point qmd at OUR index, not the shared global one.

    Two reasons this has to be isolated: the global index carries collections
    rooted elsewhere (one of them the repo, which leaked HANDOFF.md into tool
    search results), and the model configuration lives per-index — the global one
    has none.
    """
    env = dict(os.environ)
    env["INDEX_PATH"] = str(index_home / "index.sqlite")
    env["QMD_CONFIG_DIR"] = str(index_home)
    # Force CPU inference. The GPU path needs node-llama-cpp to compile Metal
    # shaders, and this machine has no Metal Toolchain — `xcrun metal` refuses,
    # the shader build fails, and the search then hangs forever instead of
    # erroring. On CPU the same query answers in about five seconds, with no
    # rebuild of anything. (Downloading the toolchain would also fix it; this
    # does not require it.)
    env.setdefault("QMD_FORCE_CPU", "1")
    return env


def _write_qmd_config(index_home: "pathlib.Path", docs: "pathlib.Path") -> None:
    cfg = index_home / "index.yml"
    body = (
        "collections:\n"
        "  web-bridge-tools:\n"
        f"    path: {docs}\n"
        '    pattern: "**/*.md"\n'
        "models:\n"
        + "".join(f"  {k}: {v}\n" for k, v in QMD_MODELS.items())
    )
    if not cfg.exists() or cfg.read_text(encoding="utf-8") != body:
        cfg.write_text(body, encoding="utf-8")


def _qmd_index(caps: list[dict]) -> str:
    """Materialise capability descriptions as markdown for qmd, in the cache dir.

    Rebuilt from the capability files whenever one changes, and re-indexed only
    then — `qmd update` on every query would put a subprocess in the hot path
    for nothing.
    """
    base = cache_dir() / "web-bridge-tools"
    base.mkdir(parents=True, exist_ok=True)
    seen, changed = set(), False
    for c in caps:
        seen.add(c["id"] + ".md")
        doc = base / (c["id"] + ".md")
        body = (f"# {c.get('title') or c['id']}\n\n{c.get('description') or ''}\n\n"
                f"适用: {', '.join(c.get('match') or [])}\n"
                f"参数: {', '.join((c.get('params') or {}).keys())}\n")
        if not doc.exists() or doc.read_text(encoding="utf-8") != body:
            doc.write_text(body, encoding="utf-8")
            changed = True
    for stale in base.glob("*.md"):
        if stale.name not in seen:
            stale.unlink(missing_ok=True)
            changed = True

    home = cache_dir() / "qmd"
    home.mkdir(parents=True, exist_ok=True)
    _write_qmd_config(home, base)

    stamp = home / ".indexed"
    if changed or not stamp.exists():
        env = _qmd_env(home)
        try:
            subprocess.run(["qmd", "update"], cwd=str(base), env=env,
                           capture_output=True, text=True, timeout=60)
            if os.environ.get("WEB_BRIDGE_QMD_VECTOR", "1") != "0":
                # embeddings are only needed for the vector path, and generating
                # them costs seconds — do it when that path is actually in use
                subprocess.run(["qmd", "embed"], cwd=str(base), env=env,
                               capture_output=True, text=True, timeout=180)
            stamp.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return str(base)


def quality(cap_id: str, idx: dict) -> dict:
    """Track record from the journal: does this tool actually work?"""
    runs = ok = 0
    last = ""
    for slot in idx.values():
        if slot.get("capability") == cap_id or slot.get("promoted_to") == cap_id:
            runs += slot.get("runs", 0)
            ok += slot.get("ok_runs", 0)
            last = max(last, slot.get("last") or "")
    return {"runs": runs, "ok": ok, "last": last}


def _quality_factor(q: dict, feedback: dict) -> float:
    """A tool that keeps failing should sink; an unproven one should not be buried.

    Laplace-smoothed success rate, so one bad run does not condemn a tool and one
    lucky run does not crown it. Explicit thumbs-down from an agent counts as
    failures, because "it ran without throwing but answered wrongly" is invisible
    to the journal.
    """
    runs = q["runs"] + feedback.get("bad", 0)
    ok = q["ok"]
    rate = (ok + 1) / (runs + 2)          # 0.5 when unproven
    proven = math.log1p(min(runs, 20)) / math.log1p(20)   # 0..1
    return 0.6 + 0.8 * rate + 0.4 * proven * rate         # ~0.6 (bad) … ~1.8 (good, proven)


def _recency_factor(last: str) -> float:
    if not last:
        return 1.0
    try:
        seen = time.mktime(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 1.0
    days = max(0.0, (time.time() - seen) / 86400)
    return 1.15 if days < 3 else (1.05 if days < 14 else 1.0)


def load_feedback() -> dict:
    path = journal.STATE_DIR / "tool-feedback.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def record_feedback(cap_id: str, ok: bool, note: str = "") -> dict:
    """Let a caller say a tool did or did not do the job.

    The journal knows whether a script threw; it cannot know that the answer was
    wrong. The agent can, so give it a way to say so — that is what stops a
    plausible-but-useless tool from ranking forever.
    """
    path = journal.STATE_DIR / "tool-feedback.json"
    data = load_feedback()
    entry = data.setdefault(cap_id, {"good": 0, "bad": 0, "notes": []})
    entry["good" if ok else "bad"] += 1
    if note:
        entry["notes"] = (entry["notes"] + [note[:200]])[-5:]
    journal.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return entry


# Below this, the whole library fits in a briefing and the model does the
# matching itself. Above it, ranking has to shortlist first.
CATALOGUE_BUDGET_CHARS = 2600


def catalogue(url: str = "") -> Optional[dict]:
    """The whole library as one-liners — when it is small enough to just hand over.

    The paraphrase failures ("把网页数据弄成 excel 能用的样子" → extract-tables) are
    a semantic matching problem, and the best semantic matcher in this system is
    the model reading the briefing, not a scorer built out of a synonym table.
    Fourteen tools cost about 570 tokens to list in full; against that, choosing
    for the model is both worse and unnecessary.

    Returns None once the library outgrows the budget, at which point search()
    shortlists instead — the ranking is not wasted, it just moves to where it is
    actually needed.
    """
    caps = capabilities.all_caps()
    if not caps:
        return None
    idx = journal._load_index()
    feedback = load_feedback()
    lines, total = [], 0
    for cap in sorted(caps, key=lambda c: c.get("id", "")):
        q = quality(cap["id"], idx)
        fb = feedback.get(cap["id"], {})
        here = " ★本页" if url and capabilities.matches(cap, url) and cap.get("match") != ["*"] else ""
        used = f" ({q['ok']}/{q['runs']}次成功)" if q["runs"] else ""
        bad = " ⚠被标记不好用" if fb.get("bad", 0) >= 2 else ""
        desc = (cap.get("description") or "").strip().split("。")[0][:64]
        line = f"- `{cap['id']}`{here} {cap.get('title') or ''}{used}{bad} — {desc}"
        total += len(line)
        if total > CATALOGUE_BUDGET_CHARS:
            return None
        lines.append(line)
    return {"count": len(lines), "chars": total, "lines": lines}


def search(query: str = "", url: str = "", limit: int = 5,
           include_generic: bool = True) -> list[dict]:
    """Best tools for this intent. URL boosts, never filters."""
    caps = capabilities.all_caps()
    if not caps:
        return []
    idx = journal._load_index()
    feedback = load_feedback()
    terms = expand(tokens(query))
    semantic = qmd_scores(query, caps) if (query and qmd_available()) else {}

    # Track record must not manufacture a match. A tool with five successful runs
    # was topping "看看这页有什么可以抓的" over inspect-page on a faint keyword
    # overlap: quality is a tie-breaker between plausible candidates, not a way
    # to win from nowhere. So relevance is measured first, and anything far below
    # the best match is dropped before quality is applied.
    # Combine the two signals on the same scale. Raw lexical scores run far
    # larger than qmd's 0..1, so adding them directly let keyword overlap drown
    # the semantic result: qmd put reader-mode first for "这篇文章太乱了想安静地读"
    # while the merged score still said extract-article, which shares more
    # characters and means the wrong thing.
    raw_lex = {}
    for cap in caps:
        if not include_generic and cap.get("match") == ["*"]:
            continue
        raw_lex[cap["id"]] = lexical_score(cap, terms)
    top_lex = max(raw_lex.values(), default=0.0) or 1.0

    relevances = {}
    for cap_id, lex in raw_lex.items():
        lex_n = lex / top_lex
        sem_n = semantic.get(cap_id, 0.0)          # already 0..1
        if semantic:
            # semantics lead where they exist; keywords keep exact ids and
            # parameter names findable
            relevances[cap_id] = 0.65 * sem_n + 0.35 * lex_n
        else:
            relevances[cap_id] = lex_n
    best_rel = max(relevances.values(), default=0.0)
    floor = best_rel * 0.28

    scored = []
    for cap in caps:
        if not include_generic and cap.get("match") == ["*"]:
            continue
        relevance = relevances.get(cap["id"], 0.0)
        # The URL is a hint about context, not a gate: a tool for this page is
        # more likely to be wanted, but a tool for another site is still the
        # right answer when the user asked for what it does.
        on_this_page = bool(url) and capabilities.matches(cap, url) and cap.get("match") != ["*"]
        if not query:
            relevance = 1.0 if on_this_page else 0.2
        elif relevance < floor and not on_this_page:
            continue                       # too weak a match for a track record to rescue
        q = quality(cap["id"], idx)
        fb = feedback.get(cap["id"], {})
        score = relevance * _quality_factor(q, fb) * _recency_factor(q["last"])
        if on_this_page:
            score *= 1.6
        if score <= 0:
            continue
        scored.append({
            "id": cap["id"],
            "title": cap.get("title") or cap["id"],
            # one line only: the point is to keep the caller's context small
            "summary": (cap.get("description") or "").strip().split("。")[0][:110],
            "params": list((cap.get("params") or {}).keys()),
            "match": cap.get("match") or ["*"],
            "on_this_page": on_this_page,
            "runs": q["runs"], "ok_runs": q["ok"],
            "reported_bad": fb.get("bad", 0),
            "score": round(score, 3),
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]
