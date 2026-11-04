JSON.stringify((() => {
  const MAX_CANDIDATES = 8;
  const MAX_CANDIDATE_POOL = 64;
  const MAX_JSON_LD_BYTES = 1000000;
  const candidates = new Map();
  const short = (value, limit = 240) => String(value || '').slice(0, limit * 4)
    .replace(/\s+/g, ' ').trim().slice(0, limit);
  const number = value => {
    if (value && typeof value === 'object') value = value.value;
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 && parsed <= 100000 ? Math.round(parsed) : 0;
  };
  const resolveUrl = value => {
    const input = String(value || '');
    if (!input || input.length > 4096) return '';
    const raw = input.trim();
    if (!raw || /^(?:data|blob|javascript):/i.test(raw)) return '';
    try {
      const parsed = new URL(raw, document.baseURI);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.port === '0') return '';
      parsed.hash = '';
      return parsed.href.length <= 4096 ? parsed.href : '';
    } catch (_) { return ''; }
  };
  const utilityPattern = /(?:^|[\s_\-/.])(logo|icon|avatar|sprite|emoji|tracking|pixel|spinner|placeholder|badge|favicon|blank|transparent|spacer)(?:[\s_\-/.]|$)/i;
  const add = candidate => {
    const url = resolveUrl(candidate.url);
    if (!url) return false;
    const normalized = {
      url,
      source: short(candidate.source, 80) || 'unknown',
      role: ['representative', 'content', 'gallery', 'thumbnail'].includes(candidate.role)
        ? candidate.role : 'content',
      width: number(candidate.width),
      height: number(candidate.height),
      alt: short(candidate.alt),
      caption: short(candidate.caption),
      score: number(candidate.score) || 1
    };
    const utilityContext = `${normalized.url} ${normalized.alt} ${normalized.caption}`;
    if (
      utilityPattern.test(utilityContext)
      || (normalized.width && normalized.height && Math.max(normalized.width, normalized.height) < 128)
    ) return false;

    const existing = candidates.get(url);
    if (existing) {
      const preferred = normalized.score >= existing.score ? normalized : existing;
      const fallback = preferred === normalized ? existing : normalized;
      candidates.set(url, {
        ...preferred,
        width: preferred.width || fallback.width,
        height: preferred.height || fallback.height,
        alt: preferred.alt || fallback.alt,
        caption: preferred.caption || fallback.caption,
        score: Math.max(preferred.score, fallback.score)
      });
      return true;
    }
    if (candidates.size >= MAX_CANDIDATE_POOL) {
      let weakestKey = '';
      let weakestScore = Infinity;
      for (const [key, value] of candidates) {
        if (value.score < weakestScore) {
          weakestKey = key;
          weakestScore = value.score;
        }
      }
      if (normalized.score <= weakestScore) return false;
      candidates.delete(weakestKey);
    }
    candidates.set(url, normalized);
    return true;
  };

  const ogBlocks = [];
  let currentOg = null;
  const finishOg = () => {
    if (currentOg) ogBlocks.push(currentOg);
    currentOg = null;
  };
  const metas = document.getElementsByTagName('meta');
  for (let index = 0; index < Math.min(metas.length, 200); index++) {
    const meta = metas[index];
    const key = short(meta.getAttribute('property') || meta.getAttribute('name'), 100).toLowerCase();
    const content = meta.getAttribute('content') || '';
    if (key === 'og:image') {
      finishOg();
      currentOg = { url: content, secureUrl: '', width: 0, height: 0, alt: '' };
    } else if (key === 'og:image:url') {
      const currentUrl = currentOg ? resolveUrl(currentOg.url) : '';
      const aliasUrl = resolveUrl(content);
      if (currentOg && currentUrl && currentUrl === aliasUrl) {
        currentOg.url = content;
      } else {
        finishOg();
        currentOg = { url: content, secureUrl: '', width: 0, height: 0, alt: '' };
      }
    } else if (currentOg && key === 'og:image:secure_url') {
      currentOg.secureUrl = content;
    } else if (currentOg && key === 'og:image:width') {
      currentOg.width = content;
    } else if (currentOg && key === 'og:image:height') {
      currentOg.height = content;
    } else if (currentOg && key === 'og:image:alt') {
      currentOg.alt = content;
    } else if (currentOg && key.startsWith('og:') && !key.startsWith('og:image:')) {
      finishOg();
    }
  }
  finishOg();
  let acceptedOg = 0;
  for (let index = 0; index < ogBlocks.length; index++) {
    const block = ogBlocks[index];
    const score = Math.max(100, 120 - index);
    let accepted = false;
    if (block.secureUrl) {
      accepted = add({
        url: block.secureUrl,
        source: 'og:image:secure_url',
        role: 'representative',
        width: block.width,
        height: block.height,
        alt: block.alt,
        score
      });
    }
    if (!accepted) {
      accepted = add({
        url: block.url,
        source: 'og:image',
        role: 'representative',
        width: block.width,
        height: block.height,
        alt: block.alt,
        score
      });
    }
    if (accepted) acceptedOg++;
  }
  if (acceptedOg === 0) {
    let twitterAlt = '';
    for (let index = 0; index < Math.min(metas.length, 200); index++) {
      const meta = metas[index];
      const key = short(meta.getAttribute('name') || meta.getAttribute('property'), 100).toLowerCase();
      if (key === 'twitter:image:alt') twitterAlt = short(meta.getAttribute('content'));
    }
    for (let index = 0; index < Math.min(metas.length, 200); index++) {
      const meta = metas[index];
      const key = short(meta.getAttribute('name') || meta.getAttribute('property'), 100).toLowerCase();
      if (!['twitter:image', 'twitter:image:src'].includes(key)) continue;
      add({
        url: meta.getAttribute('content') || '',
        source: key,
        role: 'representative',
        alt: twitterAlt,
        score: 110
      });
    }
  }

  const imageKeys = new Set([
    'image', 'images', 'contenturl', 'thumbnail', 'thumbnailurl',
    'primaryimageofpage', 'photo', 'photos', 'screenshot'
  ]);
  let jsonNodes = 0;
  let jsonBytes = 0;
  const extractJsonImage = (value, key, depth) => {
    if (depth > 8 || jsonNodes++ > 5000 || value == null) return;
    const safeKey = String(key || '').slice(0, 80);
    const normalizedKey = safeKey.replace(/[-_]/g, '').toLowerCase();
    if (imageKeys.has(normalizedKey)) {
      const values = Array.isArray(value) ? value : [value];
      for (const item of values.slice(0, 20)) {
        if (typeof item === 'string') {
          add({ url: item, source: `jsonld:${safeKey}`, role: 'representative', score: 115 });
        } else if (item && typeof item === 'object') {
          const imageUrl = item.contentUrl || item.thumbnailUrl || item.url || item['@id'];
          add({
            url: imageUrl,
            source: `jsonld:${safeKey}`,
            role: normalizedKey.includes('thumbnail') ? 'thumbnail' : 'representative',
            width: item.width,
            height: item.height,
            alt: item.caption || item.name || item.description,
            caption: item.caption,
            score: normalizedKey.includes('thumbnail') ? 95 : 116
          });
        }
      }
    }
    if (Array.isArray(value)) {
      for (let index = 0; index < Math.min(value.length, 100); index++) {
        extractJsonImage(value[index], key, depth + 1);
      }
    } else if (value && typeof value === 'object') {
      let count = 0;
      for (const childKey in value) {
        if (!Object.prototype.hasOwnProperty.call(value, childKey)) continue;
        if (count++ >= 100) break;
        if (String(childKey).slice(0, 80).toLowerCase() === 'logo') continue;
        extractJsonImage(value[childKey], childKey, depth + 1);
      }
    }
  };
  const allScripts = document.getElementsByTagName('script');
  let jsonScriptCount = 0;
  for (let index = 0; index < Math.min(allScripts.length, 1000) && jsonScriptCount < 20; index++) {
    const script = allScripts[index];
    if (short(script.getAttribute('type'), 80).toLowerCase() !== 'application/ld+json') continue;
    jsonScriptCount++;
    const childNodes = script.childNodes;
    if (childNodes.length > 100) continue;
    let payloadLength = 0;
    let plainTextOnly = true;
    for (let childIndex = 0; childIndex < childNodes.length; childIndex++) {
      const child = childNodes[childIndex];
      if (![3, 4].includes(child.nodeType)) {
        plainTextOnly = false;
        break;
      }
      payloadLength += Number(child.length || 0);
      if (jsonBytes + payloadLength > MAX_JSON_LD_BYTES) break;
    }
    if (!plainTextOnly || !payloadLength || jsonBytes + payloadLength > MAX_JSON_LD_BYTES) continue;
    const payload = script.textContent || '';
    jsonBytes += payload.length;
    try { extractJsonImage(JSON.parse(payload), '', 0); } catch (_) {}
  }

  const srcsetUrl = value => {
    let best = null;
    const parts = String(value || '').slice(0, 16384).split(',', 50);
    for (const part of parts) {
      const match = part.trim().match(/^(\S+)(?:\s+(\d+(?:\.\d+)?)(w|x))?$/);
      if (!match) continue;
      const weight = Number(match[2] || 1) * (match[3] === 'x' ? 10000 : 1);
      if (!best || weight > best.weight) best = { url: match[1], weight };
    }
    return best?.url || '';
  };
  const boundedText = (element, limit = 240) => {
    if (!element) return '';
    const childNodes = element.childNodes;
    let output = '';
    for (let index = 0; index < Math.min(childNodes.length, 20) && output.length < limit * 4; index++) {
      const node = childNodes[index];
      if (![3, 4].includes(node.nodeType)) continue;
      output += ` ${node.substringData(0, Math.max(0, limit * 4 - output.length))}`;
    }
    return short(output, limit);
  };
  const visible = element => {
    try {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0 &&
        rect.width > 0 && rect.height > 0;
    } catch (_) { return false; }
  };
  const unwanted = (element, url, alt) => {
    const context = `${short(url, 4096)} ${short(alt)} ${short(element.id, 120)} ` +
      `${short(element.className, 200)} ${short(element.parentElement?.id, 120)} ` +
      `${short(element.parentElement?.className, 200)}`;
    return utilityPattern.test(context);
  };
  const boundedAncestor = (element, predicate) => {
    let current = element?.parentElement || null;
    for (let depth = 0; current && depth < 12; depth++, current = current.parentElement) {
      if (predicate(current)) return current;
    }
    return null;
  };
  const images = document.images;
  for (let imageIndex = 0; imageIndex < Math.min(images.length, 1000); imageIndex++) {
    const image = images[imageIndex];
    const choices = [
      ['img.currentSrc', image.currentSrc],
      ['img.src', image.getAttribute('src')],
      ['img.srcset', srcsetUrl(image.getAttribute('srcset'))],
      ['img.data-src', image.getAttribute('data-src')],
      ['img.data-srcset', srcsetUrl(image.getAttribute('data-srcset'))],
      ['img.data-original', image.getAttribute('data-original')],
      ['img.data-lazy-src', image.getAttribute('data-lazy-src')]
    ];
    const alt = short(
      image.getAttribute('alt') || image.getAttribute('aria-label') || image.getAttribute('title') || ''
    );
    const hasLazyAlternative = choices.slice(3).some(([, value]) =>
      resolveUrl(value) && !unwanted(image, value, alt)
    );
    const selected = choices.find(([source, value]) => {
      if (!resolveUrl(value) || unwanted(image, value, alt)) return false;
      const usesLoadedSource = source === 'img.currentSrc' || source === 'img.src';
      if (!usesLoadedSource || !hasLazyAlternative) return true;
      return Boolean(
        image.naturalWidth
        && image.naturalHeight
        && Math.max(image.naturalWidth, image.naturalHeight) >= 128
      );
    });
    if (!selected) continue;
    const [source, rawUrl] = selected;
    const usesLoadedSource = source === 'img.currentSrc' || source === 'img.src';
    const width = number((usesLoadedSource && image.naturalWidth) || image.getAttribute('width'));
    const height = number((usesLoadedSource && image.naturalHeight) || image.getAttribute('height'));
    if (width && height && Math.max(width, height) < 128) continue;
    const figure = boundedAncestor(image, element => element.tagName === 'FIGURE');
    const inMain = Boolean(boundedAncestor(image, element =>
      ['MAIN', 'ARTICLE'].includes(element.tagName) || element.getAttribute('role') === 'main'
    ));
    const isVisible = visible(image);
    const caption = figure?.getElementsByTagName('figcaption')[0] || null;
    add({
      url: rawUrl,
      source,
      role: figure ? 'gallery' : 'content',
      width,
      height,
      alt,
      caption: boundedText(caption),
      score: 55 + (figure ? 20 : 0) + (inMain ? 10 : 0) + (isVisible ? 8 : 0) +
        (width * height >= 480000 ? 5 : 0)
    });
  }
  const videos = document.getElementsByTagName('video');
  let videoCount = 0;
  for (let videoIndex = 0; videoIndex < Math.min(videos.length, 1000) && videoCount < 100; videoIndex++) {
    const video = videos[videoIndex];
    if (!video.getAttribute('poster')) continue;
    videoCount++;
    add({
      url: video.getAttribute('poster'),
      source: 'video.poster',
      role: 'thumbnail',
      width: video.videoWidth || video.getAttribute('width'),
      height: video.videoHeight || video.getAttribute('height'),
      alt: video.getAttribute('aria-label') || video.getAttribute('title') || '',
      score: 50
    });
  }

  return Array.from(candidates.values())
    .sort((a, b) => b.score - a.score || (b.width * b.height) - (a.width * a.height))
    .slice(0, MAX_CANDIDATES)
    .map((candidate, index) => ({ id: `img-${index + 1}`, ...candidate }));
})())
