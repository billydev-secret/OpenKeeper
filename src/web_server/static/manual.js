// Manual page script: last-updated stamp, sidebar TOC, TOC search, and the
// print button. Externalised from manual.html so the dashboard can send
// `script-src 'self'` with no 'unsafe-inline' (see _CSP in server.py).
//
// Not part of the js/ module graph: js/ is the dashboard app, and the manual
// is a standalone page that loads none of it.
  // ── Last-updated stamp (from the served file's modification time) ──
  (function () {
    const el = document.getElementById('cover-updated');
    const d = new Date(document.lastModified);
    if (el && !isNaN(d)) {
      el.textContent = '📅 Updated ' + d.toLocaleDateString(undefined, { year: 'numeric', month: 'long' });
    }
  })();

  // ── Build the sidebar TOC from the document's own headings ─────────
  // The headings are the single source of truth, so the TOC cannot
  // drift from the content. h2 → section header, h3 → sub-entry.
  const headings = Array.from(document.querySelectorAll('h2[id], h3[id]'));
  const tocList = document.getElementById('toc-list');
  const tocEntries = []; // { li, parentLi, heading, searchText }

  function headingLabel(h) {
    const clone = h.cloneNode(true);
    const num = clone.querySelector('.section-num');
    let prefix = '';
    if (num) { prefix = num.textContent.trim() + '. '; num.remove(); }
    return prefix + clone.textContent.trim();
  }

  function blockText(h) {
    // Text of everything under this heading until the next h2/h3.
    let node = h.nextElementSibling, parts = [h.textContent];
    while (node && node.tagName !== 'H2' && node.tagName !== 'H3') {
      parts.push(node.textContent || '');
      node = node.nextElementSibling;
    }
    return parts.join(' ').toLowerCase();
  }

  (function buildToc() {
    const coverLi = document.createElement('li');
    coverLi.innerHTML = '<a href="#cover">Dungeon Keeper</a>';
    tocList.appendChild(coverLi);

    let lastH2Li = null;
    for (const h of headings) {
      const li = document.createElement('li');
      if (h.tagName === 'H2') li.className = 'section-header';
      const a = document.createElement('a');
      a.href = '#' + h.id;
      a.textContent = headingLabel(h);
      li.appendChild(a);
      tocList.appendChild(li);
      if (h.tagName === 'H2') lastH2Li = li;
      tocEntries.push({ li, parentLi: h.tagName === 'H3' ? lastH2Li : null, searchText: blockText(h) });
    }
  })();

  // ── TOC search: filter entries by heading + section body text ──────
  const tocSearch = document.getElementById('toc-search-input');
  const tocNoResults = document.getElementById('toc-no-results');
  tocSearch.addEventListener('input', () => {
    const q = tocSearch.value.trim().toLowerCase();
    let any = false;
    const visible = new Set();
    for (const e of tocEntries) {
      if (!q || e.searchText.includes(q)) {
        visible.add(e.li);
        if (e.parentLi) visible.add(e.parentLi); // keep section header for matching subsections
        any = true;
      }
    }
    for (const e of tocEntries) e.li.classList.toggle('toc-hidden', !visible.has(e.li));
    tocNoResults.style.display = (q && !any) ? 'block' : 'none';
  });

  // ── Highlight active sidebar link on scroll ────────────────────────
  const links = document.querySelectorAll('.sidebar nav a[href^="#"]');
  const observer = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        links.forEach(l => l.classList.remove('active'));
        const target = document.querySelector('.sidebar nav a[href="#' + e.target.id + '"]');
        if (target) target.classList.add('active');
      }
    });
  }, { rootMargin: '0px 0px -70% 0px' });
  headings.forEach(h => observer.observe(h));

// The print button used to be an inline onclick=, which no CSP nonce or hash
// covers without 'unsafe-hashes'. It is a listener now.
document.getElementById('print-btn')?.addEventListener('click', () => window.print());
