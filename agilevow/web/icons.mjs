// A small set of line icons, drawn for this page. Built with DOM calls (no markup strings, no inline styles) so
// they pass the strict CSP, and they take their colour from `currentColor`.
const NS = 'http://www.w3.org/2000/svg';

// 24x24 grid, 1.8 stroke. Each entry is a list of path data strings, or ['circle', cx, cy, r].
const SHAPES = {
  tasks: ['M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z', 'M8.5 12.2l2.4 2.4 4.6-5'],
  boards: ['M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z', 'M9.7 4v16', 'M14.3 4v9'],
  folder: ['M3.5 7.5a2 2 0 0 1 2-2H10l2 2.4h6.5a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z'],
  plus: ['M12 5.5v13', 'M5.5 12h13'],
  refresh: ['M19.5 11.5a7.5 7.5 0 1 0-2 5.6', 'M19.8 5.2v5h-5'],
  search: [['circle', 11, 11, 6.5], 'M16 16l4 4'],
  chevronDown: ['M7 9.8l5 5 5-5'],
  chevronRight: ['M9.8 7l5 5-5 5'],
  download: ['M12 4.5v10', 'M7.8 10.8l4.2 4.2 4.2-4.2', 'M5 19.5h14'],
  upload: ['M12 15V5', 'M7.8 8.7L12 4.5l4.2 4.2', 'M5 19.5h14'],
  user: [['circle', 12, 8.6, 3.6], 'M5 20c.6-3.6 3.6-5.6 7-5.6s6.4 2 7 5.6'],
  leaf: ['M5 19c0-8.6 4.7-13.6 14-14 0 9.4-5 14.4-14 14z', 'M5 19l7.5-7.5'],
  shield: ['M12 3.5l7 2.8v5.4c0 4.4-2.9 7.7-7 9.3-4.1-1.6-7-4.9-7-9.3V6.3z'],
  doc: ['M7 3.5h7l4 4V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5A1.5 1.5 0 0 1 7.5 3.5z', 'M14 3.5v4h4', 'M9.2 12.4h5.6', 'M9.2 16h5.6'],
  // status icons, one per column
  backlog: ['M6 4.5h12a1.5 1.5 0 0 1 1.5 1.5v12A1.5 1.5 0 0 1 18 19.5H6A1.5 1.5 0 0 1 4.5 18V6A1.5 1.5 0 0 1 6 4.5z', 'M4.5 13.5h4l1.2 2h4.6l1.2-2h4'],
  ready: ['M12 4l1.9 5.1L19 11l-5.1 1.9L12 18l-1.9-5.1L5 11l5.1-1.9z'],
  doing: ['M12 4.2a7.8 7.8 0 1 1-7.8 7.8'],
  review: ['M2.8 12S6.3 6.2 12 6.2 21.2 12 21.2 12 17.7 17.8 12 17.8 2.8 12 2.8 12z', ['circle', 12, 12, 2.6]],
  post_validation: [['circle', 12, 12, 7.8], 'M8.6 12.2l2.4 2.4 4.4-4.8'],
  done: [['circle', 12, 12, 7.8], 'M8.6 12.2l2.4 2.4 4.4-4.8'],
  blocked: [['circle', 12, 12, 7.8], 'M10 9.2v5.6', 'M14 9.2v5.6'],
  cancelled: [['circle', 12, 12, 7.8], 'M9.4 9.4l5.2 5.2', 'M14.6 9.4l-5.2 5.2'],
  checkCircle: [['circle', 12, 12, 8.6], 'M8.2 12.3l2.7 2.7 4.9-5.3'],
};

export function icon(name, size = 18) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', `icon icon-${name}`);
  for (const shape of SHAPES[name] || []) {
    if (Array.isArray(shape)) {
      const circle = document.createElementNS(NS, 'circle');
      circle.setAttribute('cx', String(shape[1])); circle.setAttribute('cy', String(shape[2])); circle.setAttribute('r', String(shape[3]));
      svg.append(circle);
    } else {
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', shape);
      svg.append(path);
    }
  }
  return svg;
}

// Initials and a stable colour for a name: "agent-1" -> "A1", "local-user" -> "LU". The same name always gets the
// same colour, so a person is recognisable across cards without any account system.
export function initials(name) {
  const words = String(name || '').split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  if (!words.length) return '?';
  const chars = words.length === 1 ? Array.from(words[0]).slice(0, 2) : [Array.from(words[0])[0], Array.from(words[1])[0]];
  return chars.join('').toLocaleUpperCase();
}

export function hue(name) {
  let h = 0;
  for (const ch of String(name || '')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return h % 6;
}
