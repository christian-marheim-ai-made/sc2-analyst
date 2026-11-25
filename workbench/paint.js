/* The workbench's painter: it fetches the view models (src/sc2analyst/workbench_vm.py) and paints
   them. It decides nothing - which series, labels, colours, scales and verdicts are the view
   model's. What is here: building elements, scaling a value to a pixel, routing by the URL. */
(function () {
  'use strict';

  const SVGNS = 'http://www.w3.org/2000/svg';
  const state = { debug: false, scrubTimer: null };

  // --- small builders ---------------------------------------------------------------------------
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'on') { for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn); }
      else if (k === 'class') n.className = v;
      else if (k === 'html') n.innerHTML = v;      // only ever the server's own SVG markup
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  }
  function svg(tag, attrs, ...kids) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) n.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  }
  async function api(path) {
    const r = await fetch(path);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  function fmt(v) {
    if (v === null || v === undefined) return '—';
    if (typeof v !== 'number') return String(v);
    if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString('en-US');
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
  }
  function signed(v) { return v === null || v === undefined ? '—' : (v > 0 ? '+' : '') + fmt(v); }
  function clock(t) {
    const s = Math.max(0, Math.floor(t));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function chip(prov, age) {
    if (!state.debug || !prov) return null;
    const cls = prov.startsWith('exact') ? 'exact' : prov;
    const text = prov === 'sample' && age !== null && age !== undefined ? 'sample · ' + age + ' s' : prov;
    return el('span', { class: 'wb-chip ' + cls, title: 'where this number comes from' }, text);
  }
  function panel(title, ...kids) { return el('section', { class: 'wb-panel' }, title ? el('h2', {}, title) : null, ...kids); }
  function screen(...kids) { const s = document.getElementById('screen'); s.replaceChildren(...kids); }
  function fail(e) { screen(panel('Could not show this', el('p', { class: 'wb-bad' }, String(e.message || e)))); }

  // --- routing ----------------------------------------------------------------------------------
  function route() {
    const h = location.hash.slice(1) || '/library';
    const [path, query = ''] = h.split('?');
    return { parts: path.split('/').filter(Boolean).map(decodeURIComponent), q: Object.fromEntries(new URLSearchParams(query)) };
  }
  function hashWith(parts, q) {
    const qs = new URLSearchParams(Object.entries(q).filter(([, v]) => v !== '' && v !== undefined && v !== null)).toString();
    return '#/' + parts.map(encodeURIComponent).join('/') + (qs ? '?' + qs : '');
  }
  function crumbs(items) {
    document.getElementById('crumbs').replaceChildren(...items.map(([label, href]) =>
      href ? el('a', { href }, label) : el('span', {}, label)));
  }

  // --- charts -----------------------------------------------------------------------------------
  function scaleX(t, xmax, w, pad) { return pad + (t / xmax) * (w - 2 * pad); }
  function scaleY(v, lo, hi, h, pad) { return h - pad - ((v - lo) / (hi - lo)) * (h - 2 * pad); }

  function lineChart(o) {
    const w = o.width || 1000, h = o.height || 200, pad = 28;
    const [lo, hi] = o.range;
    const root = svg('svg', { class: 'wb-chart', viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': o.label || '' });
    for (const f of o.fights || []) {
      const x0 = scaleX(f.start, o.xmax, w, pad), x1 = Math.max(x0 + 2, scaleX(f.end, o.xmax, w, pad));
      root.append(svg('rect', { x: x0, y: pad, width: x1 - x0, height: h - 2 * pad,
                                fill: f.benefited_colour || '#8b95a7', 'fill-opacity': 0.14 },
                      svg('title', {}, `Fight ${f.id} ${f.clock}`)));
    }
    if (lo < 0 && hi > 0) {
      const y = scaleY(0, lo, hi, h, pad);
      root.append(svg('line', { x1: pad, x2: w - pad, y1: y, y2: y, stroke: '#5b6577', 'stroke-dasharray': '4 4' }));
    }
    for (let m = 0; m * 60 <= o.xmax; m += 5) {
      const x = scaleX(m * 60, o.xmax, w, pad);
      root.append(svg('line', { x1: x, x2: x, y1: h - pad, y2: h - pad + 4, stroke: '#5b6577' }),
                  svg('text', { x, y: h - 8, 'text-anchor': 'middle' }, m + '′'));
    }
    root.append(svg('text', { x: pad, y: 14 }, fmt(hi)), svg('text', { x: pad, y: h - pad - 4 }, fmt(lo)));
    for (const ln of o.lines) {
      let seg = [];
      const segs = [];
      ln.values.forEach((v, i) => {
        if (v === null || v === undefined) { if (seg.length) segs.push(seg); seg = []; return; }
        seg.push(scaleX(o.t[i], o.xmax, w, pad).toFixed(1) + ',' + scaleY(v, lo, hi, h, pad).toFixed(1));
      });
      if (seg.length) segs.push(seg);
      for (const s of segs) root.append(svg('polyline', { points: s.join(' '), fill: 'none', stroke: ln.colour,
                                                          'stroke-width': 1.8 }, svg('title', {}, ln.label)));
    }
    const cursor = svg('line', { x1: pad, x2: pad, y1: pad, y2: h - pad, stroke: '#fbbf24', 'stroke-width': 1,
                                 visibility: 'hidden' });
    root.append(cursor);
    root.moveCursor = (t) => {
      const x = scaleX(t, o.xmax, w, pad);
      cursor.setAttribute('x1', x); cursor.setAttribute('x2', x); cursor.setAttribute('visibility', 'visible');
    };
    return root;
  }

  function fightStrip(vm, key) {
    const w = 1000, h = 60, pad = 28, xmax = vm.header.end;
    const maxv = Math.max(1, ...vm.fights.map(f => f.value));
    const root = svg('svg', { class: 'wb-chart', viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'fights' });
    for (const f of vm.fights) {
      const x0 = scaleX(f.start, xmax, w, pad), x1 = Math.max(x0 + 3, scaleX(f.end, xmax, w, pad));
      const bh = 6 + 40 * Math.sqrt(f.value / maxv);
      const r = svg('rect', { x: x0, y: h - 8 - bh, width: x1 - x0, height: bh, rx: 2,
                              fill: f.benefited_colour || '#5b6577',
                              stroke: f.disagree ? '#fbbf24' : 'none', 'stroke-width': f.disagree ? 2 : 0,
                              style: 'cursor:pointer', 'data-el': 'fight-bar-' + f.id },
                    svg('title', {}, `Fight ${f.id} · ${f.clock} · value ${fmt(f.value)}` +
                        (f.benefited_label ? ` · benefited: ${f.benefited_label}` : '') +
                        (f.disagree ? ' · the two verdicts disagree' : '')));
      r.addEventListener('click', () => { location.hash = hashWith(['game', key, 'fight', String(f.id)], qDebug()); });
      root.append(r);
    }
    return root;
  }
  function qDebug() { return state.debug ? { debug: '1' } : {}; }

  // --- screens ----------------------------------------------------------------------------------
  async function library(q) {
    crumbs([['Library']]);
    const vm = await api('/api/library?' + new URLSearchParams(q).toString());
    const sel = (name, label, options) => el('label', {}, label + ' ',
      el('select', { 'data-el': 'filter-' + name, on: { change: (e) => { location.hash = hashWith(['library'], { ...q, [name]: e.target.value }); } } },
        el('option', { value: '' }, 'any'),
        ...options.map(o => el('option', { value: o, selected: q[name] === o }, o))));
    const filters = el('div', { class: 'wb-filters' },
      sel('map', 'Map', vm.filters.maps), sel('format', 'Format', vm.filters.formats),
      sel('race', 'Race', vm.filters.races), sel('opening', 'Opening', vm.filters.openings),
      sel('result', 'Your result', ['Win', 'Loss']),
      el('label', {}, el('input', { type: 'checkbox', 'data-el': 'filter-owner', checked: q.owner === '1',
        on: { change: (e) => { location.hash = hashWith(['library'], { ...q, owner: e.target.checked ? '1' : '' }); } } }),
        ' Player_1 played'));
    const rows = vm.games.map(g => el('tr', { class: 'wb-click', 'data-el': 'game-' + g.key,
      on: { click: () => { location.hash = hashWith(['game', g.key], qDebug()); } } },
      el('td', {}, g.start), el('td', {}, g.map), el('td', {}, g.format), el('td', {}, g.races),
      el('td', {}, g.length), el('td', {}, g.result || '')));
    screen(
      panel('Games', el('p', { class: 'wb-dim' }, `${vm.shown} of ${vm.count} games`), filters,
        el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table' },
          el('tr', {}, ...['Start', 'Map', 'Format', 'Races', 'Length', 'Your result'].map(x => el('th', {}, x))),
          ...rows))),
      await folderPicker(q.path || ''));
  }

  async function folderPicker(path) {
    const box = el('div', {});
    const vm = await api('/api/folders' + (path ? '?path=' + encodeURIComponent(path) : ''));
    const go = (p) => { location.hash = hashWith(['library'], { ...route().q, path: p || '' }); };
    const status = el('div', { class: 'wb-log', 'data-el': 'scan-log' });
    async function poll() {
      const s = await api('/api/library/scan');
      status.textContent = (s.folder ? `Folder: ${s.folder}\n` : '') + s.log.join('\n') +
        (s.running ? '\n… running' : (s.exit === null ? '' : `\nfinished, exit ${s.exit}`));
      if (s.running) setTimeout(poll, 2000);
    }
    const kids = [el('p', { class: 'wb-dim' }, 'Pick your replay folder. A scan makes an anonymised copy of each new replay on L:, imports a new map, and builds its record - the existing tools, nothing new.')];
    if (vm.path) {
      kids.push(el('p', {}, el('b', {}, vm.path), ' · ', `${vm.replays_here} replays here `,
        vm.parent ? el('a', { href: '#', on: { click: (e) => { e.preventDefault(); go(vm.parent); } } }, '↑ up') : null, ' ',
        el('a', { href: '#', on: { click: (e) => { e.preventDefault(); go(''); } } }, 'roots')));
      if (vm.replays_here) kids.push(el('button', { class: 'wb-button', 'data-el': 'scan-start',
        on: { click: async () => { try { await post('/api/library/scan', { folder: vm.path }); poll(); } catch (e) { status.textContent = e.message; } } } },
        `Scan this folder (${vm.replays_here} replays)`));
    }
    kids.push(el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table' },
      ...vm.folders.map(f => el('tr', { class: 'wb-click', on: { click: () => go(f.path) } },
        el('td', {}, f.name), el('td', {}, f.replays === null ? '' : f.replays + ' replays'))))));
    kids.push(status);
    box.append(panel('Add replays from a folder', ...kids));
    poll();
    return box;
  }

  function teamsBlock(h) {
    return el('div', { class: 'wb-teams' }, ...h.teams.map(t => el('div', { class: 'wb-team', style: `border-color:${t.colour}` },
      el('b', {}, `${t.label} · ${t.result || ''}`),
      ...t.players.map(p => el('div', { title: p.opening_note || '' }, el('span', { class: 'wb-swatch', style: `background:${p.colour}` }),
        `${p.name} · ${p.race}`, p.opening ? el('span', { class: 'wb-chip' }, p.opening.toLowerCase()) : null)))));
  }

  async function game(key) {
    const vm = await api(`/api/game/${encodeURIComponent(key)}/series` + (state.debug ? '?debug=1' : ''));
    const h = vm.header;
    crumbs([['Library', '#/library'], [`${h.map} · ${h.start}`]]);
    const xmax = h.end;
    const charts = [];

    // the gap chart: team A minus team B, the lines the person picks
    let gapPanel = null;
    if (vm.gap) {
      const on = new Set(vm.gap.lines.filter(l => l.default).map(l => l.key));
      const holder = el('div', {});
      const toggles = el('div', { class: 'wb-toggles' });
      const draw = () => {
        const lines = vm.gap.lines.filter(l => on.has(l.key));
        const lo = Math.min(...lines.map(l => vm.gap.ranges[l.key][0]), 0);
        const hi = Math.max(...lines.map(l => vm.gap.ranges[l.key][1]), 0);
        const c = lineChart({ t: vm.t, lines, range: [lo, hi === lo ? lo + 1 : hi], xmax, fights: vm.fights, label: 'gap' });
        charts[0] = c;
        holder.replaceChildren(c);
        toggles.replaceChildren(...vm.gap.lines.map(l => el('button', { class: on.has(l.key) ? 'on' : '', 'data-el': 'gap-' + l.key,
          style: on.has(l.key) ? `border-color:${l.colour};color:${l.colour}` : '',
          on: { click: () => { on.has(l.key) ? on.delete(l.key) : on.add(l.key); if (!on.size) on.add(l.key); draw(); } } },
          l.label, chip(l.provenance))));
      };
      draw();
      gapPanel = panel(`${vm.gap.a} − ${vm.gap.b}`,
        el('p', { class: 'wb-dim' }, `Above the dashed line ${vm.gap.a} is ahead. Shaded: the fights, in the colour of the team that benefited.`),
        toggles, holder);
    }

    // the scrubber and the snapshot under it
    const snapBox = el('div', {}, el('p', { class: 'wb-dim' }, 'Move the slider to see every player at that second.'));
    const scrub = el('input', { type: 'range', class: 'wb-scrub', min: 0, max: Math.floor(xmax), step: 1, value: 0,
      'data-el': 'scrubber', on: { input: (e) => {
        const t = Number(e.target.value);
        charts.forEach(c => c && c.moveCursor && c.moveCursor(t));
        clearTimeout(state.scrubTimer);
        state.scrubTimer = setTimeout(() => snapshot(key, t, snapBox), 150);
      } } });

    // each player on their own: one chart per metric, one line per player, one scale per metric
    const multiples = el('div', { class: 'wb-multiples' }, ...vm.metrics.map(m => {
      const lines = vm.players.map(p => ({ label: p.name, colour: p.colour, values: p.metrics[m.key].values }));
      const c = lineChart({ t: vm.t, lines, range: vm.ranges[m.key], xmax, height: 170, label: m.label });
      charts.push(c);
      return el('div', {}, el('h3', {}, m.label, chip(vm.players[0] ? vm.players[0].metrics[m.key].provenance : null)), c);
    }));
    const legend = el('div', { class: 'wb-legend' }, ...vm.players.map(p =>
      el('span', {}, el('span', { class: 'wb-swatch', style: `background:${p.colour}` }), p.name)));

    const fightRows = vm.fights.map(f => el('tr', { class: 'wb-click', 'data-el': 'fight-row-' + f.id,
      on: { click: () => { location.hash = hashWith(['game', key, 'fight', String(f.id)], qDebug()); } } },
      el('td', {}, String(f.id)), el('td', {}, f.clock), el('td', {}, f.where || '—'), el('td', {}, fmt(f.value)),
      el('td', {}, String(f.deaths)), el('td', {}, f.benefited_label || '—'), el('td', {}, f.lost_less_label || '—'),
      el('td', {}, f.disagree ? el('span', { class: 'wb-flag', title: 'won the fight, lost the economy - or the reverse' }, 'differ') : '')));

    screen(
      panel(`${h.map} · ${h.start} · ${h.format} · ${h.end_clock}`, teamsBlock(h)),
      gapPanel,
      panel('At a second', scrub, snapBox),
      panel('Fights', fightStrip(vm, key),
        el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table' },
          el('tr', {}, ...['#', 'When', 'Where', 'Value', 'Deaths', 'Benefited (economy counted)', 'Lost less value', ''].map(x => el('th', {}, x))),
          ...fightRows))),
      panel('Each player on their own', legend, multiples),
      panel('Events', el('div', { class: 'wb-events' }, ...vm.events.map(e => el('div', {}, `${clock(e.t)} · ${e.label}`)))));
    const t0 = Number(route().q.t || 0);
    if (t0) { scrub.value = t0; charts.forEach(c => c && c.moveCursor && c.moveCursor(t0)); }
    snapshot(key, t0, snapBox);
  }

  async function snapshot(key, t, box) {
    try {
      const vm = await api(`/api/game/${encodeURIComponent(key)}/at?t=${t}` + (state.debug ? '&debug=1' : ''));
      const head = el('tr', {}, el('th', {}, vm.clock), ...vm.metrics.map(m => el('th', {}, m.label)));
      const row = (name, colour, cells, cls) => el('tr', { class: cls || '' },
        el('td', {}, el('span', { class: 'wb-swatch', style: `background:${colour}` }), name),
        ...vm.metrics.map(m => el('td', {}, fmt(cells[m.key].v), chip(cells[m.key].provenance, cells[m.key].age))));
      const rows = [];
      for (const team of vm.teams) {
        for (const p of vm.players.filter(p => p.team === team.team)) rows.push(row(p.name, p.colour, p.cells));
        rows.push(row(team.label, team.colour, team.cells, 'wb-teamrow'));
      }
      box.replaceChildren(el('div', { class: 'wb-row' },
        el('div', { class: 'wb-grow wb-scroll' }, el('table', { class: 'wb-table', 'data-el': 'snapshot' }, head, ...rows)),
        vm.minimap ? el('div', { class: 'wb-minimap', html: vm.minimap }) : el('p', { class: 'wb-dim' }, vm.minimap_note)));
    } catch (e) { box.replaceChildren(el('p', { class: 'wb-bad' }, e.message)); }
  }

  async function fight(key, id) {
    const vm = await api(`/api/game/${encodeURIComponent(key)}/fight/${id}` + (state.debug ? '?debug=1' : ''));
    crumbs([['Library', '#/library'], ['Game', hashWith(['game', key], qDebug())], [`Fight ${vm.id}`]]);
    const v = vm.verdict;
    const verdict = el('div', { class: 'wb-verdict' },
      el('div', {}, el('b', {}, 'Benefited, the economy counted: '), v.benefited || 'not known',
        el('div', { class: 'wb-dim' }, `standing gap ${signed(v.dN)} now · income gap ${signed(v.dr)} / min after · over the next 5 min ${signed(v.benefit_5min)} (an estimate; seen from ${v.team_a})`)),
      el('div', {}, el('b', {}, 'Lost less value: '), v.lost_less || 'even'),
      v.disagree ? el('div', { class: 'wb-flag' }, 'The two verdicts disagree: one team won the fight and the other the economy.') : null);
    const head = el('tr', {}, el('th', {}, `${clock(vm.before_t)} → ${clock(vm.after_t)}`), ...vm.metrics.map(m => el('th', {}, m.label)));
    const row = (name, colour, table, cls) => el('tr', { class: cls || '' },
      el('td', {}, el('span', { class: 'wb-swatch', style: `background:${colour}` }), name),
      ...vm.metrics.map(m => { const c = table[m.key]; return el('td', { title: `${fmt(c.before)} → ${fmt(c.after)}` }, signed(c.delta), chip(c.provenance)); }));
    const rows = [];
    for (const team of vm.teams) {
      for (const p of vm.players.filter(p => p.team === team.team)) rows.push(row(p.name, p.colour, p.table));
      rows.push(row(team.label, team.colour, team.table, 'wb-teamrow'));
    }
    const losses = el('table', { class: 'wb-table' },
      el('tr', {}, el('th', {}, 'Lost'), ...vm.loss_classes.map(c => el('th', {}, c)), el('th', {}, 'unpriced')),
      ...vm.players.map(p => el('tr', {}, el('td', {}, el('span', { class: 'wb-swatch', style: `background:${p.colour}` }), p.name),
        ...vm.loss_classes.map(c => el('td', {}, p.losses[c].count ? `${fmt(p.losses[c].value)} (${p.losses[c].count})` : '')),
        el('td', {}, p.unpriced ? String(p.unpriced) : ''))));
    const nav = el('p', {},
      vm.prev ? el('a', { href: hashWith(['game', key, 'fight', String(vm.prev)], qDebug()) }, '← fight ' + vm.prev) : null, ' ',
      vm.next ? el('a', { href: hashWith(['game', key, 'fight', String(vm.next)], qDebug()) }, 'fight ' + vm.next + ' →') : null);
    const deaths = vm.death_rows ? panel('Every death (debug)', el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table' },
      el('tr', {}, ...['When', 'Lost by', 'Type', 'Killed by', 'Killer type', 'Value', 'Class'].map(x => el('th', {}, x))),
      ...vm.death_rows.map(d => el('tr', {}, el('td', {}, d.clock), el('td', {}, d.victim), el('td', {}, d.type),
        el('td', {}, d.killer), el('td', {}, d.killer_type || ''), el('td', {}, d.value === null ? 'unpriced' : fmt(d.value)), el('td', {}, d.class)))))) : null;
    screen(
      panel(`Fight ${vm.id} · ${vm.clock} · ${vm.where || 'place unknown'} · value ${fmt(vm.value)} · ${vm.deaths} deaths`, nav, verdict,
        (() => { const box = el('div', {}); box.append(el('button', { class: 'wb-button', 'data-el': 'ask-fight',
          on: { click: () => askFight(key, vm.id, box) } }, 'Ask the model about this fight (T-fight)'), ' ',
          el('button', { class: 'wb-button', 'data-el': 'packet-fight', on: { click: async () => {
            try { const pk = await post('/api/frontier/packet', { task: 'T-fight', game: key, target: { fight: Number(vm.id) }, label: `${key} fight ${vm.id}` });
              try { await navigator.clipboard.writeText(pk.text); } catch (e) { /* the Frontier screen has it */ }
              box.append(el('p', {}, 'Packet made and copied: ', el('a', { href: '#/frontier' }, 'paste the reply on the Frontier screen')));
            } catch (e) { box.append(el('p', { class: 'wb-bad' }, e.message)); } } } }, 'Copy for ChatGPT')); return box; })()),
      panel('Before → after, each player and each team (Δ; hover for both values)', el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table', 'data-el': 'fight-table' }, head, ...rows))),
      el('div', { class: 'wb-row' },
        el('div', { class: 'wb-grow' }, panel('What each player lost, by class (value, count)', losses,
          el('p', { class: 'wb-dim' }, 'Income denied by the workers lost, a lower bound: ' + vm.denied_income))),
        vm.minimap ? panel('Where they died', el('div', { class: 'wb-minimap', html: vm.minimap })) : null),
      deaths);
  }

  // --- studies and aggregates (W7) -------------------------------------------------------------
  function barChart(c) {
    const w = 640, h = 34 + 26 * c.bars.length, pad = 150;
    const max = Math.max(1, ...c.bars.map(b => Math.abs(b.value || 0)));
    const root = svg('svg', { class: 'wb-chart', viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': c.title });
    c.bars.forEach((b, i) => {
      const y = 20 + 26 * i, bw = ((Math.abs(b.value || 0)) / max) * (w - pad - 70);
      root.append(svg('text', { x: pad - 8, y: y + 13, 'text-anchor': 'end' }, b.label),
                  svg('rect', { x: pad, y, width: Math.max(1, bw), height: 18, rx: 3, fill: '#7aa2ff' }),
                  svg('text', { x: pad + bw + 6, y: y + 13 }, fmt(b.value) + (c.unit || '')));
    });
    return root;
  }
  function tablesOf(vm) {
    return vm.tables.map(t => panel(t.title, el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table' },
      el('tr', {}, ...t.columns.map(c => el('th', {}, c))),
      ...t.rows.map((r, i) => el('tr', t.hrefs ? { class: 'wb-click', on: { click: () => { location.hash = t.hrefs[i]; } } } : {},
        ...r.map(x => el('td', {}, x === null || x === undefined ? '—' : String(x)))))))));
  }
  function reviewedBlock(vm, body) {
    const box = el('div', {});
    const paint = (rec) => box.replaceChildren(rec
      ? el('p', { class: 'wb-dim' }, `Reviewed ${rec.at} — the model may now read this table.`)
      : el('div', {}, el('p', { class: 'wb-dim' }, 'Not reviewed: the model does not read this table until you mark it (R-M).'),
          el('button', { class: 'wb-button', 'data-el': 'mark-reviewed', on: { click: async () => {
            try { paint(await post('/api/reviewed', body)); } catch (e) { box.append(el('p', { class: 'wb-bad' }, e.message)); } } } },
            'I have reviewed this table')));
    paint(vm.reviewed);
    return box;
  }
  async function studies() {
    crumbs([['Studies']]);
    const vm = await api('/api/studies');
    const form = el('div', { class: 'wb-filters' });
    const last = el('input', { type: 'text', value: '30', size: 4, 'data-el': 'agg-last' });
    const fmtSel = el('select', { 'data-el': 'agg-format' }, ...['', '1v1', '2v2', '3v3'].map(v => el('option', { value: v }, v || 'any format')));
    const opSel = el('select', { 'data-el': 'agg-opening' }, ...['', 'ONE-BASE ATTACK', 'FAST EXPAND', 'STANDARD', 'NO EXPANSION'].map(v => el('option', { value: v }, v || 'any opening')));
    const status = el('span', { class: 'wb-dim' });
    form.append(el('span', {}, 'Siege tanks over your last'), last, el('span', {}, 'games'), fmtSel, opSel,
      el('button', { class: 'wb-button', 'data-el': 'agg-run', on: { click: async () => {
        status.textContent = 'running…';
        try {
          const r = await post('/api/aggregate/run', { repository: 'siege', last: last.value, fmt: fmtSel.value, opening: opSel.value });
          location.hash = hashWith(['aggregate', r.repository, r.selection, r.version], {});
        } catch (e) { status.textContent = e.message; } } } }, 'Run'), status);
    const item = (label, href, rec) => el('tr', { class: 'wb-click', on: { click: () => { location.hash = href; } } },
      el('td', {}, label), el('td', {}, rec ? 'reviewed ' + rec.at.slice(0, 10) : el('span', { class: 'wb-flag' }, 'not reviewed')));
    screen(
      panel('Studies — your three questions over the library', el('table', { class: 'wb-table' },
        ...vm.studies.map(s => item(`${s.id} · ${s.title} (${s.version})`, hashWith(['study', s.id, s.version], {}), s.reviewed)))),
      panel('Aggregates — one question over a chosen set of your games', form, el('table', { class: 'wb-table' },
        ...vm.aggregates.map(a => item(`${a.repository} · ${a.selection} (${a.version})`,
          hashWith(['aggregate', a.repository, a.selection, a.version], {}), a.reviewed)))));
  }
  async function study(id, ver) {
    const vm = await api(`/api/study/${encodeURIComponent(id)}/${encodeURIComponent(ver)}`);
    crumbs([['Studies', '#/studies'], [vm.id]]);
    screen(panel(`${vm.id} · ${vm.title}`, el('p', { class: 'wb-dim' }, vm.causation),
        vm.links.length ? el('p', {}, 'Open the games: ', ...vm.links.map(l => el('a', { href: l.href, style: 'margin-right:12px' }, l.label))) : null,
        reviewedBlock(vm, { kind: 'study', id: vm.id, version: vm.version })),
      ...vm.charts.map(c => panel(c.title, barChart(c))), ...tablesOf(vm));
  }
  async function aggregateScreen(repo, sel, ver) {
    const vm = await api(`/api/aggregate/${[repo, sel, ver].map(encodeURIComponent).join('/')}`);
    crumbs([['Studies', '#/studies'], [`${vm.repository} · ${vm.selection}`]]);
    screen(panel(`${vm.repository} · ${vm.selection} · ${vm.games} games`, el('p', { class: 'wb-dim', style: 'white-space:pre-wrap' }, vm.about),
        reviewedBlock(vm, { kind: 'aggregate', repository: vm.repository, selection: vm.selection, version: vm.version })),
      ...vm.charts.map(c => panel(c.title, barChart(c))), ...tablesOf(vm));
  }

  // --- the model (W9) ---------------------------------------------------------------------------
  async function model() {
    crumbs([['Model']]);
    const st = await api('/api/model');
    const runs = await api('/api/runs');
    const note = el('p', { class: 'wb-dim' });
    const start = (backend) => async () => {
      note.textContent = backend === 'llamacpp' ? 'starting the model server on the card…' : 'starting the stand-in…';
      try {
        const r = await post('/api/model/start', { backend });
        if (r.started === false) { note.textContent = r.why; return; }
        location.reload();                  // the kit is loaded with the server's address, once
      } catch (e) { note.textContent = e.message; }
    };
    const controls = st.llm_server
      ? el('div', {}, el('p', {}, `Model server up (${st.model}) at ${st.llm_server}.`),
          el('button', { class: 'wb-button', 'data-el': 'model-stop', on: { click: async () => { await post('/api/model/stop', {}); location.reload(); } } }, 'Stop the model server'))
      : el('div', {}, el('p', { class: 'wb-dim' }, `No model server. The thinker needs ${st.need_mib} MiB of the card; it is loaded only when a task runs, and the card is given back when it idles.`),
          el('button', { class: 'wb-button', 'data-el': 'model-start-card', on: { click: start('llamacpp') } }, 'Start the model server (the card)'), ' ',
          el('button', { class: 'wb-button', 'data-el': 'model-start-standin', on: { click: start('mock') } }, 'Start the stand-in (no card)'));
    const rows = runs.map(r => el('tr', { class: 'wb-click', on: { click: () => { location.hash = hashWith(['run', r.correlation_id], {}); } } },
      el('td', {}, r.task), el('td', {}, JSON.stringify(r.target || {}).slice(0, 80)), el('td', {}, r.status || ''),
      el('td', {}, r.kept === undefined ? '' : `${r.kept} kept · ${r.dropped} dropped`), el('td', {}, r.workspace || '')));
    const check = el('div', { 'data-el': 'check-host' });
    screen(panel('The model server', controls, note,
        el('p', {}, el('a', { href: '#/grade' }, 'Grade the claims (blind)'), ' · ', el('a', { href: '#/compare' }, 'Compare prompts and models'), ' · ', el('a', { href: '#/frontier' }, 'The ChatGPT packets'))),
      st.llm_server ? planPanel() : null,
      panel('Runs', el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table' },
        el('tr', {}, ...['Task', 'Target', 'Status', 'Claims', 'Workspace'].map(x => el('th', {}, x))), ...rows))),
      st.llm_server ? panel('Check — requests held for you before they are sent (debug runs)', check) : null);
    if (st.llm_server && window.llmKit) { try { llmKit.place('check', check); } catch (e) { console.error(e); } }
  }

  function planPanel() {
    const fam = el('select', { 'data-el': 'plan-task' }, el('option', { value: 'T-fight' }, 'T-fight: the biggest fights of each game'),
      el('option', { value: 'T-opening' }, 'T-opening: every one-base attack'));
    const per = el('input', { type: 'text', value: '5', size: 3, 'data-el': 'plan-per' });
    const when = el('input', { type: 'time', value: '23:00', 'data-el': 'plan-at' });
    const out = el('p', { class: 'wb-dim' });
    const body = () => ({ task: fam.value, set: '20', fights_per_game: Number(per.value) });
    const at = () => { const [h, m] = when.value.split(':').map(Number); const d = new Date(); d.setHours(h, m, 0, 0);
      if (d.getTime() < Date.now()) d.setDate(d.getDate() + 1); return d.getTime() / 1000; };
    return panel('Plan tonight — the 20-game set, one task at a time, when the card has room',
      el('div', { class: 'wb-filters' }, fam, el('span', {}, 'fights per game'), per, el('span', {}, 'not before'), when,
        el('button', { class: 'wb-button', 'data-el': 'plan-preview', on: { click: async () => {
          try { const r = await post('/api/batch/preview', body());
            out.textContent = `${r.tasks} tasks over ${r.games} games` + (r.hours !== null ? ` - about ${r.hours} h at ${r.seconds_each} s each` : ` - ${r.note}`);
          } catch (e) { out.textContent = e.message; } } } }, 'How big is it?'),
        el('button', { class: 'wb-button', 'data-el': 'plan-go', on: { click: async () => {
          try { const r = await post('/api/batch/plan', Object.assign(body(), { not_before: at() }));
            out.textContent = `${r.planned} tasks planned, not before ${new Date(r.not_before * 1000).toLocaleString()}. Leave the workbench running.`;
          } catch (e) { out.textContent = e.message; } } } }, 'Plan tonight')), out);
  }

  async function frontierScreen() {
    crumbs([['Model', '#/model'], ['Frontier']]);
    const ps = await api('/api/frontier/packets');
    const card = (pk) => {
      const reply = el('textarea', { rows: 6, style: 'width:100%', 'data-el': 'reply-' + pk.packet_id, placeholder: "paste ChatGPT Pro's whole reply here" });
      const out = el('p', { class: 'wb-dim' });
      return panel(`${pk.task} · ${pk.label}`,
        el('div', { class: 'wb-toggles' },
          el('button', { class: 'wb-button', 'data-el': 'copy-' + pk.packet_id, on: { click: async () => {
            try { await navigator.clipboard.writeText(pk.text); out.textContent = 'copied - paste it into ChatGPT Pro'; } catch (e) { out.textContent = 'copy refused by the browser: select the text below'; } } } }, 'Copy for ChatGPT'),
          el('button', { class: 'wb-button', 'data-el': 'import-' + pk.packet_id, on: { click: async () => {
            try { const r = await post('/api/frontier/import', { packet_id: pk.packet_id, reply: reply.value });
              out.replaceChildren(r.answer_dropped ? `answer dropped: ${r.answer_dropped}` : `${r.kept} claims kept, ${r.dropped} dropped - `,
                el('a', { href: hashWith(['run', r.correlation_id], {}) }, 'open it'), ' · then grade it blind');
            } catch (e) { out.textContent = e.message; } } } }, 'Import the reply')),
        out, reply, el('details', {}, el('summary', {}, 'the packet'), el('pre', { class: 'wb-log' }, pk.text)));
    };
    screen(panel('Frontier — the same task on ChatGPT Pro, by your paste', el('p', { class: 'wb-dim' },
        'Nothing is sent by the workbench. Copy a packet, paste it into ChatGPT Pro, paste the whole reply back: the same checks run on it, and Grade shows it blind beside the local answers.')),
      ...ps.map(card));
  }

  async function gradeScreen(q) {
    crumbs([['Model', '#/model'], ['Grade']]);
    const seed = Number(q.seed || 7);
    const list = await api('/api/grade/next?seed=' + seed);
    const card = (c) => {
      const box = el('div', { class: 'wb-panel' });
      const done = el('span', { class: 'wb-dim' });
      const btn = (value, label) => el('button', { class: 'wb-button', 'data-el': `grade-${value}-${c.hash}`, on: { click: async () => {
        try { await post('/api/grade', { hash: c.hash, cid: c.cid, grade: value }); done.textContent = 'graded ' + label; } catch (e) { done.textContent = e.message; } } } }, label);
      box.append(el('p', {}, c.text),
        el('div', { class: 'wb-log' }, c.cites.map(f => `${f.id}: ${f.text}`).join('\n')),
        el('p', { class: 'wb-dim' }, `${c.task} · `, c.game_key ? el('a', { href: hashWith(['game', c.game_key], {}) }, c.game_key) : ''),
        el('div', { class: 'wb-toggles' }, btn('right', '✓ right'), btn('wrong', '✗ wrong'), btn('useless', '~ right but useless'), btn('insight', '★ insight'), done));
      return box;
    };
    screen(panel('Grade — blind: the model and the prompt version are hidden', el('p', { class: 'wb-dim' },
        `${list.length} claims not yet graded (seed ${seed}). Your grades decide which prompt, model and route is kept.`),
        el('a', { href: hashWith(['grade'], { seed: String(seed + 1) }) }, 'another order')),
      ...list.map(card));
  }

  async function compareScreen() {
    crumbs([['Model', '#/model'], ['Compare']]);
    const rows = await api('/api/compare');
    screen(panel('Compare — per task, prompt version and model', el('div', { class: 'wb-scroll' }, el('table', { class: 'wb-table' },
      el('tr', {}, ...['Task', 'Version', 'Model', 'Runs', 'Claims kept by the checks', 'Graded', 'Precision (✓+★)', 'Insight', 'Seconds (p50)', 'Answer tokens (p50)'].map(x => el('th', {}, x))),
      ...rows.map(r => el('tr', {}, el('td', {}, r.task), el('td', {}, r.version), el('td', {}, r.model), el('td', {}, String(r.runs)),
        el('td', {}, r.auto_pass === null ? '—' : r.auto_pass + '%'), el('td', {}, String(r.graded)),
        el('td', {}, r.precision === null ? '—' : r.precision + '%'), el('td', {}, r.insight === null ? '—' : r.insight + '%'),
        el('td', {}, fmt(r.seconds_p50)), el('td', {}, fmt(r.tokens_p50))))))));
  }

  async function runScreen(cid) {
    const r = await api(`/api/run/${encodeURIComponent(cid)}`);
    crumbs([['Model', '#/model'], [r.task + ' · ' + cid]]);
    const kept = new Set((r.verdicts ? r.verdicts.kept : []).map(c => c.text));
    const factLink = (f) => {
      const t = f.src && f.src.t;
      return r.game_key && t !== null && t !== undefined
        ? el('a', { href: hashWith(['game', r.game_key], { t: String(Math.floor(t)) }), title: 'open the timeline at this second' }, f.id)
        : el('span', {}, f.id);
    };
    const facts = el('table', { class: 'wb-table' }, el('tr', {}, ...['Fact', 'Text', 'Source'].map(x => el('th', {}, x))),
      ...(r.facts || []).map(f => el('tr', {}, el('td', {}, factLink(f)), el('td', { style: 'white-space:normal;text-align:left' }, f.text),
        el('td', {}, f.src ? `${f.src.repo} · ${f.src.key}` : ''))));
    const claims = [];
    if (r.verdicts) {
      for (const c of r.verdicts.kept) claims.push(el('tr', {}, el('td', {}, '✓ kept'), el('td', { style: 'white-space:normal;text-align:left' }, c.text), el('td', {}, (c.cites || []).join(' '))));
      for (const d of r.verdicts.dropped) claims.push(el('tr', {}, el('td', { class: 'wb-bad' }, '✗ ' + d.why), el('td', { style: 'white-space:normal;text-align:left' }, d.claim.text), el('td', {}, (d.claim.cites || []).join(' '))));
    }
    screen(panel(`${r.task} (${r.version}) · ${r.status}`,
        el('p', { class: 'wb-dim' }, `model ${r.model || ''} · ${r.usage ? (r.usage.prompt_tokens + ' prompt + ' + r.usage.completion_tokens + ' answer tokens') : ''}`),
        r.verdicts && r.verdicts.answer_dropped ? el('p', { class: 'wb-bad' }, 'Answer dropped: ' + r.verdicts.answer_dropped) : null,
        r.answer && r.answer.cannot_answer && r.answer.cannot_answer.length ? el('p', {}, 'Cannot answer: ' + r.answer.cannot_answer.join(' · ')) : null),
      panel('Claims, each checked against the facts it cites', el('table', { class: 'wb-table' },
        el('tr', {}, ...['Verdict', 'Claim', 'Cites'].map(x => el('th', {}, x))), ...claims)),
      panel('The facts the model was given (click one to open the timeline at its second)', el('div', { class: 'wb-scroll' }, facts)),
      panel('The raw answer', el('pre', { class: 'wb-log' }, r.text || '(none)')),
      panel("The bootstrap's own record of this request", el('div', { class: 'wb-log' },
        (r.bootstrap_record || []).join('\n') || '(not in this workspace)')));
  }

  async function askFight(key, id, box) {
    try {
      const r = await post('/api/run', { task: 'T-fight', game: key, target: { fight: Number(id) }, debug: state.debug });
      box.replaceChildren(el('p', {}, 'Asked: ', el('a', { href: hashWith(['run', r.correlation_id], {}) }, r.correlation_id),
        state.debug ? ' — held in Check on the Model screen until you send it.' : ' — the answer lands on the Model screen.'));
    } catch (e) { box.replaceChildren(el('p', { class: 'wb-bad' }, e.message)); }
  }

  // --- start ------------------------------------------------------------------------------------
  async function render() {
    const r = route();
    window.scrollTo(0, 0);
    state.debug = r.q.debug === '1';
    document.getElementById('debug').checked = state.debug;
    try {
      if (r.parts[0] === 'model') await model();
      else if (r.parts[0] === 'run') await runScreen(r.parts[1]);
      else if (r.parts[0] === 'grade') await gradeScreen(r.q);
      else if (r.parts[0] === 'frontier') await frontierScreen();
      else if (r.parts[0] === 'compare') await compareScreen();
      else if (r.parts[0] === 'studies') await studies();
      else if (r.parts[0] === 'study') await study(r.parts[1], r.parts[2] || 'v1');
      else if (r.parts[0] === 'aggregate') await aggregateScreen(r.parts[1], r.parts[2], r.parts[3]);
      else if (r.parts[0] === 'game' && r.parts[2] === 'fight') await fight(r.parts[1], r.parts[3]);
      else if (r.parts[0] === 'game') await game(r.parts[1]);
      else await library(r.q);
    } catch (e) { fail(e); }
  }

  async function boot() {
    document.getElementById('debug').addEventListener('change', (e) => {
      const r = route();
      location.hash = hashWith(r.parts, { ...r.q, debug: e.target.checked ? '1' : '' });
    });
    window.addEventListener('hashchange', render);
    try {
      const cfg = await api('/api/config');
      if (cfg.llm_server) {                  // the kit is asked of the bootstrap server, never guessed
        window.llmServer = cfg.llm_server;
        const s = document.createElement('script');
        s.src = '/llm-bootstrap-kit.js';
        s.onload = () => {
          try {
            llmKit.container('bottom', { name: 'sc2-workbench', slots: { start: ['session'], middle: ['queue'], end: ['log'] } });
            llmKit.requestView('sc2-facts', {
              title: 'The facts, by line (sc2-analyst)',
              shows: (held) => JSON.stringify(held).includes('FACTS\\n'),
              view: (node, held) => {
                const msgs = (held.messages || []).filter(m => m.role === 'user');
                const text = msgs.length ? String(msgs[msgs.length - 1].content) : '';
                const facts = text.split('QUESTION')[0].replace('FACTS', '').trim().split('\n');
                node.append(el('div', { class: 'wb-log' }, facts.join('\n')));
                return () => {};
              } });
          }
          catch (e) { console.error(e); }
        };
        document.head.append(s);
      }
    } catch (e) { console.error(e); }
    render();
  }
  boot();
})();
