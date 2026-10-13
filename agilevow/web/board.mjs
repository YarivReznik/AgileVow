// The server owns status, revision, dependency, source and lease validation.
// Task text is always rendered as text; claim tokens never enter DOM or exports.
// Every user-visible string comes from i18n.mjs. Do not use `t` as a local name below: it is the translator.
import { t, locale, applyDocumentLocale } from '/i18n.mjs';
import { icon, initials } from '/icons.mjs';

// A count needs its own one/other strings: "1 boards" is wrong in English and the other locales differ.
function plural(key, n) { return t(`${key}.${n === 1 ? 'one' : 'other'}`, {n}); }
applyDocumentLocale();
const $ = id => document.getElementById(id);
document.querySelectorAll('[data-icon]').forEach(slot => slot.replaceChildren(icon(slot.dataset.icon, Number(slot.dataset.size) || 18, slot.closest('.nav-item') ? 2 : 1.8)));
const statuses = ['backlog', 'ready', 'doing', 'review', 'post_validation', 'done', 'blocked', 'cancelled'];
const statusKeys = {backlog:'status.backlog',ready:'status.ready',doing:'status.doing',review:'status.review',post_validation:'status.post_validation',done:'status.done',blocked:'status.blocked',cancelled:'status.cancelled'};
const labels = Object.fromEntries(statuses.map(s => [s, t(statusKeys[s])]));
const transitionTargets = {backlog:['ready','blocked','cancelled'],ready:['backlog','blocked','cancelled'],doing:['review','post_validation','blocked','cancelled'],review:['post_validation','ready','blocked','cancelled'],post_validation:['blocked','cancelled'],done:['backlog'],blocked:['backlog','ready','cancelled'],cancelled:['backlog']};
const state = {boards:[],plans:[],tasks:[],link_kinds:null,selected:null,events:[],busy:false,editor:null};
const leaseKey = 'agilevow.leases.v1';
const actorKey = 'agilevow.actor';
const langKey = 'agilevow.lang';
const themeKey = 'agilevow.theme';
// The principle a new board can start with. Principles are what agents read and are checked against, so it is stored in
// English whatever language the page is shown in. agilevow/principles.py holds the same text; tests/test_principles.py
// keeps the two identical.
const REPORT_AS_YOU_WORK = {key:'report-as-you-work',title:'Report as you work',content:'Keep the card telling the story. When you claim it, comment what you are about to do. Comment each meaningful step, finding or decision as it happens, not at the end. When anything changes (the plan, the scope, a blocker, a dependency), comment what changed and why before you go on. Keep the next step current, and before you stop or hand over, leave it so that someone else could pick the card up.'};
let leases = {};
let seenVersion = null;   // the server's change marker when the board was last loaded (see liveTick)
try { const saved = JSON.parse(sessionStorage.getItem(leaseKey) || '{}'); if (saved && typeof saved === 'object' && !Array.isArray(saved)) leases = saved; } catch { /* Storage can be disabled. */ }
function rememberLease(id, lease) { if (lease) leases[id] = {token:lease,actor:actor()}; else delete leases[id]; try { sessionStorage.setItem(leaseKey, JSON.stringify(leases)); } catch { /* In-memory claims still work. */ } }
function actor() { return $('actor').value.trim() || 'local-user'; }
function leaseFor(task) { return task.status === 'doing' && leases[task.id]?.actor === actor() ? leases[task.id].token : undefined; }
function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
function append(parent, ...children) { parent.append(...children.filter(Boolean)); return parent; }
function button(text, fn, className = '') { const node = el('button', className, text); node.type = 'button'; node.addEventListener('click', fn); return node; }
function mutationButton(text, fn, className = '') { const node = button(text, fn, className); node.dataset.mutation = ''; node.disabled = state.busy; return node; }
function notice(node, text, kind = 'error') { node.className = `notice ${kind}`; node.textContent = text; node.hidden = !text; if (text && kind === 'error' && node.closest('dialog')?.open) node.scrollIntoView({block:'nearest'}); }
function field(name, label, value = '', options = {}) {
  const wrap = el('label', 'field'); wrap.append(el('span', '', label));
  const input = document.createElement(options.type === 'textarea' ? 'textarea' : options.type === 'select' ? 'select' : 'input');
  input.name = name; if (input.tagName === 'INPUT') input.type = options.type || 'text';
  if (options.options) for (const [val, text] of options.options) { const option = el('option', '', text); option.value = val; input.append(option); }
  input.value = value ?? ''; if (options.required) input.required = true;
  if (options.placeholder) input.placeholder = options.placeholder;
  if (options.rows) input.rows = options.rows;
  wrap.append(input); if (options.help) wrap.append(el('span', 'help', options.help)); return {wrap,input};
}
function lines(text) { return String(text || '').split('\n').map(v => v.trim()).filter(Boolean); }
function safeHref(ref) {
  if (typeof ref !== 'string' || !ref.trim() || /[\u0000-\u001f\u007f]/.test(ref)) return null;
  try { const url = new URL(ref, location.origin); return ['http:','https:'].includes(url.protocol) ? url.href : null; } catch { return null; }
}
function reference(ref, label, href) {
  const target = safeHref(href || (/^https?:\/\//i.test(ref || '') || String(ref).startsWith('/') ? ref : ''));
  if (!target) return el('span', 'mono', label || ref);
  const a = el('a', '', label || ref); a.href = target; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
}
function timeValue(value) { return typeof value === 'number' ? value * 1000 : value; }
function date(value) { if (!value) return '—'; const dt = new Date(timeValue(value)); return Number.isNaN(dt.valueOf()) ? String(value) : dt.toLocaleString(locale, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
function expired(task) { return task.claim_expires_at && new Date(timeValue(task.claim_expires_at)).valueOf() <= Date.now(); }
function planFor(task) { return state.plans.find(p => p.id === task.plan_id); }
function boardFor(task) { return state.boards.find(b => b.id === (task.board_id || planFor(task)?.board_id)); }
function scopeFor(task) { return planFor(task)?.scope || boardFor(task)?.scope || ''; }
function taskName(id) { const found = state.tasks.find(tk => tk.id === id); return found ? `${found.key} · ${found.title}` : id; }
function evidenceCount(task) { return (task.acceptance || []).filter((_, i) => task.evidence?.some(e => e.criterion === i && e.ref?.trim())).length; }
// The server decides which link kinds exist (snapshot.link_kinds); an unknown kind is shown by its raw name.
function kindLabel(kind) { const key = 'linkKind.' + kind; const text = t(key); return text === key ? kind : text; }
function priorityOptions() { return [[0,t('priority.p0')],[1,t('priority.p1')],[2,t('priority.p2')],[3,t('priority.p3')]]; }
async function request(path, body, options = null) {
  const response = await fetch(path, options || {method:body ? 'POST' : 'GET',headers:body ? {'Content-Type':'application/json'} : undefined,body:body ? JSON.stringify(body) : undefined,cache:'no-store'});
  const raw = await response.text(); let data; try { data = JSON.parse(raw); } catch { data = {}; }
  if (!response.ok) { const detail = typeof data.error === 'string' ? data.error : data.error?.message || data.message || `${response.status} ${response.statusText}`; const err = new Error(detail); err.status = response.status; throw err; }
  if (!raw || !Object.keys(data).length) throw new Error(t('error.emptyResponse'));
  return data;
}
function setBusy(value) {
  state.busy = value; document.querySelectorAll('[data-mutation]').forEach(b => { b.disabled = value; });
  if (value) document.querySelectorAll('dialog input:not(:disabled),dialog textarea:not(:disabled),dialog select:not(:disabled),#actor:not(:disabled)').forEach(input => { input.disabled = true; input.dataset.busyDisabled = ''; });
  else document.querySelectorAll('[data-busy-disabled]').forEach(input => { input.disabled = false; delete input.dataset.busyDisabled; });
}
async function mutate(path, payload, errorNode, onSuccess) {
  return performMutation(() => request(path, {...payload,actor:actor()}),errorNode,onSuccess);
}
async function performMutation(work, errorNode, onSuccess) {
  if (state.busy) return; setBusy(true);
  try { const result = await work(); await onSuccess(result); }
  catch (err) {
    // 409: the saved data moved on (refresh and keep the draft). 403: who is acting may not do this; the only one the page can
    // meet is someone passing a task they worked on, so point them to another reviewer, not to another name.
    notice(errorNode, err.message + (err.status === 409 ? '\n' + t('error.conflict') : err.status === 403 ? '\n' + t('error.askAnotherReviewer') : ''));
    if (err.status === 409) {
      try {
        await refresh();
        if (state.selected) { const latest = await request(`/api/tasks/item?id=${encodeURIComponent(state.selected.id)}`); state.selected = latest.task; state.events = latest.events || []; const compare = $('latest-saved'); if (compare) { compare.hidden = false; compare.querySelector('pre').textContent = JSON.stringify(latest.task,null,2); } }
        if (state.editor?.plan) state.editor.plan = state.plans.find(p => p.id === state.editor.plan.id) || state.editor.plan;
        if (state.editor?.board) state.editor.board = state.boards.find(b => b.id === state.editor.board.id) || state.editor.board;
        if ($('editor-dialog').open && (state.editor?.plan || state.editor?.board)) {
          let compare = $('editor-latest-saved');
          if (!compare) { compare = append(el('details','notice warning'),el('summary','',t('conflict.latestRecord')),el('pre','saved-comparison')); compare.id = 'editor-latest-saved'; $('editor-fields').append(compare); }
          compare.querySelector('pre').textContent = JSON.stringify(state.editor.plan || state.editor.board,null,2);
        }
      }
      catch (refreshError) { notice(errorNode, `${errorNode.textContent}\n${t('error.refreshFailed',{message:refreshError.message})}`); }
    }
  } finally { setBusy(false); }
}
// Rebuild a select's options only when they differ: replacing them closes a dropdown the user has open.
function setOptions(select, options) {
  const signature = list => JSON.stringify(list.map(o => [o.value, o.text]));
  if (signature(options) === signature([...select.options])) return;
  const value = select.value; select.replaceChildren(...options); select.value = value;
}
async function refresh() {
  // The marker is read first: a write that lands between the two reads only causes one extra reload, never a missed one.
  const marker = await request('/api/tasks/version').catch(() => null);
  const data = await request('/api/tasks'); state.boards = data.boards || []; state.plans = data.plans || []; state.tasks = data.tasks || [];
  if (marker) seenVersion = marker.version;
  const kinds = Array.isArray(data.link_kinds) ? data.link_kinds.filter(k => typeof k === 'string' && k) : []; state.link_kinds = kinds.length ? kinds : null;
  setOptions($('board-filter'), [new Option(t('filter.allBoards'),''), ...state.boards.map(b => new Option(b.title,b.id))]);
  populateProjects();
  setOptions($('scope-filter'), [new Option(t('filter.allScopes'),''), ...[...new Set([...state.boards,...state.plans].map(p => p.scope).filter(Boolean))].sort().map(s => new Option(s,s))]);
  $('updated-at').textContent = t('board.updatedAt',{time:new Date().toLocaleTimeString(locale, {hour:'2-digit',minute:'2-digit'})});
  renderBoard();
}
function populateProjects() {
  const board = $('board-filter').value;
  setOptions($('plan-filter'), [new Option(t('filter.allPlans'),''), ...state.plans.filter(p => !board || p.board_id === board).map(p => new Option(p.title,p.id))]);
}
function avatar(name) { const node = el('span', 'avatar', initials(name)); node.setAttribute('aria-hidden', 'true'); return node; }
function emptyAvatar() { const node = el('span', 'avatar av-none'); node.append(icon('user', 14)); node.setAttribute('aria-hidden', 'true'); return node; }
// The actor is free text, not an account: show it as initials so who is acting is always visible.
function syncActor() {
  const name = actor();
  $('whoami-avatar').textContent = initials(name);
  $('whoami-name').textContent = name;
}
// People already named on a task are offered as "acting as" suggestions; any other name can still be typed.
function syncActorList() {
  const names = [...new Set(state.tasks.map(tk => tk.owner).filter(Boolean))].sort();
  $('actor-list').replaceChildren(...names.map(name => Object.assign(document.createElement('option'), {value:name})));
}
function stat(iconName, key, n) {
  const badge = el('span', `stat-icon stat-${key}`); badge.append(icon(iconName, 24, iconName === 'spinner' ? 2.6 : 2));
  return append(el('li', 'stat'), badge, append(el('span', 'stat-text'), el('strong', 'stat-value', n), el('span', 'stat-label', plural(`stat.${key}`, n))));
}
function chooseBoard(id) { $('board-filter').value = id; $('plan-filter').value = ''; populateProjects(); renderBoard(); }
function choosePlan(id) { $('plan-filter').value = id; renderBoard(); }

// Popovers: one at a time, anchored to the button that opened them. Escape, a click elsewhere, a resize,
// a scroll or choosing something closes it.
let popover = null;
function closePopover() { if (!popover) return; popover.panel.hidden = true; popover.anchor.setAttribute('aria-expanded', 'false'); popover = null; }
function showPopover(panel, anchor) {
  closePopover(); panel.hidden = false; anchor.setAttribute('aria-expanded', 'true'); popover = {panel, anchor};
  const a = anchor.getBoundingClientRect(), rtl = document.documentElement.dir === 'rtl', beside = !!anchor.closest('.sidebar');
  let left = beside ? (rtl ? a.left - panel.offsetWidth - 8 : a.right + 8) : (rtl ? a.left : a.right - panel.offsetWidth), top = beside ? a.top : a.bottom + 8;
  left = Math.max(12, Math.min(left, innerWidth - panel.offsetWidth - 12)); top = Math.max(12, Math.min(top, innerHeight - panel.offsetHeight - 12));
  panel.style.left = `${left}px`; panel.style.top = `${top}px`;
}
function togglePopover(panel, anchor) { if (popover?.anchor === anchor) closePopover(); else showPopover(panel, anchor); }
function menuItem(label, count, checked, choose) {
  const item = el('button', 'menu-item'); item.type = 'button'; item.title = label;
  item.setAttribute('role', 'menuitemradio'); item.setAttribute('aria-checked', String(checked));
  const text = el('span', 'menu-label', label); text.dir = 'auto';
  append(item, text, count === null ? null : el('span', 'menu-count', count));
  item.addEventListener('click', () => { closePopover(); choose(); }); return item;
}
// The sidebar lists the real boards and mini projects; choosing one is the same as using the filters in the toolbar.
function openNavMenu(anchor, kind) {
  const open = tk => tk.status !== 'cancelled';
  const selectedBoard = state.boards.find(b => b.id === $('board-filter').value), selectedPlan = state.plans.find(p => p.id === $('plan-filter').value);
  const items = kind === 'boards'
    ? [menuItem(t('filter.allBoards'), null, !selectedBoard, () => chooseBoard('')), ...state.boards.map(b => menuItem(b.title, state.tasks.filter(tk => open(tk) && boardFor(tk)?.id === b.id).length, b.id === selectedBoard?.id, () => chooseBoard(b.id)))]
    : [menuItem(t('filter.allPlans'), null, !selectedPlan, () => choosePlan('')), ...state.plans.filter(pl => !selectedBoard || pl.board_id === selectedBoard.id).map(pl => menuItem(pl.title, state.tasks.filter(tk => open(tk) && tk.plan_id === pl.id).length, pl.id === selectedPlan?.id, () => choosePlan(pl.id)))];
  const none = kind === 'boards' ? !state.boards.length : !state.plans.length;
  const panel = $('nav-menu'); panel.setAttribute('aria-label', t(kind === 'boards' ? 'nav.boards' : 'nav.plans'));
  panel.replaceChildren(...(none ? [el('div', 'menu-empty', t(kind === 'boards' ? 'nav.noBoards' : 'nav.noPlans'))] : items));
  showPopover(panel, anchor); panel.querySelector('[aria-checked="true"]')?.focus();
}
function cardFooter(task) {
  const done = (task.checklist || []).filter(c => c.checked).length, total = task.acceptance?.length || 0;
  const who = el('span', task.owner ? 'owner' : 'owner unassigned');
  who.append(task.owner ? avatar(task.owner) : emptyAvatar(), el('span', 'owner-name', task.owner || t('task.unassigned')));
  const complete = total > 0 && done === total, checks = el('span', `checks${complete ? ' complete' : ''}`, t('card.checked', {done, total}));
  checks.title = t('card.checkedTitle', {done, total}); if (complete) checks.append(icon('checkFilled', 15));
  return append(el('div', 'card-bottom'), who, checks);
}
function renderBoard() {
  const selectedBoard = state.boards.find(b => b.id === $('board-filter').value);
  const selectedPlan = state.plans.find(p => p.id === $('plan-filter').value);
  const scoped = state.tasks.filter(tk => (!selectedBoard || boardFor(tk)?.id === selectedBoard.id) && (!selectedPlan || tk.plan_id === selectedPlan.id) && (!$('scope-filter').value || scopeFor(tk) === $('scope-filter').value));
  const active = scoped.filter(tk => tk.status !== 'cancelled'), complete = active.filter(tk => tk.status === 'done').length;
  const progress = active.length ? Math.round(complete / active.length * 100) : 0;
  const counts = {boards:state.boards.length,plans:selectedBoard ? state.plans.filter(p => p.board_id === selectedBoard.id).length : state.plans.length,doing:scoped.filter(tk => tk.status === 'doing').length,blocked:scoped.filter(tk => tk.status === 'blocked').length};
  $('summary-eyebrow').textContent = selectedPlan ? `${selectedBoard?.title || state.boards.find(b => b.id === selectedPlan.board_id)?.title || t('common.board')} / ${selectedPlan.kind || t('common.miniProject')}` : selectedBoard?.scope || t('summary.overview');
  $('summary-title').textContent = selectedPlan?.title || selectedBoard?.title || t('summary.titleDefault');
  const description = selectedPlan?.description || selectedBoard?.description || (state.boards.length ? '' : t('summary.descriptionEmpty'));
  $('summary-description').textContent = description; $('summary-description').hidden = !description;
  $('summary-stats').replaceChildren(...(state.boards.length ? [...(selectedBoard ? [] : [stat('inbox', 'boards', counts.boards)]), stat('folder', 'plans', counts.plans), stat('spinner', 'doing', counts.doing), stat('blocked', 'blocked', counts.blocked)] : []));
  $('edit-plan-button').hidden = !selectedPlan;
  $('edit-board-button').hidden = !selectedBoard;
  $('progress-label').textContent = `${complete} / ${active.length}`; $('progress').setAttribute('aria-valuenow',progress); $('progress-value').style.width = `${progress}%`;
  $('side-progress-label').textContent = `${complete}/${active.length}`; $('side-ring').style.setProperty('--p', String(progress)); $('side-bar').style.width = `${progress}%`;
  const search = $('search').value.trim().toLocaleLowerCase();
  const tasks = scoped.filter(tk => !search || [tk.title,tk.key,tk.outcome,tk.next_step,tk.owner,...(tk.tags || []),planFor(tk)?.title,boardFor(tk)?.title].filter(Boolean).join(' ').toLocaleLowerCase().includes(search));
  const chosenStatus = $('status-filter').value;
  const visible = chosenStatus ? [chosenStatus] : $('show-inactive').checked ? statuses : statuses.slice(0,6);
  const shown = tasks.filter(tk => visible.includes(tk.status));
  const excluded = tasks.length - shown.length;
  $('board-count').textContent = (shown.length === 1 ? t('board.taskOne') : t('board.taskMany',{n:shown.length})) + (excluded ? t('board.outsideSuffix',{n:excluded}) : '');
  $('nav-count').textContent = String(shown.length); syncActorList();
  $('empty-board').hidden = state.boards.length > 0;
  $('board').className = `board${chosenStatus ? ' single' : visible.length > 5 ? ' extra' : ''}`;
  $('board').style.setProperty('--status-columns',String(visible.length));
  $('board').replaceChildren(principlesColumn(selectedBoard,selectedPlan),...visible.map(status => {
    const inColumn = tasks.filter(tk => tk.status === status).sort((a,b) => a.priority-b.priority || new Date(timeValue(a.created_at || 0)).valueOf() - new Date(timeValue(b.created_at || 0)).valueOf());
    const column = el('section','column'); column.dataset.status = status; column.setAttribute('aria-label',`${labels[status]} (${inColumn.length})`);
    const mark = el('span','status-icon'); mark.append(icon(status,17,2));
    const head = append(el('div','column-header'),mark,el('h3','',labels[status]),el('span','column-count',inColumn.length));
    if (['backlog','ready'].includes(status)) { const add = button('',() => openTaskEditor(status),'column-add'); add.append(icon('plus',16)); add.setAttribute('aria-label',t('column.addAria',{status:labels[status].toLocaleLowerCase()})); head.append(add); }
    const cards = el('div','cards'); cards.append(...inColumn.map(task => {
      const card = button('',() => openTask(task.id),'task-card'); card.dataset.taskId = task.id; card.setAttribute('aria-label',t('card.aria',{title:task.title,status:labels[status],priority:task.priority}));
      const top = append(el('div','card-top'),el('span','task-key',`${scopeFor(task) ? `${scopeFor(task)} / ` : ''}${task.key || task.id}`),el('span',`priority p${task.priority}`,`P${task.priority}`));
      const flags = el('div','flex wrap');
      if (task.principles_changed) flags.append(el('span','badge warn',t('badge.principlesChanged')));
      if (task.corrections?.some(c => !c.checked)) flags.append(el('span','badge blocked',t('badge.needsCorrection')));
      if (task.source_changed) flags.append(el('span','badge warn',t('badge.sourceChanged')));
      if (task.status === 'doing' && expired(task)) flags.append(el('span','badge blocked',t('badge.claimExpired')));
      const waiting = (task.depends_on || []).filter(id => state.tasks.find(tk => tk.id === id)?.status !== 'done').length;
      if (waiting) flags.append(el('span','badge',t('badge.waitingOn',{n:waiting})));
      if (task.blocked_reason && task.status === 'blocked') flags.append(el('span','badge blocked',t('badge.blocked')));
      append(card,top,el('div','task-title',task.title),selectedPlan ? null : el('div','help',`${selectedBoard ? '' : `${boardFor(task)?.title || t('common.board')} / `}${planFor(task)?.title || t('common.miniProject')}`),el('div','task-outcome',task.next_step || task.outcome),flags.childNodes.length ? flags : null,cardFooter(task));
      card.querySelectorAll('.task-title,.task-outcome,.help').forEach(node => { node.dir = 'auto'; });
      const keyNode = card.querySelector('.task-key'); keyNode.title = keyNode.textContent; // a long key is cut with an ellipsis; the whole of it is on hover
      return card;
    }));
    if (!inColumn.length) cards.append(el('div','empty-column',search ? t('column.noMatch') : t('column.none')));
    return append(column,head,cards);
  }));
}
function principleCards(principles) {
  return (principles || []).map((p, i) => {
    // The icon alternates between two marks; it carries no meaning of its own.
    const mark = el('span', i % 2 ? 'principle-icon leaf-mark' : 'principle-icon'); mark.append(icon(i % 2 ? 'leaf' : 'doc', 30, i % 2 ? 1.7 : 1.9));
    const card = append(el('article', 'principle-card'), append(el('div', 'principle-head'), mark, el('h4', '', p.title)), el('p', '', p.content));
    card.dir = 'auto'; return card;
  });
}
function principlesColumn(selectedBoard,selectedPlan) {
  const column = el('section','column principles-column'); column.id = 'principles-column';
  column.setAttribute('aria-label',t('principles.title'));
  const leaf = el('span', 'status-icon leaf'); leaf.append(icon('leaf', 32, 1.7));
  append(column,append(el('div','column-header'),leaf,el('h3','',t('principles.title'))),el('p','help',t('principles.help')));
  const boards = state.boards.filter(b => (!selectedBoard || b.id === selectedBoard.id) && (!selectedPlan || b.id === selectedPlan.board_id) && (!$('scope-filter').value || b.scope === $('scope-filter').value || state.plans.some(p => p.board_id === b.id && p.scope === $('scope-filter').value)));
  for (const board of boards) {
    const group = el('div','principle-group');
    const edit = mutationButton(t('principles.edit'),() => openPrinciplesEditor(board),'link-button'); edit.append(icon('chevronRight',14));
    const shield = el('span', 'principle-icon shield'); shield.append(icon('shield', 30, 1.9));
    append(group, append(el('div', 'principle-board'), shield, append(el('div', 'principle-text'), el('strong', '', board.title), edit)));
    append(group,...principleCards(board.principles));
    if (!board.principles?.length) group.append(el('p','help',t('principles.none')));
    column.append(group);
  }
  return column;
}
function openPrinciplesEditor(board) {
  openEditor(t('principles.title'),t('principles.editorHelp'),container => {
    const rows=[],list=el('div','stack'); container.append(list);
    function draw(){ list.replaceChildren(...rows.map(r => r.node)); }
    function add(p={}) {
      // The wording is the author's own and may be in any language, so each box picks its direction from what is typed.
      const title=field('principle_title',t('principles.fieldTitle'),p.title,{required:true}); title.input.dir='auto';
      const content=field('principle_content',t('principles.fieldContent'),p.content,{type:'textarea',required:true,rows:5}); content.input.dir='auto';
      const row={key:p.key || 'principle-'+crypto.randomUUID(),title:title.input,content:content.input};
      const move=delta=>{const i=rows.indexOf(row),to=i+delta;if(to<0||to>=rows.length)return;rows.splice(i,1);rows.splice(to,0,row);draw();};
      row.node=append(el('div','principle-editor-row'),title.wrap,content.wrap,append(el('div','actions'),button('↑',()=>move(-1)),button('↓',()=>move(1)),button(t('principles.remove'),()=>{rows.splice(rows.indexOf(row),1);draw();},'quiet')));
      rows.push(row);draw();
    }
    for(const p of board.principles || [])add(p);
    container.append(button(t('principles.add'),()=>add()));
    const note=field('principles_note',t('principles.note'),'',{type:'textarea',required:true});note.input.dir='auto';container.append(note.wrap);
    return {rows,note:note.input};
  },(fields,error)=>{
    const saved=state.editor.board;
    mutate('/api/boards/update',{id:saved.id,revision:saved.revision,principles:fields.rows.map(r=>({key:r.key,title:r.title.value.trim(),content:r.content.value.trim()})),note:fields.note.value.trim()},error,async()=>{await refresh();$('editor-dialog').close();notice($('global-notice'),t('principles.updatedNotice'),'success');});
  },{board});
}
function understandingSection(task) {
  const context=task.understanding_context;
  const section=append(el('section','understanding-context'),el('h3','',t('understanding.title')),el('p','help',t('understanding.help')));
  if(context){
    append(section,...principleCards(context.principles));
    if(!context.principles.length)section.append(el('p','help',t('understanding.none')));
    if(task.principles_changed)section.append(el('p','notice warning',t('understanding.changed')));
    section.append(button(t('understanding.copy'),async()=>{try{await navigator.clipboard.writeText(context.text);notice($('global-notice'),t('understanding.copied'),'success');}catch{notice($('global-notice'),t('understanding.copyFailed'));}}));
    section.append(append(el('details'),el('summary','',t('understanding.fullContext')),el('pre','context-text',context.text)));
  }
  return section;
}
function validationSection(task,onSave) {
  const section=append(el('section','section stack validation-section'),el('h3','',t('validation.title')),el('p','help',t('validation.help')));
  const form=el('form','stack');const inputs=[];
  const summary=field('validation_summary',t('validation.summary'),'',{type:'textarea',required:true});form.append(summary.wrap);
  const requirements=[...task.acceptance.map((text,i)=>({kind:'criterion',key:i,text})),...(task.understanding_context?.principles||[]).map(p=>({kind:'principle',key:p.key,text:t('validation.principleRequirement',{title:p.title,content:p.content})}))];
  for(const [i,requirement] of requirements.entries()){
    const text=requirement.text;
    const row=append(el('div','criterion'),el('strong','',text));
    const result=field('validation_result_'+i,t('validation.result'),'',{type:'select',required:true,options:[['',t('validation.choose')],['pass',t('validation.pass')],['fail',t('validation.fail')]]});
    const fields={};for(const [key,label]of [['observed',t('validation.observed')],['ref',t('validation.ref')],['expected',t('validation.expected')],['avoid',t('validation.avoid')],['fix',t('validation.fix')]]){
      const f=field('validation_'+key+'_'+i,label,'',{type:key==='ref'?'text':'textarea',required:['observed','ref'].includes(key)});fields[key]=f.input;row.append(f.wrap);
    }
    result.input.addEventListener('change',()=>{for(const key of ['expected','avoid','fix'])fields[key].required=result.input.value==='fail';});
    row.prepend(result.wrap);form.append(row);inputs.push({requirement,result:result.input,fields});
  }
  const error=el('div','notice error');error.hidden=true;error.role='alert';
  form.append(error,mutationButton(t('validation.save'),()=>form.requestSubmit(),'primary'));
  form.addEventListener('submit',event=>{
    event.preventDefault();if($('task-edit-form')?.dataset.dirty){notice(error,t('validation.saveFirst'));return;}
    const rows=inputs.map(r=>({kind:r.requirement.kind,key:r.requirement.key,passed:r.result.value==='pass',...Object.fromEntries(Object.entries(r.fields).map(([k,v])=>[k,v.value.trim()]))}));
    const checks=rows.filter(r=>r.kind==='criterion').map(({kind,key,...rest})=>({criterion:key,...rest}));
    const principles=rows.filter(r=>r.kind==='principle').map(({kind,...rest})=>rest);
    const current=state.selected;
    mutate('/api/tasks/action',{id:current.id,revision:current.revision,op:'validate',principles_sha256:current.understanding_context.principles_sha256,validation:{result:rows.every(c=>c.passed)?'pass':'fail',summary:summary.input.value.trim(),checks,principles}},error,onSave);
  });section.append(form);return section;
}
function openEditor(title, help, setup, submit, context = {}, saveKey = 'action.save') {
  state.editor = context; $('editor-title').textContent = title; $('editor-help').textContent = help;
  $('editor-fields').replaceChildren(); const error = el('div','notice error'); error.id = 'editor-error'; error.role = 'alert'; error.hidden = true;
  const fields = setup($('editor-fields')); $('editor-fields').append(error);
  $('editor-form').onsubmit = event => { event.preventDefault(); submit(fields,error); };
  $('editor-save').textContent = t(saveKey);
  $('editor-dialog').showModal(); $('editor-dialog').scrollTop = 0;
}
function openBoardEditor(board = null) {
  openEditor(board ? t('boardEditor.edit') : t('boardEditor.new'),t('boardEditor.help'),container => {
    const fields = {};
    for (const [key,label,value,options] of [
      ['title',t('boardEditor.fieldTitle'),board?.title,{required:true,placeholder:t('boardEditor.titlePlaceholder')}],
      ['source_key',t('boardEditor.fieldKey'),board?.source_key,{required:true,placeholder:t('boardEditor.keyPlaceholder')}],
      ['scope',t('common.scope'),board?.scope,{placeholder:t('boardEditor.scopePlaceholder')}],
      ['description',t('common.purpose'),board?.description,{type:'textarea',placeholder:t('boardEditor.descriptionPlaceholder')}]
    ]) { const f = field(key,label,value,options); fields[key] = f.input; container.append(f.wrap); }
    if (board) { fields.source_key.readOnly = true; return fields; }
    // A new board starts with one principle, ticked by default: whoever works on a card says what they are doing as they go.
    const offer = field('default_principle',t('principles.defaultOffer'),'',{type:'checkbox',help:REPORT_AS_YOU_WORK.content});
    offer.wrap.querySelector('.help').dir = 'ltr';
    offer.input.checked = true; offer.wrap.classList.add('field-check'); container.append(offer.wrap);
    return {...fields, defaultPrinciple: offer.input};
  },(fields,error) => {
    const saved = state.editor.board, {defaultPrinciple, ...inputs} = fields;
    const payload = Object.fromEntries(Object.entries(inputs).map(([key,input]) => [key,input.value.trim()]));
    if (saved) { payload.id = saved.id; payload.revision = saved.revision; delete payload.source_key; }
    else if (defaultPrinciple?.checked) payload.principles = [{...REPORT_AS_YOU_WORK}];
    mutate(`/api/boards/${saved ? 'update' : 'create'}`,payload,error,async result => { await refresh(); $('board-filter').value = result.board.id; $('plan-filter').value = ''; populateProjects(); renderBoard(); $('editor-dialog').close(); notice($('global-notice'),saved ? t('boardEditor.updatedNotice') : t('boardEditor.createdNotice'),'success'); });
  },{board});
}
function openPlanEditor(plan = null) {
  if (!state.boards.length) { openBoardEditor(); return; }
  openEditor(plan ? t('planEditor.edit') : t('planEditor.new'),t('planEditor.help'),container => {
    const fields = {};
    for (const [key,label,value,options] of [
      ['board_id',t('common.board'),plan?.board_id || $('board-filter').value || state.boards[0].id,{type:'select',options:state.boards.map(b => [b.id,b.title])}],
      ['title',t('planEditor.fieldTitle'),plan?.title,{required:true,placeholder:t('planEditor.titlePlaceholder')}],
      ['source_key',t('planEditor.fieldKey'),plan?.source_key,{required:true,help:t('planEditor.keyHelp'),placeholder:t('planEditor.keyPlaceholder')}],
      ['kind',t('planEditor.fieldKind'),plan?.kind || 'mixed',{placeholder:t('planEditor.kindPlaceholder')}],
      ['scope',t('common.scope'),plan?.scope || state.boards.find(b => b.id === $('board-filter').value)?.scope,{placeholder:t('planEditor.scopePlaceholder')}],
      ['description',t('common.purpose'),plan?.description,{type:'textarea',placeholder:t('planEditor.descriptionPlaceholder')}],
      ['sources',t('common.watchedDocuments'),(plan?.sources || []).map(s => s.path).join('\n'),{type:'textarea',help:t('planEditor.sourcesHelp'),placeholder:t('planEditor.sourcesPlaceholder')}]
    ]) { const f = field(key,label,value,options); fields[key] = f.input; container.append(f.wrap); }
    if (plan) { const note = field('note',t('common.changeNote'),'',{placeholder:t('planEditor.notePlaceholder')}); fields.note = note.input; container.append(note.wrap); }
    if (plan) { fields.source_key.readOnly = true; fields.board_id.disabled = true; }
    return fields;
  }, (fields,error) => {
    const p = state.editor.plan; const payload = {board_id:fields.board_id.value,title:fields.title.value.trim(),source_key:fields.source_key.value.trim(),kind:fields.kind.value.trim(),scope:fields.scope.value.trim(),description:fields.description.value.trim(),sources:lines(fields.sources.value)};
    if (p) { Object.assign(payload,{id:p.id,revision:p.revision,note:fields.note.value.trim()}); delete payload.board_id; delete payload.source_key; if (JSON.stringify(payload.sources) === JSON.stringify(p.sources.map(s => s.path))) delete payload.sources; }
    mutate(`/api/plans/${p ? 'update' : 'create'}`,payload,error,async result => { await refresh(); $('board-filter').value = result.plan.board_id; populateProjects(); $('plan-filter').value = result.plan.id; renderBoard(); $('editor-dialog').close(); notice($('global-notice'),p ? t('planEditor.updatedNotice') : t('planEditor.createdNotice'),'success'); });
  },{plan});
}
function openTaskEditor(status = 'backlog') {
  if (!state.plans.length) { openPlanEditor(); return; }
  openEditor(t('taskEditor.new'),t('taskEditor.help'),container => {
    const fields = {};
    for (const [key,label,value,options] of [
      ['plan_id',t('common.miniProject'),$('plan-filter').value || state.plans.find(p => p.board_id === $('board-filter').value)?.id || state.plans[0].id,{type:'select',options:state.plans.map(p => [p.id,`${state.boards.find(b => b.id === p.board_id)?.title || t('common.board')} / ${p.title}`])}],
      ['key',t('taskEditor.fieldKey'),'',{required:true,placeholder:t('taskEditor.keyPlaceholder')}],
      ['title',t('taskEditor.fieldTitle'),'',{required:true,placeholder:t('taskEditor.titlePlaceholder')}],
      ['outcome',t('common.outcome'),'',{type:'textarea',required:true,placeholder:t('taskEditor.outcomePlaceholder')}],
      ['acceptance',t('common.acceptance'),'',{type:'textarea',required:true,help:t('taskEditor.acceptanceHelp')}],
      ['priority',t('common.priority'),'2',{type:'select',options:priorityOptions()}],
      ['status',t('taskEditor.fieldStatus'),status,{type:'select',options:[['backlog',labels.backlog],['ready',labels.ready]]}],
      ['next_step',t('common.nextStep'),'',{placeholder:t('taskEditor.nextStepPlaceholder')}],
      ['depends_on',t('common.dependencies'),'',{type:'textarea',help:t('taskEditor.dependenciesHelp')}],
      ['sources',t('taskEditor.fieldSources'),'',{type:'textarea',help:t('taskEditor.sourcesHelp')}]
    ]) { const f = field(key,label,value,options); fields[key] = f.input; container.append(f.wrap); }
    const preview=el('div','understanding-context');container.prepend(preview);
    const showContext=()=>{const board=state.boards.find(b=>b.id===state.plans.find(p=>p.id===fields.plan_id.value)?.board_id);preview.replaceChildren(el('h3','',t('taskEditor.principlesPreview')),...principleCards(board?.principles));};
    fields.plan_id.addEventListener('change',showContext);showContext();
    return fields;
  },(fields,error) => {
    const payload = Object.fromEntries(Object.entries(fields).map(([key,input]) => [key,input.value.trim()]));
    payload.acceptance = lines(payload.acceptance); payload.depends_on = lines(payload.depends_on); payload.sources = lines(payload.sources); payload.priority = Number(payload.priority);
    mutate('/api/tasks/create',payload,error,async result => { await refresh(); $('editor-dialog').close(); await openTask(result.task.id); notice($('global-notice'),t('taskEditor.createdNotice'),'success'); });
  });
}
function openImport() {
  if (!state.boards.length) { openBoardEditor(); return; }
  // A sample of the file format. Its keys are the format; its values are illustrative and deliberately not translated.
  const example = {source_key:'example-plan',title:'A useful objective',description:'Why this work matters',scope:'shared',sources:[],tasks:[{key:'first-step',title:'First useful step',outcome:'The intended result',acceptance:['An observable result is verified'],priority:2,status:'backlog',depends_on:[]}]};
  openEditor(t('import.title'),t('import.help'),container => {
    const board = field('board_id',t('import.intoBoard'),$('board-filter').value || state.boards[0].id,{type:'select',options:state.boards.map(b => [b.id,b.title])});
    const file = field('file',t('import.loadFile'),'',{type:'file'}); file.input.accept = '.json,application/json';
    const json = field('json',t('import.json'),'',{type:'textarea',required:true,rows:15,placeholder:JSON.stringify(example,null,2)}); json.input.classList.add('mono');
    file.input.addEventListener('change', async () => { const selected = file.input.files[0]; if (selected) { if (selected.size > 5_000_000) { notice($('editor-error'),t('import.tooLarge')); return; } json.input.value = await selected.text(); } });
    const details = append(el('details'),el('summary','',t('import.showFormat')),el('pre','mono subtle-panel',JSON.stringify(example,null,2)));
    append(container,board.wrap,file.wrap,json.wrap,details); return {json:json.input,board:board.input};
  },(fields,error) => {
    let payload; try { payload = JSON.parse(fields.json.value); } catch (err) { notice(error,t('import.invalidJson',{message:err.message})); return; }
    if (!payload || Array.isArray(payload) || typeof payload !== 'object') { notice(error,t('import.notObject')); return; }
    payload.board_id = fields.board.value;
    mutate('/api/plans/import',payload,error,async result => { await refresh(); $('board-filter').value = result.plan.board_id; populateProjects(); $('plan-filter').value = result.plan.id; renderBoard(); $('editor-dialog').close(); notice($('global-notice'),result.imported === false ? t('import.alreadyImported') : plural('import.done',result.tasks?.length || 0),'success'); });
  },{},'action.importPlan');
}
async function exportBoard() {
  try { const data = await request('/api/tasks/export'); const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'})); const link = el('a'); link.href = url; link.download = `agilevow-${new Date().toISOString().slice(0,10)}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000); notice($('global-notice'),t('export.done'),'success'); }
  catch (err) { notice($('global-notice'),err.message); }
}
async function openTask(id) {
  try { const data = await request(`/api/tasks/item?id=${encodeURIComponent(id)}`); state.selected = data.task; state.events = data.events || []; renderTask(); if (!$('task-dialog').open) $('task-dialog').showModal(); $('task-dialog').scrollTop = 0; }
  catch (err) { notice($('global-notice'),err.message); }
}
function renderSources(sources) {
  const list = el('ul','list');
  for (const source of sources || []) list.append(append(el('li'),reference(source.path,source.path,source.href),el('span','source-hash',source.sha256 || t('source.noFingerprint'))));
  if (!list.childNodes.length) list.append(el('li','muted',t('source.none')));
  return list;
}
function updateSavedTask(task) {
  state.selected = task;
  const index = state.tasks.findIndex(tk => tk.id === task.id); if (index >= 0) state.tasks[index] = task;
  if ($('task-revision')) $('task-revision').textContent = t('task.revision',{n:task.revision});
  renderBoard();
}
function attachmentList(container, task) {
  container.replaceChildren();
  for (const attachment of task.attachments || []) {
    const href = attachment.href || `/api/tasks/attachment?task=${encodeURIComponent(task.id)}&attachment=${encodeURIComponent(attachment.id)}`;
    const download = reference(attachment.filename,attachment.filename,href); if (download.tagName === 'A') download.download = attachment.filename;
    const size = attachment.size >= 1024 * 1024 ? `${(attachment.size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1,Math.round(attachment.size / 1024))} KB`;
    container.append(append(el('li','subtle-panel'),append(el('div','flex spread wrap'),download,el('span','badge',`${attachment.kind} · ${size}`)),el('span','help',`${attachment.author || ''} · ${date(attachment.created_at)}`),el('span','source-hash',attachment.sha256)));
  }
  if (!container.childNodes.length) container.append(el('li','muted',t('attach.none')));
}
function attachmentsSection(task) {
  const section = append(el('section','section stack'),el('h3','',t('attach.title')),el('p','help',t('attach.help')));
  const list = el('ul','list'); list.id = 'attachment-list'; attachmentList(list,task);
  const form = el('form','stack'); form.id = 'attachment-form';
  const kind = field('attachment_kind',t('attach.kind'),'asset',{type:'select',options:[['asset',t('attach.kindAsset')],['document',t('attach.kindDocument')]]});
  const files = field('attachments',t('attach.files'),'',{type:'file',help:t('attach.filesHelp')}); files.input.multiple = true; files.input.id = 'attachment-files';
  const error = el('div','notice'); error.hidden = true; error.role = 'alert'; const uploaded = new Set();
  files.input.addEventListener('change',() => { uploaded.clear(); });
  const submit = mutationButton(t('attach.upload'),() => form.requestSubmit());
  append(form,append(el('div','two-up'),kind.wrap,files.wrap),error,submit);
  form.addEventListener('submit',event => {
    event.preventDefault();
    const pending = [...files.input.files].filter(file => !uploaded.has(file));
    if (!pending.length) { notice(error,t('attach.choose')); return; }
    const oversized = pending.find(file => file.size > 32 * 1024 * 1024); if (oversized) { notice(error,t('attach.oversized',{name:oversized.name})); return; }
    performMutation(async () => {
      for (const file of pending) {
        const current = state.selected; const headers = {'Content-Type':file.type || 'application/octet-stream','X-Task-Revision':String(current.revision),'X-Task-Actor':encodeURIComponent(actor()),'X-Task-File-Name':encodeURIComponent(file.name),'X-Task-File-Kind':kind.input.value};
        const lease = leaseFor(current); if (lease) headers['X-Task-Lease'] = lease;
        try { const result = await request(`/api/tasks/attach?id=${encodeURIComponent(current.id)}`,null,{method:'POST',headers,body:file,cache:'no-store'}); uploaded.add(file); updateSavedTask(result.task); attachmentList(list,result.task); }
        catch (err) { err.message = `${file.name}: ${err.message}\n${uploaded.size ? t('attach.partial',{n:uploaded.size}) : t('attach.kept')}`; throw err; }
      }
      return {count:pending.length};
    },error,async result => { files.input.value = ''; uploaded.clear(); notice(error,result.count === 1 ? t('attach.doneOne') : t('attach.doneMany',{n:result.count}),'success'); await refresh(); });
  }); append(section,list,form); return section;
}
const commentKeys = {note:'comment.kind.note',conversation:'comment.kind.conversation',decision:'comment.kind.decision'};
const commentLabel = kind => commentKeys[kind] ? t(commentKeys[kind]) : kind;
function commentList(container, task) {
  container.replaceChildren();
  for (const comment of task.comments || []) {
    const item = append(el('li','subtle-panel'),append(el('div','flex spread wrap'),el('strong','',comment.author),el('span','badge',commentLabel(comment.kind))),el('p','comment-body',comment.body),el('span','help',date(comment.created_at)));
    if (comment.source_ref) append(item,el('br'),reference(comment.source_ref,comment.source_ref)); container.append(item);
  }
  if (!container.childNodes.length) container.append(el('li','muted',t('comment.none')));
}
function discussionSection(task) {
  const section = append(el('section','section stack'),el('h3','',t('comment.title')),el('p','help',t('comment.help')));
  const list = el('ol','list'); list.id = 'comment-list'; commentList(list,task);
  const form = el('form','stack'); form.id = 'comment-form';
  const kind = field('comment_kind',t('comment.type'),'note',{type:'select',options:Object.keys(commentKeys).map(k => [k,commentLabel(k)])});
  const text = field('comment_body',t('comment.message'),'',{type:'textarea',required:true,placeholder:t('comment.messagePlaceholder')}); text.input.id = 'comment-body'; text.input.maxLength = 12000;
  const source = field('source_ref',t('comment.source'),'',{placeholder:t('comment.sourcePlaceholder')}); source.input.id = 'comment-source';
  const error = el('div','notice'); error.role = 'alert'; error.hidden = true;
  append(form,kind.wrap,text.wrap,source.wrap,error,mutationButton(t('comment.add'),() => form.requestSubmit()));
  form.addEventListener('submit',event => {
    event.preventDefault(); const current = state.selected; const payload = {id:current.id,revision:current.revision,kind:kind.input.value,body:text.input.value.trim(),source_ref:source.input.value.trim()}; const lease = leaseFor(current); if (lease) payload.lease = lease;
    mutate('/api/tasks/comment',payload,error,async result => { updateSavedTask(result.task); commentList(list,result.task); text.input.value = ''; source.input.value = ''; notice(error,t('comment.added'),'success'); await refresh(); });
  }); append(section,list,form); return section;
}
function renderTask() {
  const task = state.selected; $('task-title').textContent = task.title; $('task-eyebrow').textContent = `${planFor(task)?.title || t('common.plan')} / ${task.key}`;
  const body = $('task-body'); body.replaceChildren();
  const error = el('div','notice error'); error.id = 'task-error'; error.role = 'alert'; error.hidden = true;
  const revision = el('span','mono',t('task.revision',{n:task.revision})); revision.id = 'task-revision';
  const meta = append(el('div','detail-meta'),append(el('span','badge'),el('span',`status-dot status-${task.status}`),el('span','',labels[task.status])),el('span','badge',`P${task.priority}`),revision);
  if (task.source_changed) meta.append(el('span','badge warn',t('badge.sourceChanged')));
  const facts = el('dl','detail-facts');
  for (const [label,value,mono] of [[t('task.owner'),task.owner || t('task.unassigned')],[t('task.claimExpires'),date(task.claim_expires_at)],[t('task.updated'),date(task.updated_at)],[t('task.id'),task.id,true]]) facts.append(append(el('div'),el('dt','',label),el('dd',mono ? 'mono' : '',value)));
  const comparison = append(el('details','notice warning'),el('summary','',t('conflict.latestTask')),el('pre','saved-comparison')); comparison.id = 'latest-saved'; comparison.hidden = true;
  append(body,understandingSection(task),meta,facts,error,comparison);
  if(task.validations?.length){const last=task.validations.at(-1);body.append(append(el('section','subtle-panel'),el('h3','',last.result==='pass'?t('validation.passed'):t('validation.failed')),el('p','',last.summary)));}
  if(task.status==='post_validation')body.append(validationSection(task,async result=>{state.selected=result.task;await refresh();await openTask(task.id);notice($('global-notice'),result.task.status==='done'?t('validation.savedDone'):t('validation.savedReturned'),'success');}));
  const actionSection = el('section','action-group subtle-panel'); actionSection.append(el('h3','',t('task.statusOwnership')));
  const actionNote = field('action_note',t('task.actionNote'),'',{placeholder:t('task.actionNotePlaceholder')}); actionSection.append(actionNote.wrap);
  const doneKeys = {claim:'task.done.claim',renew:'task.done.renew',release:'task.done.release',recover:'task.done.recover',reconcile:'task.done.reconcile',acknowledge_principles:'task.done.acknowledge'};
  const currentAction = async (op, extra = {}) => {
    if ($('task-edit-form')?.dataset.dirty || $('comment-body')?.value.trim() || $('comment-source')?.value.trim() || $('attachment-files')?.files.length) { notice(error,t('task.draftsFirst')); return; }
    const current = state.selected; const payload = {op,id:current.id,revision:current.revision,note:actionNote.input.value.trim(),...extra}; if (op==='claim'||op==='acknowledge_principles'||(op==='transition'&&['review','post_validation'].includes(extra.status))) payload.principles_sha256=current.understanding_context?.principles_sha256; const lease = leaseFor(current); if (lease && (['renew','release','acknowledge_principles'].includes(op) || (op === 'transition' && ['review','post_validation'].includes(extra.status)))) payload.lease = lease;
    await mutate('/api/tasks/action',payload,error,async result => { if (result.lease) rememberLease(current.id,result.lease); if (['release','recover'].includes(op) || (result.task && result.task.status !== 'doing')) rememberLease(current.id,null); state.selected = result.task; await refresh(); await openTask(current.id); notice($('global-notice'),op === 'transition' ? t('task.done.transition',{status:labels[extra.status]}) : t(doneKeys[op]),'success'); });
  };
  const actionButtons = el('div','actions');
  if (task.status === 'review' && task.corrections?.some(c=>!c.checked)) actionButtons.append(mutationButton(t('action.claimCorrections'),()=>currentAction('claim'),'primary'));
  if (task.principles_changed && (task.status!=='doing'||leaseFor(task))) actionButtons.append(mutationButton(t('principles.acknowledge'),()=>currentAction('acknowledge_principles')));
  if (task.status === 'ready') actionButtons.append(mutationButton(t('action.claimStart'),() => currentAction('claim'),'primary'));
  if (task.status === 'doing') {
    if (expired(task)) { actionSection.append(el('p','help',t('task.claimExpiredHelp'))); actionButtons.append(mutationButton(t('action.recover'),() => currentAction('recover'))); }
    else if (leaseFor(task)) actionButtons.append(mutationButton(t('action.renew'),() => currentAction('renew')),mutationButton(t('action.release'),() => currentAction('release')));
    else actionSection.append(el('p','help',t('task.claimHeldBy',{owner:task.owner || t('task.anotherWorker')})));
  }
  if (task.source_changed) actionButtons.append(mutationButton(t('action.reconcile'),() => currentAction('reconcile')));
  for (const target of transitionTargets[task.status] || []) {
    if (target === 'done') { const done = mutationButton(t('action.markDone'),() => currentAction('transition',{status:'done'}),'primary'); done.title = t('action.markDoneTitle'); actionButtons.append(done); }
    else actionButtons.append(mutationButton(target === 'post_validation' ? t('action.finishToValidation') : target === 'review' ? t('action.sendToReview') : t('action.moveTo',{status:labels[target]}),() => currentAction('transition',{status:target}),target === 'cancelled' ? 'danger' : ''));
  }
  append(actionSection,actionButtons,el('p','help',t('task.saveBeforeStatus'))); body.append(actionSection);
  const form = el('form'); form.id = 'task-edit-form';
  const detailFields = {};
  for (const [key,label,value,options] of [
    ['title',t('common.title'),task.title,{required:true}],['outcome',t('common.outcome'),task.outcome,{type:'textarea',required:true}],['next_step',t('common.nextStep'),task.next_step,{}],
    ['priority',t('common.priority'),task.priority,{type:'select',options:priorityOptions()}],
    ['acceptance',t('common.acceptance'),(task.acceptance || []).join('\n'),{type:'textarea',required:true,help:t('task.acceptanceHelp')}],
    ['tags',t('task.tags'),(task.tags || []).join(', '),{placeholder:t('task.tagsPlaceholder')}]
  ]) { const f = field(key,label,value,options); detailFields[key] = f.input; form.append(f.wrap); }
  if (task.blocked_reason) form.append(append(el('div','subtle-panel'),el('h3','',t('task.blockedReason')),el('p','help',task.blocked_reason)));
  const dependencies = field('depends_on',t('common.dependencies'),'',{type:'select',options:state.tasks.filter(tk => tk.id !== task.id).map(tk => [tk.id,`${tk.key} · ${tk.title} (${labels[tk.status]})`]),help:t('task.dependenciesHelp')});
  dependencies.input.multiple = true; for (const option of dependencies.input.options) option.selected = (task.depends_on || []).includes(option.value);
  const clearDependencies = button(t('task.clearDependencies'),() => { for (const option of dependencies.input.options) option.selected = false; form.dataset.dirty = 'true'; },'small quiet');
  append(form,dependencies.wrap,clearDependencies);
  if (task.depends_on?.length) { const dependencyList = el('ul','list'); for (const id of task.depends_on) { const dep = state.tasks.find(tk => tk.id === id); dependencyList.append(el('li','muted',`${dep?.status === 'done' ? '✓' : '○'} ${taskName(id)}`)); } form.append(dependencyList); }
  const evidenceSection = append(el('section','section stack'),el('h3','',t('evidence.title',{done:(task.checklist || []).filter(c=>c.checked).length,total:task.acceptance?.length || 0})),el('p','help',t('evidence.help')));
  const evidenceInputs = [];
  for (const [i,criterion] of (task.acceptance || []).entries()) {
    const saved = (task.evidence || []).filter(e => e.criterion === i); const row = append(el('div','criterion'),append(el('div','criterion-title'),el('span','criterion-number',i+1),el('span','',criterion)));
    const check=field(`check_${i}`,t('evidence.checked'),'',{type:'checkbox'});check.input.checked=!!task.checklist?.find(c=>c.criterion===i)?.checked;row.append(check.wrap);
    const group = el('div','evidence-fields'); const ref = field(`evidence_ref_${i}`,t('evidence.ref'),saved[0]?.ref,{placeholder:t('evidence.refPlaceholder')}); const note = field(`evidence_note_${i}`,t('evidence.note'),saved[0]?.note,{type:'textarea',placeholder:t('evidence.notePlaceholder')});
    append(group,ref.wrap,note.wrap); row.append(group);
    if (saved.length) { const existing = el('ul','list'); for (const evidence of saved) existing.append(append(el('li'),reference(evidence.ref,evidence.ref,evidence.href),el('p','help',evidence.note))); row.append(existing); }
    evidenceInputs.push({criterion:i,ref:ref.input,note:note.input,check:check.input,extra:saved.slice(1)}); evidenceSection.append(row);
  }
  form.append(evidenceSection);
  const correctionInputs=[];
  if(task.corrections?.length){
    const corrections=append(el('section','section stack'),el('h3','',t('corrections.title')));
    for(const c of task.corrections){
      const row=append(el('div','criterion'),el('strong','',c.fix));
      for(const [label,text]of [[t('corrections.observed'),c.observed],[t('corrections.expected'),c.expected],[t('corrections.avoid'),c.avoid]])row.append(el('p','',label+': '+text));
      const check=field('correction_'+c.id,t('corrections.fixed'),'',{type:'checkbox'});check.input.checked=!!c.checked;
      const note=field('correction_note_'+c.id,t('corrections.note'),c.verification,{type:'textarea'});
      const ref=field('correction_ref_'+c.id,t('corrections.ref'),c.verification_ref);
      append(row,check.wrap,note.wrap,ref.wrap);corrections.append(row);correctionInputs.push({id:c.id,check:check.input,note:note.input,ref:ref.input});
    }form.append(corrections);
  }
  const linksSection = append(el('section','section stack'),el('h3','',t('links.title'))); const linkRows = el('div','stack'); const linkInputs = [];
  function addLink(link = {}) {
    const row = el('div','link-row'), kind = el('select'), ref = el('input'), label = el('input'); kind.setAttribute('aria-label',t('links.kindAria'));
    const kinds = state.link_kinds || ['file','url','task'];
    for (const k of link.kind && !kinds.includes(link.kind) ? [...kinds,link.kind] : kinds) kind.append(new Option(kindLabel(k),k));
    kind.value = link.kind || (kinds.includes('file') ? 'file' : kinds[0]); ref.value = link.ref || ''; ref.placeholder = t('links.refPlaceholder'); ref.setAttribute('aria-label',t('links.refAria')); label.value = link.label || ''; label.placeholder = t('links.labelPlaceholder'); label.setAttribute('aria-label',t('links.labelAria')); label.className = 'label-input';
    const record = {kind,ref,label,row}; linkInputs.push(record); const remove = button('×',() => { linkInputs.splice(linkInputs.indexOf(record),1); row.remove(); form.dataset.dirty = 'true'; },'quiet'); remove.setAttribute('aria-label',t('links.removeAria'));
    append(row,kind,ref,remove,label); if (link.ref) { const existing = append(el('div','help link-existing'),reference(link.ref,link.label || link.ref,link.href)); if (link.record_status) existing.append(el('span','badge',link.record_status)); row.append(existing); } linkRows.append(row);
  }
  for (const link of task.links || []) addLink(link);
  append(linksSection,el('p','help',t('links.help')),linkRows,button(t('links.add'),() => { addLink(); form.dataset.dirty = 'true'; },'small quiet')); form.append(linksSection);
  const sourcesSection = append(el('section','section stack'),el('h3','',t('common.watchedDocuments')));
  const planSources = planFor(task)?.sources || [], planPaths = new Set(planSources.map(s => s.path));
  const ownSources = (task.sources || []).filter(s => !planPaths.has(s.path));
  const sources = field('sources',t('source.taskPaths'),ownSources.map(s => s.path).join('\n'),{type:'textarea',help:t('source.taskPathsHelp')}); append(sourcesSection,sources.wrap,renderSources(ownSources));
  if (planSources.length) append(sourcesSection,el('p','help',t('source.inherited')),renderSources((task.sources || []).filter(s => planPaths.has(s.path))));
  if (task.changed_sources?.length) { const drift = el('ul','list notice warning'); for (const source of task.changed_sources) drift.append(append(el('li'),el('strong','',source.path),el('span','source-hash',t('source.recorded',{hash:source.recorded_sha256 || '—'})),el('span','source-hash',source.source_missing ? t('source.currentMissing') : t('source.current',{hash:source.current_sha256 || '—'})))); sourcesSection.append(drift); }
  form.append(sourcesSection);
  const editNote = field('note',t('common.changeNote'),'',{placeholder:t('task.changeNotePlaceholder')}); form.append(editNote.wrap);
  const saveRow = append(el('div','flex spread wrap'),button(t('task.discard'),() => openTask(task.id),'quiet'),mutationButton(t('task.save'),() => form.requestSubmit(),'primary')); form.append(saveRow);
  form.addEventListener('submit',event => {
    event.preventDefault(); const current = state.selected;
    if ($('comment-body')?.value.trim() || $('comment-source')?.value.trim() || $('attachment-files')?.files.length) { notice(error,t('task.draftsBeforeSave')); return; }
    const values = {...Object.fromEntries(Object.entries(detailFields).map(([key,input]) => [key,input.value.trim()])),depends_on:[...dependencies.input.selectedOptions].map(o => o.value),sources:lines(sources.input.value)};
    values.acceptance = lines(values.acceptance); values.priority = Number(values.priority); values.tags = [...new Set(values.tags.split(',').map(tag => tag.trim()).filter(Boolean))];
    values.evidence = evidenceInputs.flatMap(e => e.ref.value.trim() || e.note.value.trim() ? [{criterion:e.criterion,ref:e.ref.value.trim(),note:e.note.value.trim()},...e.extra.map(({criterion,ref,note}) => ({criterion,ref,note}))] : e.extra.map(({criterion,ref,note}) => ({criterion,ref,note})));
    values.links = linkInputs.filter(l => l.ref.value.trim()).map(l => ({kind:l.kind.value,ref:l.ref.value.trim(),label:l.label.value.trim()}));
    const original = {...task,sources:ownSources.map(s => s.path),evidence:(task.evidence || []).map(({criterion,ref,note}) => ({criterion,ref,note})),links:(task.links || []).map(({kind,ref,label}) => ({kind,ref,label}))};
    const payload = {id:current.id,revision:current.revision,note:editNote.input.value.trim()};
    for (const [key,value] of Object.entries(values)) if (JSON.stringify(value) !== JSON.stringify(original[key] ?? (Array.isArray(value) ? [] : ''))) payload[key] = value;
    const executionChecks=evidenceInputs.map(e=>({criterion:e.criterion,checked:e.check.checked}));
    if(JSON.stringify(executionChecks)!==JSON.stringify((task.checklist || []).map(c=>({criterion:c.criterion,checked:c.checked}))))payload.checklist_checks=executionChecks;
    const repairs=correctionInputs.map(c=>({id:c.id,checked:c.check.checked,note:c.note.value.trim(),ref:c.ref.value.trim()}));
    if(JSON.stringify(repairs)!==JSON.stringify((task.corrections || []).map(c=>({id:c.id,checked:!!c.checked,note:c.verification||'',ref:c.verification_ref||''}))))payload.correction_checks=repairs;
    const correction = ['outcome','acceptance','depends_on','sources'].some(key => key in payload);
    if (correction && payload.evidence?.length) { notice(error,t('task.correctionFirst')); return; }
    if (correction) {payload.evidence = [];delete payload.checklist_checks;delete payload.correction_checks;}
    const lease = leaseFor(current); if (lease && !correction) payload.lease = lease;
    mutate('/api/tasks/update',payload,error,async result => { if (result.task.status !== 'doing') rememberLease(current.id,null); await refresh(); await openTask(current.id); notice($('global-notice'),correction ? t('task.correctionSaved') : t('task.saved'),'success'); });
  });
  form.addEventListener('input',() => { form.dataset.dirty = 'true'; }); form.addEventListener('change',() => { form.dataset.dirty = 'true'; });
  body.append(form,attachmentsSection(task),discussionSection(task));
  const history = append(el('section','section'),el('h3','',t('activity.title'))); const eventList = el('ol','list events');
  for (const event of [...state.events].reverse()) { const item = el('li'); const type = event.op || event.type || event.action || t('activity.updated'); const target = event.to_status || event.status; append(item,el('p','',`${event.actor || t('activity.system')} · ${type}${target ? ` ${t('activity.arrow')} ${labels[target] || target}` : ''}${event.revision ? ` · r${event.revision}` : ''}`),el('time','',date(event.at || event.created_at || event.timestamp)),event.note ? el('p','event-note',event.note) : null,event.changes?.length ? el('p','help',t('activity.changed',{changes:event.changes.join(', ')})) : null); eventList.append(item); }
  if (!state.events.length) eventList.append(el('li','muted',t('activity.none'))); history.append(eventList); body.append(history);
}
for (const id of ['plan-filter','scope-filter','status-filter','show-inactive']) $(id).addEventListener('change',renderBoard);
$('board-filter').addEventListener('change',() => { $('plan-filter').value = ''; populateProjects(); renderBoard(); });
$('search').addEventListener('input',renderBoard);
$('actor').addEventListener('change',() => { try { localStorage.setItem(actorKey,actor()); } catch { /* Optional preference. */ } if ($('task-dialog').open) { notice($('task-error'),t('task.actorChanged'),'warning'); } });
try { $('actor').value = localStorage.getItem(actorKey) || 'local-user'; } catch { /* Default actor works without storage. */ }
syncActor(); $('actor').addEventListener('input', syncActor);
// Sidebar. Home clears every filter; Tasks clears the board, project and status filters and brings the columns into view.
$('nav-home').addEventListener('click', () => { for (const id of ['board-filter','plan-filter','scope-filter','status-filter']) $(id).value = ''; $('search').value = ''; $('show-inactive').checked = false; populateProjects(); renderBoard(); scrollTo({top:0}); });
$('nav-tasks').addEventListener('click', () => { for (const id of ['board-filter','plan-filter','status-filter']) $(id).value = ''; populateProjects(); renderBoard(); $('board').scrollIntoView({block:'start'}); });
$('nav-boards').addEventListener('click', () => { if (popover?.anchor === $('nav-boards')) closePopover(); else openNavMenu($('nav-boards'), 'boards'); });
$('nav-plans').addEventListener('click', () => { if (popover?.anchor === $('nav-plans')) closePopover(); else openNavMenu($('nav-plans'), 'plans'); });
// Roadmap, knowledge, reports and settings are not features of this tool. They say so instead of doing nothing.
document.querySelectorAll('[data-soon]').forEach(item => item.addEventListener('click', () => { notice($('global-notice'), t('nav.soon', {name:t(item.dataset.soon)}), 'warning'); $('global-notice').scrollIntoView({block:'nearest'}); }));
$('more-button').addEventListener('click', () => togglePopover($('more-panel'), $('more-button')));
$('whoami').addEventListener('click', () => { $('actor').scrollIntoView({block:'center'}); $('actor').focus(); $('actor').select(); });
$('actor-avatar').replaceChildren(icon('user', 14));
// Light or dark. With nothing chosen the style sheet follows the system; the toggle makes the choice explicit and remembers it.
const darkQuery = matchMedia('(prefers-color-scheme: dark)');
const isDark = () => (document.documentElement.dataset.theme || (darkQuery.matches ? 'dark' : 'light')) === 'dark';
function syncTheme() { const dark = isDark(), label = t(dark ? 'theme.toLight' : 'theme.toDark'); $('theme-toggle').replaceChildren(icon(dark ? 'moon' : 'sun', 18)); $('theme-toggle').setAttribute('aria-label', label); $('theme-toggle').title = label; }
try { const saved = localStorage.getItem(themeKey); if (saved === 'dark' || saved === 'light') document.documentElement.dataset.theme = saved; } catch { /* Storage can be disabled; the system preference applies. */ }
$('theme-toggle').addEventListener('click', () => { const next = isDark() ? 'light' : 'dark'; document.documentElement.dataset.theme = next; try { localStorage.setItem(themeKey, next); } catch { /* The choice lasts until the page closes. */ } syncTheme(); });
darkQuery.addEventListener('change', syncTheme); syncTheme();
document.addEventListener('pointerdown', event => { if (popover && !popover.panel.contains(event.target) && !popover.anchor.contains(event.target)) closePopover(); });
document.addEventListener('keydown', event => {
  if (!popover) return;
  if (event.key === 'Escape') { const {anchor} = popover; closePopover(); anchor.focus(); return; }
  const items = [...popover.panel.querySelectorAll('.menu-item')], at = items.indexOf(document.activeElement);
  if (items.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { event.preventDefault(); items[(at + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length].focus(); }
});
addEventListener('resize', closePopover); addEventListener('scroll', closePopover, {passive:true});

// The search box in the top bar finds a task, board, mini project or person and goes there. (The field above the
// columns filters what the columns show; this one navigates.)
const searchBox = $('global-search'), searchResults = $('search-results');
let searchHits = [], searchAt = -1;
function findHits(query) {
  const needle = query.trim().toLocaleLowerCase(); if (!needle) return [];
  const has = (...parts) => parts.filter(Boolean).join(' ').toLocaleLowerCase().includes(needle);
  const owners = [...new Set(state.tasks.map(tk => tk.owner).filter(Boolean))].filter(name => has(name)).slice(0, 4);
  return [
    ...state.tasks.filter(tk => has(tk.title, tk.key, tk.outcome, tk.next_step)).slice(0, 6).map(tk => ({group:'search.tasks', title:tk.title, sub:`${tk.key || ''} · ${labels[tk.status]}`, go:() => openTask(tk.id)})),
    ...state.boards.filter(b => has(b.title, b.scope)).slice(0, 4).map(b => ({group:'search.boards', title:b.title, sub:b.scope || '', go:() => chooseBoard(b.id)})),
    ...state.plans.filter(pl => has(pl.title, pl.scope)).slice(0, 4).map(pl => ({group:'search.plans', title:pl.title, sub:state.boards.find(b => b.id === pl.board_id)?.title || '', go:() => { chooseBoard(pl.board_id); choosePlan(pl.id); }})),
    ...owners.map(name => ({group:'search.people', title:name, sub:(n => n === 1 ? t('board.taskOne') : t('board.taskMany', {n}))(state.tasks.filter(tk => tk.owner === name).length), go:() => { $('search').value = name; renderBoard(); }})),
  ];
}
function closeSearch() { searchResults.hidden = true; searchBox.setAttribute('aria-expanded', 'false'); searchBox.removeAttribute('aria-activedescendant'); searchAt = -1; }
function markSearch(index) {
  searchAt = index; searchResults.querySelectorAll('[role=option]').forEach((node, i) => node.setAttribute('aria-selected', String(i === index)));
  const node = searchResults.querySelectorAll('[role=option]')[index]; if (node) { searchBox.setAttribute('aria-activedescendant', node.id); node.scrollIntoView({block:'nearest'}); } else searchBox.removeAttribute('aria-activedescendant');
}
function pickSearch(index) { const hit = searchHits[index]; if (!hit) return; closeSearch(); searchBox.value = ''; hit.go(); }
function renderSearch() {
  const query = searchBox.value; searchHits = findHits(query);
  if (!query.trim()) { closeSearch(); return; }
  const nodes = []; let lastGroup = '';
  if (!searchHits.length) nodes.push(el('div', 'menu-empty', t('search.none', {q:query.trim().slice(0, 60)})));
  searchHits.forEach((hit, i) => {
    if (hit.group !== lastGroup) { lastGroup = hit.group; nodes.push(el('div', 'result-group', t(hit.group))); }
    const row = el('button', 'result-item'); row.type = 'button'; row.id = `search-hit-${i}`; row.tabIndex = -1; row.setAttribute('role', 'option'); row.setAttribute('aria-selected', 'false');
    const title = el('span', 'result-title', hit.title), sub = el('span', 'result-sub', hit.sub); title.dir = 'auto'; sub.dir = 'auto';
    append(row, title, hit.sub ? sub : null); row.addEventListener('click', () => pickSearch(i)); nodes.push(row);
  });
  searchResults.replaceChildren(...nodes); searchResults.setAttribute('aria-label', t('search.resultsAria')); searchResults.hidden = false; searchBox.setAttribute('aria-expanded', 'true'); markSearch(searchHits.length ? 0 : -1);
}
searchBox.addEventListener('input', renderSearch);
searchBox.addEventListener('focus', renderSearch);
searchBox.addEventListener('keydown', event => {
  if (event.key === 'Escape') { if (!searchResults.hidden) { closeSearch(); event.stopPropagation(); } return; }
  if (searchResults.hidden || !searchHits.length) return;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); markSearch((searchAt + (event.key === 'ArrowDown' ? 1 : -1) + searchHits.length) % searchHits.length); }
  else if (event.key === 'Enter') { event.preventDefault(); pickSearch(searchAt); }
});
searchResults.addEventListener('pointerdown', event => event.preventDefault());   // keep focus in the box until the click lands
searchBox.addEventListener('blur', closeSearch);
// The language is remembered in storage; if storage is blocked it travels in the URL instead.
$('lang-switch').value = locale;
$('lang-switch').addEventListener('change',() => {
  const next = $('lang-switch').value; const url = new URL(location.href); let remembered = false;
  try { localStorage.setItem(langKey,next); remembered = true; } catch { /* Fall back to the URL below. */ }
  if (remembered) url.searchParams.delete('lang'); else url.searchParams.set('lang',next);
  if (url.href === location.href) location.reload(); else location.assign(url.href);
});
document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click',() => $(b.dataset.close).close()));
$('new-board-button').addEventListener('click',() => openBoardEditor()); $('empty-create').addEventListener('click',() => openBoardEditor());
$('edit-board-button').addEventListener('click',() => openBoardEditor(state.boards.find(b => b.id === $('board-filter').value)));
$('new-plan-button').addEventListener('click',() => openPlanEditor());
$('edit-plan-button').addEventListener('click',() => openPlanEditor(state.plans.find(p => p.id === $('plan-filter').value)));
$('new-task-button').addEventListener('click',() => openTaskEditor()); $('import-button').addEventListener('click',openImport); $('export-button').addEventListener('click',exportBoard);
$('refresh-button').addEventListener('click',async () => { try { await refresh(); notice($('global-notice'),t('board.refreshed'),'success'); } catch (err) { notice($('global-notice'),err.message); } });
// Live updates. Agents change the board from other processes (the CLI, MCP, the API), so every two seconds the page asks
// one small question, whether anything has been written since it last loaded, and reloads the board only when the answer
// changes. It waits while the tab is hidden, while the page's own change is in flight, and while a dropdown has focus.
// An open task keeps its draft; if that task changed elsewhere, it says so instead of replacing what is being typed.
const LIVE_MS = 2000;
let liveBusy = false;
async function liveTick() {
  if (liveBusy || document.hidden || state.busy || document.activeElement?.tagName === 'SELECT') return;
  liveBusy = true;
  try {
    const {version} = await request('/api/tasks/version');
    if (version === seenVersion) return;
    await refresh();
    const open = $('task-dialog').open && state.selected, latest = open && state.tasks.find(tk => tk.id === state.selected.id);
    if (latest && latest.revision !== state.selected.revision && latest.revision !== state.liveNoticed) {
      state.liveNoticed = latest.revision; notice($('task-error'), t('task.changedElsewhere'), 'warning');
    }
  } catch { /* the server is restarting or has stopped; the next tick tries again */ }
  finally { liveBusy = false; }
}
setInterval(liveTick, LIVE_MS);
document.addEventListener('visibilitychange', () => { if (!document.hidden) liveTick(); });
await refresh().catch(err => { $('board-count').textContent = t('board.loadFailed'); $('summary-description').textContent = t('board.loadHint'); notice($('global-notice'),err.message); });
const params = new URLSearchParams(location.search);
if (state.boards.some(b => b.id === params.get('board'))) { $('board-filter').value = params.get('board'); populateProjects(); }
if (state.plans.some(p => p.id === params.get('plan') && (!$('board-filter').value || p.board_id === $('board-filter').value))) $('plan-filter').value = params.get('plan');
renderBoard();
if (params.get('task')) await openTask(params.get('task'));
