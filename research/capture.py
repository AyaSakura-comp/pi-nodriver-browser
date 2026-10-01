"""Acquisition-only HTML ceiling; no relevance selection or semantic trimming."""
from .contracts import natural


def research_capture_js(limit):
    natural(limit, 'capture limit', 1)
    if limit > 1_000_000:
        raise ValueError('capture limit exceeded')
    return r'''(() => {
        const text = document.body?.innerText || '';
        const limit = LIMIT;
        let end = Math.min(text.length, limit);
        // JS lengths are UTF-16 units; do not transport an unpaired surrogate.
        if (end > 0 && end < text.length &&
            text.charCodeAt(end-1) >= 0xD800 && text.charCodeAt(end-1) <= 0xDBFF &&
            text.charCodeAt(end) >= 0xDC00 && text.charCodeAt(end) <= 0xDFFF) end--;
        return {text:text.slice(0,end),sourceChars:text.length,capturedChars:end,
                captureLimit:limit,captureUnits:'utf16',truncated:end<text.length};
    })()'''.replace('LIMIT', str(limit))
