/* The DOM, as small as it can be.

   el(tag, props, ...kids)  an element. In `props`: `on<event>` is a listener,
                            `class` the class list, `style` an object of
                            styles, anything else an attribute - `data-el`
                            above all, the control's stable name. A prop that
                            is null, undefined or false is left out; so is a
                            child that is. A string child is text.
   setKids(node, kids)      replace every child of `node`.
   named(name, root)        the element whose `data-el` is `name`.

   A view builds its piece with these and nothing else, and never keeps a
   node another view built. */

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else if (key === 'class') {
      node.setAttribute('class', String(value));
    } else if (key === 'style' && typeof value === 'object') {
      for (const [name, v] of Object.entries(value)) node.style[name] = v;
    } else {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  appendKids(node, kids);
  return node;
}

function appendKids(node, kids) {
  for (const kid of [kids].flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
  }
}

function setKids(node, kids) {
  while (node.firstChild) node.removeChild(node.firstChild);
  appendKids(node, kids);
}

function named(name, root) {
  return (root || document).querySelector(`[data-el="${name}"]`);
}
