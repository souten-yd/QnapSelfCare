/* SVG chart inspection cursor, shared by the dashboard and wellness charts. */
(function () {
  'use strict';
  const namespace = 'http://www.w3.org/2000/svg';
  const svgElement = (document, name, attributes) => {
    const element = document.createElementNS(namespace, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
  };

  window.attachChartCursor = function attachChartCursor(container, svg, points, options) {
    if (!points.length) return;
    const {viewWidth, top, bottom} = options;
    const document = svg.ownerDocument;
    const entries = points.filter(point => Number.isFinite(point.x) && point.fields?.length)
      .sort((left, right) => left.x - right.x);
    if (!entries.length) return;

    svg.classList.add('chart-cursor-svg');
    svg.setAttribute('tabindex', '0');
    svg.setAttribute('aria-label', svg.getAttribute('aria-label') +
      '。タップまたは左右にドラッグして値を確認。キーボードでは左右矢印で移動、Escapeで解除。');

    const overlay = svgElement(document, 'g', {
      class: 'chart-cursor-overlay', visibility: 'hidden', 'pointer-events': 'none', 'aria-hidden': 'true'
    });
    const guide = svgElement(document, 'line', {
      y1: top, y2: bottom, stroke: 'var(--chart-label)', 'stroke-width': 1.5,
      'stroke-dasharray': '4 3'
    });
    overlay.append(guide);
    svg.append(overlay);

    const readout = document.createElement('div');
    readout.className = 'chart-cursor-readout';
    readout.setAttribute('role', 'status');
    readout.setAttribute('aria-live', 'polite');
    readout.hidden = true;
    container.append(readout);

    let selectedIndex = -1;
    let draggingPointer = null;

    function clear() {
      selectedIndex = -1;
      overlay.setAttribute('visibility', 'hidden');
      readout.hidden = true;
    }

    function select(index) {
      if (index === selectedIndex || index < 0 || index >= entries.length) return;
      selectedIndex = index;
      const point = entries[index];
      guide.setAttribute('x1', point.x);
      guide.setAttribute('x2', point.x);
      overlay.replaceChildren(guide);
      for (const field of point.fields) {
        if (!Number.isFinite(field.y)) continue;
        overlay.append(svgElement(document, 'circle', {
          cx: point.x, cy: field.y, r: 5, fill: field.color || 'var(--chart-primary)',
          stroke: 'var(--surface)', 'stroke-width': 2
        }));
      }
      overlay.setAttribute('visibility', 'visible');

      const heading = document.createElement('strong');
      heading.textContent = point.label;
      const values = document.createElement('span');
      values.className = 'chart-cursor-values';
      for (const field of point.fields) {
        const value = document.createElement('span');
        value.className = 'chart-cursor-value';
        value.style.borderColor = field.color || 'var(--chart-primary)';
        value.textContent = field.label + ' ' + field.value + (field.unit ? ' ' + field.unit : '');
        values.append(value);
      }
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'chart-cursor-close secondary';
      close.setAttribute('aria-label', 'グラフのカーソルを解除');
      close.textContent = '×';
      close.addEventListener('click', clear);
      readout.replaceChildren(heading, values, close);
      readout.hidden = false;
    }

    function selectAt(clientX) {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const x = (clientX - rect.left) / rect.width * viewWidth;
      let closest = 0;
      for (let index = 1; index < entries.length; index++) {
        if (Math.abs(entries[index].x - x) < Math.abs(entries[closest].x - x)) closest = index;
      }
      select(closest);
    }

    svg.addEventListener('pointerdown', event => {
      if (event.button != null && event.button !== 0) return;
      draggingPointer = event.pointerId;
      selectAt(event.clientX);
      if (svg.setPointerCapture) {
        try { svg.setPointerCapture(event.pointerId); } catch (_) { /* unsupported browser */ }
      }
    });
    svg.addEventListener('pointermove', event => {
      if (draggingPointer === event.pointerId) selectAt(event.clientX);
    });
    const stopDragging = event => {
      if (draggingPointer !== event.pointerId) return;
      if (event.type === 'pointerup') selectAt(event.clientX);
      draggingPointer = null;
      if (svg.releasePointerCapture && svg.hasPointerCapture?.(event.pointerId)) {
        svg.releasePointerCapture(event.pointerId);
      }
    };
    svg.addEventListener('pointerup', stopDragging);
    svg.addEventListener('pointercancel', stopDragging);
    svg.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowLeft') next = selectedIndex < 0 ? entries.length - 1 : Math.max(0, selectedIndex - 1);
      else if (event.key === 'ArrowRight') next = selectedIndex < 0 ? 0 : Math.min(entries.length - 1, selectedIndex + 1);
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = entries.length - 1;
      else if (event.key === 'Escape') { clear(); event.preventDefault(); return; }
      else return;
      event.preventDefault();
      select(next);
    });
  };
})();
