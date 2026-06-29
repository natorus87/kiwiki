function getCookieValue(name) {
  var m = document.cookie.match('(?:^|; )' + name.replace(/([.$?*|{}()[\]\\/+^])/g, '\\$1') + '=([^;]*)');
  return m ? decodeURIComponent(m[1]) : '';
}
function getKey() {
  try { localStorage.removeItem('kiwiki_api_key'); } catch (e) {}
  return '';
}
function apiHeaders(extra) {
  return Object.assign({}, extra || {});
}

function kwIsMobileSidebar() {
  return window.matchMedia && window.matchMedia('(max-width: 768px)').matches;
}

function openSidebar() {
  var s = document.querySelector('.sidebar');
  var b = document.getElementById('sidebar-backdrop');
  var btn = document.querySelector('.hamburger');
  if (s) {
    s.classList.add('open');
    if (kwIsMobileSidebar()) s.style.transform = 'translateX(0)';
  }
  if (b) b.classList.add('open');
  if (btn) btn.setAttribute('aria-expanded', 'true');
}
function closeSidebar() {
  var s = document.querySelector('.sidebar');
  var b = document.getElementById('sidebar-backdrop');
  var btn = document.querySelector('.hamburger');
  if (s) {
    s.classList.remove('open');
    s.style.transform = kwIsMobileSidebar() ? 'translateX(-100%)' : '';
  }
  if (b) b.classList.remove('open');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}
function toggleSidebar() {
  var s = document.querySelector('.sidebar');
  if (s && s.classList.contains('open')) closeSidebar();
  else openSidebar();
}
function loadFile(path, el) {
  document.querySelectorAll('.file-item').forEach(function(item) { item.classList.remove('active'); });
  if (el) el.classList.add('active');
  kwSetActiveFile(path);
  var results = document.getElementById('search-results');
  if (results) results.innerHTML = '';
  htmx.ajax('GET', '/ui/file?path=' + encodeURIComponent(path), { target: '#main-content', swap: 'innerHTML' });
  if (window.innerWidth <= 768) closeSidebar();
}

function kwOpenDoc(path) {
  if (window.location.pathname === '/' && typeof loadFile === 'function') {
    loadFile(path);
    kwCloseAccountMenu();
    return;
  }
  window.location.href = '/?file=' + encodeURIComponent(path);
}

function kwCloseAccountMenu() {
  var account = document.querySelector('.sidebar-account');
  var menu = document.getElementById('sidebar-account-menu');
  var button = document.querySelector('.sidebar-account-button');
  if (account) account.classList.remove('open');
  if (menu) {
    menu.setAttribute('aria-hidden', 'true');
    menu.inert = true;
  }
  if (button) button.setAttribute('aria-expanded', 'false');
}

function kwToggleAccountMenu() {
  var account = document.querySelector('.sidebar-account');
  var menu = document.getElementById('sidebar-account-menu');
  var button = document.querySelector('.sidebar-account-button');
  if (!account || !menu || !button) return;
  var isOpen = account.classList.toggle('open');
  menu.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
  menu.inert = !isOpen;
  button.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
}

