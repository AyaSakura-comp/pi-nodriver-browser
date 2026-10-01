"""Tiered element grounding: intent + snapshot -> one @ref, or none.

Tier 0  exact name     : exactly one candidate's visible name appears verbatim in the intent.
Tier 1  embed + Laya   : e5 top-1 and Laya (choice, no none_match) agree and are both confident.
Tier 2  shortlist judge: a local LLM sees ONLY the <=8 shortlisted labels (+ "none").
The full DOM never reaches the main agent or the judge.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field

import laya_intent as L
from judge import judge
from anchors import anchor_match

_VERBS = re.compile(r"^(請|幫我|我要|我想|想要)?\s*(點擊|點選|點一下|點|按下|按|打開|開啟|進入|前往|查看|看|去|選|選擇)?\s*", re.I)


@dataclass
class Grounding:
    status: str                       # match | none | ambiguous | no_candidates
    ref: str | None = None
    label: str | None = None
    tier: str | None = None
    shortlist: list = field(default_factory=list)   # [(ref, label)]
    timings: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


_KIND = re.compile(r"^\s*(link|button|input(\s+type=\w+)?|textbox|dropdown|div|span|li|label|a)\s*:\s*", re.I)


def exact_name_match(intent: str, cands: list[L.Element]) -> L.Element | None:
    # The autopilot copies labels as "button: ×"; strip the kind prefix and trailing hint brackets.
    stripped = re.sub(r"\s*\[[^\]]*\]\s*$", "", _KIND.sub("", intent)).rstrip("…")
    whole = [c for c in cands if _compact(c.name() or L._label(c).split(": ", 1)[-1]) == _compact(stripped)]
    if len(whole) == 1:
        return whole[0]
    q = _compact(_VERBS.sub("", stripped))
    # A hit must cover a real share of the intent: "系統" inside "加不斷電系統的組合" is not a target name.
    hits = [c for c in cands if len(_compact(c.name())) >= 2 and _compact(c.name()) in q
            and len(_compact(c.name())) / max(1, len(q)) >= 0.3]
    if not hits:
        return None
    hits.sort(key=lambda c: -len(_compact(c.name())))
    best = hits[0]
    # The winner must be strictly longer than every other hit, and no other candidate may share its name.
    if len(hits) > 1 and len(_compact(hits[1].name())) == len(_compact(best.name())):
        return None
    if len(hits) > 1 and _compact(hits[1].name()) not in _compact(best.name()):
        return None
    return best


MEMORY_PATH = os.environ.get("INTENT_MEMORY", os.path.expanduser("~/.cache/laya-browser-intent/memory.json"))


class GroundingMemory:
    """(site, action, target) -> element name that a slow tier chose before. Replayed only when an
    element with exactly that name is on the page again, so a changed page falls back to the tiers."""

    def __init__(self, path: str = MEMORY_PATH):
        self.path = path
        try:
            self.data = json.load(open(path))
        except Exception:
            self.data = {}

    @staticmethod
    def key(site: str, action: str, target: str) -> str:
        return f"{site}|{action}|{_compact(target)}"

    def get(self, site, action, target, cands):
        name = self.data.get(self.key(site, action, target))
        hits = [c for c in cands if name and c.name() == name]
        return hits[0] if len(hits) == 1 else None

    def put(self, site, action, target, elem):
        self.data[self.key(site, action, target)] = elem.name()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        json.dump(self.data, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, self.path)


class Router:
    def __init__(self, embedder, shortlist_k: int = 8, use_judge: bool = True,
                 laya_p: float = 0.6, embed_margin: float = 0.02):
        self.E = embedder
        self.k = shortlist_k
        self.use_judge = use_judge
        self.laya_p = laya_p
        self.embed_margin = embed_margin
        self.memory = GroundingMemory()

    def shortlist(self, intent: str, cands: list[L.Element]):
        labels = [L._label(c) for c in cands]
        t = time.perf_counter()
        es = self.E.scores(intent, labels)
        ms = (time.perf_counter() - t) * 1000
        lex = L.lexical_scores(intent, cands)
        by_e = sorted(range(len(cands)), key=lambda i: -es[i])
        by_l = [i for i in sorted(range(len(cands)), key=lambda i: -lex[i]) if lex[i] > 0]
        picked = []
        for i in by_e[: self.k - 3] + by_l[:3] + by_e[self.k - 3:]:
            if i not in picked:
                picked.append(i)
            if len(picked) >= self.k:
                break
        return picked, es, lex, ms

    def ground(self, intent: str, elements: list[L.Element], action: str = "click", goal: str = "",
               site: str = "", judge_url: str | None = None, judge_model: str | None = None,
               judge_headers: dict | None = None) -> Grounding:
        g = self._ground(intent, elements, action, goal, site,
                         judge_url=judge_url, judge_model=judge_model, judge_headers=judge_headers)
        if site and g.status == "match" and g.tier in ("laya", "judge"):
            elem = next((e for e in elements if e.ref == g.ref), None)
            if elem is not None:
                self.memory.put(site, action, intent, elem)
        return g

    def _ground(self, intent, elements, action, goal, site,
                judge_url: str | None = None, judge_model: str | None = None,
                judge_headers: dict | None = None) -> Grounding:
        cands, stats = L.build_candidates(elements, action)
        g = Grounding("no_candidates", trace={"stats": stats, "candidates": len(cands)})
        if not cands:
            return g
        if action in ("fill", "select") and len(cands) == 1:
            c = cands[0]
            return Grounding("match", c.ref, L._label(c), "single", trace=g.trace)

        hit = exact_name_match(intent, cands)
        if hit is not None:
            return Grounding("match", hit.ref, L._label(hit), "exact", trace=g.trace)
        if site:
            hit = self.memory.get(site, action, intent, cands)
            if hit is not None:
                return Grounding("match", hit.ref, L._label(hit), "memory", trace=g.trace)
        hit = anchor_match(intent, cands)
        if hit is not None:
            return Grounding("match", hit.ref, L._label(hit), "anchor", trace=g.trace)

        idx, es, lex, ems = self.shortlist(intent, cands)
        short = [cands[i] for i in idx]
        g.shortlist = [(c.ref, L._label(c, 120)) for c in short]
        g.timings["embed_ms"] = round(ems, 1)

        # Tier 1: Laya over the embedding-ordered shortlist (no none_match: it is uncalibrated).
        lr = L.laya_choose(intent, short, goal, with_none=False, action=action)
        g.timings["laya_ms"] = round(lr["ms"], 1)
        laya_top, laya_p = lr["ranked"][0]
        e_sorted = sorted(es[i] for i in idx)
        e_top = cands[max(idx, key=lambda i: es[i])]
        e_margin = e_sorted[-1] - e_sorted[-2] if len(e_sorted) > 1 else 1.0
        g.trace.update(laya=[(k, round(v, 3)) for k, v in lr["ranked"][:3]], embed_top=e_top.ref,
                       embed_margin=round(e_margin, 3))
        # With a single candidate "agreement" is automatic and says nothing: let the judge (which has none) decide.
        if len(short) >= 2 and laya_top == e_top.ref and laya_p >= self.laya_p and e_margin >= self.embed_margin:
            return Grounding("match", e_top.ref, L._label(e_top), "laya", g.shortlist, g.timings, g.trace)

        if not self.use_judge:
            return Grounding("ambiguous", laya_top, None, "laya", g.shortlist, g.timings, g.trace)

        # Tier 2: shortlist judge with an explicit none, asked twice with the candidate order reversed.
        # Only an order-independent answer is acted on; disagreement goes back to the agent.
        q = intent + (f"（整體任務：{goal}）" if goal else "")
        c1, ms1, raw1 = judge(q, g.shortlist, url=judge_url, model=judge_model, headers=judge_headers)
        # Second opinion only when the judge's pick is not backed by the embedding or Laya top-1
        # (on the 30-case eval this skipped 11/20 second passes with identical outcomes).
        if len(short) >= 2 and c1 != "none" and c1 in (e_top.ref, laya_top):
            c2, ms2 = c1, 0.0
            g.trace["second_pass"] = "skipped"
        else:
            c2, ms2, raw2 = judge(q, list(reversed(g.shortlist)), url=judge_url, model=judge_model, headers=judge_headers)
        g.timings["judge_ms"] = round(ms1 + ms2, 1)
        g.trace["judge"] = [c1, c2]
        g.trace["judge_raw"] = raw1
        if "none" in (c1, c2):   # any "none" vote: do not click; nothing clearly performs the intent
            return Grounding("none", None, None, "judge", g.shortlist, g.timings, g.trace)
        if c1 != c2:
            return Grounding("ambiguous", None, None, "judge", g.shortlist, g.timings, g.trace)
        return Grounding("match", c1, dict(g.shortlist)[c1], "judge", g.shortlist, g.timings, g.trace)
