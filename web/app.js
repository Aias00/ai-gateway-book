'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const { root, chapter, book } = document.body.dataset;
  const navigation = JSON.parse($('#book-navigation').textContent);
  const slugs = new Set(navigation.map(item => item.slug));
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, String(value)); } catch { /* Reading remains available. */ } },
  };
  const key = suffix => `aias-reader:${book}:${suffix}`;
  const theme = $('#theme');
  function applyTheme(value) {
    const dark = value === 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.body.classList.toggle('dark', dark);
    theme.textContent = dark ? '浅色' : '深色';
    theme.setAttribute('aria-label', dark ? '切换为浅色模式' : '切换为深色模式');
    $('meta[name="theme-color"]').content = dark ? '#1c1f24' : '#f7f7f3';
  }
  applyTheme(store.get('aias-reader-theme'));
  theme.hidden = false;
  theme.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    store.set('aias-reader-theme', next);
    applyTheme(next);
  });
  addEventListener('storage', event => {
    if (event.key === 'aias-reader-theme' || event.key === null) {
      applyTheme(event.key === null ? null : event.newValue);
    }
  });
  let size = Number(store.get(key('font'))) || 18;
  size = Math.max(16, Math.min(26, size));
  function applySize() {
    document.documentElement.style.setProperty('--reader-size', size + 'px');
    $('#smaller').disabled = size === 16;
    $('#larger').disabled = size === 26;
  }
  for (const [id, delta] of [['smaller', -1], ['larger', 1]]) {
    const control = $('#' + id);
    control.hidden = false;
    control.addEventListener('click', () => {
      size = Math.max(16, Math.min(26, size + delta));
      store.set(key('font'), size);
      applySize();
    });
  }
  applySize();
  const mobile = matchMedia('(max-width: 960px)');
  const contents = $('#contents');
  contents.open = !mobile.matches;
  mobile.addEventListener('change', () => { contents.open = !mobile.matches; });
  for (const link of $('#sidebar nav').querySelectorAll('a')) {
    link.addEventListener('click', () => { if (mobile.matches) contents.open = false; });
  }
  const last = store.get(key('last'));
  if (slugs.has(last) && last !== chapter) {
    const control = $('#continue');
    control.href = root + 'chapters/' + last + '.html';
    control.hidden = false;
  }
  if (chapter && slugs.has(chapter)) {
    store.set(key('last'), chapter);
    const saved = Number(store.get(key('position:' + chapter)));
    if (saved > 0 && !location.hash) {
      requestAnimationFrame(() => requestAnimationFrame(() => scrollTo(0, saved)));
    }
    let pending;
    addEventListener('scroll', () => {
      clearTimeout(pending);
      pending = setTimeout(() => store.set(key('position:' + chapter), Math.round(scrollY)), 120);
    }, { passive: true });
    addEventListener('pagehide', () => store.set(key('position:' + chapter), Math.round(scrollY)));
  }
  const form = $('#search-form');
  const results = $('#search-results');
  const status = $('#search-status');
  form.hidden = false;
  let searchIndex;
  let searchVersion = 0;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const version = ++searchVersion;
    results.replaceChildren();
    const query = $('#search').value.trim();
    if (!query) { status.textContent = '输入概念、代码或章节名称后搜索。'; return; }
    status.textContent = '正在搜索…';
    const queryAtStart = query;
    try {
      if (!searchIndex) {
        const response = await fetch(root + 'search-index.json');
        if (!response.ok) throw new Error('Search index unavailable');
        searchIndex = await response.json();
      }
      if (version !== searchVersion || $('#search').value.trim() !== queryAtStart) return;
      const terms = query.toLocaleLowerCase().split(/\s+/).slice(0, 8);
      const hits = searchIndex.filter(item => terms.every(term =>
        (item.title + ' ' + item.text).toLocaleLowerCase().includes(term)));
      status.textContent = `找到 ${hits.length} 篇相关章节`;
      for (const item of hits) {
        const link = document.createElement('a');
        link.href = root + 'chapters/' + item.slug + '.html';
        link.textContent = item.title;
        const excerpt = document.createElement('small');
        const position = Math.max(0, item.text.toLocaleLowerCase().indexOf(terms[0]) - 36);
        excerpt.textContent = item.text.slice(position, position + 130);
        link.append(excerpt);
        results.append(link);
      }
    } catch {
      status.textContent = '搜索暂时不可用，请重试，或从下方目录阅读。';
    }
  });
})();
