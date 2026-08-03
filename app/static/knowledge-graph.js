/* Interaktiver 3D-Synapsen-Atlas. Self-hosted, ohne externe Render-Abhängigkeiten. */
(function () {
  'use strict';

  var canvas = document.getElementById('knowledge-graph');
  if (!canvas) return;

  var viewport = document.getElementById('knowledge-viewport');
  var context = canvas.getContext('2d', { alpha: false, desynchronized: true });
  var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var language = document.documentElement.lang === 'en' ? 'en' : 'de';
  var copy = {
    de: {
      loaded: function (nodes, edges) { return nodes + ' Knoten und ' + edges + ' Verbindungen geladen.'; },
      document: 'Dokument', tag: 'Tag', relation: 'Bezug', derived: 'Abgeleitete Verbindung',
      connections: 'Verbindungen', type: 'Typ', owner: 'Besitzer', empty: '–',
      follow: 'Verbindungen verfolgen', showAll: 'Gesamtes Netz zeigen',
      pause: 'Bewegung pausieren', resume: 'Bewegung fortsetzen',
      selected: function (label, count) { return label + ', ' + count + ' Verbindungen.'; },
      rebuilding: 'Wird aufgebaut…', retry: 'Erneut versuchen'
    },
    en: {
      loaded: function (nodes, edges) { return nodes + ' nodes and ' + edges + ' connections loaded.'; },
      document: 'Document', tag: 'Tag', relation: 'Relation', derived: 'Derived connection',
      connections: 'Connections', type: 'Type', owner: 'Owner', empty: '–',
      follow: 'Explore connections', showAll: 'Show entire network',
      pause: 'Pause motion', resume: 'Resume motion',
      selected: function (label, count) { return label + ', ' + count + ' connections.'; },
      rebuilding: 'Rebuilding…', retry: 'Try again'
    }
  }[language];
  var state = {
    nodes: [], edges: [], nodeById: new Map(), adjacency: new Map(),
    width: 1, height: 1, dpr: 1, yaw: -0.35, pitch: 0.18, distance: 720,
    targetX: 0, targetY: 0, targetZ: 0, selected: null, hovered: null,
    dragging: false, moved: false, lastX: 0, lastY: 0, paused: prefersReducedMotion,
    neighborhoodOnly: false, frame: 0, lastTime: performance.now(), settled: false
  };

  var palette = {
    background: '#171713', document: '#b8df78', tag: '#d6ab6d',
    concept: '#9faaa0', line: 'rgba(184, 223, 120, .13)',
    lineHot: 'rgba(196, 234, 134, .72)', text: '#f0e9dc', muted: '#a99d8d'
  };

  function hash(value) {
    var result = 2166136261;
    for (var i = 0; i < value.length; i += 1) {
      result ^= value.charCodeAt(i);
      result = Math.imul(result, 16777619);
    }
    return result >>> 0;
  }

  function seededPosition(id, index, total) {
    var seed = hash(id);
    var phi = Math.acos(1 - 2 * ((index + 0.5) / Math.max(total, 1)));
    var theta = Math.PI * (1 + Math.sqrt(5)) * index + (seed % 1000) / 1000;
    var radius = 160 + (seed % 190);
    return {
      x: radius * Math.sin(phi) * Math.cos(theta),
      y: radius * Math.cos(phi) * 0.76,
      z: radius * Math.sin(phi) * Math.sin(theta),
      vx: 0, vy: 0, vz: 0
    };
  }

  function prepareGraph(payload) {
    state.nodes = payload.nodes.map(function (node, index) {
      var point = seededPosition(node.id, index, payload.nodes.length);
      return Object.assign({}, node, point, { sx: 0, sy: 0, depth: 0, radius: node.kind === 'document' ? 6.5 : 4.2 });
    });
    state.nodeById = new Map(state.nodes.map(function (node) { return [node.id, node]; }));
    state.edges = payload.edges.filter(function (edge) {
      return state.nodeById.has(edge.source) && state.nodeById.has(edge.target);
    });
    state.adjacency = new Map(state.nodes.map(function (node) { return [node.id, new Set()]; }));
    state.edges.forEach(function (edge) {
      state.adjacency.get(edge.source).add(edge.target);
      state.adjacency.get(edge.target).add(edge.source);
    });
    document.getElementById('knowledge-node-count').textContent = String(state.nodes.length);
    document.getElementById('knowledge-edge-count').textContent = String(state.edges.length);
    document.getElementById('knowledge-a11y-status').textContent = copy.loaded(state.nodes.length, state.edges.length);
    state.settled = false;
  }

  function resize() {
    var rect = viewport.getBoundingClientRect();
    state.width = Math.max(1, rect.width);
    state.height = Math.max(1, rect.height);
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(state.width * state.dpr);
    canvas.height = Math.round(state.height * state.dpr);
    canvas.style.width = state.width + 'px';
    canvas.style.height = state.height + 'px';
    context.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
  }

  function simulate() {
    if (state.settled || state.paused) return;
    var nodes = state.nodes;
    var strength = Math.min(1, 160 / Math.max(nodes.length, 1));
    for (var i = 0; i < nodes.length; i += 1) {
      var a = nodes[i];
      a.vx += -a.x * 0.0007; a.vy += -a.y * 0.0007; a.vz += -a.z * 0.0007;
      for (var j = i + 1; j < nodes.length; j += 1) {
        var b = nodes[j];
        var dx = a.x - b.x; var dy = a.y - b.y; var dz = a.z - b.z;
        var d2 = Math.max(100, dx * dx + dy * dy + dz * dz);
        var push = 52 * strength / d2;
        a.vx += dx * push; a.vy += dy * push; a.vz += dz * push;
        b.vx -= dx * push; b.vy -= dy * push; b.vz -= dz * push;
      }
    }
    state.edges.forEach(function (edge) {
      var source = state.nodeById.get(edge.source); var target = state.nodeById.get(edge.target);
      var dx = target.x - source.x; var dy = target.y - source.y; var dz = target.z - source.z;
      var distance = Math.max(1, Math.sqrt(dx * dx + dy * dy + dz * dz));
      var pull = (distance - 115) * 0.0005;
      source.vx += dx * pull; source.vy += dy * pull; source.vz += dz * pull;
      target.vx -= dx * pull; target.vy -= dy * pull; target.vz -= dz * pull;
    });
    var energy = 0;
    nodes.forEach(function (node) {
      node.vx *= 0.88; node.vy *= 0.88; node.vz *= 0.88;
      node.x += node.vx; node.y += node.vy; node.z += node.vz;
      energy += Math.abs(node.vx) + Math.abs(node.vy) + Math.abs(node.vz);
    });
    if (energy < 0.025 * nodes.length) state.settled = true;
  }

  function project(node) {
    var x = node.x - state.targetX; var y = node.y - state.targetY; var z = node.z - state.targetZ;
    var cosY = Math.cos(state.yaw); var sinY = Math.sin(state.yaw);
    var rx = x * cosY - z * sinY; var rz = x * sinY + z * cosY;
    var cosP = Math.cos(state.pitch); var sinP = Math.sin(state.pitch);
    var ry = y * cosP - rz * sinP; var depth = y * sinP + rz * cosP + state.distance;
    var scale = Math.max(0.08, 620 / Math.max(120, depth));
    node.sx = state.width / 2 + rx * scale;
    node.sy = state.height / 2 + ry * scale;
    node.depth = depth; node.scale = scale;
  }

  function isConnected(node) {
    if (!state.selected || !state.neighborhoodOnly) return true;
    return node.id === state.selected.id || state.adjacency.get(state.selected.id).has(node.id);
  }

  function drawBackground(time) {
    context.fillStyle = palette.background;
    context.fillRect(0, 0, state.width, state.height);
    var glow = context.createRadialGradient(state.width * .52, state.height * .48, 0, state.width * .52, state.height * .48, Math.max(state.width, state.height) * .62);
    glow.addColorStop(0, 'rgba(79, 104, 48, .12)'); glow.addColorStop(.45, 'rgba(38, 48, 27, .05)'); glow.addColorStop(1, 'rgba(23, 23, 19, 0)');
    context.fillStyle = glow; context.fillRect(0, 0, state.width, state.height);
    if (!state.paused && !prefersReducedMotion) state.yaw += Math.sin(time * 0.00017) * 0.00009;
  }

  function drawEdge(edge, time) {
    var source = state.nodeById.get(edge.source); var target = state.nodeById.get(edge.target);
    if (!source || !target || source.depth < 100 || target.depth < 100) return;
    var selected = state.selected && (edge.source === state.selected.id || edge.target === state.selected.id);
    var muted = state.neighborhoodOnly && state.selected && !selected;
    context.beginPath(); context.moveTo(source.sx, source.sy); context.lineTo(target.sx, target.sy);
    context.strokeStyle = selected ? palette.lineHot : (muted ? 'rgba(159,170,160,.025)' : palette.line);
    context.lineWidth = selected ? 1.45 : 0.7; context.stroke();
    if (!state.paused && !muted && state.edges.length < 1200) {
      var offset = (time * 0.00012 + (hash(edge.id) % 100) / 100) % 1;
      var px = source.sx + (target.sx - source.sx) * offset;
      var py = source.sy + (target.sy - source.sy) * offset;
      context.beginPath(); context.arc(px, py, selected ? 1.8 : 1.1, 0, Math.PI * 2);
      context.fillStyle = selected ? '#eefbcf' : 'rgba(184,223,120,.48)'; context.fill();
    }
  }

  function nodeColor(node) { return palette[node.kind] || palette.concept; }

  function drawNode(node) {
    if (node.depth < 100) return;
    var connected = isConnected(node); var selected = state.selected === node; var hovered = state.hovered === node;
    var radius = Math.max(2.2, node.radius * Math.min(1.65, node.scale));
    context.save();
    context.globalAlpha = connected ? 1 : .1;
    if (node.kind === 'document' || selected || hovered) {
      var halo = context.createRadialGradient(node.sx, node.sy, 0, node.sx, node.sy, radius * (selected ? 5 : 3.2));
      halo.addColorStop(0, selected ? 'rgba(238,251,207,.38)' : 'rgba(184,223,120,.22)'); halo.addColorStop(1, 'rgba(184,223,120,0)');
      context.fillStyle = halo; context.beginPath(); context.arc(node.sx, node.sy, radius * (selected ? 5 : 3.2), 0, Math.PI * 2); context.fill();
    }
    context.beginPath(); context.arc(node.sx, node.sy, radius + (selected ? 2 : 0), 0, Math.PI * 2);
    context.fillStyle = nodeColor(node); context.fill();
    if (node.kind !== 'document') {
      context.strokeStyle = node.kind === 'tag' ? 'rgba(255,236,190,.72)' : 'rgba(240,233,220,.5)';
      context.lineWidth = 1; context.stroke();
    }
    if (selected || hovered || (node.kind === 'document' && node.scale > .85 && state.nodes.length < 180)) {
      context.font = (selected ? '600 13px ' : '500 11px ') + '"Geist Sans", sans-serif';
      context.textAlign = 'center'; context.textBaseline = 'top';
      context.fillStyle = selected ? palette.text : (hovered ? '#e8f4d0' : 'rgba(240,233,220,.72)');
      var label = node.label.length > 34 ? node.label.slice(0, 32) + '…' : node.label;
      context.fillText(label, node.sx, node.sy + radius + 7);
    }
    context.restore();
  }

  function render(time) {
    state.frame = window.requestAnimationFrame(render);
    simulate(); drawBackground(time);
    state.nodes.forEach(project);
    state.edges.slice().sort(function (a, b) {
      return state.nodeById.get(b.source).depth - state.nodeById.get(a.source).depth;
    }).forEach(function (edge) { drawEdge(edge, time); });
    state.nodes.slice().sort(function (a, b) { return b.depth - a.depth; }).forEach(drawNode);
  }

  function nearestNode(x, y) {
    var best = null; var bestDistance = 22;
    state.nodes.forEach(function (node) {
      if (!isConnected(node)) return;
      var distance = Math.hypot(node.sx - x, node.sy - y);
      if (distance < bestDistance) { best = node; bestDistance = distance; }
    });
    return best;
  }

  function eventPoint(event) {
    var rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function selectNode(node, announce) {
    state.selected = node; state.neighborhoodOnly = false;
    var inspector = document.getElementById('knowledge-inspector');
    if (!node) { inspector.hidden = true; return; }
    document.getElementById('knowledge-inspector-kind').textContent = node.kind === 'document' ? copy.document : (node.kind === 'tag' ? copy.tag : copy.relation);
    document.getElementById('knowledge-inspector-title').textContent = node.label;
    document.getElementById('knowledge-inspector-path').textContent = node.path || copy.derived;
    var neighbors = state.adjacency.get(node.id) || new Set();
    var meta = document.getElementById('knowledge-inspector-meta');
    meta.textContent = '';
    [[copy.connections, neighbors.size], [copy.type, node.document_type || node.kind], [copy.owner, node.owner || copy.empty]].forEach(function (item) {
      var row = document.createElement('div'); var dt = document.createElement('dt'); var dd = document.createElement('dd');
      dt.textContent = item[0]; dd.textContent = String(item[1]); row.append(dt, dd); meta.append(row);
    });
    var open = document.getElementById('knowledge-open');
    open.hidden = !node.path; open.href = node.path ? '/?file=' + encodeURIComponent(node.path) : '#';
    inspector.hidden = false;
    if (announce) document.getElementById('knowledge-a11y-status').textContent = copy.selected(node.label, neighbors.size);
  }

  function focusNode(node) {
    if (!node) return;
    state.targetX = node.x; state.targetY = node.y; state.targetZ = node.z;
    state.distance = Math.max(300, state.distance * .72);
  }

  function resetView() {
    state.yaw = -.35; state.pitch = .18; state.distance = 720;
    state.targetX = 0; state.targetY = 0; state.targetZ = 0; state.neighborhoodOnly = false;
    selectNode(null, false);
    document.getElementById('knowledge-depth').textContent = '100%';
  }

  canvas.addEventListener('pointerdown', function (event) {
    canvas.setPointerCapture(event.pointerId); state.dragging = true; state.moved = false;
    state.lastX = event.clientX; state.lastY = event.clientY;
  });
  canvas.addEventListener('pointermove', function (event) {
    var point = eventPoint(event); state.hovered = nearestNode(point.x, point.y);
    canvas.style.cursor = state.dragging ? 'grabbing' : (state.hovered ? 'pointer' : 'grab');
    if (!state.dragging) return;
    var dx = event.clientX - state.lastX; var dy = event.clientY - state.lastY;
    if (Math.abs(dx) + Math.abs(dy) > 2) state.moved = true;
    state.yaw += dx * .006; state.pitch = Math.max(-1.2, Math.min(1.2, state.pitch + dy * .005));
    state.lastX = event.clientX; state.lastY = event.clientY;
  });
  canvas.addEventListener('pointerup', function (event) {
    state.dragging = false;
    if (!state.moved) { var point = eventPoint(event); selectNode(nearestNode(point.x, point.y), true); }
  });
  canvas.addEventListener('pointercancel', function () { state.dragging = false; });
  canvas.addEventListener('dblclick', function (event) {
    var point = eventPoint(event); var node = nearestNode(point.x, point.y);
    if (node && node.path) window.location.href = '/?file=' + encodeURIComponent(node.path);
  });
  canvas.addEventListener('wheel', function (event) {
    event.preventDefault();
    state.distance = Math.max(220, Math.min(1600, state.distance * Math.exp(event.deltaY * .001)));
    document.getElementById('knowledge-depth').textContent = Math.round(720 / state.distance * 100) + '%';
  }, { passive: false });
  canvas.addEventListener('keydown', function (event) {
    var step = Math.max(10, state.distance * .025); var handled = true;
    if (event.key === 'w' || event.key === 'W') state.targetZ -= step;
    else if (event.key === 's' || event.key === 'S') state.targetZ += step;
    else if (event.key === 'a' || event.key === 'A') state.targetX -= step;
    else if (event.key === 'd' || event.key === 'D') state.targetX += step;
    else if (event.key === 'Enter' && state.selected && state.selected.path) window.location.href = '/?file=' + encodeURIComponent(state.selected.path);
    else if (event.key === 'Escape') selectNode(null, false);
    else handled = false;
    if (handled) event.preventDefault();
  });

  document.getElementById('knowledge-reset').addEventListener('click', resetView);
  document.getElementById('knowledge-motion').addEventListener('click', function (event) {
    state.paused = !state.paused; event.currentTarget.setAttribute('aria-pressed', String(state.paused));
    event.currentTarget.setAttribute('aria-label', state.paused ? copy.resume : copy.pause);
    event.currentTarget.title = state.paused ? copy.resume : copy.pause;
  });
  document.getElementById('knowledge-inspector-close').addEventListener('click', function () { selectNode(null, false); canvas.focus(); });
  document.getElementById('knowledge-follow').addEventListener('click', function () {
    if (!state.selected) return;
    state.neighborhoodOnly = !state.neighborhoodOnly; focusNode(state.selected);
    this.textContent = state.neighborhoodOnly ? copy.showAll : copy.follow;
  });
  document.getElementById('knowledge-retry').addEventListener('click', loadGraph);

  var reindex = document.getElementById('knowledge-reindex');
  if (reindex) reindex.addEventListener('click', function () {
    reindex.disabled = true; reindex.textContent = copy.rebuilding;
    fetch('/api/knowledge/reindex', { method: 'POST', credentials: 'same-origin' })
      .then(function (response) { if (!response.ok) throw new Error('reindex failed'); return response.json(); })
      .then(function () { window.setTimeout(loadGraph, 900); })
      .catch(function () { reindex.disabled = false; reindex.textContent = copy.retry; });
  });

  function loadGraph() {
    document.getElementById('knowledge-loading').hidden = false;
    document.getElementById('knowledge-empty').hidden = true;
    document.getElementById('knowledge-error').hidden = true;
    fetch('/api/knowledge/graph?max_nodes=500&max_edges=1200', { credentials: 'same-origin' })
      .then(function (response) { if (!response.ok) throw new Error('graph request failed'); return response.json(); })
      .then(function (payload) {
        document.getElementById('knowledge-loading').hidden = true;
        if (payload.status !== 'ready' || !payload.nodes.length) {
          document.getElementById('knowledge-empty').hidden = false;
          document.getElementById('knowledge-node-count').textContent = '0';
          document.getElementById('knowledge-edge-count').textContent = '0';
          return;
        }
        prepareGraph(payload); resetView();
      })
      .catch(function () {
        document.getElementById('knowledge-loading').hidden = true;
        document.getElementById('knowledge-error').hidden = false;
      });
  }

  new ResizeObserver(resize).observe(viewport);
  resize(); loadGraph(); state.frame = window.requestAnimationFrame(render);
  window.addEventListener('pagehide', function () { window.cancelAnimationFrame(state.frame); });
}());