function kwSyncSidebarForViewport() {
  var s = document.querySelector('.sidebar');
  if (!s) return;
  if (kwIsMobileSidebar()) {
    s.style.transform = s.classList.contains('open') ? 'translateX(0)' : 'translateX(-100%)';
  } else {
    s.style.transform = '';
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', kwSyncSidebarForViewport);
} else {
  kwSyncSidebarForViewport();
}
window.addEventListener('resize', kwSyncSidebarForViewport);
window.addEventListener('pageshow', kwSyncSidebarForViewport);

/* ── Tree-State Persistenz (localStorage) ──────────────────────────── */
var KW_LS_OPEN     = 'kiwiki:openFolders';
var KW_LS_ACTIVE   = 'kiwiki:activeFile';
var KW_LS_SCROLL   = 'kiwiki:treeScroll';

function kwGetOpenFolders() {
  try {
    var raw = localStorage.getItem(KW_LS_OPEN);
    if (!raw) return [];
    var arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch (e) { return []; }
}
function kwSaveOpenFolders(arr) {
  try { localStorage.setItem(KW_LS_OPEN, JSON.stringify(arr)); } catch (e) {}
}
function kwAddOpenFolder(path) {
  var arr = kwGetOpenFolders();
  if (arr.indexOf(path) === -1) { arr.push(path); kwSaveOpenFolders(arr); }
}
function kwRemoveOpenFolder(path) {
  // path + alle Sub-Pfade entfernen
  var prefix = path + '/';
  var arr = kwGetOpenFolders().filter(function(p) {
    return p !== path && p.indexOf(prefix) !== 0;
  });
  kwSaveOpenFolders(arr);
}
function kwSetActiveFile(path) {
  try { localStorage.setItem(KW_LS_ACTIVE, path || ''); } catch (e) {}
}
function kwGetActiveFile() {
  try { return localStorage.getItem(KW_LS_ACTIVE) || ''; } catch (e) { return ''; }
}
function kwSaveTreeScroll() {
  var tree = document.getElementById('file-tree');
  if (!tree) return;
  try { localStorage.setItem(KW_LS_SCROLL, String(tree.scrollTop)); } catch (e) {}
}
function kwRestoreTreeScroll() {
  var tree = document.getElementById('file-tree');
  if (!tree) return;
  try {
    var v = parseInt(localStorage.getItem(KW_LS_SCROLL) || '0', 10);
    if (!isNaN(v) && v >= 0) tree.scrollTop = v;
  } catch (e) {}
}

// Expandiert einen einzelnen Ordner-Pfad. Gibt Promise zurück, das resolved,
// wenn der Subtree geladen ist (oder die Row nicht gefunden wurde).
function kwExpandFolderByPath(path) {
  return new Promise(function(resolve) {
    var row = document.querySelector('.tree-row[data-kind="dir"][data-path="' + CSS.escape(path) + '"]');
    if (!row) { resolve(false); return; }
    if (row.classList.contains('open')) { resolve(true); return; }
    var subtree = row.nextElementSibling;
    if (!subtree || !subtree.classList.contains('subtree')) { resolve(false); return; }
    row.classList.add('open');
    htmx.ajax('GET', '/ui/files?path=' + encodeURIComponent(path), {
      target: '#' + subtree.id, swap: 'innerHTML'
    }).then(function() { resolve(true); })
      .catch(function() { resolve(false); });
  });
}

// Markiert die zuletzt aktive Datei im Tree (sofern sichtbar).
function kwMarkActiveFile() {
  var path = kwGetActiveFile();
  if (!path) return;
  document.querySelectorAll('.file-item.active').forEach(function(el) { el.classList.remove('active'); });
  var item = document.querySelector('.tree-row[data-kind="file"][data-path="' + CSS.escape(path) + '"] > .file-item');
  if (item) item.classList.add('active');
}

// Stellt aufgeklappte Ordner + Scroll-Position + Active-Marker wieder her.
// Sequenziell, Eltern vor Kindern (sort by depth).
async function kwRestoreTreeState() {
  if (window.__kwRestoringTree) return;
  window.__kwRestoringTree = true;
  try {
    var paths = kwGetOpenFolders().slice().sort(function(a, b) {
      return a.split('/').length - b.split('/').length || a.localeCompare(b);
    });
    var stale = [];
    for (var i = 0; i < paths.length; i++) {
      var ok = await kwExpandFolderByPath(paths[i]);
      if (!ok) stale.push(paths[i]);
    }
    if (stale.length) {
      var keep = kwGetOpenFolders().filter(function(p) { return stale.indexOf(p) === -1; });
      kwSaveOpenFolders(keep);
    }
    kwMarkActiveFile();
    kwRestoreTreeScroll();
  } finally {
    window.__kwRestoringTree = false;
  }
}

// Hook: nach jedem Top-Level-Tree-Swap (initial Load + nach Create/Move/Delete).
document.addEventListener('htmx:afterSwap', function(e) {
  if (e.target && e.target.id === 'file-tree') kwRestoreTreeState();
});

// Scroll-Position laufend speichern (debounced).
document.addEventListener('DOMContentLoaded', function() {
  var tree = document.getElementById('file-tree');
  if (tree) {
    var t = null;
    tree.addEventListener('scroll', function() {
      if (t) clearTimeout(t);
      t = setTimeout(kwSaveTreeScroll, 150);
    });
    tree.addEventListener('keydown', function(e) {
      var item = e.target.closest && e.target.closest('.file-item');
      if (!item) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        item.click();
      }
    });
  }
  var initialDoc = new URLSearchParams(window.location.search).get('file');
  if (initialDoc && window.location.pathname === '/') {
    loadFile(initialDoc);
    try {
      window.history.replaceState(null, '', '/');
    } catch (_err) {}
  }
});

/* ── Drag & Drop ───────────────────────────────────────────────────── */
// Schreibt persistierte Pfade nach erfolgreichem Move um.
function kwRewritePaths(src, dst) {
  var prefix = src + '/';
  var open = kwGetOpenFolders().map(function(p) {
    if (p === src) return dst;
    if (p.indexOf(prefix) === 0) return dst + '/' + p.slice(prefix.length);
    return p;
  });
  kwSaveOpenFolders(open);
  var active = kwGetActiveFile();
  if (active === src) kwSetActiveFile(dst);
  else if (active.indexOf(prefix) === 0) kwSetActiveFile(dst + '/' + active.slice(prefix.length));
}

function kwBasename(path) {
  var i = path.lastIndexOf('/');
  return i === -1 ? path : path.slice(i + 1);
}

function kwDragTargetFolder(target) {
  // Liefert Ziel-Ordner-Pfad ('' = Root) oder null bei ungültigem Target.
  if (!target) return null;
  var tree = document.getElementById('file-tree');
  if (!tree) return null;
  var row = target.closest && target.closest('.tree-row');
  if (row && row.dataset.kind === 'dir') return row.dataset.path;
  // Datei-Row: Drop landet im Ordner der Datei (oder Root, falls keiner)
  if (row && row.dataset.kind === 'file') {
    var p = row.dataset.path || '';
    var i = p.lastIndexOf('/');
    return i === -1 ? '' : p.slice(0, i);
  }
  // Außerhalb einer Row, aber im Tree → Root
  if (tree.contains(target)) return '';
  return null;
}

function kwIsInvalidMove(src, srcKind, targetFolder) {
  if (targetFolder === null) return true;
  // Ordner in sich selbst oder in eigenes Subdir
  if (srcKind === 'dir' && (targetFolder === src || targetFolder.indexOf(src + '/') === 0)) return true;
  // Schon dort
  var curParent = src.lastIndexOf('/') === -1 ? '' : src.slice(0, src.lastIndexOf('/'));
  if (curParent === targetFolder) return true;
  return false;
}

function kwClearDropMarkers() {
  document.querySelectorAll('.tree-row.drop-target').forEach(function(r) { r.classList.remove('drop-target'); });
  var tree = document.getElementById('file-tree');
  if (tree) tree.classList.remove('drop-target-root');
}

/* ── Dateibaum-Kontextmenü ─────────────────────────────────────────── */
function kwRoleLevel() {
  return (window.KIWIKI && Number(window.KIWIKI.roleLevel)) || Number(window.KIWIKI_ROLE_LEVEL) || 0;
}

function kwCanWrite() { return kwRoleLevel() >= 2; }
function kwCanAdmin() { return kwRoleLevel() >= 3; }

function kwContextForTarget(target) {
  var tree = document.getElementById('file-tree');
  var row = target && target.closest && target.closest('.tree-row');
  if (row) {
    return {
      kind: row.dataset.kind,
      path: row.dataset.path || '',
      row: row,
    };
  }
  var subtree = target && target.closest && target.closest('.subtree');
  if (subtree && subtree.dataset.parent) {
    var parentRow = document.querySelector('.tree-row[data-kind="dir"][data-path="' + CSS.escape(subtree.dataset.parent) + '"]');
    return { kind: 'dir', path: subtree.dataset.parent, row: parentRow };
  }
  if (tree && target && tree.contains(target)) {
    return { kind: 'root', path: '', row: null };
  }
  return null;
}

function kwToggleFolderFromRow(row) {
  if (!row) return;
  var item = row.querySelector('.file-item.folder');
  if (item) item.click();
}

function kwContextActions(ctx) {
  var actions = [];
  if (!ctx) return actions;

  if (ctx.kind === 'file') {
    actions.push({ label: 'Öffnen', run: function() {
      var item = ctx.row && ctx.row.querySelector('.file-item');
      loadFile(ctx.path, item);
    }});
    if (kwCanWrite()) {
      actions.push({ label: 'Bearbeiten', run: function() { openEditor(ctx.path); } });
      actions.push({ label: 'Verschieben', run: function() { moveItem(ctx.path, false); } });
    }
    if (kwCanAdmin()) {
      actions.push({ label: 'Löschen', danger: true, run: function() { deleteFile(ctx.path); } });
    }
  } else if (ctx.kind === 'dir') {
    var isOpen = ctx.row && ctx.row.classList.contains('open');
    actions.push({ label: isOpen ? 'Zuklappen' : 'Aufklappen', run: function() { kwToggleFolderFromRow(ctx.row); } });
    if (kwCanWrite()) {
      actions.push({ label: 'Neue Datei', run: function() { newFileIn(ctx.path); } });
      actions.push({ label: 'Neuer Ordner', run: function() { newFolderIn(ctx.path); } });
      actions.push({ label: 'Verschieben', run: function() { moveItem(ctx.path, true); } });
    }
    if (kwCanAdmin()) {
      actions.push({ label: 'Ordner löschen', danger: true, run: function() { deleteFolder(ctx.path); } });
    }
  } else if (ctx.kind === 'root') {
    if (kwCanWrite()) {
      actions.push({ label: 'Neue Datei', run: function() { newFileIn(''); } });
      actions.push({ label: 'Neuer Ordner', run: function() { newFolderIn(''); } });
    }
  }

  if (!actions.length) {
    actions.push({ label: 'Keine Aktionen verfügbar', disabled: true, run: function() {} });
  }
  return actions;
}

function kwCloseTreeContextMenu() {
  var menu = document.querySelector('.kw-context-menu');
  var sheet = document.querySelector('.kw-action-sheet-backdrop');
  document.removeEventListener('keydown', kwContextKeyClose);
  document.removeEventListener('click', kwContextClickClose, true);
  window.removeEventListener('resize', kwCloseTreeContextMenu);
  if (menu) menu.remove();
  if (sheet) sheet.remove();
}

function kwContextKeyClose(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    kwCloseTreeContextMenu();
  }
}

