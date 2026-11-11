// Page snapshot for the browser agent. Runs in an isolated world: page scripts
// cannot see window.__pi, and page monkey-patching cannot change these builtins.
// Walks the whole DOM in document order (not only what is on screen: the agent reads
// a long page in parts, without scrolling) and emits visible text lines with
// interactive elements inline as "[eN] role "name"", so prices stay next to their
// product link.
// A ref belongs to its element for as long as the document lives: the same element
// keeps its ref in every snapshot, so an unchanged line is the same text again (the
// agent shows only new lines) and several actions can follow one snapshot. Numbers
// start after `base`, the highest ref this browser session has used, so a ref never
// names two elements in one task.
((base) => {
  const MAX_CHARS = 120000;
  const MAX_ELEMENTS = 3000;
  const TIME_BUDGET_MS = 2500;
  const started = performance.now();
  const state = window.__pi || (window.__pi = {});
  if (!state.doc) {
    state.doc = Math.random().toString(36).slice(2, 10);
    state.ids = new WeakMap();
    state.refs = new Map();
    state.next = 0;
  }
  state.next = Math.max(state.next, base || 0);
  const refs = state.refs;
  for (const [ref, el] of refs) if (!el.isConnected) refs.delete(ref);
  const links = {};
  const vh = window.innerHeight;
  let lines = [];
  const body = lines;
  const menu = [];
  let lastName = '';
  let chars = 0;
  let count = 0;
  let truncated = false;
  let buffer = [];
  const styles = new WeakMap();
  const recentLinks = [];

  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'TITLE',
    'svg', 'CANVAS', 'OBJECT', 'EMBED', 'PATH', 'BR', 'HR', 'WBR', 'SOURCE', 'TRACK', 'MAP', 'AREA', 'DIALOG']);
  const INLINE = new Set(['inline', 'inline-block', 'inline-flex', 'inline-grid', 'contents', 'ruby', 'inline-table']);
  const ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'combobox', 'switch', 'searchbox', 'textbox', 'slider', 'spinbutton', 'treeitem']);
  const NATIVE = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY']);
  const NESTED = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=tab],[role=checkbox],[role=menuitem]';

  const style = (el) => {
    let s = styles.get(el);
    if (!s) { s = getComputedStyle(el); styles.set(el, s); }
    return s;
  };
  const clean = (s, n) => {
    s = String(s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  };
  // name: the name of the element this line lists. The text right after an element often
  // says its name again (a checkbox and its label text, a card's image alt and its link).
  const push = (line, name) => {
    if (truncated) return;
    if (!line || line === lines[lines.length - 1]) return;
    if (name === undefined && lastName && (line === lastName || line === `[img ${lastName}]`)) return;
    if (name && lines[lines.length - 1] === `[img ${name}]`) chars -= lines.pop().length + 1;
    if (chars + line.length > MAX_CHARS || performance.now() - started > TIME_BUDGET_MS) {
      truncated = true;
      return;
    }
    lines.push(line);
    chars += line.length + 1;
    lastName = name || '';
  };
  const flush = () => {
    if (buffer.length) {
      const said = lastName ? buffer.filter((text) => text !== lastName && text !== `[img ${lastName}]`) : buffer;
      if (said.length) push(clean(said.join(' '), 400));
    }
    buffer = [];
  };

  const roleOf = (el, s) => {
    const aria = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
    if (ROLES.has(aria)) return aria;
    const tag = el.tagName;
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'SELECT') return 'select';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'hidden') return null;
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox' || type === 'radio') return type;
      if (type === 'range') return 'slider';
      if (type === 'file') return 'file';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    if (el.isContentEditable && el.hasAttribute('contenteditable')) return 'textbox';
    if (el.hasAttribute('onclick')) return 'clickable';
    if (s.cursor === 'pointer') {
      const parent = el.parentElement;
      if (!parent || style(parent).cursor !== 'pointer') return 'clickable';
    }
    return null;
  };

  const HINTS = [['search', /search|magnif|搜尋/], ['cart', /cart|basket|購物車/], ['close', /close|dismiss|關閉/],
    ['menu', /menu|hamburger|nav-toggle/], ['login', /login|signin|sign-in|登入/], ['account', /account|user|member|會員/],
    ['next', /next|right|下一/], ['previous', /prev|left|上一/], ['favorite', /fav|wish|heart|like|收藏/],
    ['share', /share|分享/], ['delete', /delete|trash|remove|刪除/], ['filter', /filter|篩選/], ['sort', /sort|排序/],
    ['play', /play/], ['more', /more|ellipsis|kebab|更多/], ['home', /home|首頁/]];
  const iconHint = (el) => {
    let bag = `${el.id} ${typeof el.className === 'string' ? el.className : ''} ${el.getAttribute('data-regression') || ''} ${el.getAttribute('data-testid') || ''} ${el.getAttribute('name') || ''}`;
    for (const child of el.querySelectorAll('*')) {
      bag += ' ' + (typeof child.className === 'string' ? child.className : (child.getAttribute('class') || ''));
      if (bag.length > 600) break;
    }
    bag = bag.toLowerCase();
    for (const [hint, re] of HINTS) if (re.test(bag)) return hint;
    return '';
  };
  // Returns [name, placeholder]: a placeholder is only a hint, never the element's name.
  const nameOf = (el) => {
    let s = el.getAttribute('aria-label') || '';
    if (!s) {
      const ids = el.getAttribute('aria-labelledby');
      if (ids) s = ids.split(/\s+/).map((id) => el.ownerDocument.getElementById(id)?.innerText || '').join(' ');
    }
    const tag = el.tagName;
    let placeholder = '';
    if (!s && el.labels && el.labels.length) s = Array.from(el.labels).map((l) => l.innerText).join(' ');
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      if (!s && ['button', 'submit', 'reset'].includes((el.type || '').toLowerCase())) s = el.value;
      if (!s) s = el.title || el.alt || '';
      // A box whose text is a sibling, not a <label> (104: <span><input></span><span>台北市</span>):
      // name it after the nearest enclosing row with short text.
      if (!s && /^(checkbox|radio)$/i.test(el.type || '')) {
        for (let up = el.parentElement, depth = 0; up && depth < 3 && !s; up = up.parentElement, depth++) {
          const text = clean(up.innerText, 200);
          if (text && text.length <= 40) s = text;
        }
      }
      placeholder = el.placeholder || '';
    } else if (!s && tag !== 'SELECT') {
      s = el.innerText || '';
    }
    if (!s) s = el.querySelector('img[alt]')?.alt || el.title || el.querySelector('svg title')?.textContent || '';
    if (!s && !placeholder) {
      const hint = iconHint(el);
      if (hint) s = `(icon: ${hint})`;
    }
    return [clean(s, 120), clean(placeholder, 60)];
  };

  const describe = (el, role) => {
    let ref = state.ids.get(el);
    if (!ref) {
      ref = 'e' + (++state.next);
      state.ids.set(el, ref);
    }
    refs.set(ref, el);
    count++;
    const [name, placeholder] = nameOf(el);
    let line = `[${ref}] ${role}`;
    if (name || !placeholder) line += ` "${name}"`;
    if (placeholder) line += ` placeholder="${placeholder}"`;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      const type = (el.type || 'text').toLowerCase();
      if (!['text', 'search', 'checkbox', 'radio', 'button', 'submit', 'textarea'].includes(type)) line += ` type=${type}`;
      if (type === 'checkbox' || type === 'radio') line += el.checked ? ' checked' : ' unchecked';
      else if (el.value && type !== 'password' && !['button', 'submit', 'reset'].includes(type)) line += ` value="${clean(el.value, 80)}"`;
      else if (el.value && type === 'password') line += ' value=(filled)';
    } else if (tag === 'SELECT') {
      const options = Array.from(el.options);
      const selected = options.find((o) => o.selected);
      line += ` selected="${clean(selected ? selected.text : '', 60)}" options: ` +
        options.slice(0, 12).map((o) => clean(o.text, 30)).join(' | ') + (options.length > 12 ? ` | …(${options.length} total)` : '');
    } else {
      const checked = el.getAttribute('aria-checked') || el.getAttribute('aria-selected');
      if (checked === 'true') line += ' selected';
      const expanded = el.getAttribute('aria-expanded');
      if (expanded) line += expanded === 'true' ? ' expanded' : ' collapsed';
      if (el.getAttribute('aria-current') && el.getAttribute('aria-current') !== 'false') line += ' current';
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') line += ' disabled';
    // Where a link goes is returned beside the text, not in it: addresses were 30-56% of a
    // snapshot's characters and, cut to fit, could not be used in an answer anyway. An answer
    // names a link by its ref and the agent puts the address in.
    if (role === 'link' && /^https?:/.test(el.href || '')) links[ref] = el.href;
    return [line, name];
  };

  // clip: the visible box left by ancestors with overflow hidden/clip (null = none).
  // Content scrolled inside auto/scroll panes is kept, like content below the fold.
  const intersects = (r, c) => r.right > c.left - 1 && r.left < c.right + 1 && r.bottom > c.top - 1 && r.top < c.bottom + 1;
  // Open dialogs are listed first: they cover the page, but often sit at the end of the
  // DOM (past the character budget) or inside a wrapper below the fold that the clip test
  // drops while the page's scroll is locked (104's region picker).
  const vw = window.innerWidth;
  const dialogs = [];
  for (const el of document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog],[aria-modal=true]')) {
    if (dialogs.some((d) => d.contains(el))) continue;
    if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 40 || r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) continue;
    dialogs.push(el);
  }
  const listed = new Set(dialogs);

  const FIELDS = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]),select,textarea,[contenteditable=true],[role=searchbox],[role=combobox],[role=textbox]';
  const chromeOf = (el) => {
    const tag = el.tagName;
    const role = el.getAttribute('role') || '';
    if (tag === 'NAV' || role === 'navigation') return 'nav';
    if (tag === 'HEADER' || tag === 'FOOTER' || tag === 'ASIDE' || role === 'banner' || role === 'contentinfo' || role === 'complementary') {
      // An article's own header (title, byline) or footer is content.
      return el.parentElement && el.parentElement.closest('article,main,[role=main]') ? '' : 'side';
    }
    return '';
  };
  let inChrome = 0;
  let descend;
  const walk = (node, clip) => {
    if (truncated) return;
    if (listed.has(node)) return;
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.nodeValue;
      if (text && text.trim()) buffer.push(text.trim());
      return;
    }
    if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
      for (const child of node.childNodes) walk(child, clip);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node;
    if (SKIP.has(el.tagName)) {
      if (el.tagName === 'DIALOG' && el.open) { flush(); push('[dialog]'); for (const c of el.childNodes) walk(c, null); flush(); }
      return;
    }
    const s = style(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse') return;
    if (el.getAttribute('aria-hidden') === 'true' && !el.querySelector(NESTED)) return;
    const block = !INLINE.has(s.display);
    const rect = el.getBoundingClientRect();
    const sized = rect.width > 0 || rect.height > 0;
    if (s.position === 'fixed') clip = null;
    if (clip && sized && !intersects(rect, clip)) return;
    // Site chrome is listed after the page's content: it stood before the content on every
    // page of a site (591: 2,600 characters before the first listing). Chrome is a nav, or a
    // page-level header, footer or aside, or a block of more than 12 links inside one of
    // those; a block with a field to type in (the search box) stays where it is.
    if (lines === body && el.firstElementChild) {
      const landmark = chromeOf(el);
      if (landmark) inChrome++;
      const links = landmark || inChrome ? el.querySelectorAll('a[href]').length : 0;
      if (links > (landmark === 'nav' || !landmark ? 12 : 4) && !el.querySelector(FIELDS)) {
        flush();
        lines = menu;
        lastName = '';
        for (const child of el.childNodes) walk(child, clip);
        flush();
        lines = body;
        lastName = '';
        if (landmark) inChrome--;
        return;
      }
      if (landmark) {
        const rest = descend(el, s, rect, clip, block);
        inChrome--;
        return rest;
      }
    }
    return descend(el, s, rect, clip, block);
  };

  // The rest of walk(): an element that is not skipped and not moved to the menu.
  descend = (el, s, rect, clip, block) => {
    const sized = rect.width > 0 || rect.height > 0;
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      flush();
      let doc = null;
      try { doc = el.contentDocument; } catch (e) { doc = null; }
      if (doc && doc.body) walk(doc.body, null);
      else if (rect.width > 40 && rect.height > 40) push(`[iframe ${clean(el.title || el.src, 80)} (content not readable)]`);
      flush();
      return;
    }
    if (el.tagName === 'IMG') {
      const alt = clean(el.alt, 80);
      if (alt && rect.width > 24 && rect.height > 24) buffer.push(`[img ${alt}]`);
      return;
    }
    // A drawn value (a star rating, an icon that means something): its label is its text.
    // Google Maps writes "4.3 stars 2,548 reviews" there and hides the digits it draws.
    if (el.getAttribute('role') === 'img') {
      const label = clean(el.getAttribute('aria-label'), 80);
      if (label) { buffer.push(label); return; }
    }
    let role = count < MAX_ELEMENTS ? roleOf(el, s) : null;
    if (role && !NATIVE.has(el.tagName) && el.querySelector('input:not([type=hidden]),select,textarea')) role = null;
    // A wrapper (pointer-cursor box, or a div with a widget role) around real links/buttons:
    // list its text and inner controls. PChome marks the whole product box (title, price,
    // delivery, cart buttons) role=button; as one element it was cut to a 120-char name.
    if (role && !NATIVE.has(el.tagName) && el.querySelector(NESTED)) role = null;
    // A label whose own control is rendered: the control is listed (named by this label).
    if (role === 'clickable' && el.tagName === 'LABEL' && el.control) {
      const cs = style(el.control);
      if (cs.display !== 'none' && cs.visibility === 'visible') role = null;
    }
    if (role && (sized || role === 'checkbox' || role === 'radio' || role === 'file')) {
      flush();
      if (role === 'link' && el.href && !el.querySelector(NESTED)) {
        // Cards repeat one product link (image, name, price): list it once.
        const repeat = recentLinks.find((item) => item.href === el.href);
        if (repeat) {
          const name = nameOf(el)[0];
          if (name && name !== repeat.name) push(name);
          return;
        }
        recentLinks.push({href: el.href, name: nameOf(el)[0]});
        if (recentLinks.length > 4) recentLinks.shift();
      }
      push(...describe(el, role));
      // A card-sized link or clickable container can hold real buttons: keep walking it.
      if ((role === 'link' || role === 'clickable') && el.querySelector(NESTED)) {
        for (const child of el.childNodes) walk(child, clip);
        flush();
      }
      return;
    }
    let inner = clip;
    if (sized && /(hidden|clip)/.test(s.overflowX + s.overflowY)) {
      const box = {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom};
      if (!/(hidden|clip)/.test(s.overflowX)) { box.left = -1e9; box.right = 1e9; }
      if (!/(hidden|clip)/.test(s.overflowY)) { box.top = -1e9; box.bottom = 1e9; }
      inner = clip ? {left: Math.max(clip.left, box.left), top: Math.max(clip.top, box.top),
                      right: Math.min(clip.right, box.right), bottom: Math.min(clip.bottom, box.bottom)} : box;
    }
    if (block) flush();
    if (el.shadowRoot) {
      walk(el.shadowRoot, inner);
    } else if (el.tagName === 'SLOT') {
      for (const child of el.assignedNodes({ flatten: true })) walk(child, inner);
    } else {
      for (const child of el.childNodes) walk(child, inner);
    }
    if (block) flush();
  };

  for (const dialog of dialogs) {
    flush();
    push('[dialog]');
    for (const child of dialog.childNodes) walk(child, null);
    flush();
    push('[/dialog]');
  }
  if (document.body) walk(document.body, null);
  flush();
  if (menu.length) {
    lastName = '';
    push('Site menu:');
    for (const line of menu) lines.push(line);
  }
  const scroller = document.scrollingElement || document.documentElement;
  return {
    url: location.href,
    title: document.title,
    text: lines.join('\n'),
    doc: state.doc,
    lastRef: state.next,
    links,
    elements: count,
    truncated,
    scrollY: Math.round(window.scrollY),
    scrollHeight: Math.round(scroller ? scroller.scrollHeight : 0),
    viewportHeight: vh,
  };
})
