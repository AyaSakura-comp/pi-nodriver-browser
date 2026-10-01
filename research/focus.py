"""Per-page focus filter that runs right after each crawl (pipelined, not batched).

A small local model (e.g. Qwen3-0.6B on llama-server) copies the helpful
sentences; the host keeps only lines that occur verbatim in the page, so the
filter can drop text but never paraphrase or invent it. (Returning sentence
numbers was tried: 0.6B answers [] or long consecutive runs.) Windows fail
independently; only a total failure returns None and the caller keeps the page.

The model runs in its own llama-server process(es). Within this daemon, every
research job (possibly from different Pi sessions) shares one pool: each server
filters one page at a time, pages are served in arrival order, and each request
carries only its own page and question, so sessions cannot see each other's text.
RESEARCH_FOCUS_URL may list several servers (comma-separated, fastest first);
RESEARCH_FOCUS_MAX_WINDOWS limits the model to a page's first N windows (default 1,
0 = all); a model-free keyword pass over the whole page adds up to 3 more lines.
"""
import asyncio
import json
import os
import re
import urllib.request

_SPLIT = re.compile(r'\n+|(?<=[。！？!?])|(?<=[.;:])\s+')
_WINDOW_CHARS = 6000
_PROMPT = ('You extract evidence. Copy ONLY sentences from the page that help answer the question, '
           'verbatim, in the original language, one per line. Do not summarize, translate or add anything. '
           'If nothing helps, output NONE.')


class _Pool:
    """Daemon-wide pool of filter servers (e.g. GPU first, then CPU). Each
    server takes one page at a time; a waiting page gets the first free server
    in listed order, and waiters are served first-come first-served."""

    def __init__(self, urls):
        self.urls = urls
        self.busy = [False] * len(urls)
        self.cond = asyncio.Condition()

    async def acquire(self):
        async with self.cond:
            while all(self.busy):
                await self.cond.wait()
            i = self.busy.index(False)
            self.busy[i] = True
            return i

    async def release(self, i):
        async with self.cond:
            self.busy[i] = False
            self.cond.notify(1)


_POOLS = {}


def _pool(urls):
    key = (asyncio.get_running_loop(), tuple(urls))
    if key not in _POOLS:
        _POOLS[key] = _Pool(list(urls))
    return _POOLS[key]


def endpoints():
    return [u.strip() for u in os.environ.get('RESEARCH_FOCUS_URL', '').split(',') if u.strip()]


def endpoint():
    urls = endpoints()
    return urls[0] if urls else None


def _max_windows():
    # Default: the model reads only a page's first ~6000 chars (0 = no limit).
    value = os.environ.get('RESEARCH_FOCUS_MAX_WINDOWS', '1')
    return int(value) if value.isdigit() and int(value) > 0 else None


def sentences(text):
    return [s.strip() for s in _SPLIT.split(text) if s and s.strip()]


def _windows(text):
    window, size = [], 0
    for s in sentences(text):
        if window and size + len(s) > _WINDOW_CHARS:
            yield '\n'.join(window)
            window, size = [], 0
        window.append(s)
        size += len(s)
    if window:
        yield '\n'.join(window)


def _norm(value):
    return re.sub(r'\s+', ' ', value).strip().lower()


def _extract(url, question, title, window, timeout):
    body = {'messages': [{'role': 'system', 'content': _PROMPT},
                         {'role': 'user', 'content': f'Question: {question}\nPage title: {title}\n\nPage:\n{window}'}],
            'temperature': 0, 'max_tokens': 400, 'chat_template_kwargs': {'enable_thinking': False}}
    request = urllib.request.Request(url.rstrip('/') + '/v1/chat/completions', json.dumps(body).encode(),
                                     {'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        reply = json.load(response)
    content = re.sub(r'<think>.*?</think>', '', reply['choices'][0]['message']['content'], flags=re.S)
    source = _norm(window)
    kept = []
    for line in content.split('\n'):
        line = re.sub(r'^\s*(?:[-*•]|\d+[.)])\s+', '', line).strip()
        # Verbatim only: anything paraphrased, translated or invented is dropped.
        if len(line) >= 8 and line.upper() != 'NONE' and _norm(line) in source and line not in kept:
            kept.append(line)
    return kept


_STOP = set('the and for with that this from what why how are was were does did not you your about into than then when which who whose will can its his her our their have has had use using official please 請 的'.split())


def _terms(question, queries):
    text = ' '.join([question, *queries]).lower()
    words = {w for w in re.findall(r'[a-z][a-z0-9_.+-]{2,}', text) if w not in _STOP}
    cjk = re.sub(r'[^\u4e00-\u9fff]', ' ', text)
    grams = {chunk[i:i + 2] for chunk in cjk.split() for i in range(len(chunk) - 1)}
    return words, grams


def lexical_lines(text, question, queries=(), k=3, min_hits=2):
    """Deterministic safety net: the k sentences sharing the most distinct
    question/query terms, copied verbatim. Guards against the small model
    judging a clearly on-topic page irrelevant."""
    words, grams = _terms(question, queries)
    scored = []
    for i, s in enumerate(sentences(text)):
        if len(s) < 20 or len(s) > 600:
            continue
        low = s.lower()
        seen = set(re.findall(r'[a-z][a-z0-9_.+-]{2,}', low))
        # Prefix match for terms of 4+ letters: tilt~tilted, season~seasons.
        hits = sum(1 for w in words if w in seen or (len(w) >= 4 and any(x.startswith(w) for x in seen)))
        hits += sum(g in low for g in grams)
        if hits >= min_hits:
            scored.append((-hits, i, s))
    return [s for _, _, s in sorted(scored)[:k]]


async def _run_windows(url, question, title, windows, timeout):
    return await asyncio.gather(*(asyncio.wait_for(
        asyncio.to_thread(_extract, url, question, title, w, timeout), timeout) for w in windows),
        return_exceptions=True)


async def focus_page(question, title, text, *, timeout=20.0, queries=()):
    """Return (focused_text, stats) or None when disabled/failed/not smaller.

    focused_text is '' when every window answered NONE: the page is judged
    irrelevant and the caller delivers only a pointer to the preserved page.
    """
    urls = endpoints()
    if not urls or not text.strip():
        return None
    windows = list(_windows(text))
    limit = _max_windows()
    skipped = max(0, len(windows) - limit) if limit else 0
    if limit:
        windows = windows[:limit]
    pool = _pool(urls)
    slot = await pool.acquire()
    try:
        results = await _run_windows(urls[slot], question, title, windows, timeout)
    finally:
        await pool.release(slot)
    failed = sum(isinstance(r, BaseException) for r in results)
    if failed == len(windows):
        return None
    lines = [line for r in results if not isinstance(r, BaseException) for line in r]
    model_lines = len(lines)
    # The keyword safety net scans the WHOLE page: it is cheap and model-free.
    for line in lexical_lines(text, question, queries):
        if line not in lines:
            lines.append(line)
    focused = '\n'.join(lines)
    if len(focused) >= len(text):
        return None
    return focused, dict(windows=len(windows), failed_windows=failed, skipped_windows=skipped,
                         lines=len(lines), model_lines=model_lines, lexical_lines=len(lines) - model_lines,
                         source_chars=len(text), focused_chars=len(focused), server=slot)
