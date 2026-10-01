"""HTTP service: agent intent -> grounded, executed, verified browser action.

POST /act  {"session": "...", "action": "open|click|fill|read|observe", "target": "...",
            "value": "...", "submit": true, "goal": "...", "confirm": false, "pick": 2, "url": "..."}

The response never contains @refs or raw DOM. It reports what was done and the resulting
page state (URL, title, headings, change flags, screenshot path).
"""
from __future__ import annotations

import argparse
import os
import hashlib
import json
import queue
import re
from urllib.parse import urlparse
import threading
import time
from typing import Optional, Callable, Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

ProgressCallback = Callable[[str, str], None]

import laya_intent as L
import nd
from embed import Embedder
from router import Router
from judge import vision_judge
from planner import next_step, next_step_full, verify_done

app = FastAPI(title="laya-browser-intent")
# Experiment switch: "dom" (default) grounds clicks on the DOM and activates @refs;
# "vision" grounds every click on OmniParser boxes + Qwen vision and clicks by screen coordinates.
CLICK_MODE = os.environ.get("INTENT_CLICK_MODE", "dom")
ROUTER: Router | None = None
LOCK = threading.Lock()                 # one browser action at a time per service
PENDING: dict[str, dict] = {}           # session -> last ambiguous shortlist
HISTORY: dict[str, list[str]] = {}      # session -> visited URLs (only URLs the browser really reached)


class Step(BaseModel):
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    submit: bool = False
    url: Optional[str] = None


class Act(BaseModel):
    session: str
    steps: Optional[list[Step]] = None
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    submit: bool = False
    goal: Optional[str] = None
    confirm: bool = False
    pick: Optional[int] = None
    url: Optional[str] = None
    ref: Optional[str] = None
    max_steps: int = 8
    model: Optional[str] = None
    base_url: Optional[str] = None
    headers: Optional[dict[str, str]] = None


TEXT_CACHE: dict[str, dict] = {}        # session -> last successful `get text` (worker LOOP_GUARD blocks repeats)


def _cmd(session: str, command: str) -> dict:
    r = nd.request(command, session)
    if command == "get text":
        if r.get("ok"):
            TEXT_CACHE[session] = r
        elif "LOOP_GUARD" in str(r.get("error")) and session in TEXT_CACHE:
            return TEXT_CACHE[session]     # nothing changed since the last read; reuse it
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or f"browser command failed: {command}")
    return r


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _page_state(session: str, snap_text: str | None = None) -> dict:
    url = (_cmd(session, "get url").get("text") or "").strip()
    title = (_cmd(session, "get title").get("text") or "").strip()
    if snap_text is None:
        snap_text = _cmd(session, "snapshot -i").get("text", "")
    els = L.parse_snapshot(snap_text)
    heads = [e.name()[:60] for e in els if e.tag in ("h1", "h2", "h3") and e.name()][:4]
    # Visible text is part of the fingerprint: two states can expose identical controls (e.g. "− ＋").
    gt = _cmd(session, "get text")
    text = (gt.get("pageText") or gt.get("text") or "")[:20000]
    fp = L.fingerprint(snap_text + "\n@e-text " + hashlib.sha1(text.encode()).hexdigest())
    excerpt = re.sub(r"\s+", " ", text).strip()[:300]
    return {"url": url, "title": title, "headings": heads, "page_excerpt": excerpt, "fingerprint": fp,
            "elements": len(els), "_snap": snap_text}


def _remember(session: str, url: str) -> None:
    h = HISTORY.setdefault(session, [])
    if url and (not h or h[-1] != url):
        h.append(url)
        del h[:-20]


def do_back(req: Act) -> dict:
    h = HISTORY.get(req.session, [])
    cur = (_cmd(req.session, "get url").get("text") or "").strip()
    while h and h[-1] == cur:
        h.pop()
    if not h:
        return {"status": "NO_HISTORY", "url": cur}
    prev = h[-1]
    r = _cmd(req.session, f"open {prev}")
    st = _page_state(req.session)
    return {"status": "WENT_BACK", **_public(st), "screenshot": r.get("screenshotPath"),
            "note": "Reopened the previous URL; in-page state (search box, filters, SPA views) is not restored."}


def _settled_state(session: str, before: dict, budget: float = 2.0, quiet: float = 0.6) -> dict:
    """Page state after an action. Waits for the first change (delayed transitions), then until the page
    has been stable for `quiet` seconds (multi-stage transitions), within `budget` seconds overall."""
    deadline = time.perf_counter() + budget
    after = _page_state(session)
    changed = after["url"] != before["url"] or after["fingerprint"] != before["fingerprint"]
    stable_since = time.perf_counter()
    while time.perf_counter() < deadline:
        if changed and time.perf_counter() - stable_since >= quiet:
            break
        time.sleep(0.15)
        nxt = _page_state(session)
        if nxt["fingerprint"] != after["fingerprint"] or nxt["url"] != after["url"]:
            stable_since = time.perf_counter()
        after = nxt
        changed = changed or after["url"] != before["url"] or after["fingerprint"] != before["fingerprint"]
    return after


