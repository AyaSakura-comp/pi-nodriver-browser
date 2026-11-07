// Image candidates of a crawled page, best first (at most 8): Open Graph / Twitter card
// images, JSON-LD images, <img> (lazy-loading attributes too) and video posters. Logos,
// icons, tracking pixels and images under 128 px are dropped.
(() => {
  const candidates = new Map();
  const short = (value, limit = 240) => String(value || '').slice(0, limit * 4).replace(/\s+/g, ' ').trim().slice(0, limit);
  const number = (value) => {
    if (value && typeof value === 'object') value = value.value;
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 && parsed <= 100000 ? Math.round(parsed) : 0;
  };
  const resolveUrl = (value) => {
    const raw = String(value || '');
    if (raw.length > 4096 || !raw.trim() || /^(?:data|blob|javascript):/i.test(raw.trim())) return '';
    try {
      const url = new URL(raw.trim(), document.baseURI);
      url.hash = '';
      // "[[" or "]]" would read as an [[image: …]] marker in the crawl text.
      return /^https?:$/.test(url.protocol) && !url.username && !url.password && url.port !== '0' &&
        url.href.length <= 4096 && !/\[\[|\]\]/.test(url.href) ? url.href : '';
    } catch (_) { return ''; }
  };
  const UTILITY = /(?:^|[\s_\-/.])(logo|icon|avatar|sprite|emoji|tracking|pixel|spinner|placeholder|badge|favicon|blank|transparent|spacer)(?:[\s_\-/.]|$)/i;
  const add = (c) => {
    const url = resolveUrl(c.url);
    if (!url) return false;
    const item = {
      url, source: short(c.source, 80) || 'unknown',
      role: ['representative', 'content', 'gallery', 'thumbnail'].includes(c.role) ? c.role : 'content',
      width: number(c.width), height: number(c.height), alt: short(c.alt), caption: short(c.caption),
      score: number(c.score) || 1,
    };
    if (UTILITY.test(`${url} ${item.alt} ${item.caption}`) ||
        (item.width && item.height && Math.max(item.width, item.height) < 128)) return false;
    const old = candidates.get(url);
    if (old) {  // the same image found twice: keep the better entry, fill its gaps from the other
      const [best, other] = item.score >= old.score ? [item, old] : [old, item];
      candidates.set(url, {...best, width: best.width || other.width, height: best.height || other.height,
        alt: best.alt || other.alt, caption: best.caption || other.caption, score: Math.max(best.score, other.score)});
      return true;
    }
    if (candidates.size >= 64) {  // bounded pool: a new entry must beat the weakest
      let weakest = null;
      for (const entry of candidates) if (!weakest || entry[1].score < weakest[1].score) weakest = entry;
      if (item.score <= weakest[1].score) return false;
      candidates.delete(weakest[0]);
    }
    candidates.set(url, item);
    return true;
  };

  // Open Graph: og:image starts a block that og:image:* fields describe.
  const metas = [...document.getElementsByTagName('meta')].slice(0, 200);
  const metaKey = (meta, first, second) => short(meta.getAttribute(first) || meta.getAttribute(second), 100).toLowerCase();
  const ogBlocks = [];
  let og = null;
  for (const meta of metas) {
    const key = metaKey(meta, 'property', 'name');
    const content = meta.getAttribute('content') || '';
    if (key === 'og:image' ||
        (key === 'og:image:url' && !(og && resolveUrl(og.url) && resolveUrl(og.url) === resolveUrl(content)))) {
      og = {url: content, secureUrl: '', width: 0, height: 0, alt: ''};
      ogBlocks.push(og);
    } else if (key === 'og:image:url') og.url = content;
    else if (og && key === 'og:image:secure_url') og.secureUrl = content;
    else if (og && key === 'og:image:width') og.width = content;
    else if (og && key === 'og:image:height') og.height = content;
    else if (og && key === 'og:image:alt') og.alt = content;
    else if (og && key.startsWith('og:') && !key.startsWith('og:image:')) og = null;
  }
  let acceptedOg = 0;
  ogBlocks.forEach((block, index) => {
    const base = {source: 'og:image', role: 'representative', width: block.width, height: block.height,
                  alt: block.alt, score: Math.max(100, 120 - index)};
    if ((block.secureUrl && add({...base, url: block.secureUrl, source: 'og:image:secure_url'})) ||
        add({...base, url: block.url})) acceptedOg++;
  });
  if (!acceptedOg) {
    let alt = '';
    for (const meta of metas) if (metaKey(meta, 'name', 'property') === 'twitter:image:alt') alt = short(meta.getAttribute('content'));
    for (const meta of metas) {
      const key = metaKey(meta, 'name', 'property');
      if (key === 'twitter:image' || key === 'twitter:image:src') {
        add({url: meta.getAttribute('content') || '', source: key, role: 'representative', alt, score: 110});
      }
    }
  }

  // JSON-LD: image-like keys anywhere in the structured data (bounded walk, logos skipped).
  const IMAGE_KEYS = new Set(['image', 'images', 'contenturl', 'thumbnail', 'thumbnailurl',
    'primaryimageofpage', 'photo', 'photos', 'screenshot']);
  let jsonNodes = 0;
  const fromJson = (value, key, depth) => {
    if (depth > 8 || jsonNodes++ > 5000 || value == null) return;
    const name = String(key || '').slice(0, 80);
    const normalized = name.replace(/[-_]/g, '').toLowerCase();
    if (IMAGE_KEYS.has(normalized)) {
      const thumbnail = normalized.includes('thumbnail');
      for (const item of (Array.isArray(value) ? value : [value]).slice(0, 20)) {
        if (typeof item === 'string') {
          add({url: item, source: `jsonld:${name}`, role: 'representative', score: 115});
        } else if (item && typeof item === 'object') {
          add({url: item.contentUrl || item.thumbnailUrl || item.url || item['@id'], source: `jsonld:${name}`,
               role: thumbnail ? 'thumbnail' : 'representative', width: item.width, height: item.height,
               alt: item.caption || item.name || item.description, caption: item.caption, score: thumbnail ? 95 : 116});
        }
      }
    }
    if (Array.isArray(value)) {
      value.slice(0, 100).forEach((child) => fromJson(child, key, depth + 1));
    } else if (typeof value === 'object') {
      let count = 0;
      for (const childKey in value) {
        if (!Object.prototype.hasOwnProperty.call(value, childKey)) continue;
        if (count++ >= 100) break;
        if (String(childKey).slice(0, 80).toLowerCase() !== 'logo') fromJson(value[childKey], childKey, depth + 1);
      }
    }
  };
  const scripts = document.getElementsByTagName('script');
  let jsonBytes = 0;
  for (let index = 0, jsonScripts = 0; index < Math.min(scripts.length, 1000) && jsonScripts < 20; index++) {
    const script = scripts[index];
    if (short(script.getAttribute('type'), 80).toLowerCase() !== 'application/ld+json') continue;
    jsonScripts++;
    // Plain text only, at most 1 MB of JSON-LD per page.
    const nodes = [...script.childNodes];
    const size = nodes.reduce((sum, node) => sum + (node.length || 0), 0);
    if (nodes.length > 100 || nodes.some((node) => node.nodeType !== 3 && node.nodeType !== 4) || !size ||
        jsonBytes + size > 1000000) continue;
    const payload = script.textContent || '';
    jsonBytes += payload.length;
    try { fromJson(JSON.parse(payload), '', 0); } catch (_) {}
  }

  // <img>: the loaded source, else a lazy-loading attribute; figures and main content rank higher.
  const srcsetUrl = (value) => {
    let best = null;
    for (const part of String(value || '').slice(0, 16384).split(',', 50)) {
      const match = part.trim().match(/^(\S+)(?:\s+(\d+(?:\.\d+)?)(w|x))?$/);
      if (!match) continue;
      const weight = Number(match[2] || 1) * (match[3] === 'x' ? 10000 : 1);
      if (!best || weight > best.weight) best = {url: match[1], weight};
    }
    return best ? best.url : '';
  };
  const ownText = (element) => {
    let text = '';
    for (const node of [...(element?.childNodes || [])].slice(0, 20)) {
      if (text.length >= 960) break;
      if (node.nodeType === 3 || node.nodeType === 4) text += ` ${node.substringData(0, 960 - text.length)}`;
    }
    return short(text);
  };
  const visible = (element) => {
    try {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0 &&
        rect.width > 0 && rect.height > 0;
    } catch (_) { return false; }
  };
  const ancestor = (element, test) => {
    for (let node = element.parentElement, depth = 0; node && depth < 12; node = node.parentElement, depth++) {
      if (test(node)) return node;
    }
    return null;
  };
  const images = document.images;
  for (let index = 0; index < Math.min(images.length, 1000); index++) {
    const image = images[index];
    const alt = short(image.getAttribute('alt') || image.getAttribute('aria-label') || image.getAttribute('title') || '');
    const choices = [
      ['img.currentSrc', image.currentSrc], ['img.src', image.getAttribute('src')],
      ['img.srcset', srcsetUrl(image.getAttribute('srcset'))], ['img.data-src', image.getAttribute('data-src')],
      ['img.data-srcset', srcsetUrl(image.getAttribute('data-srcset'))],
      ['img.data-original', image.getAttribute('data-original')], ['img.data-lazy-src', image.getAttribute('data-lazy-src')],
    ];
    const usable = ([, value]) => resolveUrl(value) && !UTILITY.test(
      `${short(value, 4096)} ${short(alt)} ${short(image.id, 120)} ${short(image.className, 200)} ` +
      `${short(image.parentElement?.id, 120)} ${short(image.parentElement?.className, 200)}`);
    // A loaded source next to a lazy-loading one counts only when it is a real image (not a placeholder).
    const lazy = choices.slice(3).some(usable);
    const real = image.naturalWidth && image.naturalHeight && Math.max(image.naturalWidth, image.naturalHeight) >= 128;
    const selected = choices.find((choice, choiceIndex) => usable(choice) && (choiceIndex > 1 || !lazy || real));
    if (!selected) continue;
    const loaded = selected[0] === 'img.currentSrc' || selected[0] === 'img.src';
    const width = number((loaded && image.naturalWidth) || image.getAttribute('width'));
    const height = number((loaded && image.naturalHeight) || image.getAttribute('height'));
    if (width && height && Math.max(width, height) < 128) continue;
    const figure = ancestor(image, (element) => element.tagName === 'FIGURE');
    const inMain = ancestor(image, (element) => ['MAIN', 'ARTICLE'].includes(element.tagName) ||
      element.getAttribute('role') === 'main');
    add({url: selected[1], source: selected[0], role: figure ? 'gallery' : 'content', width, height, alt,
         caption: ownText(figure?.getElementsByTagName('figcaption')[0]),
         score: 55 + (figure ? 20 : 0) + (inMain ? 10 : 0) + (visible(image) ? 8 : 0) + (width * height >= 480000 ? 5 : 0)});
  }
  const videos = document.getElementsByTagName('video');
  for (let index = 0, posters = 0; index < Math.min(videos.length, 1000) && posters < 100; index++) {
    const video = videos[index];
    if (!video.getAttribute('poster')) continue;
    posters++;
    add({url: video.getAttribute('poster'), source: 'video.poster', role: 'thumbnail',
         width: video.videoWidth || video.getAttribute('width'), height: video.videoHeight || video.getAttribute('height'),
         alt: video.getAttribute('aria-label') || video.getAttribute('title') || '', score: 50});
  }

  const cut = (text) => Array.from(text).slice(0, 200).join('');
  return [...candidates.values()]
    .sort((a, b) => b.score - a.score || b.width * b.height - a.width * a.height)
    .slice(0, 8)
    .map((candidate, index) => ({id: `img-${index + 1}`, ...candidate, alt: cut(candidate.alt), caption: cut(candidate.caption)}));
})()
