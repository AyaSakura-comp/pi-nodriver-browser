"""Cheap text threshold, not model tokenization. No model/service calls."""
import math
import re

_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]')
_WORD = re.compile(r'[^\W_]+', re.UNICODE)


def rough_words(text: str) -> int:
    if not isinstance(text,str):
        raise ValueError('text must be a string')
    if not text.strip():
        return 0
    cjk = len(_CJK.findall(text))
    words = _WORD.findall(_CJK.sub(' ',text))
    lexical = cjk + sum(max(1,math.ceil(len(w)/8)) for w in words)
    # Long unbroken or punctuation-only runs must not evade the threshold.
    return max(lexical,math.ceil(len(text.strip())/8))
