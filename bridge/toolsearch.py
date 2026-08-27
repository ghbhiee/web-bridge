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
import sys
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


# Shared with llm-wiki, which owns this convention: one cache root, no per-owner
# level, and a globally unique <name> that carries its own project prefix. The
# flat root is the point — a nested layout would blur uniqueness, and colliding
# names are exactly what fails silently in qmd.
VECTOR_INDEX_NAME = "web-bridge-tools"


def vector_home() -> "pathlib.Path":
    """`<cache>/llm-wiki/web-bridge-tools/` — index.sqlite, index.yml, docs/.

    Sharing the root lets llm-wiki's kb.py manage this index too, without
    web-bridge having to depend on it: the qmd calls here stay self-contained so
    a checkout works on a machine that has never heard of llm-wiki.
    """
    import pathlib as _pl
    if os.name == "nt":
        base = _pl.Path(os.environ.get("LOCALAPPDATA") or (_pl.Path.home() / "AppData/Local"))
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = _pl.Path(xdg) if xdg else _pl.Path.home() / ".cache"
    return base / "llm-wiki" / VECTOR_INDEX_NAME


def qmd_available() -> bool:
    """qmd is used when installed — it is on Windows too, so it is not mac-only.

    Opt out with WEB_BRIDGE_QMD=0. Only the BM25 path (`qmd search`) runs by
    default: `vsearch` loads a local embedding model and costs seconds even when
    everything is healthy, and tool lookup sits in the hot path — briefing
    construction, exec hints, every agent question — so it has to stay in
    milliseconds. Set WEB_BRIDGE_QMD_VECTOR=1 to spend that time on the vector
    path (measured ~2.6-3.4s cold, GPU, reranking off).
    """
    if os.environ.get("WEB_BRIDGE_QMD") == "0":
        return False
    return bool(shutil.which("qmd"))


QMD_TROUBLE: dict = {}


