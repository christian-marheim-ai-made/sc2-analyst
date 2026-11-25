/* A DOM for the client's rows: enough of one to build the page, click it and
   read it - and no more. No layout: a width a view needs comes from the store,
   where the page's own observer puts it; a row sets it the same way.

   Selectors: `tag`, `#id`, `.class`, `[attr]`, `[attr="value"]`, and those
   joined without spaces (`button[data-el="x"]`). Nothing else.

   `readHtml(document, html)` puts a page's markup into the document - its
   head and its body - and hands back its scripts in document order, each
   `{src, text}`: enough to run an application's own page as its browser
   would. Plain markup only: tags, attributes, text, comments. */
'use strict';

class Node {
  constructor() { this.parentNode = null; this.childNodes = []; }
  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }
  insertBefore(node, ref) {
    if (ref === null || ref === undefined) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    const i = this.childNodes.indexOf(ref);
    if (i < 0) throw new Error('insertBefore: not a child');
    node.parentNode = this;
    this.childNodes.splice(i, 0, node);
    return node;
  }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i < 0) throw new Error('removeChild: not a child');
    this.childNodes.splice(i, 1);
    node.parentNode = null;
    return node;
  }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(text) {
    while (this.firstChild) this.removeChild(this.firstChild);
    if (text !== '' && text !== null && text !== undefined) this.appendChild(new Text(String(text)));
  }
}

/* an element's style: its properties as plain fields, and a custom property
   (`--name`) set and read as a browser does */
class Style {
  setProperty(name, value) { this[name] = String(value); }
  removeProperty(name) { delete this[name]; }
  getPropertyValue(name) { return this[name] === undefined ? '' : this[name]; }
}

class Text extends Node {
  constructor(text) { super(); this.nodeType = 3; this.data = text; }
  get textContent() { return this.data; }
  set textContent(t) { this.data = String(t); }
}

class Element extends Node {
  constructor(tag, owner) {
    super();
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.ownerDocument = owner;
    this.attributes = new Map();
    this.style = new Style();
    this.listeners = new Map();
    this.value = '';
    this.checked = false;
    this.disabled = false;
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'disabled') this.disabled = true;
    if (name === 'value') this.value = String(value);
    if (name === 'checked') this.checked = true;
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === 'disabled') this.disabled = false;
  }
  get id() { return this.getAttribute('id') || ''; }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (c) => { if (!list().includes(c)) el.className = [...list(), c].join(' '); },
      remove: (c) => { el.className = list().filter((x) => x !== c).join(' '); },
      toggle: (c, on) => {
        const want = on === undefined ? !list().includes(c) : on;
        if (want) el.classList.add(c); else el.classList.remove(c);
        return want;
      },
    };
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const l = this.listeners.get(type) || [];
    const i = l.indexOf(fn);
    if (i >= 0) l.splice(i, 1);
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    // up to the document itself, as a browser does - and no further for a
    // node that is not in the page
    for (let n = this; n && !event.stopped; n = event.bubbles === false ? null : n.parentNode) {
      event.currentTarget = n;
      for (const fn of [...((n.listeners && n.listeners.get(event.type)) || [])]) fn(event);
    }
    return !event.defaultPrevented;
  }
  click() {
    if (this.disabled) return;
    this.dispatchEvent(makeEvent('click'));
  }
  /* the stub has no layout: a row sets `rect` as a browser would measure
     the box, fractions and all - or the whole-pixel offsets it set are the
     box's size */
  getBoundingClientRect() {
    const width = this.rect ? this.rect.width : (this.offsetWidth || 0);
    const height = this.rect ? this.rect.height : (this.offsetHeight || 0);
    return { x: 0, y: 0, left: 0, top: 0, width, height, right: width, bottom: height };
  }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = null; }
  querySelectorAll(sel) {
    const out = [];
    const test = parse(sel);
    const walk = (n) => {
      for (const c of n.children) {
        if (test(c)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function makeEvent(type, fields) {
  return Object.assign({
    type, bubbles: true, defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.stopped = true; },
  }, fields || {});
}

function parse(sel) {
  const parts = sel.match(/([a-zA-Z][\w-]*)|#[\w-]+|\.[\w-]+|\[[^\]]+\]/g);
  if (!parts || parts.join('') !== sel.replace(/\s+/g, '')) throw new Error(`the test DOM does not know the selector ${sel}`);
  const tests = parts.map((p) => {
    if (p[0] === '#') return (e) => e.id === p.slice(1);
    if (p[0] === '.') return (e) => e.classList.contains(p.slice(1));
    if (p[0] === '[') {
      const m = p.slice(1, -1).match(/^([\w-]+)(?:="?([^"]*)"?)?$/);
      return (e) => e.hasAttribute(m[1]) && (m[2] === undefined || e.getAttribute(m[1]) === m[2]);
    }
    return (e) => e.tagName === p.toUpperCase();
  });
  return (e) => tests.every((t) => t(e));
}

class Document extends Element {
  constructor() {
    super('#document', null);
    this.ownerDocument = this;
    this.activeElement = null;
    this.visibilityState = 'visible';
    this.head = this.createElement('head');
    this.appendChild(this.head);
    this.body = this.createElement('body');
    this.appendChild(this.body);
  }
  createElement(tag) { return new Element(tag, this); }
  createTextNode(text) { return new Text(String(text)); }
  getElementById(id) { return this.querySelector(`#${id}`); }
  dispatchEvent(event) {
    event.target = event.target || this;
    for (const fn of [...(this.listeners.get(event.type) || [])]) fn(event);
    return !event.defaultPrevented;
  }
}

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
  'source', 'track', 'wbr']);
const RAW = new Set(['script', 'style']);

function unescapeText(text) {
  return text.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({
    amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'",
  })[e]);
}

function readHtml(document, html) {
  const scripts = [];
  const open = [document.body];
  const where = () => open[open.length - 1];
  const tokens = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)([^>]*)>|[^<]+/gi;
  let m;
  while ((m = tokens.exec(html)) !== null) {
    const [whole, closing, tag, attrs] = m;
    if (whole.startsWith('<!')) continue;
    if (closing) {
      const name = closing.toLowerCase();
      if (name === 'head' || name === 'body' || name === 'html') continue;
      if (where().tagName === name.toUpperCase()) open.pop();
      continue;
    }
    if (!tag) {
      if (whole.trim()) where().appendChild(document.createTextNode(unescapeText(whole)));
      continue;
    }
    const name = tag.toLowerCase();
    if (name === 'html') continue;
    if (name === 'head' || name === 'body') {
      open.length = 0;
      open.push(document[name]);
      continue;
    }
    const node = document.createElement(name);
    for (const a of attrs.matchAll(/([^\s=/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
      node.setAttribute(a[1], unescapeText(a[2] ?? a[3] ?? a[4] ?? ''));
    }
    where().appendChild(node);
    if (RAW.has(name)) {
      const end = html.toLowerCase().indexOf(`</${name}`, tokens.lastIndex);
      const text = html.slice(tokens.lastIndex, end < 0 ? html.length : end);
      tokens.lastIndex = end < 0 ? html.length : html.indexOf('>', end) + 1;
      node.appendChild(document.createTextNode(text));
      if (name === 'script') scripts.push({ src: node.getAttribute('src'), text });
    } else if (!VOID.has(name)) {
      open.push(node);
    }
  }
  return scripts;
}

module.exports = { Document, Element, Text, makeEvent, readHtml };
