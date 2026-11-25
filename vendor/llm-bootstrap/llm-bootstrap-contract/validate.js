/* Check JSON values against the contract's shapes. No dependencies.

   The same algorithm as validate.py, producing the same problems character
   for character - test_contract.py runs both over every case and compares,
   because two validators that disagree are two contracts. The shape language
   is described once, in validate.py.

   In a browser:  llmContract.check(value, shape, shapes)
   In node:       const { check } = require('./validate.js')
   For the agreement test:
                  node validate.js --batch <cases.json>                     */
'use strict';

// The id grammar the shape language's {"id": kind} reads - ids.js, beside this.
const ids = (typeof require !== 'undefined' && typeof module !== 'undefined')
  ? require('./ids.js') : globalThis.llmIds;

// A field is present only if the value OWNS it. `'constructor' in {}` is
// true in JavaScript, so a required field of that name would never be missing.
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

function kindOf(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

function render(v) {
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (v === null) return 'null';
  if (typeof v === 'number') return String(v);
  if (typeof v === 'string') return JSON.stringify(v);
  return kindOf(v);
}

function deref(shape, shapes) {
  while (shape && typeof shape === 'object' && !Array.isArray(shape) && own(shape, 'ref'))
    shape = shapes[shape.ref];
  return shape;
}

function label(shape) {
  if (typeof shape === 'string') return shape;
  if (Array.isArray(shape)) return shape.map(label).join(' | ');
  if (own(shape, 'ref')) return shape.ref;
  if (own(shape, 'id')) return `${shape.id} id`;
  for (const k of ['enum', 'array', 'object']) if (own(shape, k)) return k;
  return '?';
}

function topKind(shape, shapes) {
  shape = deref(shape, shapes);
  if (typeof shape === 'string')
    return shape === 'integer' ? 'number' : shape === 'any' ? null : shape;
  if (shape && typeof shape === 'object' && !Array.isArray(shape)) {
    if (own(shape, 'array')) return 'array';
    if (own(shape, 'object')) return 'object';
    if (own(shape, 'id')) return 'string';
  }
  return null;
}

function check(v, shape, shapes = {}, at = '$') {
  shape = deref(shape, shapes);
  if (typeof shape === 'string') {
    if (shape === 'any') return [];
    const got = kindOf(v);
    const ok = shape === 'integer' ? got === 'number' && Number.isInteger(v)
                                   : got === shape;
    return ok ? [] : [`${at}: expected ${shape}, got ${got}`];
  }
  if (Array.isArray(shape)) {
    if (shape.some(alt => check(v, alt, shapes, at).length === 0)) return [];
    const got = kindOf(v);
    const same = shape.filter(alt => topKind(alt, shapes) === got);
    if (same.length === 1) return check(v, same[0], shapes, at);
    return [`${at}: expected ${shape.map(label).join(' | ')}, got ${got}`];
  }
  if (own(shape, 'id')) {
    if (!ids.KINDS.includes(shape.id)) return [`${at}: malformed shape`];
    if (ids.matches(shape.id, v)) return [];
    const got = typeof v === 'string' ? render(v) : kindOf(v);
    return [`${at}: expected ${ids.SHOWN[shape.id]}, got ${got}`];
  }
  if (own(shape, 'enum')) {
    if (shape.enum.some(lit => v === lit)) return [];
    return [`${at}: expected one of [${shape.enum.map(render).join(', ')}], got ${render(v)}`];
  }
  if (own(shape, 'array')) {
    if (!Array.isArray(v)) return [`${at}: expected array, got ${kindOf(v)}`];
    return v.flatMap((item, i) => check(item, shape.array, shapes, `${at}[${i}]`));
  }
  if (own(shape, 'object')) {
    if (kindOf(v) !== 'object') return [`${at}: expected object, got ${kindOf(v)}`];
    const fields = shape.object;
    const optional = new Set(shape.optional || []);
    const names = Object.keys(fields).sort();
    const out = names.filter(n => !own(v, n) && !optional.has(n))
                     .map(n => `${at}: missing required field "${n}"`);
    if (!shape.open)
      for (const n of Object.keys(v).sort())
        if (!own(fields, n)) out.push(`${at}: unexpected field "${n}"`);
    for (const n of names)
      if (own(v, n)) out.push(...check(v[n], fields[n], shapes, `${at}.${n}`));
    return out;
  }
  return [`${at}: malformed shape`];
}

if (typeof module !== 'undefined') module.exports = { check, kindOf, render };
else globalThis.llmContract = { check, kindOf, render };

if (typeof require !== 'undefined' && typeof module !== 'undefined'
    && require.main === module) {
  const i = process.argv.indexOf('--batch');
  const doc = JSON.parse(require('fs').readFileSync(process.argv[i + 1], 'utf8'));
  process.stdout.write(JSON.stringify(doc.cases.map(c => check(c.value, c.shape, doc.shapes))));
}
