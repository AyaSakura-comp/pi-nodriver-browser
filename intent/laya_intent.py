"""Intent -> (DOM prefilter) -> Laya -> guarded action layer for pi nodriver-browser.

The agent never sees @refs. It says what it wants ("click the single-unit DGX Spark,
not the bundle"); this module snapshots the page, shortlists candidates, asks Laya to
pick one (with an explicit none_match option), executes the action, verifies the page
changed, and returns a compact post-action state.

Stdlib only so the pi extension can spawn it with any python3.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field

LAYA_URL = os.environ.get("LAYA_URL", "http://127.0.0.1:8000/v1/systemone")
NONE = "none_match"

# ---------------------------------------------------------------- snapshot parsing

_LINE = re.compile(r'^@(e\d+) <([\w-]+)>(.*)$')
_ATTR = re.compile(r'([\w-]+)="((?:[^"\\]|\\.)*)"')


@dataclass
class Element:
    ref: str
    tag: str
    text: str = ""
    attrs: dict = field(default_factory=dict)

    @property
    def offscreen(self) -> bool:
        return self.attrs.get("offscreen") == "true"

    @property
    def disabled(self) -> bool:
        return self.attrs.get("disabled") == "true"

    def name(self) -> str:
        """Human-visible accessible name."""
        parts = [self.text, self.attrs.get("aria-label", ""), self.attrs.get("label", ""),
                 self.attrs.get("placeholder", "")]
        seen, out = set(), []
        for p in parts:
            p = p.strip()
            if p and p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
        return " / ".join(out)


def parse_snapshot(text: str) -> list[Element]:
    out = []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        ref, tag, rest = m.groups()
        rest = rest.strip()
        body = ""
        if rest.startswith('"'):
            tm = re.match(r'"((?:[^"\\]|\\.)*)"', rest)
            if tm:
                body = tm.group(1)
                rest = rest[tm.end():]
        attrs = {k: v.replace('\\"', '"') for k, v in _ATTR.findall(rest)}
        out.append(Element(ref, tag, body.replace('\\"', '"'), attrs))
    return out


# ---------------------------------------------------------------- candidate building

CLICKABLE = {"a", "button", "summary", "label", "option", "li", "div", "span", "img", "td", "tr"}
PRIMARY = {"a", "button", "summary", "label"}
FILLABLE_TYPES = {"", "text", "search", "email", "tel", "url", "number", "password"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def build_candidates(elements: list[Element], action: str) -> tuple[list[Element], dict]:
    """Drop elements that cannot satisfy `action`, unlabeled icons, and container duplicates."""
    stats = {"total": len(elements), "unlabeled": 0, "dup": 0, "container": 0, "wrong_kind": 0, "disabled": 0}
    if action == "fill":
        inputs = [e for e in elements if (e.tag == "input" and e.attrs.get("type", "") in FILLABLE_TYPES)
                or e.tag == "textarea" or e.attrs.get("contenteditable") == "true"]
        inputs = [e for e in inputs if not e.disabled]
        labels = [e for e in elements if e.tag in ("label", "div", "span") and e.name()]
        input_names = {_norm(e.name()) for e in inputs if e.name()}
        extra = [l for l in labels if _norm(l.name()) not in input_names and len(_norm(l.name())) >= 2]
        keep = inputs + extra
        stats["wrong_kind"] = len(elements) - len(keep)
        return keep, stats
    if action == "select":
        keep = [e for e in elements if e.tag == "select"]
        stats["wrong_kind"] = len(elements) - len(keep)
        return keep, stats

    pool, unlabeled = [], []
    for e in elements:
        if e.tag in ("input", "textarea", "select") and e.attrs.get("type") not in ("submit", "button", "checkbox", "radio"):
            stats["wrong_kind"] += 1
            continue
        if e.tag not in CLICKABLE and e.tag != "input":
            stats["wrong_kind"] += 1
            continue
        if e.disabled:
            stats["disabled"] += 1
            continue
        if not e.name():
            stats["unlabeled"] += 1
            if e.tag in ("button", "div", "span", "a", "label", "img"):
                unlabeled.append(e)
            continue
        pool.append(e)
    # A handful of text-less controls (toggles, icon buttons) stay selectable by description;
    # dozens of icons (typical nav bars) would only add noise, so they go to the vision fallback instead.
    if 0 < len(unlabeled) <= 3:
        pool += unlabeled

    # Same visible name: keep the most actionable tag (a/button over li/div/span).
    by_name: dict[str, Element] = {}
    order = []
    for e in pool:
        key = _norm(e.name()) or f"#{e.ref}"   # text-less controls are never duplicates of each other
        cur = by_name.get(key)
        if cur is None:
            by_name[key] = e
            order.append(key)
        else:
            stats["dup"] += 1
            if cur.tag not in PRIMARY and e.tag in PRIMARY:
                by_name[key] = e
    uniq = [by_name[k] for k in order]

    # Containers: a non-primary element wrapping another candidate's text (>=2 of any kind,
    # or >=1 primary a/button) is layout, not a target; the inner control is kept instead.
    names = [_norm(e.name()) for e in uniq]
    keep = []
    for i, e in enumerate(uniq):
        if e.tag not in PRIMARY:
            inner = [j for j, n in enumerate(names) if j != i and len(n) >= 2 and n in names[i]]
            if len(inner) >= 2 or any(uniq[j].tag in PRIMARY for j in inner):
                stats["container"] += 1
                continue
        keep.append(e)
    return keep, stats


# ---------------------------------------------------------------- lexical recall

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_STOP = {"點擊", "點選", "按下", "按", "打開", "開啟", "進入", "查看", "看", "我要", "想要", "請", "幫我", "一下",
         "click", "open", "the", "a", "an", "on", "to", "go", "press", "tap", "button", "link", "please"}


def _tokens(s: str) -> list[str]:
    s = s.lower()
    toks = []
    for w in re.findall(r"[a-z0-9]+", s):
        if w not in _STOP:
            toks.append(w)
    for run in re.findall(r"[㐀-鿿豈-﫿]+", s):
        for st in sorted(_STOP, key=len, reverse=True):
            if _CJK.search(st):
                run = run.replace(st, " ")
        for seg in run.split():
            if len(seg) == 1:
                toks.append(seg)
            toks += [seg[i:i + 2] for i in range(len(seg) - 1)]
    return toks


def lexical_scores(intent: str, cands: list[Element]) -> list[float]:
    q = _tokens(intent)
    if not q:
        return [0.0] * len(cands)
    docs = [set(_tokens(c.name())) for c in cands]
    n = len(docs) or 1
    df: dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    qset = set(q)
    total = sum(math.log(1 + n / (1 + df.get(t, 0))) for t in qset) or 1.0
    out = []
    for d in docs:
        hit = sum(math.log(1 + n / (1 + df.get(t, 0))) for t in qset if t in d)
        out.append(hit / total)
    return out


# ---------------------------------------------------------------- Laya

# Common UI vocabulary, glossed both ways so English aria-labels meet Chinese intents (and vice versa).
GLOSS = [("cart", "購物車"), ("search", "搜尋"), ("close", "關閉"), ("login", "登入"), ("log in", "登入"),
         ("sign in", "登入"), ("menu", "選單"), ("next", "下一頁"), ("previous", "上一頁"), ("checkout", "結帳"),
         ("account", "帳戶"), ("home", "首頁"), ("filter", "篩選"), ("sort", "排序"), ("wishlist", "追蹤清單")]
BUNDLE = re.compile(r"(兩入|二入|[2-9]入|\*\s?[2-9]\b|[x×]\s?[2-9]\b|優惠組|組合包|套組|雙機|\+\s?(CyberPower|UPS)|不斷電)", re.I)
UNAVAILABLE = re.compile(r"(到貨通知|貨到通知|有貨通知|售完|已售完|補貨中|缺貨|暫無庫存|sold out|out of stock|notify me)", re.I)


def annotate(e: Element) -> str:
    """Visible name plus derived hints that small models miss (glosses, stock state)."""
    name = e.name()
    low = name.lower()
    extra = [zh for en, zh in GLOSS if re.search(r"\b" + en + r"\b", low) and zh not in name]
    extra += [en for en, zh in GLOSS if zh in name and en not in low][:2]
    tags = []
    if extra:
        tags.append("/".join(dict.fromkeys(extra)))
    if UNAVAILABLE.search(name):
        tags.append("缺貨 out of stock")
    if BUNDLE.search(name):
        tags.append("多件組/組合 bundle")
    return name + (f" [{'; '.join(tags)}]" if tags else "")


def _label(e: Element, limit: int = 90) -> str:
    kind = {"a": "link", "button": "button", "input": "input", "textarea": "textbox", "select": "dropdown"}.get(e.tag, e.tag)
    name = e.name() or "(no visible text: icon, switch/toggle or custom control)"
    if len(name) > limit:
        name = name[:limit] + "…"
    hints = annotate(e)[len(e.name()):]
    name += hints
    extra = ""
    if e.tag == "input" and e.attrs.get("type"):
        extra = f" type={e.attrs['type']}"
    return f"{kind}{extra}: {name}"


def laya(state: str, questions: dict, timeout: float = 5.0) -> dict:
    body = json.dumps({"state": state, "questions": questions}).encode()
    req = urllib.request.Request(LAYA_URL, body, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def laya_choose(intent: str, cands: list[Element], goal: str = "", page: str = "",
                with_none: bool = True, action: str = "click") -> dict:
    """One Laya choice over <=7 candidates (+ none_match). Returns sorted probabilities."""
    crit = {c.ref: _label(c) for c in cands}
    if with_none:
        crit[NONE] = "none of these elements matches the intent"
    state = f"User intent: {intent}"
    if goal:
        state += f"\nOverall task: {goal}"
    if page:
        state += f"\nPage: {page}"
    verb = "filled/typed into" if action == "fill" else "clicked/selected"
    q = {"type": "choice",
         "instructions": f"Which element should be {verb} to do exactly this: {intent}? "
                         f"Choose {NONE} if no element fits.",
         "criteria": crit}
    t = time.perf_counter()
    res = laya(state, {"pick": q})
    ms = (time.perf_counter() - t) * 1000
    probs = res["answers"]["pick"]["probabilities"]
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    return {"ranked": ranked, "ms": ms, "model": res.get("routing", {}).get("model")}


# ---------------------------------------------------------------- decision

@dataclass
class Decision:
    status: str               # match | none | ambiguous | direct
    ref: str | None
    label: str | None
    confidence: float
    alternatives: list        # [(ref, label, score)]
    trace: dict


def decide(intent: str, elements: list[Element], action: str = "click", goal: str = "", page: str = "",
           shortlist: int = 6, p_min: float = 0.45, margin: float = 0.15, lex_floor: float = 0.0) -> Decision:
    cands, stats = build_candidates(elements, action)
    trace = {"stats": stats, "candidates": len(cands)}
    labels = {c.ref: _label(c) for c in cands}
    if not cands:
        return Decision("none", None, None, 0.0, [], trace)
    if action in ("fill", "select") and len(cands) == 1:
        c = cands[0]
        return Decision("direct", c.ref, labels[c.ref], 1.0, [], trace)

    lex = lexical_scores(intent, cands)
    order = sorted(range(len(cands)), key=lambda i: (-lex[i], i))
    short = [cands[i] for i in order[:shortlist] if lex[i] > lex_floor] or [cands[i] for i in order[:shortlist]]
    trace["shortlist"] = [(c.ref, round(lex[cands.index(c)], 3)) for c in short]
    r = laya_choose(intent, short, goal, page)
    trace["laya_ms"] = round(r["ms"], 1)
    trace["laya_model"] = r["model"]
    trace["laya"] = [(k, round(v, 3)) for k, v in r["ranked"]]
    ranked = r["ranked"]
    top_ref, top_p = ranked[0]
    second_p = ranked[1][1] if len(ranked) > 1 else 0.0
    alts = [(k, labels.get(k, k), round(v, 3)) for k, v in ranked if k != NONE][:3]
    if top_ref == NONE:
        return Decision("none", None, None, top_p, alts, trace)
    if top_p < p_min or top_p - second_p < margin:
        return Decision("ambiguous", top_ref, labels[top_ref], top_p, alts, trace)
    return Decision("match", top_ref, labels[top_ref], top_p, alts, trace)


# ---------------------------------------------------------------- safety

RISKY = re.compile(r"(立即購買|直接購買|結帳|去買單|付款|確認訂單|送出訂單|下單|刪除|移除|取消訂單|登出|垃圾桶|"
                   r"buy now|checkout|place order|pay|purchase|delete|remove|sign out|log ?out|unsubscribe|trash)", re.I)


def is_risky(label: str | None) -> bool:
    return bool(label and RISKY.search(label))


def fingerprint(text: str) -> str:
    lines = [l for l in text.splitlines() if l.startswith("@e")]
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:12]
