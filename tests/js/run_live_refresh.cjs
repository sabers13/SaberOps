/* Exercise the production SSE controller without browser/runtime dependencies.
   Real DOM/layout interaction is additionally checked in the live browser. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(attributes = {}, text = '') {
    this.attributes = attributes;
    this.textContent = text;
    this.childNodes = [{textContent: text}];
    this.children = [];
    this.style = {};
    this.hidden = false;
    this.handlers = {};
    this.classList = {add() {}};
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  contains() { return false; }
  appendChild(node) { this.children.push(node); }
  replaceChildren(...nodes) {
    this.childNodes = nodes;
    this.textContent = nodes.map(node => node.textContent).join('');
  }
}
const listeners = {};
const header = new Element({
  'data-run-id': 'run-live', 'data-run-status': 'RUNNING',
  'data-created-at': '2026-10-05T20:34:54+00:00', 'data-completed-at': '',
});
const events = new Element();
const timer = new Element();
const badge = new Element();
const feedback = new Element();
feedback.hidden = true;
const retry = new Element();
const composer = new Element({}, 'My unsent follow-up');
const confirmation = new Element({}, 'Partly typed run confirmation');
const conversation = new Element();
conversation.scrollTop = 123;
const keys = ['chat-result', 'result', 'plan', 'details', 'workers', 'changes',
  'checks', 'review', 'routing', 'usage', 'worker-dock', 'checklist'];
const regions = keys.map(key => new Element({'data-run-refresh': key}, 'No records yet'));
const agentRegion = new Element({'data-run-refresh': 'worker-footer', 'data-agent-live': ''}, 'Live agent controls');
regions.push(agentRegion);
const elements = {'run-header': header, 'events-log': events, 'elapsed-timer': timer,
  'run-status-badge': badge, 'run-refresh-status': feedback, runRefreshRetry: retry,
  runningTask: composer, confirm_run_id: confirmation, conversation};
const document = {
  getElementById(id) { return elements[id] || null; },
  querySelector() { return null; },
  querySelectorAll(selector) { return selector === '[data-run-refresh]' ? regions : []; },
  createElement() { return new Element(); },
  importNode(node) { return {...node}; },
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
  dispatchEvent(event) { (listeners[event.type] || []).forEach(fn => fn(event)); },
};
const timeouts = new Map();
let nextTimer = 0;
let requests = [];
let enhanced = 0;
let source;
let parses = 0;
class EventSource {
  constructor() { source = this; this.handlers = {}; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  close() {}
}
class CustomEvent {
  constructor(type, options) { this.type = type; this.detail = options?.detail; }
}
function snapshot(generation, status = 'COMPLETED') {
  const nextHeader = new Element({...header.attributes, 'data-run-status': status,
    'data-completed-at': status === 'COMPLETED' ? '2026-10-05T20:44:36+00:00' : ''});
  return {
    querySelectorAll() { return []; },
    getElementById(id) {
      if (id === 'run-header') return nextHeader;
      if (id === 'run-status-badge') return new Element({}, status);
      return null;
    },
    querySelector(selector) {
      const key = selector.match(/data-run-refresh="([^"]+)"/)[1];
      return new Element({}, generation + ':' + key);
    },
  };
}
class DOMParser {
  parseFromString(value) {
    parses++;
    if (value === 'invalid') return {getElementById() { return null; }};
    return snapshot(value);
  }
}
const window = {
  location: {search: '?from=dashboard'},
  setTimeout(fn) { const id = ++nextTimer; timeouts.set(id, fn); return id; },
  fetch(url, options) {
    assert.equal(options.credentials, 'same-origin');
    if (url.endsWith('/actions')) return Promise.resolve({ok: true, json: async () => ({})});
    assert.equal(url, '/runs/run-live?from=dashboard');
    assert.equal(options.cache, 'no-store');
    return new Promise(resolve => requests.push(resolve));
  },
  SaberOpsConsole: {enhanceRunContent() { enhanced++; }},
};
const context = {window, document, EventSource, DOMParser, CustomEvent, Date,
  setInterval() { return 1; }, clearInterval() {}};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
document.dispatchEvent(new CustomEvent('DOMContentLoaded'));
function emit(type, id) {
  source.handlers[type]({data: JSON.stringify({id, event_type: type,
    created_at: '2026-10-05T20:44:36+00:00'})});
}
function flushTimer() {
  const scheduled = [...timeouts.values()]; timeouts.clear(); scheduled.forEach(fn => fn());
}
async function settle() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
function respond(body, ok = true) {
  assert.equal(requests.length, 1, 'one snapshot read at a time');
  requests.shift()({ok, text: async () => body});
}
(async () => {
  // Completion refreshes every initially-empty surface, without remounting
  // the composer, confirmation, terminal/event stream or changing scroll.
  emit('worker_completed', 1);
  emit('gate_completed', 2);
  emit('run_completed', 3);
  assert.equal(timeouts.size, 1, 'event bursts coalesce');
  assert.equal(badge.textContent, 'COMPLETED');
  assert.equal(timer.textContent, 'Duration: 09:42');
  flushTimer(); respond('complete'); await settle();
  for (const region of regions.filter(region => region !== agentRegion)) assert.equal(region.textContent, 'complete:' + region.getAttribute('data-run-refresh'));
  assert.equal(agentRegion.textContent, 'Live agent controls', 'session-owned controls survive evidence refresh');
  assert.equal(enhanced, regions.length - 1);
  assert.equal(composer.textContent, 'My unsent follow-up');
  assert.equal(confirmation.textContent, 'Partly typed run confirmation');
  assert.equal(conversation.scrollTop, 123);
  assert.equal(document.getElementById('events-log'), events);
  emit('run_completed', 3);
  assert.equal(timeouts.size, 0, 'duplicate SSE does not refresh twice');

  // A late snapshot is discarded when newer evidence arrived mid-flight.
  document.dispatchEvent(new CustomEvent('saberops:run-tab'));
  flushTimer();
  emit('review_completed', 4);
  respond('old'); await settle();
  assert.equal(parses, 1, 'stale snapshot never touches the DOM');
  assert.equal(regions[0].textContent, 'complete:chat-result');
  flushTimer(); respond('new'); await settle();
  assert.equal(regions[0].textContent, 'new:chat-result');

  // Failed and malformed reads preserve the last evidence and offer retry.
  document.dispatchEvent(new CustomEvent('saberops:run-tab'));
  flushTimer(); respond('', false); await settle();
  assert.equal(feedback.hidden, false);
  assert.equal(regions[0].textContent, 'new:chat-result');
  retry.handlers.click(); flushTimer(); respond('invalid'); await settle();
  assert.equal(feedback.hidden, false);
  retry.handlers.click(); flushTimer(); respond('recovered'); await settle();
  assert.equal(feedback.hidden, true);
  assert.equal(regions[0].textContent, 'recovered:chat-result');
  console.log('Live run refresh, ordering, draft preservation, timer and recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
