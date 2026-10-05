// A small set of line icons, drawn for this page. Built with DOM calls (no markup strings, no inline styles) so
// they pass the strict CSP, and they take their colour from `currentColor`.
const NS = 'http://www.w3.org/2000/svg';

// 24x24 grid. Each entry is a list of shapes:
//   'M…'                  a stroked path
//   ['circle', cx, cy, r] a stroked circle
//   ['soft', 'M…']        a stroked path with a pale fill (class .soft, styled in board.css)
//   ['track', cx, cy, r]  a faint full circle behind an arc
//   ['dot', cx, cy, r]    a solid disc
//   ['cut', 'M…']         a path drawn in white on top of a solid disc
const SHAPES = {
  // navigation
  home: ['M4.2 10.6L12 4l7.8 6.6', 'M6 9.6V19a1.4 1.4 0 0 0 1.4 1.4h2.8v-5.2h3.6v5.2h2.8A1.4 1.4 0 0 0 18 19V9.6'],
  tasks: ['M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z', 'M8.5 12.2l2.4 2.4 4.6-5'],
  book: ['M12 6.6C10.4 5.1 7.8 4.6 4.6 4.9V18c3.2-.3 5.8.2 7.4 1.8', 'M12 6.6c1.6-1.5 4.2-2 7.4-1.7V18c-3.2-.3-5.8.2-7.4 1.8', 'M12 6.6v13.2'],
  cube: ['M12 3.6l7.4 4.1v8.6L12 20.4l-7.4-4.1V7.7z', ['circle', 12, 12, 1.9]],
  map: ['M9 5.4L4.2 7.4v11.2L9 16.6l6 2 4.8-2V5.4l-4.8 2z', 'M9 5.4v11.2', 'M15 7.4v11.2'],
  knowledge: ['M7 4h10a2 2 0 0 1 2 2v13.5a.5.5 0 0 1-.8.4L12 15.6l-6.2 4.3a.5.5 0 0 1-.8-.4V6a2 2 0 0 1 2-2z', 'M9.4 8.6h5.2'],
  chart: ['M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z', 'M7.8 14.8l3-3.4 2.3 1.9 3.1-4'],
  gear: [['circle', 12, 12, 3], 'M12 3.6l1.5 1.8 2.3-.5.9 2.2 2.2.9-.5 2.3 1.8 1.5-1.8 1.5.5 2.3-2.2.9-.9 2.2-2.3-.5L12 20.4l-1.5-1.8-2.3.5-.9-2.2-2.2-.9.5-2.3L3.8 12l1.8-1.5-.5-2.3 2.2-.9.9-2.2 2.3.5z'],
  folder: ['M3.5 7.5a2 2 0 0 1 2-2H10l2 2.4h6.5a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z'],
  // actions and controls
  plus: ['M12 5.5v13', 'M5.5 12h13'],
  refresh: ['M19.5 11.5a7.5 7.5 0 1 0-2 5.6', 'M19.8 5.2v5h-5'],
  search: [['circle', 11, 11, 6.5], 'M16 16l4 4'],
  chevronDown: ['M7 9.8l5 5 5-5'],
  chevronRight: ['M9.8 7l5 5-5 5'],
  dots: [['dot', 5.5, 12, 1.6], ['dot', 12, 12, 1.6], ['dot', 18.5, 12, 1.6]],
  sun: [['circle', 12, 12, 3.8], 'M12 3.4v2', 'M12 18.6v2', 'M3.4 12h2', 'M18.6 12h2', 'M5.9 5.9l1.4 1.4', 'M16.7 16.7l1.4 1.4', 'M18.1 5.9l-1.4 1.4', 'M7.3 16.7l-1.4 1.4'],
  moon: ['M19.6 14.4A7.9 7.9 0 0 1 9.6 4.4 8 8 0 1 0 19.6 14.4z'],
  download: ['M12 4.5v10', 'M7.8 10.8l4.2 4.2 4.2-4.2', 'M5 19.5h14'],
  upload: ['M12 15V5', 'M7.8 8.7L12 4.5l4.2 4.2', 'M5 19.5h14'],
  user: [['circle', 12, 8.6, 3.6], 'M5 20c.6-3.6 3.6-5.6 7-5.6s6.4 2 7 5.6'],
  // the overview counts: bare marks, not discs
  inbox: ['M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z', ['dot', 12, 12, 1.5]],
  spinner: [['track', 12, 12, 8], 'M12 4a8 8 0 1 1-8 8'],
  // the principles column
  leaf: [['soft', 'M4 20C4 9.4 9.6 3.6 20.4 3.2 20.4 13.6 14.6 20 4 20z'], 'M4 20l8.2-8.2'],
  shield: [['soft', 'M12 3.5l7 2.8v5.4c0 4.4-2.9 7.7-7 9.3-4.1-1.6-7-4.9-7-9.3V6.3z'], 'M12 3.5v17.5'],
  doc: ['M7 3.5h7l4 4V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5A1.5 1.5 0 0 1 7.5 3.5z', 'M14 3.5v4h4', 'M9.2 12.4h5.6', 'M9.2 16h5.6'],
  // status icons, one per column
  backlog: ['M6 4.5h12a1.5 1.5 0 0 1 1.5 1.5v12A1.5 1.5 0 0 1 18 19.5H6A1.5 1.5 0 0 1 4.5 18V6A1.5 1.5 0 0 1 6 4.5z', 'M4.5 13.5h4l1.2 2h4.6l1.2-2h4'],
  ready: [['soft', 'M12 3.6l2 5.6 5.6 2-5.6 2-2 5.6-2-5.6-5.6-2 5.6-2z']],
  doing: ['M12 4.2a7.8 7.8 0 1 1-7.8 7.8'],
  review: ['M2.8 12S6.3 6.2 12 6.2 21.2 12 21.2 12 17.7 17.8 12 17.8 2.8 12 2.8 12z', ['dot', 12, 12, 2.6]],
  post_validation: [['soft', 'M12 4.2a7.8 7.8 0 1 0 0 15.6 7.8 7.8 0 0 0 0-15.6z'], 'M8.6 12.2l2.4 2.4 4.4-4.8'],
  done: [['soft', 'M12 4.2a7.8 7.8 0 1 0 0 15.6 7.8 7.8 0 0 0 0-15.6z'], 'M8.6 12.2l2.4 2.4 4.4-4.8'],
  blocked: [['soft', 'M12 4.2a7.8 7.8 0 1 0 0 15.6 7.8 7.8 0 0 0 0-15.6z'], 'M10 9.2v5.6', 'M14 9.2v5.6'],
  cancelled: [['soft', 'M12 4.2a7.8 7.8 0 1 0 0 15.6 7.8 7.8 0 0 0 0-15.6z'], 'M9.4 9.4l5.2 5.2', 'M14.6 9.4l-5.2 5.2'],
  checkCircle: [['circle', 12, 12, 8.6], 'M8.2 12.3l2.7 2.7 4.9-5.3'],
  checkFilled: [['dot', 12, 12, 10], ['cut', 'M7.6 12.3l3 3 5.8-6.2']],
};