function kwContextClickClose(e) {
  if (e.target.closest && (e.target.closest('.kw-context-menu') || e.target.closest('.kw-action-sheet'))) return;
  kwCloseTreeContextMenu();
}

function kwRunContextAction(action) {
  if (!action || action.disabled) return;
  kwCloseTreeContextMenu();
  action.run();
}

function kwBuildContextButton(action, useButton) {
  var el = document.createElement(useButton ? 'button' : 'div');
  el.className = 'kw-context-item' + (action.danger ? ' danger' : '') + (action.disabled ? ' disabled' : '');
  if (useButton) el.type = 'button';
  el.setAttribute('role', 'menuitem');
  el.textContent = action.label;
  if (action.disabled) el.setAttribute('aria-disabled', 'true');
  el.addEventListener('click', function(e) {
    e.preventDefault();
    e.stopPropagation();
    kwRunContextAction(action);
  });
  return el;
}

function kwOpenDesktopContextMenu(ctx, x, y) {
  var actions = kwContextActions(ctx);
  kwCloseTreeContextMenu();
  var menu = document.createElement('div');
  menu.className = 'kw-context-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', ctx.path ? ('Aktionen für ' + ctx.path) : 'Dateibaum-Aktionen');
  actions.forEach(function(action) { menu.appendChild(kwBuildContextButton(action, true)); });
  document.body.appendChild(menu);

  var rect = menu.getBoundingClientRect();
  var left = Math.min(Math.max(8, x), window.innerWidth - rect.width - 8);
  var top = Math.min(Math.max(8, y), window.innerHeight - rect.height - 8);
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
  var firstItem = menu.querySelector('.kw-context-item:not(.disabled)');
  if (firstItem) firstItem.focus();

  setTimeout(function() {
    document.addEventListener('click', kwContextClickClose, true);
    document.addEventListener('keydown', kwContextKeyClose);
    window.addEventListener('resize', kwCloseTreeContextMenu);
  }, 0);
}

function kwOpenMobileActionSheet(ctx) {
  var actions = kwContextActions(ctx);
  kwCloseTreeContextMenu();
  var backdrop = document.createElement('div');
  backdrop.className = 'kw-action-sheet-backdrop';
  backdrop.innerHTML = ''
    + '<div class="kw-action-sheet" role="menu" aria-label="Dateibaum-Aktionen">'
    + '<div class="kw-action-sheet-handle" aria-hidden="true"></div>'
    + '<div class="kw-action-sheet-title">' + escapeHtml(ctx.path || 'Dateibaum') + '</div>'
    + '<div class="kw-action-sheet-actions"></div>'
    + '<button type="button" class="kw-action-sheet-cancel">Abbrechen</button>'
    + '</div>';
  var list = backdrop.querySelector('.kw-action-sheet-actions');
  actions.forEach(function(action) { list.appendChild(kwBuildContextButton(action, true)); });
  backdrop.querySelector('.kw-action-sheet-cancel').addEventListener('click', kwCloseTreeContextMenu);
  backdrop.addEventListener('click', function(e) {
    if (e.target === backdrop) kwCloseTreeContextMenu();
  });
  document.body.appendChild(backdrop);
  requestAnimationFrame(function() { backdrop.classList.add('open'); });
  document.addEventListener('keydown', kwContextKeyClose);
}

