/* What the kit is made of. A part that brings a widget, a tool or a start
   registers it here; kit.js, the last part, places them where a page asks.

   ITEMS   {id, title, box: {w, h} in em and lines, critical,
            view(node) -> stop, panel(node) -> stop (optional)} - the widgets
           with a box: placed in a container's slot, or alone in an element
           of the page (llmKit.place), in the box they declare. `critical`:
           never shed, never put away - the way back (Customize)
   TOOLS   {id, title, rank, view(node) -> stop} - the instruments: each
           placed alone in an element of the page, filling it, or all of
           them behind one door (llmKit.place('tools', ...)), shown one at a
           time in the order of their rank - the door opens on the first
   STARTS  function() - run once, when the first thing is placed: what the
           kit must learn from the server before a person does anything
   SEEDS   {[slice]: its first value} - the part's own slice of the state

   `view(node)` builds the thing into `node` and returns what stops it: every
   watcher it started, every feed it follows. A thing that is not placed is
   stopped. An item's `panel` is shown beside it while `ui.panel` names the
   item. There is no place here for an application's own screens: an
   application builds its own page, and places these in it. */

const ITEMS = [];
const TOOLS = [];
/* how many times each tool is placed alone on the page, by its id - running
   machinery, not state (kit.js keeps it): the queue's panel says a held
   request can be checked in a Check placed on the page (queue.js) */
const PLACED = {};
const STARTS = [];
const SEEDS = {};