function shapeNode(shape) {
  const make = tag => document.createElementNS(NS, tag);
  if (typeof shape === 'string') { const path = make('path'); path.setAttribute('d', shape); return path; }
  const [kind, a, b, c] = shape;
  if (kind === 'soft' || kind === 'cut') { const path = make('path'); path.setAttribute('d', a); path.setAttribute('class', kind); return path; }
  const circle = make('circle');
  const [cx, cy, r] = kind === 'circle' || kind === 'track' || kind === 'dot' ? [a, b, c] : [0, 0, 0];
  circle.setAttribute('cx', String(cx)); circle.setAttribute('cy', String(cy)); circle.setAttribute('r', String(r));
  if (kind !== 'circle') circle.setAttribute('class', kind);
  return circle;
}

export function icon(name, size = 18, stroke = 1.8) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', String(stroke));
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', `icon icon-${name}`);
  for (const shape of SHAPES[name] || []) svg.append(shapeNode(shape));
  return svg;
}

// Initials for a name: "agent-1" -> "A1", "local-user" -> "LU". There are no accounts, so who is acting is shown
// by their name alone.
export function initials(name) {
  const words = String(name || '').split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  if (!words.length) return '?';
  const chars = words.length === 1 ? Array.from(words[0]).slice(0, 2) : [Array.from(words[0])[0], Array.from(words[1])[0]];
  return chars.join('').toLocaleUpperCase();
}