function kwOpenTreeContext(ctx, x, y) {
  if (!ctx) return;
  if (kwIsMobileSidebar()) kwOpenMobileActionSheet(ctx);
  else kwOpenDesktopContextMenu(ctx, x, y);
}

function kwInitTreeContextMenu() {
  var tree = document.getElementById('file-tree');
  if (!tree || tree.dataset.kwContextBound === '1') return;
  tree.dataset.kwContextBound = '1';

  var longPressTimer = null;
  var longPressCtx = null;
  var longPressStart = null;
  var longPressPointerId = null;

  function clearLongPress() {
    if (longPressTimer) clearTimeout(longPressTimer);
    longPressTimer = null;
    longPressCtx = null;
    longPressStart = null;
    longPressPointerId = null;
  }

  tree.addEventListener('contextmenu', function(e) {
    var ctx = kwContextForTarget(e.target);
    if (!ctx) return;
    e.preventDefault();
    kwOpenTreeContext(ctx, e.clientX, e.clientY);
  });

  tree.addEventListener('click', function(e) {
    if (!window.__kwSuppressNextTreeClick) return;
    e.preventDefault();
    e.stopPropagation();
    window.__kwSuppressNextTreeClick = false;
  }, true);

  tree.addEventListener('keydown', function(e) {
    if (!(e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey))) return;
    var ctx = kwContextForTarget(e.target);
    if (!ctx) return;
    e.preventDefault();
    var row = ctx.row || tree;
    var rect = row.getBoundingClientRect();
    kwOpenTreeContext(ctx, rect.left + Math.min(rect.width - 12, 36), rect.top + Math.min(rect.height - 8, 28));
  });

  tree.addEventListener('pointerdown', function(e) {
    if (e.pointerType !== 'touch' || e.button !== 0) return;
    var ctx = kwContextForTarget(e.target);
    if (!ctx) return;
    clearLongPress();
    longPressCtx = ctx;
    longPressPointerId = e.pointerId;
    longPressStart = { x: e.clientX, y: e.clientY };
    longPressTimer = setTimeout(function() {
      window.__kwSuppressNextTreeClick = true;
      longPressTimer = null;
      if (navigator.vibrate) {
        try { navigator.vibrate(8); } catch (_err) {}
      }
      kwOpenMobileActionSheet(longPressCtx);
      setTimeout(function() { window.__kwSuppressNextTreeClick = false; }, 450);
    }, 520);
  });

  tree.addEventListener('pointermove', function(e) {
    if (!longPressTimer || e.pointerId !== longPressPointerId || !longPressStart) return;
    var dx = Math.abs(e.clientX - longPressStart.x);
    var dy = Math.abs(e.clientY - longPressStart.y);
    if (dx > 10 || dy > 10) clearLongPress();
  });

  tree.addEventListener('pointerup', clearLongPress);
  tree.addEventListener('pointercancel', clearLongPress);
  tree.addEventListener('pointerleave', clearLongPress);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', kwInitTreeContextMenu);
} else {
  kwInitTreeContextMenu();
}

