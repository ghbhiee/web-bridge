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


def qmd_available() -> bool:
    return bool(shutil.which("qmd")) and os.environ.get("WEB_BRIDGE_QMD") == "1"


def qmd_scores(query: str, caps: list[dict]) -> dict[str, float]:
    """Semantic ranking through the local qmd index, when it is enabled.

    Optional on purpose: qmd is a native binary that is not on every machine
    (notably not the Windows install), and the corpus here is small enough that
    lexical scoring is a reasonable default rather than a fallback of last
    resort.
    """
    try:
        index_dir = _qmd_index(caps)
        out = subprocess.run(
            ["qmd", "search", query, "--json", "--limit", "20"],
            cwd=index_dir, capture_output=True, text=True, timeout=20)
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        data = json.loads(out.stdout)
        hits = data.get("results") or data.get("hits") or []
        scores = {}
        for h in hits:
            path = str(h.get("path") or h.get("file") or "")
            cap_id = os.path.splitext(os.path.basename(path))[0]
            scores[cap_id] = float(h.get("score") or 0)
        top = max(scores.values(), default=0) or 1.0
        return {k: v / top for k, v in scores.items()}
    except Exception:  # noqa: BLE001
        return {}


def _qmd_index(caps: list[dict]) -> str:
    """Materialise capability descriptions as markdown for qmd to index."""
    base = journal.STATE_DIR / "toolindex"
    base.mkdir(parents=True, exist_ok=True)
    seen = set()
    for c in caps:
        seen.add(c["id"] + ".md")
        doc = base / (c["id"] + ".md")
        body = (f"# {c.get('title') or c['id']}\n\n{c.get('description') or ''}\n\n"
                f"适用: {', '.join(c.get('match') or [])}\n"
                f"参数: {', '.join((c.get('params') or {}).keys())}\n")
        if not doc.exists() or doc.read_text(encoding="utf-8") != body:
            doc.write_text(body, encoding="utf-8")
    for stale in base.glob("*.md"):
        if stale.name not in seen:
            stale.unlink(missing_ok=True)
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

    scored = []
    for cap in caps:
        if not include_generic and cap.get("match") == ["*"]:
            continue
        lex = lexical_score(cap, terms)
        sem = semantic.get(cap["id"], 0.0) * 4.0     # comparable to lexical range
        relevance = max(lex, sem) + 0.25 * min(lex, sem)
        # The URL is a hint about context, not a gate: a tool for this page is
        # more likely to be wanted, but a tool for another site is still the
        # right answer when the user asked for what it does.
        on_this_page = bool(url) and capabilities.matches(cap, url) and cap.get("match") != ["*"]
        if not query:
            relevance = 1.0 if on_this_page else 0.2
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