def _public(state: dict) -> dict:
    return {k: v for k, v in state.items() if not k.startswith("_") and k != "fingerprint"}


def _ground(req: Act, session: str, action: str, snap: str):
    """Viewport first; if nothing fits, the whole page (offscreen refs auto-scroll on activate)."""
    site = urlparse(_cmd(session, "get url").get("text", "").strip()).netloc
    g = ROUTER.ground(req.target, L.parse_snapshot(snap), action, req.goal or "", site,
                      judge_url=req.base_url, judge_model=req.model, judge_headers=req.headers)
    g.trace["scope"] = "viewport"
    if g.status in ("none", "no_candidates"):
        # Pages often animate into the next state; look once more before widening the search.
        time.sleep(0.6)
        snap2 = _cmd(session, "snapshot -i").get("text", "")
        if L.fingerprint(snap2) != L.fingerprint(snap):
            snap = snap2
            g = ROUTER.ground(req.target, L.parse_snapshot(snap), action, req.goal or "", site,
                              judge_url=req.base_url, judge_model=req.model, judge_headers=req.headers)
            g.trace["scope"] = "viewport-settled"
    if g.status in ("none", "no_candidates", "ambiguous"):
        full = _cmd(session, "snapshot -i --full").get("text", "")
        g2 = ROUTER.ground(req.target, L.parse_snapshot(full), action, req.goal or "", site,
                           judge_url=req.base_url, judge_model=req.model, judge_headers=req.headers)
        g2.trace["scope"] = "full-page"
        g2.timings = {k: g.timings.get(k, 0) + g2.timings.get(k, 0) for k in set(g.timings) | set(g2.timings)}
        if g2.status == "match" or g.status == "no_candidates":
            g = g2
    return g, snap


def _options(g) -> list[str]:
    return [lab for _, lab in g.shortlist[:4]]


def do_open(req: Act, progress: Optional[ProgressCallback] = None) -> dict:
    p = progress or (lambda s, m, **e: None)
    p("opening", f"🌐 正在導航至：{req.url}...")
    r = _cmd(req.session, f"open {req.url}")
    st = _page_state(req.session)
    _remember(req.session, st["url"])
    p("opened", f"✓ 頁面載入完成 (標題: 「{st.get('title')}」)")
    return {"status": "OPENED", **_public(st), "screenshot": r.get("screenshotPath")}


