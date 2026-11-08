// DOM-тест страницы ESS без OrcaSlicer: минимальные заглушки document/orca,
// исполняем встроенный <script> из speed_slider.py.
//
//   node tests/page_test.js
//
// Проверяем подсветку строк и предупреждение по объёмному расходу.

'use strict';

const fs = require('fs');
const path = require('path');

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = '';
    this.style = {};
    this.textContent = '';
    this.value = '';
    this.type = '';
    this.checked = false;
    this.disabled = false;
    this.title = '';
    this.placeholder = '';
    this.min = 0;
    this.max = 0;
    this.step = '';
    this._listeners = {};
  }
  get firstChild() { return this.children.length ? this.children[0] : null; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    return child;
  }
  addEventListener(event, fn) { (this._listeners[event] = this._listeners[event] || []).push(fn); }
  dispatch(event) { (this._listeners[event] || []).forEach((fn) => fn()); }
  querySelector(selector) {
    const cls = selector.replace(/^\./, '');
    for (const child of this.children) {
      if ((child.className || '').split(/\s+/).includes(cls)) return child;
      if (child.querySelector) {
        const found = child.querySelector(selector);
        if (found) return found;
      }
    }
    return null;
  }
}

const byId = new Map();
global.document = {
  activeElement: null,
  getElementById(id) {
    if (!byId.has(id)) byId.set(id, new El('div'));
    return byId.get(id);
  },
  createElement(tag) { return new El(tag); },
};
let onMessage = null;
const posted = [];
global.orca = {
  postMessage(msg) { posted.push(msg); },
  onMessage(fn) { onMessage = fn; },
};
global.window = global;

const plugin = path.join(__dirname, '..', 'speed_slider.py');
const src = fs.readFileSync(plugin, 'utf8');
const match = src.match(/<script>\n([\s\S]*?)\n<\/script>/);
if (!match) throw new Error('в speed_slider.py не найден <script>');
eval(match[1]);
if (!onMessage) throw new Error('страница не зарегистрировала orca.onMessage');

const bodyEl = document.getElementById('ss-body');
const flowEl = document.getElementById('ss-flow');
const vfaEl = document.getElementById('ss-vfa');
const rangeEl = document.getElementById('ss-range');
const numEl = document.getElementById('ss-num');

const FLOW = {
  enabled: true,
  limit: 12,
  layerHeight: 0.2,
  maxSpeeds: {
    outer_wall_speed: 133.3,
    inner_wall_speed: 120,
    bridge_speed: 150,
  },
};

function state(value, flow = FLOW, lang = 'ru', vfa = { enabled: false, min: 0, max: 0 }) {
  return {
    kind: 'state',
    preset: 'Test',
    anchor: 200,
    value: value,
    lang: lang,
    vfa: vfa,
    flow: flow,
    notesEnabled: true,
    dirty: false,
    last: null,
    rows: [
      { key: 'inner_wall_speed', label_ru: 'Внутренняя стенка', label_en: 'Inner wall',
        base: [300], mode: 'ratio', ratio: 1.5, speed: 300, note: '' },
      { key: 'bridge_speed', label_ru: 'Мосты', label_en: 'Bridges',
        base: [30], mode: 'speed', ratio: 0.25, speed: 25, note: '' },
    ],
  };
}

function rowOut(idx) {
  return bodyEl.children[idx].querySelector('.ss-out');
}

// 1. Скорость стенки 180 > 133.3, внутренняя стенка 270 > 120: предупреждение + подсветка.
onMessage(state(180));
console.log('flow warn RU:', flowEl.textContent);
if (!/Объёмный расход 12 мм³\/с/.test(flowEl.textContent)) throw new Error('нет текста про расход');
if (!/180 > 133\.3/.test(flowEl.textContent)) throw new Error('нет конфликта внешней стенки');
if (!/270 > 120/.test(flowEl.textContent)) throw new Error('нет конфликта внутренней стенки');
if (!flowEl.className.includes('warn')) throw new Error('нет класса warn у строки расхода');
if (!rangeEl.className.includes('ss-warn')) throw new Error('ползунок не подсвечен');
if (!rowOut(0).className.includes('ss-capped')) throw new Error('строка 0 не подсвечена');
if (rowOut(1).className.includes('ss-capped')) throw new Error('строка 1 не должна быть подсвечена');
console.log('rows capped:', rowOut(0).className, '/', rowOut(1).className);

// 2. Всё в пределах: 60 стенка, 90 внутренняя, 25 мосты.
onMessage(state(60));
console.log('flow ok RU:', flowEl.textContent);
if (!/все скорости в пределах/.test(flowEl.textContent)) throw new Error('нет текста "в пределах"');
if (flowEl.className.includes('warn')) throw new Error('не должно быть warn');
if (rangeEl.className.includes('ss-warn')) throw new Error('ползунок не должен быть подсвечен');
if (rowOut(0).className.includes('ss-capped')) throw new Error('строка 0 не должна быть подсвечена');

// 3. Английский текст.
onMessage(state(180, FLOW, 'en'));
console.log('flow warn EN:', flowEl.textContent);
if (!/Volumetric flow limit 12 mm³\/s/.test(flowEl.textContent)) throw new Error('нет EN-предупреждения');
if (!/180 > 133\.3/.test(flowEl.textContent)) throw new Error('нет EN-конфликта');

// 4. Расход выключен: строки/подсветка не появляются.
onMessage(state(180, { enabled: false, limit: 0, layerHeight: 0, maxSpeeds: {} }));
if (flowEl.textContent !== '') throw new Error('при выключенном расходе текста быть не должно');
if (flowEl.className.includes('warn')) throw new Error('при выключенном расходе не должно быть warn');
if (rowOut(0).className.includes('ss-capped')) throw new Error('при выключенном расходе нет подсветки');
console.log('flow disabled: OK');

// 5. VFA-предупреждение не сломано: полоса 70–120, скорость 90.
onMessage(state(90, FLOW, 'ru', { enabled: true, min: 70, max: 120 }));
if (!/диапазон избегаемых скоростей/.test(vfaEl.textContent)) throw new Error('нет VFA-предупреждения');
if (!rangeEl.className.includes('ss-warn')) throw new Error('ползунок не подсвечен VFA');
console.log('vfa still works: OK');

console.log('posted from page:', JSON.stringify(posted[0]));
console.log('OK: DOM-тест страницы ESS прошёл');
