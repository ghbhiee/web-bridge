"""Capability registry — the discoverable script library.

A *capability* is a JS file on disk whose header carries a JSON metadata block.
Agents discover what a page can do (`for_url`), run one (`load` → injected into
the page's MAIN world), and author new ones by simply writing a file.

File format:

    /* @web-bridge-capability
    {
      "id": "extract-tables",
      "title": "提取表格",
      "description": "把页面里所有 <table> 提取成结构化 JSON",
      "kind": "extract",
      "match": ["*"],
      "params": {"csv": {"type": "boolean", "default": false, "description": "返回 CSV"}}
    }
    */
    // body: `args` (the params object) is in scope; return a JSON-safe value.

Metadata is parsed WITHOUT executing the file, so listing is cheap and safe.
`match` accepts "*" (any page) or glob patterns matched against the URL.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

CAP_DIR = Path(os.environ.get(
    "WEB_BRIDGE_CAPS", str(Path(__file__).resolve().parent.parent / "capabilities")))

HEADER_RE = re.compile(r"/\*\s*@web-bridge-capability\s*(\{.*?\})\s*\*/", re.S)

KINDS = ("extract", "automate", "restyle", "inspect", "other")


def _parse(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = HEADER_RE.search(text)
    if not m:
        return None
    try:
        meta = json.loads(m.group(1))
    except Exception:
        return None
    meta.setdefault("id", path.stem)
    meta.setdefault("title", meta["id"])
    meta.setdefault("description", "")
    meta.setdefault("kind", "other")
    meta.setdefault("match", ["*"])
    meta.setdefault("params", {})
    # `autorun` is page-beauty's "enhance" switch: the extension registers these
    # as userScripts so they fire on page load. Only meaningful for scripts that
    # change a page (restyle/automate); extraction runs on demand.
    meta.setdefault("autorun", False)
    meta["file"] = str(path)
    meta["body"] = text[m.end():].strip()
    return meta


def all_caps() -> list[dict]:
    if not CAP_DIR.is_dir():
        return []
    out = []
    for p in sorted(CAP_DIR.rglob("*.js")):
        meta = _parse(p)
        if meta:
            out.append(meta)
    return out


def matches(meta: dict, url: str) -> bool:
    pats = meta.get("match") or ["*"]
    if any(p == "*" for p in pats):
        return True
    if not url:
        return False
    for p in pats:
        if fnmatch.fnmatch(url, p) or fnmatch.fnmatch(url, f"*{p}*"):
            return True
        # also allow bare hostnames like "chatgpt.com"
        if "://" not in p and "*" not in p and p in url:
            return True
    return False


def autorun_for_registration() -> list[dict]:
    """Scripts the extension should register to run automatically on page load.

    Returned with code, because the extension registers them with
    chrome.userScripts.register — unlike on-demand runs, there is no round trip
    to the bridge at page-load time.
    """
    out = []
    for c in all_caps():
        if not c.get("autorun") or not c.get("body"):
            continue
        # Fill the declared defaults here. A manual run gets them from
        # validate_params, and an autorun that injected a bare {} instead made
        # the same script behave differently depending on how it started —
        # `args.color` was set when you pressed 运行 and undefined on page load.
        try:
            args = validate_params(c, {})
        except ValueError:
            # a required parameter has nothing to supply it on page load, so this
            # script cannot run automatically at all; set_autorun refuses these,
            # and skipping here covers files edited by hand
            continue
        out.append({"id": c["id"], "title": c.get("title") or c["id"],
                    "matches": _match_patterns(c), "code": c["body"],
                    "args": args, "kind": c.get("kind")})
    return out


def _match_patterns(meta: dict) -> list[str]:
    """Turn our loose `match` entries into real Chrome match patterns.

    Capability `match` accepts "*", a bare host, or a glob; userScripts.register
    only accepts genuine match patterns, and one bad pattern rejects the whole
    batch — so anything unrecognisable becomes a host-wide pattern rather than
    being passed through to fail.
    """
    pats = []
    for p in meta.get("match") or ["*"]:
        if p == "*":
            pats.append("*://*/*")
        elif p.startswith(("http://", "https://", "*://")):
            pats.append(p)
        elif "/" in p:
            host, _, rest = p.partition("/")
            pats.append(f"*://*.{host}/{rest}*" if "*" not in host else f"*://{host}/{rest}*")
        else:
            pats.append(f"*://*.{p}/*")
    return pats or ["*://*/*"]


def for_url(url: str = "") -> list[dict]:
    """Capabilities applicable to a page, universal ones first."""
    caps = [c for c in all_caps() if matches(c, url)]
    caps.sort(key=lambda c: (c.get("match") != ["*"], c.get("kind", ""), c["id"]))
    return caps


def get(cap_id: str) -> Optional[dict]:
    for c in all_caps():
        if c["id"] == cap_id:
            return c
    return None


def public(meta: dict) -> dict:
    """Metadata without the code body — what listings return."""
    return {k: v for k, v in meta.items() if k not in ("body", "file")} | {
        "file": os.path.basename(meta.get("file", ""))
    }


def lint(meta: dict) -> list[str]:
    """Problems that would make a capability confusing or unusable to an agent.

    Authored capabilities arrive from an LLM, so the failure mode is a plausible
    but wrong header (kind invented, params as a list, no description). Catching
    it at write time beats discovering it at call time.
    """
    problems: list[str] = []
    if meta.get("kind") not in KINDS:
        problems.append(f"kind 必须是 {'/'.join(KINDS)}，收到 {meta.get('kind')!r}")
    if not str(meta.get("description", "")).strip():
        problems.append("description 不能为空——它是 agent 判断该不该用这个能力的唯一依据")
    match = meta.get("match")
    if not isinstance(match, list) or not match or not all(isinstance(m, str) for m in match):
        problems.append('match 必须是非空字符串数组，如 ["*"] 或 ["github.com"]')
    specs = meta.get("params")
    if not isinstance(specs, dict):
        problems.append('params 必须是对象 {参数名: {type, description, ...}}')
    else:
        for name, spec in specs.items():
            if not isinstance(spec, dict):
                problems.append(f"params.{name} 必须是对象，如 {{\"type\": \"string\"}}")
                continue
            t = (spec.get("type") or "any").lower()
            if t not in _TYPES:
                problems.append(f"params.{name}.type 非法 {spec.get('type')!r}，可选 {'/'.join(_TYPES)}")
            if not str(spec.get("description", "")).strip():
                problems.append(f"params.{name} 缺少 description")
            if "default" in spec and spec.get("required"):
                problems.append(f"params.{name} 同时有 default 和 required，二选一")
    if not isinstance(meta.get("autorun", False), bool):
        problems.append("autorun 必须是 true/false（页面加载时自动运行，只对改页面的脚本有意义）")
    if meta.get("autorun") and meta.get("kind") == "extract":
        problems.append("extract 类能力不该 autorun：抽取是按需运行的，自动跑没有意义")
    if not str(meta.get("body", "")).strip():
        problems.append("元数据头之后没有代码——文件体就是要注入页面的函数体")
    return problems


def save(cap_id: str, source: str, overwrite: bool = True) -> dict:
    """Write a new/updated capability file. Validates the header parses + lints."""
    CAP_DIR.mkdir(parents=True, exist_ok=True)
    if not HEADER_RE.search(source):
        raise ValueError("缺少 /* @web-bridge-capability {...} */ 元数据头")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", cap_id)
    path = CAP_DIR / f"{safe}.js"
    existed = path.exists()
    if existed and not overwrite:
        raise ValueError(f"能力已存在: {safe}")
    previous = path.read_text(encoding="utf-8") if existed else ""
    path.write_text(source, encoding="utf-8")
    meta = _parse(path)
    if not meta:
        path.unlink(missing_ok=True)
        raise ValueError("元数据头无法解析（必须是合法 JSON）")
    problems = lint(meta)
    if problems:
        if not existed:
            path.unlink(missing_ok=True)          # don't leave a broken new file behind
        else:
            path.write_text(previous, encoding="utf-8")   # roll back to the working version
        raise ValueError("能力元数据有问题（未写入）：\n  - " + "\n  - ".join(problems))
    return public(meta)


def set_autorun(cap_id: str, on: bool) -> dict:
    """Rewrite just the autorun flag in a capability's metadata header.

    Edits the header in place rather than regenerating the file, so a
    hand-written capability keeps its formatting and comments.
    """
    meta = get(cap_id)
    if not meta:
        raise KeyError(f"未知能力 '{cap_id}'")
    if meta.get("kind") == "extract" and on:
        raise ValueError("extract 类能力不能 autorun：抽取是按需运行的")
    if on:
        # Nothing supplies arguments on a page load, so a required parameter
        # means this script could only ever fail there. Refusing is clearer than
        # a switch that turns on and quietly does nothing.
        missing = [n for n, sp in (meta.get("params") or {}).items()
                   if (sp or {}).get("required")]
        if missing:
            raise ValueError(
                f"这个能力有必填参数（{'、'.join(missing)}），页面加载时没人传，"
                f"所以不能自动运行。给它们设默认值，或改成手动运行。")
    path = Path(meta["file"])
    text = path.read_text(encoding="utf-8")
    m = HEADER_RE.search(text)
    if not m:
        raise ValueError("能力文件没有元数据头")
    block = m.group(1)
    flag = "true" if on else "false"
    # Surgical edit, not a regenerated header: re-serialising the JSON would
    # reflow a hand-written file (every capability here is hand-formatted, with
    # one param per line) and make the diff of a one-flag toggle unreadable.
    swapped, n = re.subn(r'("autorun"\s*:\s*)(true|false)', lambda mm: mm.group(1) + flag, block)
    if not n:
        # no autorun key yet — add it just before the closing brace, matching the
        # indentation the file already uses
        idx = swapped.rstrip().rfind("}")
        head, tail = swapped[:idx].rstrip(), swapped[idx:]
        comma = "" if head.endswith(("{", ",")) else ","
        indent = "  " if "\n" in head else " "
        swapped = f'{head}{comma}\n{indent}"autorun": {flag}\n{tail}'
    path.write_text(text[:m.start(1)] + swapped + text[m.end(1):], encoding="utf-8")
    return public(get(cap_id) or meta)


def delete(cap_id: str) -> bool:
    meta = get(cap_id)
    if not meta:
        return False
    Path(meta["file"]).unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------- #
# parameter validation
# --------------------------------------------------------------------------- #
# `params` metadata used to be documentation only: a typo'd or missing argument
# reached the page as `undefined` and the capability quietly returned nothing,
# which reads to an agent like "the page has no data" rather than "you called it
# wrong". Validation turns those into actionable errors, and fills in defaults
# server-side so a body can trust `args`.
#
# Supported spec keys: type, default, description, required, enum, min, max.

_TYPES = {
    "string": str, "number": (int, float), "boolean": bool,
    "object": dict, "array": list, "any": object,
}


def _coerce(name: str, spec: dict, value: Any) -> Any:
    """Best-effort convert a JSON/CLI value to the declared type."""
    want = (spec.get("type") or "any").lower()
    if want not in _TYPES:
        want = "any"
    if want == "any" or value is None:
        return value
    if want == "number":
        if isinstance(value, bool):
            raise ValueError(f"参数 '{name}' 需要数字，收到布尔值")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return int(value) if re.fullmatch(r"[+-]?\d+", value.strip()) else float(value)
            except ValueError:
                pass
        raise ValueError(f"参数 '{name}' 需要数字，收到 {type(value).__name__}: {value!r}")
    if want == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.strip().lower() in ("true", "1", "yes")
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError(f"参数 '{name}' 需要布尔值(true/false)，收到 {value!r}")
    if want == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise ValueError(f"参数 '{name}' 需要字符串，收到 {type(value).__name__}")
    if not isinstance(value, _TYPES[want]):
        raise ValueError(f"参数 '{name}' 需要 {want}，收到 {type(value).__name__}")
    return value


def _spec_line(name: str, spec: dict) -> str:
    bits = [spec.get("type", "any")]
    if spec.get("required"):
        bits.append("必填")
    if "default" in spec:
        bits.append(f"默认 {json.dumps(spec['default'], ensure_ascii=False)}")
    if spec.get("enum"):
        bits.append("可选值 " + "/".join(str(x) for x in spec["enum"]))
    desc = spec.get("description", "")
    return f"  {name} ({', '.join(bits)})" + (f" — {desc}" if desc else "")


def params_help(meta: dict) -> str:
    specs = meta.get("params") or {}
    if not specs:
        return f"能力 '{meta['id']}' 不接受参数"
    return f"'{meta['id']}' 的参数：\n" + "\n".join(_spec_line(n, s or {}) for n, s in specs.items())


def _did_you_mean(name: str, known: list[str]) -> str:
    import difflib
    near = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    return f"，是否想写 '{near[0]}'？" if near else ""


def validate_params(meta: dict, params: Optional[dict]) -> dict:
    """Check + coerce params against the capability's spec, applying defaults.

    Raises ValueError with a message that tells the caller how to fix the call.
    """
    params = dict(params or {})
    specs: dict = meta.get("params") or {}
    if not isinstance(specs, dict):
        return params
    known = list(specs)

    unknown = [k for k in params if k not in specs]
    if unknown and known:
        hints = "".join(f"\n  未知参数 '{k}'{_did_you_mean(k, known)}" for k in unknown)
        raise ValueError(f"参数不被 '{meta['id']}' 接受：{hints}\n\n{params_help(meta)}")

    out: dict = {}
    missing: list[str] = []
    for name, spec in specs.items():
        spec = spec or {}
        if name in params and params[name] is not None:
            val = _coerce(name, spec, params[name])
            if spec.get("enum") and val not in spec["enum"]:
                raise ValueError(
                    f"参数 '{name}' 只能是 {'/'.join(str(x) for x in spec['enum'])}，收到 {val!r}")
            for bound, cmp, word in (("min", lambda a, b: a < b, "不小于"), ("max", lambda a, b: a > b, "不大于")):
                if bound in spec and isinstance(val, (int, float)) and cmp(val, spec[bound]):
                    raise ValueError(f"参数 '{name}' 需{word} {spec[bound]}，收到 {val}")
            out[name] = val
        elif "default" in spec:
            out[name] = spec["default"]
        elif spec.get("required"):
            missing.append(name)
    if missing:
        raise ValueError(
            "缺少必填参数：" + "、".join(f"'{m}'" for m in missing) + f"\n\n{params_help(meta)}")
    # keep extras when the capability declares no params at all (free-form)
    if not known:
        out.update(params)
    return out