def do_read(req: Act) -> dict:
    gt = _cmd(req.session, "get text")
    text = gt.get("pageText") or gt.get("text", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    if req.target and len(text) > 1500:
        # keep paragraphs most related to the question, in page order
        paras = [p for p in text.split("\n") if p.strip()]
        scores = ROUTER.E.scores(req.target, [p[:200] for p in paras]) if paras else []
        keep = sorted(sorted(range(len(paras)), key=lambda i: -scores[i])[:40])
        text = "\n".join(paras[i] for i in keep)
    return {"status": "READ", "text": text[:4000], "truncated": len(text) > 4000}


VISION_DECIDER = os.environ.get("INTENT_VISION_DECIDER", "laya")   # laya | qwen


def _box_elements(elems: list) -> list:
    """Omni boxes -> pseudo snapshot elements built from their CDP hit-test labels (text only)."""
    out = []
    for e in elems:
        lb = e.get("label") or {}
        if not lb:
            continue
        attrs = {}
        if lb.get("placeholder"): attrs["placeholder"] = lb["placeholder"]
        if lb.get("aria"): attrs["aria-label"] = lb["aria"]
        if lb.get("type"): attrs["type"] = lb["type"]
        text = lb.get("text") or lb.get("title") or lb.get("alt") or ""
        tag = lb.get("tag") or "div"
        if tag not in L.CLICKABLE and tag not in ("input", "textarea", "select", "button", "a", "label"):
            tag = "div"                     # dd/dt/td/img/svg...: still a legitimate click target
        # Box centre landed on page background / a whole section: its "text" is the page, not a control.
        if tag in ("html", "body", "main") or (tag in ("article", "section", "div", "span", "p", "h1", "h2")
                                                and len(text) > 40 and not lb.get("role")):
            continue
        if not text and not attrs.get("placeholder") and not attrs.get("aria-label"):
            continue                       # no words to decide with: leave it to the image judge
        if tag in ("input", "textarea", "select"):
            # Keep form fields selectable by a click/fill description (the click prefilter drops inputs).
            kind = {"password": "密碼輸入欄 password field", "email": "email 輸入欄", "search": "搜尋框 search box",
                    "checkbox": "勾選框 checkbox", "radio": "選項 radio"}.get(lb.get("type", ""), "文字輸入欄 text field")
            if tag == "select":
                kind = "下拉選單 dropdown"
            text = " ".join(filter(None, [kind, attrs.get("placeholder", ""), text if tag == "select" else ""]))[:90]
            tag = "button"
        out.append(L.Element(f"b{e['id']}", tag, text, attrs))
    return out


def _decide_box(req: Act, om: dict, elems: list, action: str = "click"):
    """Pick an Omni box. INTENT_VISION_DECIDER=laya: decide on the DOM labels with the same tiers as
    the DOM path (exact/anchor/memory -> e5+Laya -> Qwen text judge); only if that finds nothing,
    fall back to Qwen looking at the numbered screenshot. Returns (box, ms, decider)."""
    t = time.perf_counter()
    candidates = elems
    if VISION_DECIDER == "laya":
        pseudo = _box_elements(elems)
        if pseudo:
            g = ROUTER.ground(req.target, pseudo, "click", req.goal or "")
            if g.status == "match" and g.ref:
                box = next((e for e in elems if f"b{e['id']}" == g.ref), None)
                if box is not None:
                    return box, (time.perf_counter() - t) * 1000, f"laya-label:{g.tier}"
        # Words did not match any labelled box: only boxes WITHOUT usable words (icons, switches, images)
        # are left for the image judge — never re-offer the labelled ones it just rejected.
        labelled = {p.ref for p in pseudo}
        candidates = [e for e in elems if f"b{e['id']}" not in labelled]
        if not candidates:
            return None, (time.perf_counter() - t) * 1000, "laya-label:none"
    pick, vms, raw = vision_judge(req.target, om["screenshotPath"], candidates,
                                  url=req.base_url, model=req.model, headers=req.headers)
    return pick, (time.perf_counter() - t) * 1000, "qwen-vision"


def _vision_click(req: Act, session: str, before: dict):
    """DOM had no named match but the page has text-less controls: OmniParser boxes + Qwen vision pick."""
    t0 = time.perf_counter()
    om = _cmd(session, "vision-mark omni")
    elems = om.get("elements") or []
    if not elems:
        return None
    pick, vms, decider = _decide_box(req, om, elems)
    if pick is None:
        return None
    x, y = pick["center"]
    r = _cmd(session, f"vision-click {x} {y}")
    after = _settled_state(session, before)
    _remember(session, before["url"])
    _remember(session, after["url"])
    changed = {"navigated": after["url"] != before["url"], "dom_changed": after["fingerprint"] != before["fingerprint"]}
    ok = changed["navigated"] or changed["dom_changed"]
    return {"status": "CLICKED" + ("" if ok else "_UNCONFIRMED"), "element": f"vision box {pick['id']} at ({x:.0f},{y:.0f})",
            "decided_by": decider, **changed, "from_url": before["url"], **_public(after),
            "screenshot": r.get("screenshotPath"), "timings_ms": {"vision_decide_ms": round(vms)},
            "total_ms": round((time.perf_counter() - t0) * 1000)}


def _vision_only_click(req: Act, session: str, before: dict, t0: float) -> dict:
    """INTENT_CLICK_MODE=vision: viewport boxes first; if no box fits, scroll one screen and look again."""
    if L.is_risky(req.target) and not req.confirm:
        return {"status": "NEEDS_CONFIRM", "would_click": f"(vision) {req.target}",
                "hint": "This looks irreversible (purchase/checkout/delete/logout). Repeat with confirm=true only if the user asked for it."}
    tried = []
    for attempt in range(2):
        om = _cmd(session, "vision-mark omni")
        elems = om.get("elements") or []
        pick, vms, decider = _decide_box(req, om, elems) if elems else (None, 0.0, "none")
        tried.append({"boxes": len(elems), "decide_ms": round(vms), "decider": decider, "pick": pick["id"] if pick else None})
        if pick is not None:
            x, y = pick["center"]
            r = _cmd(session, f"vision-click {x} {y}")
            after = _settled_state(session, before)
            _remember(session, before["url"])
            _remember(session, after["url"])
            changed = {"navigated": after["url"] != before["url"], "dom_changed": after["fingerprint"] != before["fingerprint"]}
            ok = changed["navigated"] or changed["dom_changed"]
            return {"status": "CLICKED" + ("" if ok else "_UNCONFIRMED"), "element": f"vision box {pick['id']} at ({x:.0f},{y:.0f})",
                    "decided_by": decider, **changed, "from_url": before["url"], **_public(after),
                    "screenshot": r.get("screenshotPath"), "vision_tries": tried,
                    "total_ms": round((time.perf_counter() - t0) * 1000)}
        if attempt == 0:
            _cmd(session, "scroll to 700")   # relative scroll was removed from the worker
    _cmd(session, "scroll top")
    return {"status": "NO_MATCH", "target": req.target, "searched": "vision (2 screens)", "vision_tries": tried,
            **_public(before), "hint": "No box on the screen performs this."}


def do_act(req: Act, action: str, progress: Optional[ProgressCallback] = None) -> dict:
    p = progress or (lambda s, m, **e: None)
    t0 = time.perf_counter()
    session = req.session
    before = _page_state(session)
    if CLICK_MODE == "vision" and action == "click" and not req.ref and req.pick is None:
        return _vision_only_click(req, session, before, t0)

    selected_tag = None
    if req.ref:
        # Escalated planner saw the full snapshot and named a ref: act on it directly (still risk-gated).
        el = next((e for e in L.parse_snapshot(before["_snap"]) if "@" + e.ref == req.ref.strip()), None)
        if el is None:
            return {"status": "NO_MATCH", "target": req.ref, "hint": "ref not on the current page"}
        ref, label, tier, g = el.ref, L._label(el), "full-view", None
        selected_tag = el.tag
        p("grounded", f"🎯 目標鎖定：{label} (@{ref})")
    elif req.pick is not None:
        pend = PENDING.get(session)
        if not pend or pend["fingerprint"] != before["fingerprint"]:
            return {"status": "PICK_EXPIRED", "message": "The page changed since those options were offered; restate the target."}
        if not 1 <= req.pick <= len(pend["refs"]):
            return {"status": "PICK_INVALID", "options": pend["labels"]}
        ref, label, tier, g = pend["refs"][req.pick - 1], pend["labels"][req.pick - 1], "agent-pick", None
        selected = next((e for e in L.parse_snapshot(before["_snap"]) if e.ref == ref), None)
        selected_tag = selected.tag if selected else None
        p("grounded", f"🎯 目標鎖定：{label} (選項 {req.pick})")
    else:
        p("grounding", f"🎯 正在定位「{req.target}」...")
        g, snap_used = _ground(req, session, action, before["_snap"])
        timings = {k: round(v) for k, v in g.timings.items()}
        if (g.status in ("none", "no_candidates") and action == "click"
                and g.trace.get("stats", {}).get("unlabeled", 0) > 0):
            if L.is_risky(req.target) and not req.confirm:
                return {"status": "NEEDS_CONFIRM", "would_click": f"(icon) {req.target}",
                        "hint": "Irreversible-looking target on a text-less control; repeat with confirm=true only if the user asked."}
            vres = _vision_click(req, session, before)
            if vres is not None:
                return vres
        if g.status in ("none", "no_candidates"):
            return {"status": "NO_MATCH", "target": req.target, "searched": g.trace.get("scope"),
                    "nearest": _options(g)[:3], "timings_ms": timings, **_public(before),
                    "hint": "Nothing on the page performs this. Rephrase, scroll/read the page, or it is not available here."}
        if g.status == "ambiguous":
            PENDING[session] = {"fingerprint": before["fingerprint"], "refs": [r for r, _ in g.shortlist[:4]],
                                "labels": _options(g)}
            return {"status": "AMBIGUOUS", "target": req.target, "options": _options(g), "timings_ms": timings,
                    "hint": "Call again with the same action and pick=<1-based option number>, or a more specific target."}
        ref, label, tier = g.ref, g.label, g.tier
        selected = next((e for e in L.parse_snapshot(before["_snap"]) if e.ref == ref), None)
        selected_tag = selected.tag if selected else None
        p("grounded", f"🎯 目標鎖定：{label} (依據: {tier})")

    if action == "click" and L.is_risky(label) and not req.confirm:
        return {"status": "NEEDS_CONFIRM", "would_click": label,
                "hint": "This looks irreversible (purchase/checkout/delete/logout). Repeat with confirm=true only if the user asked for it."}

    p("executing", f"⚡ 執行 {action}：{label}{f' -> \"{req.value}\"' if req.value else ''}...")
    if action == "click":
        r = _cmd(session, f"activate @{ref}")
    elif action == "select":
        r = _cmd(session, f"select @{ref} {_q(req.value or '')}")
    elif action == "fill":
        verb = "fill-submit" if req.submit else "fill"
        r = _cmd(session, f"{verb} @{ref} {_q(req.value or '')}")
    else:
        raise ValueError(action)
    PENDING.pop(session, None)

    p("settling", "⏳ 等待畫面變更與渲染穩定...")
    after = _settled_state(session, before)
    changed = {"navigated": after["url"] != before["url"], "dom_changed": after["fingerprint"] != before["fingerprint"]}
    verified = changed["navigated"] or changed["dom_changed"]
    # Some client-rendered sites swallow the normal CDP activation on anchors. Retry the
    # same grounded anchor once through the worker's deferred DOM click; never broaden or
    # repeat the target, and let NO_PROGRESS_GUARD stop the second no-op.
    if action == "click" and selected_tag == "a" and not verified:
        fallback = _cmd(session, f"click-js @{ref}")
        after = _settled_state(session, before)
        changed = {"navigated": after["url"] != before["url"], "dom_changed": after["fingerprint"] != before["fingerprint"]}
        verified = changed["navigated"] or changed["dom_changed"]
        if verified:
            tier += "+click-js-fallback"
            r = fallback
    _remember(session, before["url"])
    _remember(session, after["url"])
    p("settled", f"✓ 完成 {action}，當前標題: 「{after.get('title')}」")
    out = {"status": {"click": "CLICKED", "fill": "FILLED", "select": "SELECTED"}[action] + ("" if verified else "_UNCONFIRMED"),
           "element": label, "decided_by": tier, **changed, "from_url": before["url"], **_public(after),
           "screenshot": r.get("screenshotPath"), "total_ms": round((time.perf_counter() - t0) * 1000)}
    if not verified:
        out["hint"] = ("The action ran but URL and interactive elements did not change (toasts, badges or "
                       "background requests are invisible here). If it matters, check with observe/read or the screenshot.")
    if g is not None:
        out["timings_ms"] = {k: round(v) for k, v in g.timings.items()}
    return out


def do_scroll(req: Act, progress: Optional[ProgressCallback] = None) -> dict:
    p = progress or (lambda s, m, **e: None)
    t0 = time.perf_counter()
    session = req.session
    before = _page_state(session)
    target = (req.target or "").strip().lower()
    value = (req.value or "").strip().lower()
    combined = f"{target} {value}".strip()

    p("grounding", f"🎯 正在定位捲動目標：「{req.target or req.value or '頁面'}」...")
    r = None
    label = ""
    if any(k in combined for k in ("頂部", "top", "to-top")):
        r = _cmd(session, "scroll top")
        label = "頁面頂部 (top)"
    elif any(k in combined for k in ("底部", "bottom", "to-bottom")):
        r = _cmd(session, "scroll bottom")
        label = "頁面底部 (bottom)"
    elif any(k in combined for k in ("向上", "往上", "up")):
        amt = 600
        m = re.search(r"(\d+)", combined)
        if m:
            amt = int(m.group(1))
        r = _cmd(session, f"scroll up {amt}")
        label = f"向上捲動 {amt}px"
    elif any(k in combined for k in ("向下", "往下", "down")):
        amt = 600
        m = re.search(r"(\d+)", combined)
        if m:
            amt = int(m.group(1))
        r = _cmd(session, f"scroll down {amt}")
        label = f"向下捲動 {amt}px"
    elif req.target:
        g, snap_used = _ground(req, session, "click", before["_snap"])
        if g.status == "match" and g.ref:
            p("grounded", f"🎯 目標鎖定：{g.label} (@{g.ref})")
            r = _cmd(session, f"scroll to-ref @{g.ref}")
            label = f"元素 {g.label} (@{g.ref})"
        else:
            if any(k in target for k in ("導覽", "nav", "header", "頂", "上方")):
                r = _cmd(session, "scroll top")
                label = "頁面頂部導覽列 (top)"
            else:
                r = _cmd(session, "scroll down 600")
                label = f"向下捲動 600px (目標: {req.target})"
    else:
        r = _cmd(session, "scroll down 600")
        label = "向下捲動 600px"

    p("scrolling", f"📜 執行捲動：{label}...")
    after = _settled_state(session, before)
    _remember(session, after["url"])
    changed = {"navigated": after["url"] != before["url"], "dom_changed": after["fingerprint"] != before["fingerprint"]}
    p("settled", f"✓ 捲動完成，頁面已穩定 (標題: 「{after.get('title')}」)")
    return {
        "status": "SCROLLED",
        "action": "scroll",
        "element": label,
        "detail": f"已完成捲動至 {label}",
        **changed,
        "from_url": before["url"],
        **_public(after),
        "screenshot": r.get("screenshotPath") if r else None,
        "total_ms": round((time.perf_counter() - t0) * 1000),
    }


def do_screenshot(req: Act, progress: Optional[ProgressCallback] = None) -> dict:
    p = progress or (lambda s, m, **e: None)
    p("screenshot", "📸 正在擷取當前頁面截圖...")
    t0 = time.perf_counter()
    r = _cmd(req.session, "screenshot")
    st = _page_state(req.session)
    shot = r.get("screenshotPath")
    p("screenshot_done", "✓ 螢幕截圖已完成")
    return {
        "status": "SCREENSHOT_TAKEN",
        "url": st["url"],
        "title": st["title"],
        "screenshot": shot,
        "total_ms": round((time.perf_counter() - t0) * 1000),
    }


OK_STATUSES = {"OPENED", "CLICKED", "FILLED", "SELECTED", "SELECTED_UNCONFIRMED", "SCROLLED", "READ", "OBSERVED", "WENT_BACK", "CLICKED_UNCONFIRMED", "FILLED_UNCONFIRMED", "SCREENSHOT_TAKEN"}
SLIM = ("status", "action", "element", "detail", "decided_by", "url", "title", "page_excerpt", "text", "options", "nearest", "would_click", "hint", "message")


def do_plan(req: Act, progress: Optional[ProgressCallback] = None) -> dict:
    """Run several intents in one agent turn; stop at the first step that needs the agent."""
    p = progress or (lambda s, m, **e: None)
    t0 = time.perf_counter()
    done, last = [], {}
    total = len(req.steps or [])
    for i, st in enumerate(req.steps or []):
        step_desc = f"{st.action}"
        if st.target: step_desc += f' "{st.target}"'
        if st.url: step_desc += f' {st.url}'
        if st.value: step_desc += f' -> "{st.value}"'
        p("plan_step", f"📋 [步驟 {i + 1}/{total}] 正在執行: {step_desc}...",
          current_step=i + 1, total_steps=total, step_action=st.action, step_target=st.target or st.url or st.value)
        sub = Act(session=req.session, goal=req.goal, action=st.action, target=st.target, value=st.value,
                  submit=st.submit, url=st.url, model=req.model, base_url=req.base_url, headers=req.headers)
        last = _dispatch(sub, p)
        done.append({"step": i + 1, "action": st.action, **{k: last[k] for k in SLIM if k in last}})
        if last.get("status") not in OK_STATUSES:
            p("plan_step_failed", f"⚠️ [步驟 {i + 1}/{total}] 執行中斷: {last.get('status')}")
            break
        p("plan_step_done", f"✓ [步驟 {i + 1}/{total}] 完成: {step_desc}")
    finished = len(done) == len(req.steps or []) and last.get("status") in OK_STATUSES
    return {"status": "PLAN_DONE" if finished else "PLAN_STOPPED", "steps": done,
            "screenshot": last.get("screenshot"), "total_ms": round((time.perf_counter() - t0) * 1000)}


def _planner_elements(session: str, goal: str, snap: str, limit: int = 30) -> list[str]:
    """Compact, goal-ranked element names for the planner (never shown to the main agent)."""
    els = L.parse_snapshot(snap)
    pool = []
    for kind in ("fill", "select", "click"):
        cands, _ = L.build_candidates(els, kind)
        pool += [c for c in cands if c not in pool]
    if not pool:
        return []
    labels = [L._label(c, 160) for c in pool]
    if len(labels) > limit:
        sc = ROUTER.E.scores(goal, labels)
        keep = sorted(sorted(range(len(labels)), key=lambda i: -sc[i])[:limit])
        labels = [labels[i] for i in keep]
    return labels


def do_task(req: Act, progress: Optional[ProgressCallback] = None) -> dict:
    """Autopilot: the agent gives a goal; a no-thinking small-context planner picks each next step,
    grounding/safety/verification are the same tiers as single actions. Hands back on done/fail/confirm."""
    p = progress or (lambda s, m, **e: None)
    t0 = time.perf_counter()
    session, goal = req.session, (req.goal or req.target or "")
    trace, history, misses, escalate, boxes = [], [], 0, False, []
    p("task_start", f"🎯 開始自主瀏覽任務：「{goal}」...")
    if not req.url:
        # The agent often leaves the user's URL inside the goal text; use it only if no page is open yet.
        try:
            has_page = bool((_cmd(session, "get url").get("text") or "").strip())
        except Exception:
            has_page = False
        m = re.search(r"(https?://[^\s，。）)]+|file://[^\s，。）)]+)", goal)
        if not has_page and m:
            req.url = m.group(1)
        elif not has_page:
            return {"status": "TASK_FAILED", "reason": "No page is open and the goal contains no URL; pass url."}
    if req.url:
        r = do_open(Act(session=session, action="open", url=req.url), p)
        trace.append({"step": 0, "action": "open", "status": r["status"], "url": r.get("url")})
        history.append(f"opened {r.get('url')}")
    last = {}
    for i in range(1, max(1, min(req.max_steps, 12)) + 1):
        st = _page_state(session)
        if escalate:
            # Fallback: the compact view failed; look at the full snapshot -i plus a screenshot.
            om = _cmd(session, "vision-mark omni")      # annotated screenshot with numbered boxes
            boxes = om.get("elements") or []
            step, pms = next_step_full(goal, history, st, st["_snap"], om.get("screenshotPath"), boxes,
                                       url=req.base_url, model=req.model, headers=req.headers)
            view = "full"
        else:
            step, pms = next_step(goal, history, st, _planner_elements(session, goal, st["_snap"]),
                                  url=req.base_url, model=req.model, headers=req.headers)
            view = "compact"
        rec = {"step": i, "view": view, "plan": {k: step.get(k) for k in ("action", "ref", "target", "value", "reason") if step.get(k)},
               "planner_ms": round(pms)}
        p("task_step", f"📋 [步驟 {i}/{req.max_steps}] 規劃動作: {step.get('action')} \"{step.get('target', '')}\"")
        if step["action"] == "fail" and view == "compact":
            # The compact view hides icon-only controls; never give up before looking at the full page + screenshot.
            trace.append(rec)
            history.append(f"compact view could not find a way ({step.get('reason', '')}); looking at the full page")
            escalate = True
            continue
        if step["action"] in ("done", "fail"):
            rd = do_read(Act(session=session, action="read", target=goal))
            trace.append(rec)
            status, extra = ("TASK_FAILED", {}) if step["action"] == "fail" else ("TASK_DONE", {})
            if step["action"] == "done":
                p("verifying", "🔍 正在驗證目標是否已在當前頁面實現...")
                # Independent check: the planner's "done" must be supported by the visible page text.
                ok, evidence, vms = verify_done(goal, history, st, rd["text"],
                                                url=req.base_url, model=req.model, headers=req.headers)
                extra = {"verified": ok, "evidence": evidence, "verify_ms": round(vms)}
                if not ok:
                    status = "TASK_UNVERIFIED"
                    extra["hint"] = "Planner claimed done but the page text does not show the goal achieved; check before answering."
            return {"status": status, "reason": step.get("reason"), **extra, "steps": trace, "url": st["url"],
                    "title": st["title"], "text": rd["text"], "total_ms": round((time.perf_counter() - t0) * 1000)}
        box = next((b for b in (boxes if view == "full" else []) if str(b["id"]) == str(step.get("box"))), None)
        if box is not None and step["action"] == "click":
            if L.is_risky(f"{step.get('target', '')} {step.get('reason', '')}") and not req.confirm:
                rec.update(status="NEEDS_CONFIRM")
                trace.append(rec)
                return {"status": "TASK_NEEDS_CONFIRM", "would_click": f"box {box['id']}: {step.get('target')}", "steps": trace,
                        "hint": "Irreversible step reached. Only continue if the user explicitly asked (task with confirm=true).",
                        "total_ms": round((time.perf_counter() - t0) * 1000)}
            before = _page_state(session)
            x, y = box["center"]
            p("task_exec", f"⚡ 執行點擊視覺方塊 {box['id']} ({step.get('target', '')})...")
            r = _cmd(session, f"vision-click {x} {y}")
            after = _settled_state(session, before)
            moved = after["url"] != before["url"] or after["fingerprint"] != before["fingerprint"]
            last = {"status": "CLICKED" if moved else "CLICKED_UNCONFIRMED", "element": f"box {box['id']} ({step.get('target', '')})",
                    "decided_by": "full-view-box", "title": after["title"], "url": after["url"]}
            _remember(session, after["url"])
            rec.update({k: last[k] for k in ("status", "element", "decided_by")})
            trace.append(rec)
            p("task_settled", f"✓ 步驟 {i} 點擊完成，當前頁面: 「{after.get('title', '')}」")
            if moved:
                misses, escalate = 0, False
                history.append(f"click box {box['id']} '{step.get('target')}' -> CLICKED, now: {after['page_excerpt'][:80]}")
            else:
                misses += 1
                escalate = True
                history.append(f"click box {box['id']} '{step.get('target')}' -> no visible change")
                if misses >= 2:
                    break
            continue
        ref = step.get("ref") if view == "full" and str(step.get("ref") or "").startswith("@e") else None
        sub = Act(session=session, action=step["action"], target=step.get("target") or (ref or ""),
                  value=step.get("value"), submit=bool(step.get("submit")), goal=goal, ref=ref, confirm=req.confirm,
                  model=req.model, base_url=req.base_url, headers=req.headers)
        p("task_exec", f"⚡ 執行步驟 {i}: {step['action']} \"{step.get('target', '')}\"...")
        last = do_act(sub, step["action"], p)
        if ref and last.get("status") == "NO_MATCH" and step["action"] == "click":
            # Hallucinated/invalid ref from the full view: fall back to picking on the screenshot itself.
            before = _page_state(session)
            v = _vision_click(Act(session=session, action="click", target=step.get("target") or goal,
                                  model=req.model, base_url=req.base_url, headers=req.headers), session, before)
            if v is not None:
                last = v
        rec.update({k: last[k] for k in ("status", "element", "decided_by") if k in last})
        trace.append(rec)
        status = last.get("status", "")
        p("task_settled", f"✓ 步驟 {i} 完成 ({status})，當前頁面: 「{last.get('title', '')}」")
        if status == "NEEDS_CONFIRM":
            return {"status": "TASK_NEEDS_CONFIRM", "would_click": last.get("would_click"), "steps": trace,
                    "hint": "Irreversible step reached. Only continue (click with confirm=true) if the user asked.",
                    "total_ms": round((time.perf_counter() - t0) * 1000)}
        # "_UNCONFIRMED" = the click changed nothing visible: treat as no progress, like a miss.
        if status in ("NO_MATCH", "AMBIGUOUS", "ERROR", "PICK_EXPIRED") or status.endswith("_UNCONFIRMED"):
            misses += 1
            history.append(f"{step['action']} '{step.get('target')}' -> {status} (nothing done; choose differently)")
            if misses >= 2:
                break
            escalate = True      # next decision uses the full snapshot + screenshot
            continue
        misses, escalate = 0, False
        history.append(f"{step['action']} '{step.get('target')}'"
                       + (f" = '{step.get('value')}'" if step.get("value") else "")
                       + f" -> {status}, now: {(last.get('page_excerpt') or last.get('title') or '')[:80]}")
    st = _page_state(session)
    return {"status": "TASK_STOPPED", "steps": trace, "url": st["url"], "title": st["title"],
            "page_excerpt": st["page_excerpt"], "last": {k: last.get(k) for k in ("status", "options", "nearest", "hint") if last.get(k)},
            "hint": "Autopilot stopped (repeated misses or step limit). Continue with single actions or restate the goal.",
            "total_ms": round((time.perf_counter() - t0) * 1000)}


def _dispatch(req: Act, progress: Optional[ProgressCallback] = None) -> dict:
    p = progress or (lambda s, m, **e: None)
    if req.action == "task":
        return do_task(req, p)
    if req.action == "plan":
        return do_plan(req, p)
    if req.action == "open":
        return do_open(req, p)
    if req.action == "back":
        return do_back(req)
    if req.action == "read":
        return do_read(req)
    if req.action == "cleanup":
        PENDING.pop(req.session, None)
        HISTORY.pop(req.session, None)
        TEXT_CACHE.pop(req.session, None)
        # session-cleanup only drops temp artifacts; `close` is what releases the tab (20-tab cap).
        try:
            nd.request("close", req.session)
        finally:
            nd.request("session-cleanup", req.session)
        return {"status": "CLEANED"}
    if req.action == "observe":
        st = _page_state(req.session)
        return {"status": "OBSERVED", **_public(st)}
    if req.action == "scroll":
        return do_scroll(req, p)
    if req.action == "screenshot":
        return do_screenshot(req, p)
    if req.action in ("click", "fill", "select"):
        if not req.target and req.pick is None:
            return {"status": "ERROR", "message": "target is required"}
        return do_act(req, req.action, p)
    return {"status": "ERROR", "message": f"unknown action {req.action}"}


@app.post("/act")
def act(req: Act) -> dict:
    with LOCK:
        try:
            return _dispatch(req)
        except Exception as exc:  # surfaced to the agent verbatim
            return {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}


@app.post("/act/stream")
def act_stream(req: Act):
    def event_stream():
        q: queue.Queue = queue.Queue()

        def progress_cb(stage: str, message: str, **extra: Any):
            q.put({"type": "progress", "stage": stage, "message": message, **extra})

        def run_worker():
            with LOCK:
                try:
                    res = _dispatch(req, progress=progress_cb)
                    q.put({"type": "result", "data": res})
                except Exception as exc:
                    q.put({"type": "result", "data": {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}})
                finally:
                    q.put(None)

        th = threading.Thread(target=run_worker, daemon=True)
        th.start()

        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "embedder": ROUTER.E.repo if ROUTER else None, "click_mode": CLICK_MODE}


def main():
    global ROUTER
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--embedder", default="intfloat/multilingual-e5-small")
    a = ap.parse_args()
    ROUTER = Router(Embedder(a.embedder))
    ROUTER.E.scores("warm", ["up"])
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
