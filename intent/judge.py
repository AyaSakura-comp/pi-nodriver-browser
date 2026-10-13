"""System-2 fallback: ask a local LLM to pick among a SHORTLIST only (never the full DOM)."""
import json, os, re, time, urllib.error, urllib.parse, urllib.request
DEFAULT_JUDGE_URL = "http://127.0.0.1:8001/v1/chat/completions"
DEFAULT_JUDGE_MODEL = "qwen3.6-35b-q4"
JUDGE_URL = os.environ.get("JUDGE_URL", DEFAULT_JUDGE_URL)
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE_MODEL)


def resolve_endpoint(url: str | None = None) -> str:
    env_url = os.environ.get("JUDGE_URL")
    if env_url:
        return env_url
    if not url:
        return DEFAULT_JUDGE_URL
    u = url.strip().rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return f"{u}/chat/completions"
    return f"{u}/v1/chat/completions"


def resolve_model(model: str | None = None) -> str:
    env_model = os.environ.get("JUDGE_MODEL")
    if env_model:
        return env_model
    if model and model.strip():
        return model.strip()
    return DEFAULT_JUDGE_MODEL


def make_headers(custom_headers: dict | None = None, api_key: str | None = None) -> dict:
    hdrs = {"content-type": "application/json"}
    if custom_headers:
        hdrs.update(custom_headers)
    if api_key and "Authorization" not in hdrs and "authorization" not in hdrs:
        hdrs["Authorization"] = f"Bearer {api_key}"
    return hdrs


def is_local(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def chat_body(target_url: str, model: str, max_tokens: int, messages: list) -> dict:
    """Greedy, no-thinking sampling is a llama.cpp knob; hosted models may reject it
    (Claude Opus 5.5: `temperature` is deprecated -> HTTP 400), so only local servers get it."""
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if is_local(target_url):
        body["temperature"] = 0
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def chat(target_url: str, hdrs: dict, body: dict, timeout: float) -> str:
    req = urllib.request.Request(target_url, json.dumps(body).encode(), hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        # The status line alone ("400 Bad Request") hides why the provider refused.
        detail = e.read(400).decode("utf-8", "replace").strip()
        raise RuntimeError(f"planner model HTTP {e.code} from {urllib.parse.urlparse(target_url).netloc}: {detail}") from None
    except (TimeoutError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, TimeoutError):
            raise
        # A shared local server with every slot busy queues the request past the timeout.
        raise RuntimeError(f"planner model at {urllib.parse.urlparse(target_url).netloc} did not answer within "
                           f"{timeout:g}s (server busy or down); no page action was taken for this step") from None


PROMPT = """You map a browser user's intent to ONE on-page element.
Intent: {intent}
Candidates:
{lines}
Rules: pick the element whose click performs exactly the intent. Respect constraints such as "not a bundle", quantity, brand, model, or stock. If the intent names a brand or model, the candidate must be that brand/model, not a related or compatible product. Items marked out of stock do not satisfy "in stock". A free gift (附贈/送) does not make an item a bundle; a bundle has 兩入/*2/+another product. A page heading, a promo, or a similar-but-different item is NOT a match. If no candidate performs the intent, answer none. Few candidates is NOT a reason to pick one: a "+" button is not a switch, a text-less control is not a coupon link; answer none unless the candidate itself fits.
Reply with JSON only: {{"check": "<=25 words: which candidate matches the key words of the intent and why others fail>", "choice": "<ref or none>"}}"""


def judge(intent, cands, timeout=20, url=None, model=None, headers=None):
    lines = "\n".join(f"{ref}: {label}" for ref, label in cands)
    target_model = resolve_model(model)
    target_url = resolve_endpoint(url)
    hdrs = make_headers(headers)
    body = chat_body(target_url, target_model, int(os.environ.get("JUDGE_MAX_TOKENS", "96")),
                     [{"role": "user", "content": PROMPT.format(intent=intent, lines=lines)}])
    t = time.perf_counter()
    out = chat(target_url, hdrs, body, timeout)
    ms = (time.perf_counter() - t) * 1000
    m = re.search(r'"choice"\s*:\s*"([^"]+)"', out)
    choice = m.group(1).strip() if m else "none"
    valid = {ref for ref, _ in cands}
    return (choice if choice in valid else "none"), ms, out


VISION_PROMPT = """The screenshot shows numbered boxes drawn around interactive regions.
Target the user wants to click: {intent}
Boxes (id: center x,y): {boxes}
Pick the ONE box whose click performs the target. If none does, answer none.
Reply with JSON only: {{"id": <number or "none">}}"""


def vision_judge(intent, image_path, elements, timeout=40, url=None, model=None, headers=None):
    """Multimodal fallback for icon-only / text-less controls: Qwen looks at OmniParser's numbered boxes."""
    import base64
    img = base64.b64encode(open(image_path, "rb").read()).decode()
    mime = "image/png" if image_path.endswith(".png") else "image/jpeg"
    boxes = "; ".join(f"{e['id']}: {e['center'][0]:.0f},{e['center'][1]:.0f}" for e in elements)
    target_model = resolve_model(model)
    target_url = resolve_endpoint(url)
    hdrs = make_headers(headers)
    body = chat_body(target_url, target_model, 32, [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img}"}},
        {"type": "text", "text": VISION_PROMPT.format(intent=intent, boxes=boxes)}]}])
    t = time.perf_counter()
    out = chat(target_url, hdrs, body, timeout)
    ms = (time.perf_counter() - t) * 1000
    m = re.search(r'"id"\s*:\s*"?(\w+)"?', out)
    ids = {e["id"]: e for e in elements}
    if m and m.group(1).isdigit() and int(m.group(1)) in ids:
        return ids[int(m.group(1))], ms, out
    return None, ms, out