document.addEventListener('DOMContentLoaded', function() {
  var tree = document.getElementById('file-tree');
  if (!tree) return;

  tree.addEventListener('dragstart', function(e) {
    if (kwIsMobileSidebar()) { e.preventDefault(); return; }
    var row = e.target.closest && e.target.closest('.tree-row');
    if (!row) return;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('application/x-kiwiki-path', row.dataset.path);
    e.dataTransfer.setData('application/x-kiwiki-kind', row.dataset.kind);
    // Fallback für Browser, die nur text/plain in dragover lesen können
    e.dataTransfer.setData('text/plain', row.dataset.path);
    row.classList.add('dragging');
    window.__kwDragSrc = { path: row.dataset.path, kind: row.dataset.kind };
  });

  tree.addEventListener('dragend', function() {
    document.querySelectorAll('.tree-row.dragging').forEach(function(r) { r.classList.remove('dragging'); });
    kwClearDropMarkers();
    window.__kwDragSrc = null;
  });

  tree.addEventListener('dragover', function(e) {
    var src = window.__kwDragSrc;
    if (!src) return;
    var folder = kwDragTargetFolder(e.target);
    if (kwIsInvalidMove(src.path, src.kind, folder)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    kwClearDropMarkers();
    if (folder === '') tree.classList.add('drop-target-root');
    else {
      var row = document.querySelector('.tree-row[data-kind="dir"][data-path="' + CSS.escape(folder) + '"]');
      if (row) row.classList.add('drop-target');
    }
  });

  tree.addEventListener('dragleave', function(e) {
    // Nur clearen, wenn wir den Tree komplett verlassen
    if (!tree.contains(e.relatedTarget)) kwClearDropMarkers();
  });

  tree.addEventListener('drop', function(e) {
    var src = window.__kwDragSrc;
    if (!src) return;
    var folder = kwDragTargetFolder(e.target);
    if (kwIsInvalidMove(src.path, src.kind, folder)) { kwClearDropMarkers(); return; }
    e.preventDefault();
    kwClearDropMarkers();
    var name = kwBasename(src.path);
    var dst = folder ? (folder + '/' + name) : name;
    fetch('/api/move', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ src: src.path, dst: dst }),
    }).then(function(r) {
      if (r.ok) {
        kwRewritePaths(src.path, dst);
        // Ziel-Ordner aufklappen, damit man das verschobene Item sieht
        if (folder) kwAddOpenFolder(folder);
        kwToast('Verschoben nach: ' + dst);
        htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
      } else {
        r.json().then(function(d) { kwToast('Fehler: ' + (d.detail || r.status), { type: 'error' }); });
      }
    }).catch(function(err) { kwToast('Netzwerkfehler: ' + err, { type: 'error' }); });
  });
});
function openEditor(path) {
  window.location.href = '/editor?path=' + encodeURIComponent(path);
}
async function newFileIn(folderPath) {
  var name = await kwPrompt({
    title: 'Neue Datei',
    message: folderPath ? 'Dateiname in <code>' + escapeHtml(folderPath) + '</code>:' : 'Dateiname (z. B. <code>meine-notiz.md</code>):',
    placeholder: 'meine-notiz.md',
    submitLabel: 'Erstellen',
  });
  if (!name) return;
  if (!name.endsWith('.md')) name += '.md';
  var fullPath = folderPath ? folderPath + '/' + name : name;
  window.location.href = '/editor?path=' + encodeURIComponent(fullPath);
}
async function newFolderIn(parentPath) {
  var name = await kwPrompt({
    title: 'Neuer Ordner',
    message: parentPath ? 'Ordnername in <code>' + escapeHtml(parentPath) + '</code>:' : 'Ordnerpfad:',
    placeholder: parentPath ? 'unterordner' : 'notes/projekt',
    submitLabel: 'Anlegen',
  });
  if (!name) return;
  var fullPath = parentPath ? parentPath + '/' + name : name;
  fetch('/api/folder', {
    method: 'POST',
    headers: apiHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ path: fullPath }),
  }).then(function(r) {
    if (r.ok) {
      kwToast('Ordner angelegt: ' + fullPath);
      htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
    } else r.json().then(function(d) { kwToast('Fehler: ' + (d.detail || r.status), { type: 'error' }); });
  }).catch(function(e) { kwToast('Netzwerkfehler: ' + e, { type: 'error' }); });
}
function promptCreateFolder() { newFolderIn(''); }
async function deleteFile(path) {
  var ok = await kwConfirm({
    title: 'Datei löschen?',
    message: 'Möchtest du <code>' + escapeHtml(path) + '</code> wirklich löschen?',
    confirmLabel: 'Löschen',
    danger: true,
  });
  if (!ok) return;
  fetch('/api/file?path=' + encodeURIComponent(path), {
    method: 'DELETE', headers: apiHeaders(),
  }).then(function(r) {
    if (r.ok) {
      showDeletedEmptyState('Datei', path);
      htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
      kwToast('Datei gelöscht: ' + path);
    } else r.json().then(function(d) { kwToast('Fehler: ' + (d.detail || r.status), { type: 'error' }); });
  }).catch(function(e) { kwToast('Netzwerkfehler: ' + e, { type: 'error' }); });
}
async function deleteFolder(path) {
  var ok = await kwConfirm({
    title: 'Ordner löschen?',
    message: 'Der Ordner <code>' + escapeHtml(path) + '</code> und <strong>alle enthaltenen Dateien</strong> werden unwiderruflich entfernt.',
    confirmLabel: 'Alles löschen',
    danger: true,
  });
  if (!ok) return;
  fetch('/api/folder?path=' + encodeURIComponent(path), {
    method: 'DELETE', headers: apiHeaders(),
  }).then(function(r) {
    if (r.ok) {
      showDeletedEmptyState('Ordner', path);
      htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
      kwToast('Ordner gelöscht: ' + path);
    } else r.json().then(function(d) { kwToast('Fehler: ' + (d.detail || r.status), { type: 'error' }); });
  }).catch(function(e) { kwToast('Netzwerkfehler: ' + e, { type: 'error' }); });
}
async function moveItem(path, isDir) {
  var label = isDir ? 'Ordner' : 'Datei';
  var dst = await kwPrompt({
    title: label + ' verschieben',
    message: 'Neuer Pfad für <code>' + escapeHtml(path) + '</code>:',
    defaultValue: path,
    placeholder: path,
    submitLabel: 'Verschieben',
  });
  if (!dst || dst === path) return;
  fetch('/api/move', {
    method: 'POST',
    headers: apiHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ src: path, dst: dst }),
  }).then(function(r) {
    if (r.ok) {
      kwToast('Verschoben nach: ' + dst);
      htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
    } else r.json().then(function(d) { kwToast('Fehler: ' + (d.detail || r.status), { type: 'error' }); });
  }).catch(function(e) { kwToast('Netzwerkfehler: ' + e, { type: 'error' }); });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function(c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}

function showDeletedEmptyState(label, path) {
  document.getElementById('main-content').innerHTML =
    '<div class="empty-state"><p>' + label + ' <strong>' + escapeHtml(path) + '</strong> gelöscht.</p></div>';
}

/* ── Dashboard-Dialoge (Ersatz für Browser-Popups) ─────────────────── */
var KW_ICONS = {
  info:    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  warn:    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  edit:    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  check:   '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  error:   '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
};

