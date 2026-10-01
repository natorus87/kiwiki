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
  var MAX_PAIRWISE_NODES = 180;
  var MAX_NODE_SPEED = 4;
  var MAX_NODE_RADIUS = 700;
  var MAX_SIMULATION_STEPS = 240;
  // Aufbau-Animation: Knoten spiralen von außen an ihren Platz (innen zuerst)
  var INTRO_NODE_MS = 1100;
  var MAX_PULSES = 32;
  // Nach eigener Bedienung kurz stillhalten, dann weiterdrehen (Website: sofort)
  var IDLE_ROTATE_AFTER_MS = 1500;
  var POINTER_GLOW_RADIUS = 140;
  var state = {
    nodes: [], edges: [], nodeById: new Map(), adjacency: new Map(),
    width: 1, height: 1, dpr: 1, yaw: -0.35, pitch: 0.18, distance: 720, defaultDistance: 720,
    targetX: 0, targetY: 0, targetZ: 0, selected: null, hovered: null,
    dragging: false, moved: false, lastX: 0, lastY: 0, paused: prefersReducedMotion,
    pointers: new Map(), pinchDistance: null,
    neighborhoodOnly: false, frame: 0, lastTime: performance.now(), settled: false, simulationSteps: 0,
    intro: { start: 0, end: 0, done: true, waveFired: false },
    pulses: [], rings: [], waves: [], nextPulse: 0, goal: null, distanceGoal: null,
    lastInteraction: -IDLE_ROTATE_AFTER_MS, pointer: { x: 0, y: 0, active: false }, graphRadius: 300, bgGlow: null, refitted: false, userZoomed: false
  };

  function clamp01(value) { return value < 0 ? 0 : (value > 1 ? 1 : value); }
  function easeOut(value) { return 1 - Math.pow(1 - value, 3); }

  // Leuchtpunkte einmal vorrendern: drawImage ist pro Frame deutlich billiger
  // als ein neuer Radialverlauf je Knoten (wichtig für 500-Knoten-Graphen).
  function makeGlow(rgb) {
    var size = 64; var sprite = document.createElement('canvas');
    sprite.width = size; sprite.height = size;
    var g = sprite.getContext('2d');
    var gradient = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gradient.addColorStop(0, 'rgba(' + rgb + ',.85)');
    gradient.addColorStop(0.28, 'rgba(' + rgb + ',.3)');
    gradient.addColorStop(1, 'rgba(' + rgb + ',0)');
    g.fillStyle = gradient; g.fillRect(0, 0, size, size);
    return sprite;
  }
  var glow = {
    document: makeGlow('227,169,79'), tag: makeGlow('159,183,216'),
    concept: makeGlow('159,170,160'), hot: makeGlow('253,240,212')
  };

  var palette = {
    background: '#171713', document: '#e3a94f', tag: '#9fb7d8',
    concept: '#9faaa0', line: 'rgba(227, 169, 79, .13)',
    lineHot: 'rgba(237, 186, 102, .72)', text: '#f0e9dc', muted: '#a99d8d'
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
      return Object.assign({}, node, point, {
        homeX: point.x, homeY: point.y, homeZ: point.z,
        sx: 0, sy: 0, depth: 0, radius: node.kind === 'document' ? 6.5 : 4.2
      });
    });
    var maxHome = state.nodes.reduce(function (largest, node) {
      return Math.max(largest, Math.sqrt(node.homeX * node.homeX + node.homeY * node.homeY + node.homeZ * node.homeZ));
    }, 1);
    state.nodes.forEach(function (node) {
      var home = Math.sqrt(node.homeX * node.homeX + node.homeY * node.homeY + node.homeZ * node.homeZ);
      var seed = hash(node.id);
      node.delay = 120 + (home / maxHome) * 1300 + (seed % 280);
      node.swirl = 1 + (seed % 100) / 70;
      node.k = 1; node.flash = 0;
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
    state.settled = false; state.simulationSteps = 0;
    state.pulses = []; state.rings = []; state.waves = [];
  }

  function startIntro() {
    if (prefersReducedMotion || !state.nodes.length) {
      state.intro.done = true;
      state.nodes.forEach(function (node) { node.k = 1; });
      return;
    }
    var now = performance.now();
    state.intro = { start: now, end: 0, done: false, waveFired: false };
    state.nodes.forEach(function (node) {
      node.k = 0;
      state.intro.end = Math.max(state.intro.end, node.delay + INTRO_NODE_MS);
    });
  }

  function updateIntro(now) {
    if (state.intro.done) return;
    var elapsed = now - state.intro.start;
    state.nodes.forEach(function (node) { node.k = easeOut(clamp01((elapsed - node.delay) / INTRO_NODE_MS)); });
    if (!state.intro.waveFired && elapsed > state.intro.end - 250) {
      state.intro.waveFired = true; state.waves.push(now);
      for (var i = 0; i < 8; i += 1) spawnPulse(null, now);
    }
    if (elapsed > state.intro.end + 700) {
      state.intro.done = true;
      state.nodes.forEach(function (node) { node.k = 1; });
    }
  }

  function resize() {
    var wasFitted = Math.abs(state.distance - state.defaultDistance) < 1;
    var rect = viewport.getBoundingClientRect();
    state.width = Math.max(1, rect.width);
    state.height = Math.max(1, rect.height);
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(state.width * state.dpr);
    canvas.height = Math.round(state.height * state.dpr);
    canvas.style.width = state.width + 'px';
    canvas.style.height = state.height + 'px';
    context.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    state.bgGlow = context.createRadialGradient(state.width * .52, state.height * .48, 0, state.width * .52, state.height * .48, Math.max(state.width, state.height) * .62);
    state.bgGlow.addColorStop(0, 'rgba(110, 78, 30, .16)'); state.bgGlow.addColorStop(.45, 'rgba(52, 38, 18, .06)'); state.bgGlow.addColorStop(1, 'rgba(23, 23, 19, 0)');
    if (state.nodes.length && wasFitted) setDistance(fitGraphDistance(), true);
  }

  function restoreGraphPositions(force) {
    state.nodes.forEach(function (node) {
      var valid = Number.isFinite(node.x) && Number.isFinite(node.y) && Number.isFinite(node.z) &&
        Number.isFinite(node.vx) && Number.isFinite(node.vy) && Number.isFinite(node.vz);
      if (!force && valid) return;
      node.x = node.homeX; node.y = node.homeY; node.z = node.homeZ;
      node.vx = 0; node.vy = 0; node.vz = 0;
    });
    state.settled = false; state.simulationSteps = 0;
  }

  function simulateLargeGraph(nodes) {
    var idealRadius = 190 + Math.min(90, Math.sqrt(nodes.length) * 4);
    nodes.forEach(function (node) {
      var radius = Math.max(1, Math.sqrt(node.x * node.x + node.y * node.y + node.z * node.z));
      var radialForce = (idealRadius - radius) * 0.0012;
      node.vx += node.x / radius * radialForce;
      node.vy += node.y / radius * radialForce;
      node.vz += node.z / radius * radialForce;
    });
  }

  function simulate() {
    if (state.settled || state.paused) return;
    var nodes = state.nodes;
    // Die Gesamtabstossung muss mit der Knotenzahl konstant bleiben. Die alte
    // 160/n-Skalierung liess grosse Graphen nach dem Laden aus dem Bild wachsen.
    var strength = 1 / Math.max(nodes.length, 1);
    if (nodes.length > MAX_PAIRWISE_NODES) {
      simulateLargeGraph(nodes);
    } else {
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
    }
    state.edges.forEach(function (edge) {
      var source = state.nodeById.get(edge.source); var target = state.nodeById.get(edge.target);
      var dx = target.x - source.x; var dy = target.y - source.y; var dz = target.z - source.z;
      var distance = Math.max(1, Math.sqrt(dx * dx + dy * dy + dz * dz));
      var pull = (distance - 115) * 0.0025 / distance;
      var sourceDegree = Math.max(1, state.adjacency.get(source.id).size);
      var targetDegree = Math.max(1, state.adjacency.get(target.id).size);
      source.vx += dx * pull / sourceDegree; source.vy += dy * pull / sourceDegree; source.vz += dz * pull / sourceDegree;
      target.vx -= dx * pull / targetDegree; target.vy -= dy * pull / targetDegree; target.vz -= dz * pull / targetDegree;
    });
    var energy = 0;
    nodes.forEach(function (node) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y) || !Number.isFinite(node.z) ||
          !Number.isFinite(node.vx) || !Number.isFinite(node.vy) || !Number.isFinite(node.vz)) {
        node.x = node.homeX; node.y = node.homeY; node.z = node.homeZ;
        node.vx = 0; node.vy = 0; node.vz = 0;
      }
      node.vx *= 0.88; node.vy *= 0.88; node.vz *= 0.88;
      var speed = Math.sqrt(node.vx * node.vx + node.vy * node.vy + node.vz * node.vz);
      if (speed > MAX_NODE_SPEED) {
        var speedLimit = MAX_NODE_SPEED / speed;
        node.vx *= speedLimit; node.vy *= speedLimit; node.vz *= speedLimit;
      }
      node.x += node.vx; node.y += node.vy; node.z += node.vz;
      var radius = Math.sqrt(node.x * node.x + node.y * node.y + node.z * node.z);
      if (!Number.isFinite(radius)) {
        node.x = node.homeX; node.y = node.homeY; node.z = node.homeZ;
        node.vx = 0; node.vy = 0; node.vz = 0;
      } else if (radius > MAX_NODE_RADIUS) {
        var radiusLimit = MAX_NODE_RADIUS / radius;
        node.x *= radiusLimit; node.y *= radiusLimit; node.z *= radiusLimit;
        node.vx = 0; node.vy = 0; node.vz = 0;
      }
      energy += Math.abs(node.vx) + Math.abs(node.vy) + Math.abs(node.vz);
    });
    state.simulationSteps += 1;
    if (energy < 0.025 * nodes.length || state.simulationSteps >= MAX_SIMULATION_STEPS) state.settled = true;
  }

  function project(node) {
    var px = node.x; var py = node.y; var pz = node.z;
    if (node.k < 1) {
      // Während des Aufbaus: weiter draußen und um die Hochachse verdreht
      var turn = (1 - node.k) * node.swirl; var spread = 1 + (1 - node.k) * 1.2;
      var cosT = Math.cos(turn); var sinT = Math.sin(turn);
      var tx = px * cosT - pz * sinT; var tz = px * sinT + pz * cosT;
      px = tx * spread; py *= spread; pz = tz * spread;
    }
    var x = px - state.targetX; var y = py - state.targetY; var z = pz - state.targetZ;
    var cosY = Math.cos(state.yaw); var sinY = Math.sin(state.yaw);
    var rx = x * cosY - z * sinY; var rz = x * sinY + z * cosY;
    var cosP = Math.cos(state.pitch); var sinP = Math.sin(state.pitch);
    var ry = y * cosP - rz * sinP; var depth = y * sinP + rz * cosP + state.distance;
    var scale = Math.max(0.08, 620 / Math.max(120, depth));
    node.sx = state.width / 2 + rx * scale;
    node.sy = state.height / 2 + ry * scale;
    node.depth = depth; node.scale = scale;
    // Tiefennebel: vordere Knoten klar, hintere treten zurück
    node.fog = clamp01(1 - (depth - (state.distance - state.graphRadius)) / (2.2 * Math.max(1, state.graphRadius)) * 0.7);
  }

  function isHoverNeighbor(node) {
    return !!(state.hovered && state.hovered !== node && state.adjacency.get(state.hovered.id).has(node.id));
  }

  function isConnected(node) {
    if (!state.selected || !state.neighborhoodOnly) return true;
    return node.id === state.selected.id || state.adjacency.get(state.selected.id).has(node.id);
  }

  function drawBackground(time, dt) {
    context.fillStyle = palette.background;
    context.fillRect(0, 0, state.width, state.height);
    if (state.bgGlow) { context.fillStyle = state.bgGlow; context.fillRect(0, 0, state.width, state.height); }
    if (state.paused || prefersReducedMotion || state.dragging) return;
    // Langsame Drehung, sobald niemand bedient; beim Aufbau mit abklingendem Schwung
    var introSpin = state.intro.done ? 0 : 0.0007 * Math.pow(1 - clamp01((time - state.intro.start) / (state.intro.end + 700)), 2);
    // ~5°/s wie der Hero der Website; waehrend des Aufbaus mit zusaetzlichem Schwung
    if (time - state.lastInteraction > IDLE_ROTATE_AFTER_MS || introSpin) state.yaw += dt * (0.00009 + introSpin);
  }

  function drawWaves(time) {
    for (var i = state.waves.length - 1; i >= 0; i -= 1) {
      var age = (time - state.waves[i]) / 1500;
      if (age >= 1) { state.waves.splice(i, 1); continue; }
      var cx = state.width / 2; var cy = state.height / 2;
      var radius = Math.max(state.width, state.height) * (0.05 + easeOut(age) * 0.6);
      var ring = context.createRadialGradient(cx, cy, radius * 0.82, cx, cy, radius);
      ring.addColorStop(0, 'rgba(227,169,79,0)');
      ring.addColorStop(0.85, 'rgba(237,186,102,' + (0.16 * (1 - age)).toFixed(3) + ')');
      ring.addColorStop(1, 'rgba(227,169,79,0)');
      context.fillStyle = ring; context.beginPath(); context.arc(cx, cy, radius, 0, Math.PI * 2); context.fill();
    }
  }

  // ── Aktionspotenziale: Impulse wandern über Kanten und springen weiter ──
  function spawnPulse(fromId, now, edge) {
    if (state.pulses.length >= MAX_PULSES || !state.edges.length) return;
    var candidate = edge;
    if (!candidate && fromId) {
      var options = state.edges.filter(function (item) { return item.source === fromId || item.target === fromId; });
      if (!options.length) return;
      candidate = options[Math.floor(Math.random() * options.length)];
    }
    if (!candidate) candidate = state.edges[Math.floor(Math.random() * state.edges.length)];
    var forward = fromId ? candidate.source === fromId : Math.random() < 0.5;
    state.pulses.push({
      from: forward ? candidate.source : candidate.target, to: forward ? candidate.target : candidate.source,
      t: 0, speed: 0.55 + Math.random() * 0.5, hot: !!fromId && !!state.selected && fromId === state.selected.id
    });
  }

  function updatePulses(time, dt) {
    if (state.paused || prefersReducedMotion) return;
    for (var i = state.pulses.length - 1; i >= 0; i -= 1) {
      var pulse = state.pulses[i];
      pulse.t += dt * 0.001 * pulse.speed;
      if (pulse.t >= 1) {
        state.pulses.splice(i, 1);
        var arrived = state.nodeById.get(pulse.to);
        if (arrived) arrived.flash = Math.max(arrived.flash, pulse.hot ? 0.8 : 0.35);
        if (Math.random() < 0.45) spawnPulse(pulse.to, time);
      }
    }
    // Umgebungs-Impulse nur bei kleineren Graphen; große bleiben ruhig
    if (state.intro.done && state.nodes.length <= MAX_PAIRWISE_NODES && time > state.nextPulse) {
      spawnPulse(null, time);
      state.nextPulse = time + 380 + Math.random() * 620;
    }
    state.nodes.forEach(function (node) { if (node.flash > 0) node.flash = Math.max(0, node.flash - dt / 1400); });
  }

  function drawPulses() {
    state.pulses.forEach(function (pulse) {
      var a = state.nodeById.get(pulse.from); var b = state.nodeById.get(pulse.to);
      if (!a || !b || a.depth < 100 || b.depth < 100) return;
      if (state.neighborhoodOnly && state.selected && !(isConnected(a) && isConnected(b))) return;
      var x = a.sx + (b.sx - a.sx) * pulse.t; var y = a.sy + (b.sy - a.sy) * pulse.t;
      var size = (pulse.hot ? 22 : 14) * Math.min(1.4, (a.scale + b.scale) / 2);
      context.globalAlpha = Math.sin(Math.PI * pulse.t) * (0.55 + 0.45 * ((a.fog + b.fog) / 2));
      context.drawImage(pulse.hot ? glow.hot : glow.document, x - size / 2, y - size / 2, size, size);
    });
    context.globalAlpha = 1;
  }

  function drawRings(time) {
    for (var i = state.rings.length - 1; i >= 0; i -= 1) {
      var ring = state.rings[i]; var age = (time - ring.start) / 1100;
      if (age >= 1 || ring.node.depth < 100) { state.rings.splice(i, 1); continue; }
      context.strokeStyle = 'rgba(237,186,102,' + (0.75 * (1 - age)).toFixed(3) + ')';
      context.lineWidth = 1.5; context.beginPath();
      context.arc(ring.node.sx, ring.node.sy, 8 + easeOut(age) * 46 * Math.min(1.4, ring.node.scale), 0, Math.PI * 2);
      context.stroke();
    }
  }

  function drawEdge(edge, time) {
    var source = state.nodeById.get(edge.source); var target = state.nodeById.get(edge.target);
    if (!source || !target || source.depth < 100 || target.depth < 100) return;
    // Beim Aufbau wächst eine Kante erst, wenn beide Enden angekommen sind
    var grow = 1;
    if (!state.intro.done) {
      grow = easeOut(clamp01((time - state.intro.start - Math.max(source.delay, target.delay) - 300) / 650));
      if (grow <= 0) return;
    }
    var selected = state.selected && (edge.source === state.selected.id || edge.target === state.selected.id);
    var hoverEdge = state.hovered && (edge.source === state.hovered.id || edge.target === state.hovered.id);
    var muted = state.neighborhoodOnly && state.selected && !selected;
    var fog = (source.fog + target.fog) / 2;
    context.beginPath(); context.moveTo(source.sx, source.sy);
    context.lineTo(source.sx + (target.sx - source.sx) * grow, source.sy + (target.sy - source.sy) * grow);
    if (selected) context.strokeStyle = palette.lineHot;
    else if (muted) context.strokeStyle = 'rgba(159,170,160,.025)';
    else if (hoverEdge) context.strokeStyle = 'rgba(237,186,102,.42)';
    else context.strokeStyle = 'rgba(227,169,79,' + (0.035 + fog * fog * 0.15).toFixed(3) + ')';
    context.lineWidth = selected ? 1.45 : (hoverEdge ? 1 : 0.7); context.stroke();
  }

  function nodeColor(node) { return palette[node.kind] || palette.concept; }

  function drawNode(node) {
    if (node.depth < 100 || node.k <= 0.001) return;
    var connected = isConnected(node); var selected = state.selected === node; var hovered = state.hovered === node;
    var neighbor = isHoverNeighbor(node);
    // Zeiger-Naehe: Knoten im Umkreis leuchten auf, wie im Hero der Website
    var near = 0;
    if (state.pointer.active && !state.dragging) {
      near = Math.max(0, 1 - Math.hypot(node.sx - state.pointer.x, node.sy - state.pointer.y) / POINTER_GLOW_RADIUS);
    }
    // Kurzes Aufglühen beim Ankommen während des Aufbaus
    var land = state.intro.done ? 0 : Math.max(0, 1 - Math.abs(node.k - 0.9) / 0.2) * 0.9;
    var lift = Math.min(1, node.flash + land + (neighbor ? 0.35 : 0) + near * 0.55);
    var radius = Math.max(2.2, node.radius * Math.min(1.65, node.scale)) * (0.35 + 0.65 * node.k) * (1 + lift * 0.45);
    var light = selected || hovered ? 1 : Math.min(1, 0.4 + node.fog * 0.6 + lift);
    context.save();
    context.globalAlpha = (connected ? 1 : .1) * light * node.k;
    if (state.nodes.length <= MAX_PAIRWISE_NODES || selected || hovered || lift > 0.05) {
      var haloSize = radius * (selected ? 12 : 8 + lift * 7);
      context.globalAlpha = (connected ? 1 : .1) * node.k * (selected ? 0.9 : 0.35 + 0.4 * lift) * light;
      context.drawImage(selected || lift > 0.5 ? glow.hot : (glow[node.kind] || glow.concept), node.sx - haloSize / 2, node.sy - haloSize / 2, haloSize, haloSize);
      context.globalAlpha = (connected ? 1 : .1) * light * node.k;
    }
    context.beginPath(); context.arc(node.sx, node.sy, radius + (selected ? 2 : 0), 0, Math.PI * 2);
    context.fillStyle = nodeColor(node); context.fill();
    if (node.kind !== 'document') {
      context.strokeStyle = node.kind === 'tag' ? 'rgba(214,226,242,.72)' : 'rgba(240,233,220,.5)';
      context.lineWidth = 1; context.stroke();
    }
    if (node.k > 0.97 && (selected || hovered || neighbor || (node.kind === 'document' && node.scale > .85 && state.nodes.length < 180))) {
      if (!selected && !hovered) context.globalAlpha = (connected ? 1 : .1) * Math.min(1, 0.35 + node.fog * 0.65 + (neighbor ? 0.3 : 0));
      context.font = (selected ? '600 13px ' : '500 11px ') + '"Geist Sans", sans-serif';
      context.textAlign = 'center'; context.textBaseline = 'top';
      context.fillStyle = selected ? palette.text : (hovered ? '#f6e8cc' : 'rgba(240,233,220,.72)');
      var label = node.label.length > 34 ? node.label.slice(0, 32) + '…' : node.label;
      context.fillText(label, node.sx, node.sy + radius + 7);
    }
    context.restore();
  }

  function updateCamera() {
    // Kamerafahrten weich statt Sprung (Fokus, Zurücksetzen)
    if (state.goal) {
      state.targetX += (state.goal.x - state.targetX) * 0.12;
      state.targetY += (state.goal.y - state.targetY) * 0.12;
      state.targetZ += (state.goal.z - state.targetZ) * 0.12;
      if (Math.abs(state.goal.x - state.targetX) + Math.abs(state.goal.y - state.targetY) + Math.abs(state.goal.z - state.targetZ) < 0.5) {
        state.targetX = state.goal.x; state.targetY = state.goal.y; state.targetZ = state.goal.z; state.goal = null;
      }
    }
    if (state.distanceGoal !== null) {
      var next = state.distance + (state.distanceGoal - state.distance) * 0.12;
      if (Math.abs(state.distanceGoal - next) < 1) { next = state.distanceGoal; state.distanceGoal = null; }
      setDistance(next);
    }
  }

  function refitAfterSettle() {
    // fitGraphDistance() läuft beim Laden auf den Startpositionen; die Simulation
    // zieht den Graphen danach deutlich zusammen. Während des Einschwingens alle
    // 20 Schritte weich nachführen, danach einmal endgültig.
    if (state.refitted || state.userZoomed || state.selected || !state.nodes.length) return;
    if (!state.settled && state.simulationSteps % 20 !== 0) return;
    if (state.settled) state.refitted = true;
    var fitted = fitGraphDistance();
    state.defaultDistance = fitted;
    if (prefersReducedMotion) setDistance(fitted, true);
    else state.distanceGoal = fitted;
  }

  function render(time) {
    state.frame = window.requestAnimationFrame(render);
    var dt = Math.min(64, Math.max(0, time - state.lastTime)); state.lastTime = time;
    simulate(); refitAfterSettle(); updateIntro(time); updateCamera(); updatePulses(time, dt);
    drawBackground(time, dt); drawWaves(time);
    state.nodes.forEach(project);
    state.edges.slice().sort(function (a, b) {
      return state.nodeById.get(b.source).depth - state.nodeById.get(a.source).depth;
    }).forEach(function (edge) { drawEdge(edge, time); });
    drawPulses();
    state.nodes.slice().sort(function (a, b) { return b.depth - a.depth; }).forEach(drawNode);
    drawRings(time);
  }

  function nearestNode(x, y) {
    var best = null; var bestDistance = 22;
    state.nodes.forEach(function (node) {
      if (!isConnected(node) || node.k < 0.5) return;
      var distance = Math.hypot(node.sx - x, node.sy - y);
      if (distance < bestDistance) { best = node; bestDistance = distance; }
    });
    return best;
  }

  function eventPoint(event) {
    var rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function fitGraphDistance() {
    if (!state.nodes.length) return 720;
    // Ausreisser (ein einzelner weit draussen haengender Tag) duerfen die Kamera
    // nicht wegschieben: 95. Perzentil der Abstaende statt Maximum.
    var radii = state.nodes.map(function (node) {
      return Math.sqrt(node.x * node.x + node.y * node.y + node.z * node.z);
    }).sort(function (a, b) { return a - b; });
    var radius = Math.max(1, radii[Math.min(radii.length - 1, Math.floor(radii.length * 0.95))]);
    state.graphRadius = radius;
    // 0.56: der Graph fuellt den Raum wie im Website-Hero; nur der vorderste Knoten
    // streift beim Drehen kurz den Rand (Fit rechnet auf den naechsten Punkt).
    var visibleRadius = Math.max(90, Math.min(state.width, state.height) * 0.56);
    return Math.max(220, Math.min(4000, radius + radius * 620 / visibleRadius));
  }

  function setDistance(distance, updateDefault) {
    state.distance = Math.max(220, Math.min(4000, distance));
    if (updateDefault) state.defaultDistance = state.distance;
    document.getElementById('knowledge-depth').textContent = Math.round(state.defaultDistance / state.distance * 100) + '%';
  }

  function pointerDistance() {
    var points = Array.from(state.pointers.values());
    if (points.length !== 2) return null;
    return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
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
    if (!prefersReducedMotion) {
      var now = performance.now();
      node.flash = 1; state.rings.push({ node: node, start: now });
      state.edges.filter(function (edge) { return edge.source === node.id || edge.target === node.id; })
        .slice(0, 14).forEach(function (edge) { spawnPulse(node.id, now, edge); });
    }
    var open = document.getElementById('knowledge-open');
    open.hidden = !node.path; open.href = node.path ? '/?file=' + encodeURIComponent(node.path) : '#';
    inspector.hidden = false;
    if (announce) document.getElementById('knowledge-a11y-status').textContent = copy.selected(node.label, neighbors.size);
  }

  function focusNode(node) {
    if (!node) return;
    if (prefersReducedMotion) {
      state.targetX = node.x; state.targetY = node.y; state.targetZ = node.z;
      setDistance(Math.max(300, state.distance * .72));
      return;
    }
    state.goal = { x: node.x, y: node.y, z: node.z };
    state.distanceGoal = Math.max(300, state.distance * .72);
  }

  function resetView() {
    state.yaw = -.35; state.pitch = .18;
    state.neighborhoodOnly = false; state.distanceGoal = null; state.refitted = false; state.userZoomed = false;
    if (prefersReducedMotion) { state.targetX = 0; state.targetY = 0; state.targetZ = 0; state.goal = null; }
    else state.goal = { x: 0, y: 0, z: 0 };
    restoreGraphPositions(true);
    selectNode(null, false);
    setDistance(fitGraphDistance(), true);
  }

  canvas.addEventListener('pointerdown', function (event) {
    if (!state.pointers.has(event.pointerId) && state.pointers.size >= 2) return;
    canvas.setPointerCapture(event.pointerId);
    state.lastInteraction = performance.now(); state.distanceGoal = null;
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size === 1) {
      state.dragging = true; state.moved = false;
      state.lastX = event.clientX; state.lastY = event.clientY;
    } else if (state.pointers.size === 2) {
      state.dragging = false; state.moved = true; state.pinchDistance = pointerDistance();
    }
  });
  canvas.addEventListener('pointermove', function (event) {
    var point = eventPoint(event); state.hovered = nearestNode(point.x, point.y);
    state.pointer.x = point.x; state.pointer.y = point.y; state.pointer.active = event.pointerType !== 'touch';
    if (state.pointers.has(event.pointerId)) {
      state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    }
    if (state.pointers.size === 2) {
      var distance = pointerDistance();
      if (distance && state.pinchDistance) {
        state.userZoomed = true; state.distanceGoal = null;
        setDistance(state.distance * state.pinchDistance / distance);
      }
      state.pinchDistance = distance; state.moved = true;
      return;
    }
    canvas.style.cursor = state.dragging ? 'grabbing' : (state.hovered ? 'pointer' : 'grab');
    if (!state.dragging) return;
    var dx = event.clientX - state.lastX; var dy = event.clientY - state.lastY;
    if (Math.abs(dx) + Math.abs(dy) > 2) state.moved = true;
    state.yaw += dx * .006; state.pitch = Math.max(-1.2, Math.min(1.2, state.pitch + dy * .005));
    state.lastX = event.clientX; state.lastY = event.clientY;
  });
  canvas.addEventListener('pointerup', function (event) {
    var selectOnRelease = state.pointers.size === 1 && !state.moved;
    state.pointers.delete(event.pointerId);
    state.pinchDistance = null;
    if (state.pointers.size === 1) {
      var remaining = Array.from(state.pointers.values())[0];
      state.lastX = remaining.x; state.lastY = remaining.y; state.dragging = true; state.moved = true;
    } else {
      state.dragging = false;
    }
    if (selectOnRelease) { var point = eventPoint(event); selectNode(nearestNode(point.x, point.y), true); }
  });
  canvas.addEventListener('pointercancel', function (event) {
    state.pointers.delete(event.pointerId); state.pinchDistance = null; state.dragging = false; state.moved = true;
  });
  canvas.addEventListener('lostpointercapture', function (event) {
    state.pointers.delete(event.pointerId); state.pinchDistance = null; state.dragging = false; state.moved = true;
  });
  canvas.addEventListener('pointerleave', function () { state.pointer.active = false; state.hovered = null; });
  canvas.addEventListener('dblclick', function (event) {
    var point = eventPoint(event); var node = nearestNode(point.x, point.y);
    if (node && node.path) window.location.href = '/?file=' + encodeURIComponent(node.path);
  });
  canvas.addEventListener('wheel', function (event) {
    event.preventDefault();
    state.lastInteraction = performance.now(); state.distanceGoal = null; state.userZoomed = true;
    setDistance(state.distance * Math.exp(event.deltaY * .001));
  }, { passive: false });
  canvas.addEventListener('keydown', function (event) {
    var step = Math.max(10, state.distance * .025); var handled = true;
    state.lastInteraction = performance.now(); state.goal = null;
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
        prepareGraph(payload); resetView(); startIntro();
      })
      .catch(function () {
        document.getElementById('knowledge-loading').hidden = true;
        document.getElementById('knowledge-error').hidden = false;
      });
  }

  new ResizeObserver(resize).observe(viewport);
  resize(); loadGraph(); state.frame = window.requestAnimationFrame(render);
  window.addEventListener('pagehide', function () {
    window.cancelAnimationFrame(state.frame); state.frame = 0; state.pointers.clear();
  });
  window.addEventListener('pageshow', function () {
    if (!state.frame) { resize(); state.frame = window.requestAnimationFrame(render); }
  });
}());
