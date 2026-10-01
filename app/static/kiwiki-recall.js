/* kiwiki Recall — Befehlspalette im Spotlight-Stil.
 *
 * ⌘K / Strg+K (überall) oder „/“ (außerhalb von Eingabefeldern) öffnet eine
 * Palette über der App: Live-Suche über /api/search mit Präfix-Treffern,
 * zuletzt geöffnete Notizen und die wichtigsten Aktionen an einem Ort.
 * Tastatur zuerst: ↑/↓ wählen, ↵ öffnen, Esc schließt und gibt den Fokus zurück.
 *
 * Außerdem hier, weil es dieselbe Bewegungssprache teilt:
 *   - Lesefortschritt als Linie unter dem Header (nur bei geöffneter Notiz)
 *   - gleitender Daumen im Segment-Umschalter der Startseite
 */
(function () {
  'use strict';

  var MRU_KEY = 'kiwiki_recall_recent';
  var MRU_MAX = 6;
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

  function t(key, fallback) {
    return (typeof kwText === 'function') ? kwText(key, fallback) : (fallback || key);
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function canWrite() { return typeof kwCanWrite === 'function' ? kwCanWrite() : false; }
  function canAdmin() { return typeof kwCanAdmin === 'function' ? kwCanAdmin() : false; }

  /* ── Zuletzt geöffnet (lokal, pro Browser) ─────────────────────────── */
  function mruRead() {
    try {
      return JSON.parse(localStorage.getItem(MRU_KEY) || '[]').map(function (entry) {
        return typeof entry === 'string' ? { path: entry } : entry;
      }).filter(function (entry) { return entry && typeof entry.path === 'string'; });
    } catch (e) { return []; }
  }
  function mruPush(path, title) {
    if (!path) return;
    var list = mruRead().filter(function (entry) { return entry.path !== path; });
    list.unshift({ path: path, title: title || '' });
    try { localStorage.setItem(MRU_KEY, JSON.stringify(list.slice(0, MRU_MAX))); } catch (e) {}
  }
  // Eine Notiz gilt als geöffnet, sobald ihre Ansicht eingeschwungen ist —
  // unabhängig davon, ob Baum, Suche, Link oder Recall sie geöffnet hat.
  document.addEventListener('htmx:afterSettle', function () {
    var file = new URLSearchParams(location.search).get('file');
    var view = document.querySelector('#main-content .file-view');
    if (!file || !view) return;
    var heading = view.querySelector('.file-title, .markdown-content h1');
    mruPush(file, heading ? heading.textContent.trim() : '');
  });
  function titleFromPath(path) {
    var base = path.split('/').pop().replace(/\.md$/i, '');
    return base.replace(/[-_]+/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  /* ── Aktionen ─────────────────────────────────────────────────────── */
  function actions() {
    var list = [];
    if (canWrite() && typeof kwNewNote === 'function') {
      list.push({ id: 'new', label: t('recallNewNote', 'Neue Notiz'), hint: 'N', icon: 'plus', run: function () { kwNewNote(); } });
    }
    list.push({ id: 'atlas', label: t('recallAtlas', 'Neuronaler Atlas öffnen'), icon: 'atlas', run: function () { location.href = '/knowledge'; } });
    list.push({ id: 'tags', label: t('recallTags', 'Alle Tags'), icon: 'tag', run: function () { location.href = '/?view=tags'; } });
    list.push({ id: 'history', label: t('recallHistory', 'Suchverlauf'), icon: 'clock', run: function () { location.href = '/?view=search-history'; } });
    if (typeof toggleSidebar === 'function' && document.querySelector('.sidebar')) {
      list.push({ id: 'sidebar', label: t('recallSidebar', 'Seitenleiste ein-/ausblenden'), icon: 'sidebar', run: function () { toggleSidebar(); } });
    }
    var other = (document.documentElement.lang === 'en') ? 'de' : 'en';
    list.push({ id: 'lang', label: t('recallLanguage', 'Switch to English'), icon: 'globe', run: function () {
      var url = new URL(location.href); url.searchParams.set('lang', other); location.href = url.toString();
    } });
    if (canAdmin()) {
      list.push({ id: 'settings', label: t('recallSettings', 'Einstellungen'), icon: 'gear', run: function () { location.href = '/settings'; } });
    }
    return list;
  }

  var ICONS = {
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    recent: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    atlas: '<circle cx="12" cy="12" r="2"/><circle cx="5" cy="7" r="2"/><circle cx="19" cy="7" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="18" r="2"/><path d="M10.4 10.8 6.6 8.2M13.6 10.8l3.8-2.6M10.6 13.4 7.4 16.6M13.4 13.4l3.2 3.2"/>',
    tag: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    clock: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    sidebar: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'
  };
  function icon(name) {
    return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || ICONS.doc) + '</svg>';
  }

  /* ── DOM ──────────────────────────────────────────────────────────── */
  var root, panel, input, list, lens, status, lastFocus = null;
  var items = [], active = 0, query = '', requestId = 0, controller = null, debounce = null, open = false;
  // Enter während einer laufenden Suche: auf die frischen Treffer warten, statt
  // den veralteten ersten Eintrag zu öffnen.
  var pending = false, chooseWhenReady = false;

  function build() {
    if (root) return;
    root = document.createElement('div');
    root.className = 'recall';
    root.hidden = true;
    root.innerHTML =
      '<div class="recall-scrim" data-recall-close></div>' +
      '<div class="recall-panel" role="dialog" aria-modal="true" aria-label="' + esc(t('recallTitle', 'Recall')) + '">' +
        '<div class="recall-field">' + icon('search') +
          '<input class="recall-input" type="text" autocomplete="off" spellcheck="false" role="combobox" ' +
          'aria-autocomplete="list" aria-expanded="true" aria-controls="recall-list" ' +
          'placeholder="' + esc(t('recallPlaceholder', 'Notiz suchen oder Befehl …')) + '">' +
          '<kbd class="recall-esc">esc</kbd>' +
        '</div>' +
        '<div class="recall-body"><div class="recall-lens" aria-hidden="true"></div>' +
          '<div class="recall-list" id="recall-list" role="listbox" aria-label="' + esc(t('recallResults', 'Ergebnisse')) + '"></div>' +
        '</div>' +
        '<div class="recall-foot" aria-hidden="true">' +
          '<span><kbd>↑</kbd><kbd>↓</kbd> ' + esc(t('recallNavigate', 'auswählen')) + '</span>' +
          '<span><kbd>↵</kbd> ' + esc(t('recallOpen', 'öffnen')) + '</span>' +
          '<span class="recall-status" role="status"></span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(root);
    panel = root.querySelector('.recall-panel');
    input = root.querySelector('.recall-input');
    list = root.querySelector('.recall-list');
    lens = root.querySelector('.recall-lens');
    status = root.querySelector('.recall-status');

    root.addEventListener('click', function (e) {
      if (e.target.closest('[data-recall-close]')) { close(); return; }
      var option = e.target.closest('.recall-item');
      if (option) { active = Number(option.dataset.index); choose(); }
    });
    list.addEventListener('pointermove', function (e) {
      var option = e.target.closest('.recall-item');
      if (option && Number(option.dataset.index) !== active) { active = Number(option.dataset.index); paintActive(false); }
    });
    input.addEventListener('input', function () {
      query = input.value;
      active = 0;
      clearTimeout(debounce);
      if (!query.trim()) { pending = false; refresh(); return; }
      pending = true;
      debounce = setTimeout(refresh, 110);
    });
    input.addEventListener('keydown', onKey);
  }

  /* ── Datenquellen ─────────────────────────────────────────────────── */
  function ftsQuery(raw) {
    // Präfixsuche: „oau“ findet „OAuth“. Sonderzeichen würden FTS5 aus dem Tritt bringen.
    var words = raw.replace(/[^\p{L}\p{N}\s]+/gu, ' ').trim().split(/\s+/).filter(Boolean).slice(0, 6);
    if (!words.length) return '';
    return words.map(function (w, i) { return i === words.length - 1 && w.length >= 2 ? w + '*' : w; }).join(' ');
  }

  function matchAction(a, q) {
    var hay = a.label.toLowerCase();
    return q.split(/\s+/).every(function (part) { return hay.indexOf(part) !== -1; });
  }

  function refresh() {
    debounce = null;
    var q = query.trim().toLowerCase();
    var sections = [];
    if (!q) {
      var recent = mruRead();
      if (recent.length) sections.push({ title: t('recallRecent', 'Zuletzt geöffnet'), items: recent.map(function (entry) {
        return { kind: 'note', path: entry.path, title: entry.title || titleFromPath(entry.path), sub: entry.path, icon: 'recent' };
      }) });
      sections.push({ title: t('recallActions', 'Aktionen'), items: actions().map(asActionItem) });
      setStatus('');
      render(sections);
      return;
    }
    var matched = actions().filter(function (a) { return matchAction(a, q); }).map(asActionItem);
    var fts = ftsQuery(query);
    if (!fts) { pending = false; render(matched.length ? [{ title: t('recallActions', 'Aktionen'), items: matched }] : []); return; }

    var id = ++requestId;
    if (controller) controller.abort();
    controller = ('AbortController' in window) ? new AbortController() : null;
    setStatus(t('recallSearching', 'Suche …'), true);
    // Aktionen sofort zeigen; Notizen kommen dazu, sobald die Antwort da ist
    render(matched.length ? [{ title: t('recallActions', 'Aktionen'), items: matched }] : [], true);
    fetch('/api/search', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: fts }),
      signal: controller ? controller.signal : undefined
    }).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (rows) {
      if (id !== requestId || !open) return;
      var notes = (rows || []).slice(0, 8).map(function (row) {
        return { kind: 'note', path: row.path, title: row.title || titleFromPath(row.path), sub: row.path, snippet: row.snippet, icon: 'doc' };
      });
      var sections = [];
      if (notes.length) sections.push({ title: t('recallNotes', 'Notizen'), items: notes });
      if (matched.length) sections.push({ title: t('recallActions', 'Aktionen'), items: matched });
      setStatus(notes.length ? notes.length + ' ' + t('recallHits', 'Treffer') : '');
      pending = false;
      render(sections);
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      if (id !== requestId || !open) return;
      setStatus(t('recallUnavailable', 'Suche gerade nicht erreichbar'));
      pending = false;
      render(matched.length ? [{ title: t('recallActions', 'Aktionen'), items: matched }] : []);
    });
  }

  function asActionItem(a) { return { kind: 'action', title: a.label, icon: a.icon, run: a.run, hint: a.hint }; }

  function setStatus(text, busy) {
    status.textContent = text;
    root.classList.toggle('is-busy', !!busy);
  }

  /* ── Darstellung ──────────────────────────────────────────────────── */
  function highlight(text) {
    var safe = esc(text);
    var words = query.trim().split(/\s+/).filter(function (w) { return w.length > 1; }).map(function (w) {
      return w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    });
    if (!words.length) return safe;
    return safe.replace(new RegExp('(' + words.map(esc).join('|') + ')', 'gi'), '<mark>$1</mark>');
  }

  function render(sections, pending) {
    items = [];
    var html = '';
    sections.forEach(function (section) {
      if (!section.items.length) return;
      html += '<div class="recall-group" role="group" aria-label="' + esc(section.title) + '">' +
              '<div class="recall-group-title" aria-hidden="true">' + esc(section.title) + '</div>';
      section.items.forEach(function (item) {
        var i = items.length; items.push(item);
        html += '<div class="recall-item" role="option" id="recall-opt-' + i + '" data-index="' + i + '" aria-selected="false">' +
                  '<span class="recall-icon">' + icon(item.icon) + '</span>' +
                  '<span class="recall-text"><span class="recall-title">' + highlight(item.title) + '</span>' +
                  (item.sub ? '<span class="recall-sub">' + esc(item.sub) + '</span>' : '') +
                  (item.snippet ? '<span class="recall-snippet">' + highlight(item.snippet) + '</span>' : '') +
                  '</span>' +
                  (item.kind === 'action' ? '<span class="recall-kind">' + esc(t('recallRun', 'Ausführen')) + '</span>' : '<span class="recall-kind">↵</span>') +
                '</div>';
      });
      html += '</div>';
    });
    if (!items.length && !pending) {
      html = '<div class="recall-empty"><strong>' + esc(t('recallNothing', 'Nichts gefunden')) + '</strong>' +
             '<span>' + esc(t('recallNothingHelp', 'Anderer Begriff, oder mit Esc zurück.')) + '</span></div>';
    }
    list.innerHTML = html;
    active = Math.min(active, Math.max(0, items.length - 1));
    if (!items.length) active = 0;
    paintActive(true);
    if (!pending && chooseWhenReady) { chooseWhenReady = false; choose(); }
  }

  function paintActive(jump) {
    var options = list.querySelectorAll('.recall-item');
    options.forEach(function (o, i) { o.setAttribute('aria-selected', String(i === active)); });
    var current = options[active];
    if (!current) { lens.style.opacity = '0'; input.removeAttribute('aria-activedescendant'); return; }
    input.setAttribute('aria-activedescendant', current.id);
    // Die Linse gleitet zum Treffer (Federkurve in CSS); beim Neuaufbau springt sie
    var top = current.offsetTop - list.scrollTop + list.offsetTop;
    lens.classList.toggle('no-anim', !!jump || reduceMotion);
    lens.style.opacity = '1';
    lens.style.transform = 'translateY(' + top + 'px)';
    lens.style.height = current.offsetHeight + 'px';
    var view = list.getBoundingClientRect(); var box = current.getBoundingClientRect();
    if (box.bottom > view.bottom) list.scrollTop += box.bottom - view.bottom + 6;
    else if (box.top < view.top) list.scrollTop -= view.top - box.top + 6;
  }

  function move(delta) {
    if (!items.length) return;
    active = (active + delta + items.length) % items.length;
    paintActive(false);
  }

  function choose() {
    var item = items[active];
    if (!item) return;
    close(true);
    if (item.kind === 'action') { item.run(); return; }
    if (typeof loadFile === 'function') loadFile(item.path);
    else location.href = '/?file=' + encodeURIComponent(item.path);
  }

  function onKey(e) {
    if (e.key === 'ArrowDown' || (e.key === 'Tab' && !e.shiftKey) || (e.ctrlKey && e.key === 'n')) { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp' || (e.key === 'Tab' && e.shiftKey) || (e.ctrlKey && e.key === 'p')) { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); if (pending) chooseWhenReady = true; else choose(); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  }

  /* ── Öffnen / Schließen ───────────────────────────────────────────── */
  function openRecall(prefill) {
    build();
    if (open) { input.focus(); input.select(); return; }
    open = true;
    lastFocus = document.activeElement;
    root.hidden = false;
    document.documentElement.classList.add('recall-open');
    input.value = prefill || '';
    query = input.value;
    active = 0;
    refresh();
    requestAnimationFrame(function () { root.classList.add('is-open'); input.focus(); });
  }

  function close(keepFocusFree) {
    if (!open) return;
    open = false;
    pending = false; chooseWhenReady = false;
    if (controller) controller.abort();
    clearTimeout(debounce);
    root.classList.remove('is-open');
    document.documentElement.classList.remove('recall-open');
    var done = function () { if (!open) root.hidden = true; };
    if (reduceMotion) done(); else setTimeout(done, 180);
    if (!keepFocusFree && lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
  }

  function typingTarget(el) {
    return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
  }

  document.addEventListener('keydown', function (e) {
    var combo = (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K');
    if (combo) { e.preventDefault(); if (open) close(); else openRecall(); return; }
    if (e.key === '/' && !open && !typingTarget(e.target) && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault(); openRecall();
    }
  }, true);

  window.kwOpenRecall = openRecall;

  /* ── Hinweis im Header-Suchfeld ───────────────────────────────────── */
  function decorateHeader() {
    // Mobil: die Lupe im Suchfeld wird zur Schaltfläche für Recall (kein Tastaturkürzel verfügbar)
    var lensIcon = document.querySelector('header .search-wrap .search-icon');
    if (lensIcon && !lensIcon.closest('button')) {
      var lensButton = document.createElement('button');
      lensButton.type = 'button';
      lensButton.className = 'search-lens-button';
      lensButton.setAttribute('aria-label', t('recallOpenLabel', 'Recall öffnen'));
      lensIcon.parentNode.insertBefore(lensButton, lensIcon);
      lensButton.appendChild(lensIcon);
      lensButton.addEventListener('click', function () {
        var field = document.querySelector('header .search-input');
        openRecall(field ? field.value : '');
      });
    }
    var wrap = document.querySelector('header .search-wrap');
    if (!wrap || wrap.querySelector('.search-recall')) return;
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'search-recall';
    button.setAttribute('aria-label', t('recallOpenLabel', 'Recall öffnen'));
    button.title = t('recallOpenLabel', 'Recall öffnen');
    button.innerHTML = '<kbd>' + (isMac ? '⌘' : esc(t('recallCtrl', 'Strg'))) + '</kbd><kbd>K</kbd>';
    button.addEventListener('click', function () {
      var field = wrap.querySelector('.search-input');
      openRecall(field ? field.value : '');
    });
    wrap.appendChild(button);
  }

  /* ── Lesefortschritt ──────────────────────────────────────────────── */
  function bindProgress() {
    var area = document.getElementById('main-content');
    var header = document.querySelector('body > header');
    if (!area || !header) return;
    var ticking = false;
    function update() {
      ticking = false;
      var reading = !!area.querySelector('.file-view .markdown-content');
      var max = area.scrollHeight - area.clientHeight;
      var value = reading && max > 40 ? Math.min(1, area.scrollTop / max) : 0;
      header.style.setProperty('--read', value.toFixed(4));
      header.classList.toggle('is-reading', reading && max > 40);
    }
    area.addEventListener('scroll', function () { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
    document.addEventListener('htmx:afterSettle', function () { requestAnimationFrame(update); });
    update();
  }

  /* ── Segment-Umschalter: Daumen folgt der Auswahl ──────────────────── */
  // Der Daumen wird auf das aktive Segment vermessen (Position + Breite),
  // damit er bei unterschiedlich langen Labels genau passt.
  function syncSegments(instant) {
    document.querySelectorAll('.recent-switch').forEach(function (group) {
      var tabs = Array.prototype.slice.call(group.querySelectorAll('.recent-tab'));
      var active = tabs.filter(function (tab) { return tab.getAttribute('aria-pressed') === 'true'; })[0] || tabs[0];
      if (!active) return;
      // Breite der fetten Variante reservieren (CSS ::after liest data-label)
      tabs.forEach(function (tab) { if (!tab.dataset.label) tab.dataset.label = tab.textContent.trim(); });
      var first = !group.classList.contains('has-thumb');
      if (first || instant) group.classList.add('is-measuring');
      group.classList.add('has-thumb');
      group.style.setProperty('--seg-x', (active.getBoundingClientRect().left - group.getBoundingClientRect().left - group.clientLeft) + 'px');
      group.style.setProperty('--seg-w', active.getBoundingClientRect().width + 'px');
      if (first || instant) {
        void group.offsetWidth;
        requestAnimationFrame(function () { group.classList.remove('is-measuring'); });
      }
    });
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('.recent-tab')) requestAnimationFrame(function () { syncSegments(false); });
  });
  window.addEventListener('resize', function () { syncSegments(true); });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { syncSegments(true); });

  function init() { decorateHeader(); bindProgress(); syncSegments(true); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}());