// opts.message wird als HTML in den Dialog eingefügt — Caller muss alle
// untrusted Werte (User-Pfade, API-Antworten) selbst via escapeHtml() escapen.
// opts.title / opts.placeholder / opts.defaultValue / Button-Labels werden hier escaped.
function kwDialog(opts) {
  return new Promise(function(resolve) {
    var backdrop = document.createElement('div');
    backdrop.className = 'kw-modal-backdrop';

    var iconKey = opts.danger ? 'warn' : (opts.iconKey || (opts.input ? 'edit' : 'info'));
    var html = ''
      + '<div class="kw-modal' + (opts.danger ? ' danger' : '') + '" role="dialog" aria-modal="true">'
      + '<div class="kw-modal-icon">' + (KW_ICONS[iconKey] || KW_ICONS.info) + '</div>'
      + '<div class="kw-modal-title">' + escapeHtml(opts.title || '') + '</div>'
      + '<div class="kw-modal-msg">' + (opts.message || '') + '</div>'
      + (opts.input
          ? '<input type="text" class="kw-modal-input" placeholder="' + escapeHtml(opts.placeholder || '') + '" value="' + escapeHtml(opts.defaultValue || '') + '">'
          : '')
      + '<div class="kw-modal-actions">'
      + (opts.cancelLabel !== null
          ? '<button type="button" class="kw-btn kw-btn-secondary" data-action="cancel">' + escapeHtml(opts.cancelLabel || 'Abbrechen') + '</button>'
          : '')
      + '<button type="button" class="kw-btn ' + (opts.danger ? 'kw-btn-danger' : 'kw-btn-primary') + '" data-action="ok">' + escapeHtml(opts.confirmLabel || opts.submitLabel || 'OK') + '</button>'
      + '</div>'
      + '</div>';
    backdrop.innerHTML = html;
    document.body.appendChild(backdrop);

    var input = backdrop.querySelector('.kw-modal-input');
    var okBtn = backdrop.querySelector('[data-action="ok"]');
    var cancelBtn = backdrop.querySelector('[data-action="cancel"]');
    var settled = false;

    function close(result) {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey);
      backdrop.classList.remove('open');
      setTimeout(function() { backdrop.remove(); }, 180);
      resolve(result);
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(opts.input ? null : false); }
      else if (e.key === 'Enter' && (!input || document.activeElement === input || document.activeElement === okBtn)) {
        e.preventDefault();
        close(opts.input ? (input.value.trim() || null) : true);
      }
    }
    okBtn.addEventListener('click', function() {
      close(opts.input ? (input.value.trim() || null) : true);
    });
    if (cancelBtn) cancelBtn.addEventListener('click', function() { close(opts.input ? null : false); });
    backdrop.addEventListener('click', function(e) {
      if (e.target === backdrop) close(opts.input ? null : false);
    });
    document.addEventListener('keydown', onKey);

    requestAnimationFrame(function() {
      backdrop.classList.add('open');
      if (input) { input.focus(); input.select(); }
      else okBtn.focus();
    });
  });
}

function kwConfirm(opts) {
  return kwDialog(Object.assign({ confirmLabel: 'Bestätigen' }, opts || {}, { input: false }));
}
function kwPrompt(opts) {
  return kwDialog(Object.assign({ submitLabel: 'OK' }, opts || {}, { input: true }));
}
function kwAlert(opts) {
  if (typeof opts === 'string') opts = { message: opts };
  return kwDialog(Object.assign({ title: 'Hinweis', cancelLabel: null, confirmLabel: 'OK' }, opts || {}, { input: false }));
}

function kwToast(message, options) {
  options = options || {};
  var stack = document.getElementById('kw-toast-stack');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'kw-toast-stack';
    stack.className = 'kw-toast-stack';
    document.body.appendChild(stack);
  }
  var t = document.createElement('div');
  var isError = options.type === 'error';
  t.className = 'kw-toast' + (isError ? ' error' : '');
  t.innerHTML = '<span class="kw-toast-icon">' + (isError ? KW_ICONS.error : KW_ICONS.check) + '</span>'
              + '<span class="kw-toast-msg">' + escapeHtml(message) + '</span>';
  stack.appendChild(t);
  requestAnimationFrame(function() { t.classList.add('show'); });
  var ttl = options.duration || (isError ? 4200 : 2600);
  setTimeout(function() {
    t.classList.remove('show');
    setTimeout(function() { t.remove(); }, 220);
  }, ttl);
}
function toggleFolder(el, path, treeId) {
  var subtree = document.getElementById(treeId);
  var row = el.closest('.tree-row');
  if (row.classList.contains('open')) {
    subtree.innerHTML = '';
    row.classList.remove('open');
    el.setAttribute('aria-expanded', 'false');
    kwRemoveOpenFolder(path);
  } else {
    row.classList.add('open');
    el.setAttribute('aria-expanded', 'true');
    kwAddOpenFolder(path);
    htmx.ajax('GET', '/ui/files?path=' + encodeURIComponent(path), {
      target: '#' + treeId, swap: 'innerHTML'
    }).then(function() {
      // Wenn ein Sub-Ordner laut Persistenz offen sein soll, hier nachholen.
      if (!window.__kwRestoringTree) kwRestoreTreeState();
    });
  }
}

function kwInitSidebarResizer() {
  var resizer   = document.getElementById('sidebar-resizer');
  var sidebar   = document.querySelector('.sidebar');
  var logoWrap  = document.querySelector('.logo-wrap');
  var dragging = false, startX, startW;

  if (!resizer || !sidebar) return;
  if (resizer.dataset.kwBound === '1') return;
  resizer.dataset.kwBound = '1';

  if (window.innerWidth > 768) {
    var savedW = parseInt(localStorage.getItem('kiwiki_sidebar_w'), 10);
    if (savedW && savedW > 0) {
      sidebar.style.width  = savedW + 'px';
      if (logoWrap) logoWrap.style.width = savedW + 'px';
    }
  }

  function startDrag(e) {
    if (e.pointerType && e.pointerType !== 'mouse' && e.pointerType !== 'pen') return;
    dragging = true;
    startX   = e.clientX;
    startW   = sidebar.getBoundingClientRect().width;
    resizer.classList.add('dragging');
    document.body.style.cursor    = 'col-resize';
    document.body.style.userSelect = 'none';
    if (resizer.setPointerCapture && e.pointerId !== undefined) {
      try { resizer.setPointerCapture(e.pointerId); } catch (_err) {}
    }
    e.preventDefault();
  }

  function moveDrag(e) {
    if (!dragging) return;
    var w = Math.max(1, startW + e.clientX - startX);
    sidebar.style.width = w + 'px';
    if (logoWrap) logoWrap.style.width = w + 'px';
  }

  function endDrag() {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove('dragging');
    document.body.style.cursor     = '';
    document.body.style.userSelect = '';
    localStorage.setItem('kiwiki_sidebar_w', parseInt(sidebar.style.width, 10));
  }

  resizer.addEventListener('pointerdown', startDrag);
  document.addEventListener('pointermove', moveDrag);
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);

  resizer.addEventListener('mousedown', startDrag);
  document.addEventListener('mousemove', moveDrag);
  document.addEventListener('mouseup', endDrag);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', kwInitSidebarResizer);
} else {
  kwInitSidebarResizer();
}

