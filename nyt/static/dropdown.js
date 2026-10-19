/*
 * nyt dropdowns — replaces the browser's native <select> popup (which can't be styled) with a themed menu.
 *
 * Progressive enhancement: every <select> on the page is hidden but kept in the DOM as the source of truth, so
 * existing code that reads `select.value`, listens for `change`, or uses an inline `onchange` keeps working.
 *   - opt out of a select with the `data-native` attribute
 *   - give an <option> a right-aligned secondary label with `data-hint="12"`
 *   - after changing `select.value` from script, call NytDropdown.sync(select)
 */
(function () {
  'use strict';

  const CSS = `
    .nd { display: inline-block; position: relative; vertical-align: middle; }

    .nd-native {
      position: absolute !important;
      width: 1px !important;
      height: 1px !important;
      margin: 0 !important;
      opacity: 0 !important;
      pointer-events: none !important;
    }

    .nd-btn {
      display: inline-flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      max-width: 240px;
      font-family: var(--font-body, 'Nunito', sans-serif);
      font-size: 13px;
      font-weight: 700;
      color: var(--ink, #2d2b26);
      background: var(--surface, #fffdf8);
      border: 2px solid var(--ink, #2d2b26);
      border-radius: var(--radius-pill, 50px);
      padding: 5px 10px 5px 14px;
      cursor: pointer;
      outline: none;
      box-shadow: 2px 2px 0 var(--shadow, #e0c898);
      transition: border-color 0.15s, transform 0.08s, box-shadow 0.08s;
    }
    .nd-btn:hover { border-color: var(--coral, #f4845f); }
    .nd-btn:active { transform: translateY(1px); box-shadow: 1px 1px 0 var(--shadow, #e0c898); }
    .nd-btn:focus-visible, .nd-btn.open { border-color: var(--coral, #f4845f); }
    .nd-btn:disabled { opacity: 0.45; cursor: default; pointer-events: none; }
    .nd-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .nd-chev { flex-shrink: 0; color: var(--muted, #a09880); transition: transform 0.18s; }
    .nd-btn.open .nd-chev { transform: rotate(180deg); }

    .nd-menu {
      position: fixed;
      z-index: 800;
      min-width: 150px;
      max-width: 320px;
      overflow-y: auto;
      padding: 5px;
      display: flex;
      flex-direction: column;
      gap: 1px;
      background: var(--surface, #fffdf8);
      border: 2.5px solid var(--ink, #2d2b26);
      border-radius: 12px 6px 14px 8px / 8px 14px 6px 12px;
      box-shadow: 5px 5px 0 var(--shadow, #e0c898);
      animation: nd-pop 0.12s ease;
    }
    @keyframes nd-pop {
      from { opacity: 0; transform: translateY(-4px) scale(0.97); }
      to   { opacity: 1; transform: none; }
    }

    .nd-opt {
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      font-family: var(--font-body, 'Nunito', sans-serif);
      font-size: 13.5px;
      font-weight: 700;
      color: var(--ink, #2d2b26);
      background: none;
      border: 0;
      border-radius: 7px 4px 8px 5px / 5px 8px 4px 7px;
      padding: 7px 9px 7px 11px;
      cursor: pointer;
      text-align: left;
      outline: none;
      transition: background 0.1s, color 0.1s;
    }
    .nd-opt:hover, .nd-opt:focus-visible { background: var(--coral-bg, #fde8e0); }
    .nd-opt.active { color: var(--coral, #f4845f); background: var(--coral-bg, #fde8e0); }
    .nd-opt:disabled { opacity: 0.4; cursor: default; }
    .nd-opt-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .nd-opt-hint { flex-shrink: 0; font-size: 11.5px; font-weight: 700; color: var(--muted, #a09880); }
    .nd-opt.active .nd-opt-hint { color: inherit; opacity: 0.7; }
    .nd-check { flex-shrink: 0; width: 14px; height: 14px; display: flex; opacity: 0; color: var(--coral, #f4845f); }
    .nd-opt.active .nd-check { opacity: 1; }
  `;

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function icon(pathD, strokeWidth, className) {
    const svg  = document.createElementNS(SVG_NS, 'svg');
    const path = document.createElementNS(SVG_NS, 'path');
    for (const [k, v] of Object.entries({
      width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      'stroke-width': strokeWidth, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true',
    })) svg.setAttribute(k, v);
    if (className) svg.setAttribute('class', className);
    path.setAttribute('d', pathD);
    svg.append(path);
    return svg;
  }

  const chevron = () => icon('m6 9 6 6 6-6', 2.75, 'nd-chev');
  const check   = () => icon('M20 6 9 17l-5-5', 3);

  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);

  const registry = new WeakMap();
  let current = null;           // the dropdown whose menu is open
  let raf = 0;

  function optionButtons(st) { return [...st.menu.querySelectorAll('.nd-opt')]; }

  function renderOptions(st) {
    const buttons = [...st.select.options].map((o, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'nd-opt';
      b.setAttribute('role', 'option');
      b.dataset.i = String(i);
      b.disabled = o.disabled;

      const label = document.createElement('span');
      label.className = 'nd-opt-label';
      label.textContent = o.textContent;
      b.append(label);

      if (o.dataset.hint) {
        const hint = document.createElement('span');
        hint.className = 'nd-opt-hint';
        hint.textContent = o.dataset.hint;
        b.append(hint);
      }

      const mark = document.createElement('span');
      mark.className = 'nd-check';
      mark.append(check());
      b.append(mark);
      return b;
    });
    st.menu.replaceChildren(...buttons);
    markActive(st);
  }

  function markActive(st) {
    optionButtons(st).forEach((b, i) => {
      const on = i === st.select.selectedIndex;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
    });
  }

  function sync(select) {
    const st = registry.get(select);
    if (!st) return;
    st.label.textContent = select.options[select.selectedIndex]?.textContent ?? '';
    st.btn.disabled = select.disabled;
    markActive(st);
  }

  function position(st) {
    const r = st.btn.getBoundingClientRect();
    const m = st.menu;
    m.style.minWidth = `${Math.max(r.width, 150)}px`;
    m.style.maxHeight = '';
    const h = m.offsetHeight, w = m.offsetWidth;
    const below = window.innerHeight - r.bottom - 12;
    const above = r.top - 12;
    const openUp = h > below && above > below;
    const room = openUp ? above : below;
    m.style.maxHeight = `${Math.max(120, Math.min(320, room))}px`;
    const mh = Math.min(h, parseFloat(m.style.maxHeight));
    m.style.top  = `${openUp ? r.top - mh - 6 : r.bottom + 6}px`;
    m.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 8))}px`;
  }

  function open(st) {
    if (st.btn.disabled) return;
    if (current && current !== st) close(current);
    current = st;
    document.body.appendChild(st.menu);
    st.btn.classList.add('open');
    st.btn.setAttribute('aria-expanded', 'true');
    markActive(st);
    position(st);
    (st.menu.querySelector('.nd-opt.active') || st.menu.querySelector('.nd-opt:not(:disabled)'))?.focus();

    // Follow the button while open (handles page/dialog scrolling) and bail out if it was removed.
    const tick = () => {
      if (current !== st) return;
      if (!st.select.isConnected) { close(st); return; }
      position(st);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
  }

  function close(st, refocus = false) {
    if (!st || current !== st) return;
    cancelAnimationFrame(raf);
    current = null;
    st.menu.remove();
    st.btn.classList.remove('open');
    st.btn.setAttribute('aria-expanded', 'false');
    if (refocus && st.btn.isConnected) st.btn.focus();
  }

  function choose(st, i) {
    const changed = st.select.selectedIndex !== i;
    st.select.selectedIndex = i;
    sync(st.select);
    close(st, true);
    if (changed) {
      st.select.dispatchEvent(new Event('input',  { bubbles: true }));
      st.select.dispatchEvent(new Event('change', { bubbles: true }));   // also fires inline onchange="…"
    }
  }

  function move(st, delta, absolute) {
    const opts = optionButtons(st).filter(b => !b.disabled);
    if (!opts.length) return;
    const at = opts.indexOf(document.activeElement);
    const next = absolute !== undefined ? (absolute < 0 ? opts.length - 1 : 0)
                                        : Math.max(0, Math.min(opts.length - 1, (at < 0 ? 0 : at + delta)));
    opts[next].focus();
  }

  let typed = '', typedAt = 0;
  function typeahead(st, ch) {
    const now = Date.now();
    typed = now - typedAt > 700 ? ch : typed + ch;
    typedAt = now;
    const opts = optionButtons(st).filter(b => !b.disabled);
    const hit = opts.find(b => b.textContent.trim().toLowerCase().startsWith(typed.toLowerCase()));
    if (hit) hit.focus();
  }

  function enhance(select) {
    if (registry.has(select) || select.multiple || select.size > 1 || select.hasAttribute('data-native')) return;

    const wrap  = document.createElement('div');
    const btn   = document.createElement('button');
    const label = document.createElement('span');
    const menu  = document.createElement('div');

    wrap.className  = 'nd';
    btn.type        = 'button';
    btn.className   = 'nd-btn';
    label.className = 'nd-label';
    menu.className  = 'nd-menu';
    menu.setAttribute('role', 'listbox');
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');
    const name = select.getAttribute('aria-label') || select.labels?.[0]?.textContent?.trim();
    if (name) { btn.setAttribute('aria-label', name); menu.setAttribute('aria-label', name); }

    btn.append(label, chevron());
    wrap.append(btn);

    select.classList.add('nd-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    select.insertAdjacentElement('afterend', wrap);

    const st = { select, wrap, btn, label, menu };
    registry.set(select, st);
    renderOptions(st);
    sync(select);

    btn.addEventListener('click', () => (current === st ? close(st) : open(st)));
    btn.addEventListener('keydown', e => {
      if (['ArrowDown', 'ArrowUp'].includes(e.key)) { e.preventDefault(); open(st); }
    });
    menu.addEventListener('click', e => {
      const b = e.target.closest('.nd-opt');
      if (b && !b.disabled) choose(st, parseInt(b.dataset.i, 10));
    });
    menu.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(st, 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(st, -1); }
      else if (e.key === 'Home') { e.preventDefault(); move(st, 0, 0); }
      else if (e.key === 'End') { e.preventDefault(); move(st, 0, -1); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(st, true); }
      else if (e.key === 'Tab') { close(st); }
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey && e.key !== ' ') typeahead(st, e.key);
    });

    // Keep in step with the real <select>: options added/removed, disabled toggled, or `change` fired by script.
    new MutationObserver(() => { renderOptions(st); sync(select); })
      .observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
    select.addEventListener('change', () => sync(select));
  }

  function enhanceWithin(root) {
    if (root.nodeType !== 1) return;
    if (root.tagName === 'SELECT') enhance(root);
    root.querySelectorAll?.('select').forEach(enhance);
  }

  document.addEventListener('mousedown', e => {
    if (current && !current.menu.contains(e.target) && !current.btn.contains(e.target)) close(current);
  }, true);
  window.addEventListener('blur', () => close(current));
  window.addEventListener('resize', () => close(current));

  new MutationObserver(muts => muts.forEach(m => m.addedNodes.forEach(enhanceWithin)))
    .observe(document.documentElement, { childList: true, subtree: true });
  document.querySelectorAll('select').forEach(enhance);

  window.NytDropdown = {
    enhance,
    sync,
    closeAll: () => close(current),
  };
})();
