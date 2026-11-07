"""The research page capture: the page's innerText up to `limit` UTF-16 units, and how much was cut."""


def research_capture_js(limit):
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
    })()'''.replace('LIMIT', str(int(limit)))