document.addEventListener('click', function(e) {
  var results = document.getElementById('search-results');
  if (results && !results.closest('form').contains(e.target)) {
    results.innerHTML = '';
  }
  var account = document.querySelector('.sidebar-account');
  if (account && !account.contains(e.target)) kwCloseAccountMenu();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    kwCloseAccountMenu();
    var results = document.getElementById('search-results');
    if (results && results.innerHTML) {
      results.innerHTML = '';
      var input = document.querySelector('.search-input');
      if (input) input.focus();
    }
  }
});

window.addEventListener('unhandledrejection', function(e) {
  if (typeof kwToast === 'function') {
    kwToast('Unerwarteter Fehler: ' + (e.reason && e.reason.message || e.reason || 'unbekannt'), { type: 'error' });
  }
});

/* ── Sidebar Tree Filter ──────────────────────────────────────────── */
function kwFilterTree(query) {
  var tree = document.getElementById('file-tree');
  if (!tree) return;
  var q = query.toLowerCase().trim();
  var rows = tree.querySelectorAll('.tree-row');
  rows.forEach(function(row) {
    if (!q) { row.style.display = ''; return; }
    var name = row.querySelector('.item-name');
    var match = name && name.textContent.toLowerCase().indexOf(q) !== -1;
    row.style.display = match ? '' : 'none';
    if (match) {
      var parent = row.parentElement;
      while (parent && parent !== tree) {
        if (parent.classList && parent.classList.contains('subtree') && parent.dataset.parent) {
          var parentRow = document.querySelector('.tree-row[data-kind="dir"][data-path="' + CSS.escape(parent.dataset.parent) + '"]');
          if (parentRow) { parentRow.style.display = ''; parentRow.classList.add('open'); }
        }
        parent = parent.parentElement;
      }
    }
  });
}

/* ── Copy Path ────────────────────────────────────────────────────── */
function kwCopyPath(path) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(path).then(function() {
      kwToast('Pfad kopiert: ' + path);
    }).catch(function() { kwCopyPathFallback(path); });
  } else { kwCopyPathFallback(path); }
}
function kwCopyPathFallback(path) {
  var ta = document.createElement('textarea');
  ta.value = path;
  ta.style.cssText = 'position:fixed;left:-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); kwToast('Pfad kopiert: ' + path); } catch(e) { kwToast('Kopieren fehlgeschlagen', {type:'error'}); }
  ta.remove();
}

/* ── Inline Rename ────────────────────────────────────────────────── */
function kwInlineRename(path) {
  var row = document.querySelector('.tree-row[data-path="' + CSS.escape(path) + '"]');
  if (!row) return;
  var item = row.querySelector('.file-item');
  var nameEl = row.querySelector('.item-name');
  if (!item || !nameEl) return;
  var oldName = nameEl.textContent;
  var input = document.createElement('input');
  input.type = 'text';
  input.className = 'tree-rename-input';
  input.value = oldName;
  input.setAttribute('aria-label', 'Neuer Name');
  nameEl.replaceWith(input);
  input.focus();
  input.select();
  function finish() {
    var newName = input.value.trim() || oldName;
    var span = document.createElement('span');
    span.className = 'item-name';
    span.textContent = newName;
    input.replaceWith(span);
    if (newName === oldName) return;
    var parts = path.split('/');
    parts[parts.length - 1] = newName;
    var newPath = parts.join('/');
    fetch('/ui/rename', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/x-www-form-urlencoded' }),
      body: 'old_path=' + encodeURIComponent(path) + '&new_path=' + encodeURIComponent(newPath),
    }).then(function(r) {
      if (r.ok) {
        row.dataset.path = newPath;
        row.querySelector('.file-item').dataset.path = newPath;
        var cb = row.querySelector('.tree-checkbox');
        if (cb) cb.dataset.path = newPath;
        kwToast('Umbenannt: ' + newName);
        htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
      } else { kwToast('Fehler beim Umbenennen', {type:'error'}); htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' }); }
    }).catch(function() { kwToast('Netzwerkfehler', {type:'error'}); });
  }
  input.addEventListener('blur', finish);
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); finish(); }
    if (e.key === 'Escape') { input.value = oldName; finish(); }
  });
}
document.addEventListener('dblclick', function(e) {
  var nameEl = e.target.closest && e.target.closest('.item-name');
  if (!nameEl) return;
  var row = nameEl.closest && nameEl.closest('.tree-row');
  if (!row || !row.dataset.path) return;
  e.preventDefault();
  e.stopPropagation();
  kwInlineRename(row.dataset.path);
}, true);

/* ── Multi-Select ─────────────────────────────────────────────────── */
window.__kwSelected = new Set();
window.__kwSelectMode = false;

function kwToggleSelectMode() {
  window.__kwSelectMode = !window.__kwSelectMode;
  var tree = document.getElementById('file-tree');
  var btn = document.getElementById('select-toggle');
  if (tree) tree.classList.toggle('select-mode', window.__kwSelectMode);
  if (btn) {
    btn.classList.toggle('active', window.__kwSelectMode);
    btn.setAttribute('aria-pressed', window.__kwSelectMode ? 'true' : 'false');
  }
  if (!window.__kwSelectMode) kwClearSelection();
}

function kwToggleSelect(cb) {
  var path = cb.dataset.path;
  if (cb.checked) { window.__kwSelected.add(path); cb.closest('.tree-row').classList.add('selected'); }
  else { window.__kwSelected.delete(path); cb.closest('.tree-row').classList.remove('selected'); }
  kwUpdateSelectionBar();
}
function kwClearSelection() {
  window.__kwSelected.clear();
  document.querySelectorAll('.tree-checkbox').forEach(function(cb) { cb.checked = false; });
  document.querySelectorAll('.tree-row.selected').forEach(function(r) { r.classList.remove('selected'); });
  kwUpdateSelectionBar();
}
function kwUpdateSelectionBar() {
  var bar = document.getElementById('selection-bar');
  var count = document.getElementById('selection-count');
  if (!bar || !count) return;
  var n = window.__kwSelected.size;
  if (n === 0) { bar.hidden = true; return; }
  bar.hidden = false;
  count.textContent = n + (n === 1 ? ' ausgewählt' : ' ausgewählt');
}
function kwGetSelected() { return Array.from(window.__kwSelected); }

