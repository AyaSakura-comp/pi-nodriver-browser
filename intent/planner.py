"""System-1 autopilot planner: a tiny-context, no-thinking LLM call that picks the NEXT browser step."""
import json, re, time, urllib.request
from judge import JUDGE_URL, JUDGE_MODEL, resolve_endpoint, resolve_model, make_headers

PROMPT = """You drive a web browser one step at a time toward the user's goal. Decide ONLY the next step.
Goal: {goal}
Steps done so far:
{history}
Current page: {title} | {url}
Visible text (start): {excerpt}
Elements on the page (visible names; "(no text)" = icon/switch/custom control):
{elements}

Rules:
- click/fill/select: "target" = copy the element name from the list when possible, plus distinguishing words.
- fill: put the text in "value"; set "submit": true to press Enter (e.g. a search box).
- select: dropdown target + option text in "value".
- done: the current page already shows what the goal asks for (or the goal's action is complete).
- fail: the goal cannot be reached from here (explain in reason).
- Never click a different control as a substitute for the one the goal needs.
- Never buy, checkout, pay, delete or log out unless the goal explicitly says so.
Reply with JSON only: {{"action":"click|fill|select|done|fail","target":"","value":"","submit":false,"reason":"<=12 words"}}"""


def next_step(goal, history, state, elements, timeout=30, url=None, model=None, headers=None):
    hist = "\n".join(f"{i+1}. {h}" for i, h in enumerate(history)) or "(none yet)"
    els = "\n".join(f"- {e}" for e in elements) or "(none)"
    target_model = resolve_model(model)
    target_url = resolve_endpoint(url)
    hdrs = make_headers(headers)
    body = {"model": target_model, "temperature": 0, "max_tokens": 120,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": PROMPT.format(
                goal=goal, history=hist, title=state.get("title", ""), url=state.get("url", ""),
                excerpt=state.get("page_excerpt", "")[:400], elements=els)}]}
    t = time.perf_counter()
    req = urllib.request.Request(target_url, json.dumps(body).encode(), hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)["choices"][0]["message"]["content"]
    ms = (time.perf_counter() - t) * 1000
    m = re.search(r"\{.*\}", out, re.S)
    try:
        step = json.loads(m.group(0)) if m else {}
    except Exception:
        step = {}
    if step.get("action") not in ("click", "fill", "select", "done", "fail"):
        step = {"action": "fail", "reason": f"unparseable planner output: {out[:80]}"}
    return step, ms


FULL_PROMPT = """You drive a web browser toward the user's goal. The compact view was not enough, so you now see
the FULL interactive element list (with @refs) and a screenshot of the current viewport. Decide ONLY the next step.
Goal: {goal}
Steps done so far:
{history}
Current page: {title} | {url}
Full element list (snapshot -i):
{snapshot}
The image is the current screen with NUMBERED BOXES around interactive regions: {boxes}

Rules:
- For elements with visible text, give its exact "ref" (e.g. "@e3"). For icon-only / text-less controls
  (listed as a bare "<div>" or "<button>" without text), refs cannot be told apart: look at the image and
  give the number of its box in "box" instead. Always describe the element in "target".
- fill/select: put the text/option in "value"; "submit": true presses Enter after fill.
- done: the page already shows what the goal needs; fail: impossible from here.
- If no element performs the goal, answer fail. Never click a different control as a substitute
  (a bell is not a cart, a product card is not an add-to-cart button).
- Only use refs that appear in the list above.
- Never buy, checkout, pay, delete or log out unless the goal explicitly says so.
Reply with JSON only: {{"action":"click|fill|select|done|fail","ref":"@eN or empty","box":null,"target":"","value":"","submit":false,"reason":"<=12 words"}}"""


def next_step_full(goal, history, state, snapshot, screenshot_path=None, boxes=None, timeout=60, url=None, model=None, headers=None):
    """Escalation: the planner sees the whole `snapshot -i` plus a screenshot (Qwen is multimodal)."""
    import base64
    hist = "\n".join(f"{i+1}. {h}" for i, h in enumerate(history)) or "(none yet)"
    snap = "\n".join(l for l in snapshot.splitlines() if l.startswith("@e"))[:12000]
    content = []
    if screenshot_path:
        try:
            img = base64.b64encode(open(screenshot_path, "rb").read()).decode()
            mime = "image/png" if screenshot_path.endswith(".png") else "image/jpeg"
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img}"}})
        except OSError:
            pass
    content.append({"type": "text", "text": FULL_PROMPT.format(goal=goal, history=hist, title=state.get("title", ""),
                                                              url=state.get("url", ""), snapshot=snap or "(no interactive elements)",
                                                              boxes="; ".join(f"{b['id']}: center {b['center'][0]:.0f},{b['center'][1]:.0f}" for b in (boxes or [])) or "(none)")})
    target_model = resolve_model(model)
    target_url = resolve_endpoint(url)
    hdrs = make_headers(headers)
    body = {"model": target_model, "temperature": 0, "max_tokens": 150,
            "chat_template_kwargs": {"enable_thinking": False}, "messages": [{"role": "user", "content": content}]}
    t = time.perf_counter()
    req = urllib.request.Request(target_url, json.dumps(body).encode(), hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)["choices"][0]["message"]["content"]
    ms = (time.perf_counter() - t) * 1000
    m = re.search(r"\{.*\}", out, re.S)
    try:
        step = json.loads(m.group(0)) if m else {}
    except Exception:
        step = {}
    if step.get("action") not in ("click", "fill", "select", "done", "fail"):
        step = {"action": "fail", "reason": f"unparseable planner output: {out[:80]}"}
    return step, ms


VERIFY_PROMPT = """Goal: {goal}
Steps done: {history}
Current page: {title}
Visible page text: {text}
Does the visible page state show the goal has actually been achieved? Answer strictly from the page text.
Judge the page's MAIN item/state (the page title names it). Related products, recommendations, "優惠組"/bundle
offers or other listings elsewhere on the page do not change what the main item is.
Reply JSON only: {{"achieved": true|false, "evidence": "<=15 words quoted or paraphrased from the page"}}"""


def verify_done(goal, history, state, text, timeout=30, url=None, model=None, headers=None):
    target_model = resolve_model(model)
    target_url = resolve_endpoint(url)
    hdrs = make_headers(headers)
    body = {"model": target_model, "temperature": 0, "max_tokens": 80,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": VERIFY_PROMPT.format(
                goal=goal, history="; ".join(history) or "(none)", title=state.get("title", ""), text=text[:1500])}]}
    t = time.perf_counter()
    req = urllib.request.Request(target_url, json.dumps(body).encode(), hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)["choices"][0]["message"]["content"]
    ms = (time.perf_counter() - t) * 1000
    ok = bool(re.search(r'"achieved"\s*:\s*true', out))
    m = re.search(r'"evidence"\s*:\s*"([^"]*)"', out)
    return ok, (m.group(1) if m else out[:80]), ms
