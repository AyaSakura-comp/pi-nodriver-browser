(() => {
  const rows = [];
  for (const anchor of document.querySelectorAll('a')) {
    const heading = anchor.querySelector('h3');
    if (!heading) continue;
    const title = (heading.innerText || heading.textContent || '').trim();
    const url = anchor.href || '';
    if (!title || !url) continue;
    const card = anchor.closest('.MjjYud, .g') || anchor.closest('[data-snhf]')?.parentElement || anchor.parentElement?.parentElement?.parentElement;
    const snippetElement = card?.querySelector('[data-sncf="1"], .VwiC3b, [data-snf="nke7rc"]');
    const explicitSnippet = (snippetElement?.innerText || snippetElement?.textContent || '').trim();
    const lines = (card?.innerText || '')
      .split('\n')
      .map(line => line.trim())
      .filter(line => line && line !== title && !/^https?:\/\//i.test(line));
    const snippet = explicitSnippet || lines.slice(0, 4).join(' ');
    rows.push({ title, url, snippet: snippet.slice(0, 600),
      truncated: snippet.length > 600 || (!explicitSnippet && lines.length > 4) });
  }
  return rows.slice(0, 20);
})()
