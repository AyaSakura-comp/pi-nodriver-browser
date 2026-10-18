"""System-1 autopilot planner: a tiny-context, no-thinking LLM call that picks the NEXT browser step."""
import json, re, time
from judge import resolve_endpoint, resolve_model, make_headers, chat_body, chat

def next_step(goal, history, state, elements, timeout=30, url=None, model=None, headers=None):
    hist = "\n".join(f"{i+1}. {h}" for i, h in enumerate(history)) or "(none yet)"
    candidates = _candidates(elements)
    els = "\n".join(f"{i}: {name}" + (" OPTIONS " + "; ".join(f"{j}: {v}" for j, v in enumerate(options, 1)) if options else "") for i, (name, options) in enumerate(candidates, 1)) or "(none)"
    target_model = resolve_model(model)
    target_url = resolve_endpoint(url)
    hdrs = make_headers(headers)
    body = chat_body(target_url, target_model, 32, [{"role": "user", "content": COMPACT_PROMPT.format(
        goal=goal, history=hist, title=state.get("title", ""), url=state.get("url", ""),
        excerpt=state.get("page_excerpt", "")[:900], elements=els)}])
    t = time.perf_counter()
    out = chat(target_url, hdrs, body, timeout)
    ms = (time.perf_counter() - t) * 1000
    return _compact_step(out, candidates), ms




COMPACT_PROMPT = """Choose ONE next browser action toward the goal, using ONLY the current candidates.
Reply ONLY one command, no JSON object, explanation, equals sign or extra lines.
Syntax examples (replace numeric IDs with actual candidate IDs):
C 2
S 1 2
S 1 "exact option text"
F 3 "text" 1
R down
D
X
C clicks.
F fills an input or search box (e.g. F 1 "query" 1); final 1 means press Enter to submit, final 0 means no Enter.
S selects a dropdown option; S is ONLY for dropdown menus, NEVER for searching or typing text.
R scrolls down/up/top/bottom when goal content is not visible.
D means done ONLY if current page text already shows goal achieved.
X means cannot reach goal.
Quoted text must be a JSON string. IDs start at 1. Never guess IDs or substitute controls.
Never buy, checkout, pay, delete or log out unless the goal explicitly requests it.

Goal: {goal}
History: {history}
Page: {title} | {url}
Relevant page text: {excerpt}
Candidates (call-local IDs):
{elements}
"""


def _candidates(elements):
    result = []
    for element in elements:
        name, sep, raw = str(element).partition(" | options: ")
        # Only consume explicitly supplied metadata; never infer options from other text.
        options = raw.split("; ") if sep else []
        result.append((name, options))
    return result


def _compact_step(out, candidates):
    fail = {"action": "fail", "reason": f"invalid compact planner command or unavailable candidate: {out[:80]!r}"}
    text = out.strip()
    if text == "D":
        return {"action": "done"}
    if text == "X":
        return {"action": "fail", "reason": "planner cannot reach the goal from current candidates"}
    m = re.fullmatch(r"R (down|up|top|bottom)", text)
    if m:
        return {"action": "scroll", "value": m[1]}
    m = re.fullmatch(r"([CSFLB]) ([1-9][0-9]*)(?: (.+))?", text)
    if not m or int(m[2]) > len(candidates):
        # Fallback for backward compatibility with JSON dialect or legacy tests
        if text.startswith("{") and text.endswith("}"):
            try:
                step = json.loads(text)
                if step.get("action") in ("click", "fill", "select", "scroll", "done", "fail"):
                    return step
            except Exception:
                pass
        return fail
    action, index, tail = m[1], int(m[2]) - 1, m[3]
    name, options = candidates[index]
    if action in ("C", "L", "B"):
        return {"action": "click", "target": name} if tail is None else fail
    # Support fill action (or model mistaking S for search when followed by submit flag)
    if (action == "F" or action == "S") and tail and (tail.endswith(" 0") or tail.endswith(" 1")):
        try:
            value, end = json.JSONDecoder().raw_decode(tail)
            submit = tail[end:]
            if isinstance(value, str) and submit in (" 0", " 1"):
                return {"action": "fill", "target": name, "value": value, "submit": submit == " 1"}
        except ValueError:
            pass
    if action == "S":
        if options:
            if tail and re.fullmatch(r"[1-9][0-9]*", tail):
                if int(tail) > len(options):
                    return fail
                value = options[int(tail)-1]
            else:
                # Safe compatibility: quoted text must exactly identify ONE listed option.
                try:
                    value = json.loads(tail or "")
                except (ValueError, TypeError):
                    return fail
                if not isinstance(value, str) or options.count(value) != 1:
                    return fail
        else:
            try:
                value = json.loads(tail or "")
            except (ValueError, TypeError):
                return fail
            if not isinstance(value, str) or not value:
                return fail
        return {"action": "select", "target": name, "value": value}
    if not tail:
        return fail
    try:
        value, end = json.JSONDecoder().raw_decode(tail)
    except ValueError:
        return fail
    submit = tail[end:]
    if not isinstance(value, str) or submit not in (" 0", " 1"):
        return fail
    return {"action": "fill", "target": name, "value": value, "submit": submit == " 1"}


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
    body = chat_body(target_url, target_model, 150, [{"role": "user", "content": content}])
    t = time.perf_counter()
    out = chat(target_url, hdrs, body, timeout)
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
    body = chat_body(target_url, target_model, 80, [{"role": "user", "content": VERIFY_PROMPT.format(
        goal=goal, history="; ".join(history) or "(none)", title=state.get("title", ""), text=text[:1500])}])
    t = time.perf_counter()
    out = chat(target_url, hdrs, body, timeout)
    ms = (time.perf_counter() - t) * 1000
    ok = bool(re.search(r'"achieved"\s*:\s*true', out))
    m = re.search(r'"evidence"\s*:\s*"([^"]*)"', out)
    return ok, (m.group(1) if m else out[:80]), ms