/* ── Batch Actions ────────────────────────────────────────────────── */
async function kwBatchDelete() {
  var paths = kwGetSelected();
  if (!paths.length) return;
  var ok = await kwConfirm({
    title: paths.length + ' Dateien löschen?',
    message: 'Alle ausgewählten Dateien werden <strong>unwiderruflich</strong> gelöscht.',
    confirmLabel: 'Alles löschen',
    danger: true,
  });
  if (!ok) return;
  var done = 0, failed = 0;
  for (var i = 0; i < paths.length; i++) {
    try {
      var r = await fetch('/api/file?path=' + encodeURIComponent(paths[i]), { method: 'DELETE', headers: apiHeaders() });
      if (r.ok) done++; else failed++;
    } catch(e) { failed++; }
  }
  kwClearSelection();
  htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
  kwToast(done + ' gelöscht' + (failed ? ', ' + failed + ' fehlgeschlagen' : ''));
}
async function kwBatchMove() {
  var paths = kwGetSelected();
  if (!paths.length) return;
  var dst = await kwPrompt({
    title: paths.length + ' Dateien verschieben',
    message: 'Zielordner:',
    placeholder: 'notes/projekt',
    submitLabel: 'Verschieben',
  });
  if (!dst) return;
  var done = 0, failed = 0;
  for (var i = 0; i < paths.length; i++) {
    var name = paths[i].split('/').pop();
    var target = dst + '/' + name;
    try {
      var r = await fetch('/api/move', {
        method: 'POST',
        headers: apiHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ src: paths[i], dst: target }),
      });
      if (r.ok) done++; else failed++;
    } catch(e) { failed++; }
  }
  kwClearSelection();
  htmx.ajax('GET', '/ui/files?path=.', { target: '#file-tree', swap: 'innerHTML' });
  kwToast(done + ' verschoben' + (failed ? ', ' + failed + ' fehlgeschlagen' : ''));
}
async function kwBatchTag() {
  var paths = kwGetSelected();
  if (!paths.length) return;
  var tags = await kwPrompt({
    title: 'Tags setzen',
    message: 'Kommaseparierte Tags (werden zu bestehenden hinzugefügt):',
    placeholder: 'python, refactor',
    submitLabel: 'Setzen',
  });
  if (!tags) return;
  var tagList = tags.split(',').map(function(t){return t.trim()}).filter(Boolean);
  var done = 0;
  for (var i = 0; i < paths.length; i++) {
    try {
      var fc = await fetch('/api/file?path=' + encodeURIComponent(paths[i])).then(function(r){return r.json()});
      var existing = (fc.frontmatter && fc.frontmatter.tags) || [];
      var merged = existing.concat(tagList.filter(function(t){return existing.indexOf(t)===-1}));
      await fetch('/api/file', {
        method: 'PUT',
        headers: apiHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ path: paths[i], content: fc.content.replace(/^---[\s\S]*?---\n/, '---\n' + merged.map(function(t){return 'tags: [' + merged.join(', ') + ']'}).join('\n') + '\n---\n') }),
      });
      done++;
    } catch(e) {}
  }
  kwToast(done + ' Dateien getaggt');
}
function kwExportSelected() {
  var paths = kwGetSelected();
  if (!paths.length) return;
  var form = document.createElement('form');
  form.method = 'POST';
  form.action = '/ui/export';
  form.style.display = 'none';
  var input = document.createElement('input');
  input.type = 'hidden';
  input.name = 'paths';
  input.value = paths.join(',');
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
  form.remove();
}
function kwExportFile(path) {
  var form = document.createElement('form');
  form.method = 'POST';
  form.action = '/ui/export';
  form.style.display = 'none';
  var input = document.createElement('input');
  input.type = 'hidden';
  input.name = 'paths';
  input.value = path;
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
  form.remove();
}

/* ── Keyboard Shortcuts ───────────────────────────────────────────── */
(function() {
  var shortcuts = { dd: 'delete', mm: 'move', ee: 'edit', rr: 'rename' };
  var buffer = '';
  var timer = null;
  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
    buffer += e.key;
    if (timer) clearTimeout(timer);
    timer = setTimeout(function() { buffer = ''; }, 600);
    var active = document.querySelector('.tree-row .file-item.active');
    if (!active) return;
    var path = active.dataset.path;
    var row = active.closest('.tree-row');
    var kind = row && row.dataset.kind;
    if (buffer === 'dd' && kind === 'file' && kwCanAdmin()) { deleteFile(path); buffer = ''; }
    else if (buffer === 'dd' && kind === 'dir' && kwCanAdmin()) { deleteFolder(path); buffer = ''; }
    else if (buffer === 'mm') { moveItem(path, kind === 'dir'); buffer = ''; }
    else if (buffer === 'ee' && kind === 'file' && kwCanWrite()) { openEditor(path); buffer = ''; }
    else if (buffer === 'rr') { kwInlineRename(path); buffer = ''; }
  });
  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
    if (e.key === 'Escape') { kwClearSelection(); }
  });
})();

/* ── Toggle Folder by Name (for breadcrumb) ───────────────────────── */
function toggleFolderByName(path) {
  var row = document.querySelector('.tree-row[data-kind="dir"][data-path="' + CSS.escape(path) + '"]');
  if (!row) { window.location.href = '/?file=' + encodeURIComponent(path); return; }
  var item = row.querySelector('.file-item.folder');
  if (item) item.click();
}