def _note_qmd_trouble(reason: str) -> None:
    """Record why the semantic path gave nothing, so it can be seen."""
    QMD_TROUBLE["reason"] = reason
    QMD_TROUBLE["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if os.environ.get("WEB_BRIDGE_QMD_QUIET") != "1":
        print(f"[web-bridge] vector search unavailable, using keywords only: {reason}",
              file=sys.stderr)


def qmd_scores(query: str, caps: list[dict]) -> dict[str, float]:
    """Semantic ranking through the local qmd index, when it is enabled.

    Optional on purpose: qmd is a native binary that is not on every machine
    (notably not the Windows install), and the corpus here is small enough that
    lexical scoring is a reasonable default rather than a fallback of last
    resort.
    """
    try:
        index_dir = _qmd_index(caps)
        # Vector runs only on an explicit query: the hot path (briefing, exec
        # hints) uses the catalogue and never shells out to qmd at all.
        vector = os.environ.get("WEB_BRIDGE_QMD_VECTOR", "1") != "0"
        if vector:
            # `qmd query` with a typed line, NOT `qmd vsearch`. Only `query`
            # parses the vec:/lex:/hyde: grammar; `vsearch` treats any input as
            # plain text and runs it through a 1.2GB query-expansion model to
            # invent extra variants first. That model is not optional and cannot
            # be dodged by leaving `generate:` out of index.yml — qmd falls back
            # to the same default — so on a machine without it, a vector search
            # silently downloads 1.2GB and looks hung. Verified with the model
            # absent: `vsearch` (with or without the prefix) times out at 30s and
            # this path answers in 0.8s, printing `Structured search: 1 queries
            # (vec)`. Unlike vsearch, `query` does rerank, so --no-rerank is real
            # here: it is the second model load this hot-ish path cannot afford.
            cmd = ["qmd", "query", "vec: " + " ".join(query.split()),
                   "--no-rerank", "--json"]
        else:
            cmd = ["qmd", "search", query, "--json"]
        # Hard timeout: a hanging search binary must never hold up a tool lookup.
        env = _qmd_env(vector_home())
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
        if not scores:
            _note_qmd_trouble("qmd returned no hit matching a known capability")
        return {k: v / top for k, v in scores.items()}
    except Exception as exc:  # noqa: BLE001
        # Falling back to lexical ranking is right — a search must not fail
        # because a helper binary did — but doing it *silently* is not. A dead
        # vector path looks exactly like a slightly different ranking from the
        # outside, which is how a 30s timeout went unnoticed here while the
        # results merely got worse. Leave a trace instead.
        _note_qmd_trouble(f"{type(exc).__name__}: {exc}")
        return {}


# Pinned to the same pair llm-wiki settled on rather than guessed, so both
# indexes share one model download. No `generate` entry on purpose: query
# expansion needs a separate 1.2GB model and only ever runs for `qmd query`,
# which this module never calls. qmd back-fills a `generate:` line into index.yml
# on every `update` regardless, so deleting the model file does not keep it away
# — not calling `qmd query` is what does.
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
    # Deliberately NOT setting QMD_FORCE_CPU. node-llama-cpp prints
    # `ggml_metal_library_init_from_source: error compiling source` when no Metal
    # Toolchain is installed, but that is only its first strategy failing: it then
    # loads the bundled prebuilt default.metallib and runs on the GPU normally.
    # `qmd doctor` confirms the GPU is live. Forcing CPU off the back of that
    # error line makes queries about twice as slow (measured, --no-rerank path:
    # 2.6-3.4s on GPU vs 4.5-6.1s on CPU).
    return env


def _write_qmd_config(index_home: "pathlib.Path", docs: "pathlib.Path") -> None:
    cfg = index_home / "index.yml"
    body = (
        "collections:\n"
        f"  {VECTOR_INDEX_NAME}:\n"
        f"    path: {docs}\n"
        '    pattern: "**/*.md"\n'
        "models:\n"
        + "".join(f"  {k}: {v}\n" for k, v in QMD_MODELS.items())
    )
    if not cfg.exists():
        cfg.write_text(body, encoding="utf-8")
        return
    # Do not clobber a config that already points where we need it. This file is
    # shared ground: llm-wiki's kb.py writes the same layout here plus an
    # `ignore:` block, and qmd itself back-fills a `generate:` model line on
    # every update. Rewriting on any difference would strip both on each rebuild
    # and have the other side put them back — two owners fighting over one file.
    # So only rewrite when the config no longer describes our collection.
    current = cfg.read_text(encoding="utf-8")
    intact = (f"  {VECTOR_INDEX_NAME}:" in current
              and f"path: {docs}" in current
              and all(v in current for v in QMD_MODELS.values()))
    if not intact:
        cfg.write_text(body, encoding="utf-8")


def _qmd_index(caps: list[dict]) -> str:
    """Materialise capability descriptions as markdown for qmd, in the cache dir.

    Rebuilt from the capability files whenever one changes, and re-indexed only
    then — `qmd update` on every query would put a subprocess in the hot path
    for nothing.
    """
    base = vector_home() / "docs"
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

    home = vector_home()
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
    # Provisional tools (session tails) are deliberately absent here. They are a
    # guess about which script mattered -- roughly 7 of 17 when the rule was
    # replayed over this journal -- and the catalogue only works because the
    # whole library fits in a briefing. Letting guesses in would spend that
    # budget on noise and, past the limit, disable the catalogue outright.
    # search() still ranks them, which is where a wrong guess costs nothing.
    # Machine-described tools are excluded, `auto` and `provisional` alike. The
    # catalogue earns its keep because every line is a sentence a model can judge
    # at a glance; a title that is a fragment of source code is not, and one such
    # entry was advertising "send whatever is in the compose box" as a
    # zero-parameter tool. They stay in search() -- ranked first there for a
    # plain-language query even without the page boost -- and rejoin the
    # catalogue as soon as web_save_capability gives them a real description.
    caps = [c for c in capabilities.all_caps()
            if not (c.get("provisional") or c.get("auto"))]
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
