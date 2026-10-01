"""Distinctive anchors: quoted text, prices and model codes in the target that pick out exactly one candidate."""
import re
import laya_intent as L

_Q = re.compile(r'[「『"“]([^」』"”]{2,60})[」』"”]')
_PRICE = re.compile(r'\$\s?\d[\d,]{2,}')
_CODE = re.compile(r'\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{3,}\b')


def _c(s):
    return re.sub(r"\s+", "", s).lower()


def anchor_match(intent: str, cands: list[L.Element]):
    names = [_c(c.name()) for c in cands]
    anchors = [_c(a) for a in _Q.findall(intent)] + [_c(p) for p in _PRICE.findall(intent)]
    anchors += [_c(x) for x in _CODE.findall(intent)]
    hit = None
    for a in dict.fromkeys(anchors):
        idx = [i for i, n in enumerate(names) if a and a in n]
        if len(idx) == 1:
            if hit is not None and hit != idx[0]:
                return None           # anchors disagree -> let the slower tiers decide
            hit = idx[0]
        # anchors hitting 0 or several candidates are simply non-distinctive
    return cands[hit] if hit is not None else None
