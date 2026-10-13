// Pure DOM tests: no NAS/health data or remote services required.
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');

const dom = new JSDOM('<!doctype html><div id="chart"></div>', {
  url: 'http://localhost/', runScripts: 'outside-only'
});
const {window: w} = dom;
const d = w.document;
w.eval(fs.readFileSync('web/chart-cursor.js', 'utf8'));

const container = d.getElementById('chart');
const svg = d.createElementNS('http://www.w3.org/2000/svg', 'svg');
svg.setAttribute('viewBox', '0 0 500 230');
svg.setAttribute('aria-label', '血圧の推移');
container.append(svg);
svg.getBoundingClientRect = () => ({left: 10, width: 250, top: 0, height: 115});

w.attachChartCursor(container, svg, [
  {x: 45, label: '2026/10/13 07:00', fields: [
    {label: '最高血圧', value: 125, unit: 'mmHg', color: 'red', y: 40},
    {label: '最低血圧', value: 78, unit: 'mmHg', color: 'blue', y: 160}
  ]},
  {x: 460, label: '2026/10/14 07:00', fields: [
    {label: '最高血圧', value: 130, unit: 'mmHg', color: 'red', y: 37},
    {label: '最低血圧', value: 82, unit: 'mmHg', color: 'blue', y: 153}
  ]}
], {viewWidth: 500, top: 20, bottom: 195});

const info = container.querySelector('.chart-cursor-readout');
assert(info.hidden);
assert.equal(svg.getAttribute('tabindex'), '0');
assert.equal(svg.querySelector('.chart-cursor-overlay').getAttribute('visibility'), 'hidden');

function pointer(type, clientX, pointerId = 1) {
  const event = new w.Event(type, {bubbles: true});
  Object.defineProperties(event, {
    clientX: {value: clientX}, pointerId: {value: pointerId}, button: {value: 0}
  });
  svg.dispatchEvent(event);
}

pointer('pointerdown', 25);
assert.equal(info.hidden, false);
assert.match(info.textContent, /2026\/10\/13/);
assert.match(info.textContent, /最高血圧 125 mmHg/);
assert.match(info.textContent, /最低血圧 78 mmHg/);
assert.equal(svg.querySelectorAll('.chart-cursor-overlay circle').length, 2);

pointer('pointermove', 245);
assert.match(info.textContent, /2026\/10\/14/);
assert.match(info.textContent, /最高血圧 130 mmHg/);
pointer('pointerup', 245);
assert.equal(svg.querySelector('.chart-cursor-overlay line').getAttribute('x1'), '460');

svg.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'ArrowLeft', bubbles: true, cancelable: true}));
assert.match(info.textContent, /最高血圧 125 mmHg/);
svg.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));
assert(info.hidden);
assert.equal(svg.querySelector('.chart-cursor-overlay').getAttribute('visibility'), 'hidden');

svg.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'End', bubbles: true, cancelable: true}));
assert.match(info.textContent, /最高血圧 130 mmHg/);
info.querySelector('button').click();
assert(info.hidden);

// A new render replaces its SVG/readout instead of leaving stale selected values behind.
container.replaceChildren();
assert.equal(container.querySelector('.chart-cursor-readout'), null);
w.attachChartCursor(container, svg, [], {viewWidth: 500, top: 20, bottom: 195});
assert.equal(container.querySelector('.chart-cursor-readout'), null);

dom.window.close();
console.log('Chart cursor touch/keyboard/value display: passed');
