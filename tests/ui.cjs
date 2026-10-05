/* Real browser review. Every board, attachment and lease lives in a temp project; nothing outside it is touched.
 *
 *   npm install && npx playwright install chromium && node tests/ui.cjs
 *
 * AGILEVOW_BROWSER_CHANNEL=chrome uses the system Chrome instead of Playwright's Chromium.
 * TASK_BOARD_REVIEW_DIR=/some/dir keeps the screenshots and report.json there.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const repo = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'agilevow-ui-'));
const output = process.env.TASK_BOARD_REVIEW_DIR || path.join(temp, 'review');
fs.mkdirSync(output, {recursive:true});
fs.mkdirSync(path.join(temp, 'docs'));
fs.writeFileSync(path.join(temp, 'docs/brief.md'), 'A clear task with retained evidence.\n');
fs.writeFileSync(path.join(temp, 'chat.txt'), 'User: retain discussion on each task.\n');
const host = spawn('python3', ['-u','-m','agilevow','serve','--root',temp,'--port','0','--quiet'], {cwd:repo,stdio:['ignore','pipe','pipe']});
let serverErrors = '';
host.stderr.on('data', chunk => {serverErrors += chunk.toString();});
const checks = [];
const passed = name => checks.push(name);
let browser;
const pauseUntil = async fn => { for (let i=0;i<80;i++) { if (await fn()) return; await new Promise(r=>setTimeout(r,50)); } throw Error('Timed out waiting for UI state'); };

/* Finds boxes that leave the box that is supposed to hold them. A long unbreakable string must wrap or be clipped,
 * never widen its container: with an auto grid track it makes every sibling lay out at the string's width while the
 * container stays column-wide, so text spills over the next column and a dialog's close button ends up outside it.
 * Runs in the page. `scope` limits the walk (a selector), `ignore` skips subtrees. */
function findLeaks({scope, ignore}) {
  const describe = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/).join('.') : '');
  const leaks = [];
  for (const el of document.querySelectorAll(scope + ' *')) {
    if (el.tagName === 'OPTION' || el.closest('[hidden]') || (ignore && el.closest(ignore))) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.display === 'contents') continue;
    const box = el.getBoundingClientRect();
    if (!box.width && !box.height) continue;
    const parent = el.parentElement, parentStyle = getComputedStyle(parent), parentBox = parent.getBoundingClientRect();
    const limit = parentBox.right - parseFloat(parentStyle.paddingRight) - parseFloat(parentStyle.borderRightWidth);
    const beyondParent = box.right - limit, textBeyond = el.scrollWidth - el.clientWidth;
    // A parent that scrolls or clips is allowed to hold something wider than itself.
    if (beyondParent > 1 && parentStyle.overflowX === 'visible') leaks.push(`box ${describe(el)} in ${describe(parent)} +${Math.round(beyondParent)}px`);
    else if (textBeyond > 1 && style.overflowX === 'visible' && style.display !== 'inline') leaks.push(`text ${describe(el)} +${Math.round(textBeyond)}px`);
  }
  return leaks;
}

(async () => {
  const base = await new Promise((resolve,reject) => {
    const timer = setTimeout(()=>reject(Error('Server startup timeout: '+serverErrors)),10000);
    let seen = '';
    host.once('error',reject); host.once('exit',code=>reject(Error('Server exited '+code+': '+serverErrors)));
    host.stdout.on('data',chunk=>{seen += chunk.toString(); const m = seen.match(/http:\/\/127\.0\.0\.1:\d+/); if (m) {clearTimeout(timer);resolve(m[0]);}});
  });
  const api = async (route, payload) => {
    const response = await fetch(base+route,{method:payload?'POST':'GET',headers:payload?{'Content-Type':'application/json'}:{},body:payload?JSON.stringify(payload):undefined});
    const data = await response.json(); assert.equal(response.status,200,JSON.stringify(data)); return data;
  };
  browser = await chromium.launch({headless:true,channel:process.env.AGILEVOW_BROWSER_CHANNEL || undefined});
  const context = await browser.newContext({viewport:{width:1440,height:1050}});
  const page = await context.newPage();
  const errors = []; page.on('pageerror',err=>errors.push(err.message));
  // A failed request is a console error too (a 404 for a favicon, a blocked script): none is allowed.
  // The one refusal the conflict step below provokes on purpose is expected, and counted; any other is an error.
  let expectConflict = false; const conflicts = [];
  page.on('console',msg=>{ if (msg.type() !== 'error') return;
    const line = `console: ${msg.text()} (${msg.location().url})`;
    if (expectConflict && /409/.test(msg.text()) && msg.location().url.endsWith('/api/tasks/update')) conflicts.push(line); else errors.push(line); });
  await page.goto(base+'/');
  await pauseUntil(async()=>/tasks/.test(await page.locator('#board-count').textContent()));   // the count lives in the "..." menu, hidden until opened
  await page.locator('#actor').fill('Zoë Ünal');
  await page.locator('#actor').dispatchEvent('change');
  // Who is acting is shown as initials beside the filters and at the top right: there are no accounts, only the text typed here.
  assert.equal(await page.locator('#whoami-name').innerText(),'Zoë Ünal');
  assert.equal(await page.locator('#whoami-avatar').innerText(),'ZÜ');
  const edit = page.locator('#editor-dialog');
  const fill = async (name,value) => edit.locator(`[name="${name}"]`).fill(value);
  const submitEditor = async () => {await page.locator('#editor-save').click(); await edit.waitFor({state:'hidden'});};
  await page.locator('#new-board-button').click();
  await fill('title','Shared tools'); await fill('source_key','tools'); await fill('scope','shared'); await fill('description','Tools for every project'); await submitEditor();
  const boardId = await page.locator('#board-filter').inputValue(); assert.match(boardId,/^board-/);
  await page.getByRole('button',{name:'Edit principles',exact:true}).click();
  await page.getByRole('button',{name:'＋ Add principle',exact:true}).click();
  await fill('principle_title','Quiet by default');
  await fill('principle_content','No flashing at rest. <script>window.injected = true</script>');
  await fill('principles_note','User-defined shared context');await submitEditor();
  assert.equal(await page.locator('#principles-column').getByText('Quiet by default',{exact:true}).count(),1);
  assert.equal(await page.evaluate(()=>window.injected),undefined);
  await page.locator('#new-plan-button').click();
  await fill('title','A complete handoff'); await fill('source_key','handoff'); await fill('kind','tooling');
  await fill('description','Keep the plan, work and evidence together'); await fill('sources','docs/brief.md'); await submitEditor();
  const planId = await page.locator('#plan-filter').inputValue(); assert.match(planId,/^plan-/);
  // One of each must read as one of each, not "1 boards".
  await page.locator('#board-filter').selectOption(''); await page.locator('#plan-filter').selectOption('');
  assert.deepEqual(await page.$$eval('#summary-stats .stat',items => items.map(li => li.innerText.replace(/\s+/g,' ').trim())),['1 board','1 mini project','0 in progress','0 blocked']);
  await page.locator('#board-filter').selectOption(boardId); await page.locator('#plan-filter').selectOption(planId);
  await page.locator('#new-task-button').click();
  await fill('key','one'); await fill('title','Make the next step clear'); await fill('outcome','A later session can resume the task');
  await fill('acceptance','Evidence is retained\nDocuments are linked');
  await edit.locator('[name="status"]').selectOption('ready'); await submitEditor();
  await page.locator('#task-dialog').waitFor();
  let task = (await api('/api/tasks')).tasks[0];
  const taskId = task.id;
  assert.equal(task.understanding_context.principles.length,1);
  assert.equal(await page.locator('#task-body > .understanding-context').count(),1);
  assert.match(await page.locator('#task-body').innerText(),/No flashing at rest/);
  passed('Principles edited in their own column and inherited before task context as safe text');
  passed('Create board, mini project and task through browser forms');
  const act = async name => {
    const old = await page.locator('#task-revision').innerText();
    await page.getByRole('button',{name,exact:true}).click();
    await pauseUntil(async()=>await page.locator('#task-revision').innerText() !== old);
    await pauseUntil(async()=>await page.getByRole('button',{name:'Save task details',exact:true}).isEnabled());
  };
  await act('Claim & start');
  assert.equal((await api('/api/tasks/item?id='+taskId)).task.status,'doing');
  assert.equal(await page.getByRole('button',{name:'Recover expired claim',exact:true}).count(),0);
  assert(!(await page.locator('#task-body').innerText()).includes('1970'));
  for (let i=0;i<2;i++) {
    await page.locator(`[name="evidence_ref_${i}"]`).fill('docs/brief.md');
    await page.locator(`[name="evidence_note_${i}"]`).fill('Verified the linked source and retained result.');
    await page.locator(`[name="check_${i}"]`).check();
  }
  await act('Save task details');
  await page.locator('#attachment-form [name="attachment_kind"]').selectOption('document');
  await page.locator('#attachment-files').setInputFiles(path.join(temp,'chat.txt'));
  await page.getByRole('button',{name:'Upload files',exact:true}).click();
  await page.locator('#attachment-list').getByRole('link',{name:'chat.txt',exact:true}).waitFor();
  await pauseUntil(async()=>await page.getByRole('button',{name:'Upload files',exact:true}).isEnabled());
  task = (await api('/api/tasks/item?id='+taskId)).task;
  assert.equal(task.attachments.length,1);
  const attachment = await fetch(base+task.attachments[0].href); assert.equal(await attachment.text(),fs.readFileSync(path.join(temp,'chat.txt'),'utf8'));
  await page.locator('[name="comment_kind"]').selectOption('conversation');
  await page.locator('#comment-body').fill('User: every task should retain our discussion.');
  await page.locator('#comment-source').fill('Current test conversation excerpt');
  await page.getByRole('button',{name:'Add record',exact:true}).click();
  await page.locator('#comment-list').getByText('User: every task should retain our discussion.',{exact:true}).waitFor();
  await pauseUntil(async()=>await page.getByRole('button',{name:'Add record',exact:true}).isEnabled());
  assert.equal((await api('/api/tasks/item?id='+taskId)).task.comments[0].author,'Zoë Ünal');
  passed('Unicode owner claims, uploads and conversation records retain content');
  assert(await page.locator('a[href*="/api/tasks/artifact?"]').count() >= 1);
  await act('Send to review');await act('Finish work → Post validation');
  const validate = async failPrinciple => {
    const section=page.locator('.validation-section');
    await section.locator('[name="validation_summary"]').fill(failPrinciple?'Idle flashing violates the binding principle':'Replayed output meets all goals and principles');
    for(let i=0;i<3;i++){
      const fail=failPrinciple&&i===2;
      await section.locator(`[name="validation_result_${i}"]`).selectOption(fail?'fail':'pass');
      await section.locator(`[name="validation_observed_${i}"]`).fill(fail?'The idle border flashes':'The tested result meets this requirement');
      await section.locator(`[name="validation_ref_${i}"]`).fill('docs/brief.md');
      if(fail){
        await section.locator(`[name="validation_expected_${i}"]`).fill('A steady border at rest');
        await section.locator(`[name="validation_avoid_${i}"]`).fill('A pulsing border when no action occurred');
        await section.locator(`[name="validation_fix_${i}"]`).fill('Remove the idle pulse and replay the quiet state');
      }
    }
    await section.getByRole('button',{name:'Save the validation and continue according to the result',exact:true}).click();
    await pauseUntil(async()=> (await api('/api/tasks/item?id='+taskId)).task.status===(failPrinciple?'review':'done'));
    await section.waitFor({state:'detached'});
  };
  await page.screenshot({path:path.join(output,'post-validation.png')});
  await validate(true);
  task=(await api('/api/tasks/item?id='+taskId)).task;
  assert.equal(task.corrections.length,1);assert.equal(task.corrections[0].principle_key,task.understanding_context.principles[0].key);
  await page.screenshot({path:path.join(output,'corrections.png')});
  await act('Claim corrections');
  const correction=task.corrections[0];
  await page.locator(`[name="correction_${correction.id}"]`).check();
  await page.locator(`[name="correction_note_${correction.id}"]`).fill('Removed idle pulse; replayed the stable border');
  await page.locator(`[name="correction_ref_${correction.id}"]`).fill('docs/brief.md');
  await act('Save task details');await act('Finish work → Post validation');await validate(false);
  passed('Post validation checks goals AND principles; failure returns to Review with explicit correction checklist; repaired work is retested before Done');
  assert.equal((await api('/api/tasks/item?id='+taskId)).task.status,'done');
  passed('Claim, evidence, review and completion use the same HTTP lifecycle');
  await page.locator('[data-close="task-dialog"]').click();
  // A second board and two miniature projects are independent, including reused keys.
  const second = (await api('/api/boards/create',{actor:'tester',source_key:'docs',title:'Documentation',scope:'documentation'})).board;
  const source = {source_key:'handoff',title:'Readable release notes',scope:'documentation',kind:'writing',sources:[],tasks:[
    {key:'baseline',title:'Capture the starting point',outcome:'A retained comparison',acceptance:['Capture exists'],status:'ready'},
    {key:'timing',title:'Tune one section',outcome:'The change reads clearly',acceptance:['Compare the same section'],depends_on:['baseline'],status:'backlog'}]};
  await page.locator('#refresh-button').click();
  await pauseUntil(async()=>await page.locator('#board-filter option').count()===3);
  await page.locator('#board-filter').selectOption(second.id);
  await page.locator('#import-button').click(); await fill('json',JSON.stringify(source)); await submitEditor();
  const secondPlan = await page.locator('#plan-filter').inputValue();
  assert.equal(await page.locator('.task-card').count(),2);
  await page.locator('#import-button').click(); await fill('json',JSON.stringify(source)); await submitEditor();
  assert.equal(await page.locator('.task-card').count(),2);
  await page.locator('#board-filter').selectOption(boardId); assert.equal(await page.locator('.task-card').count(),1);
  await page.locator('#board-filter').selectOption(''); await page.locator('#plan-filter').selectOption('');
  assert.equal(await page.locator('.task-card').count(),3);
  passed('Cross-board filtering and structured import replay prevent duplicate tasks');
  // The sidebar's Boards and Mini projects open a picker; choosing there is the same as choosing in the toolbar.
  const menuLabels = async () => page.locator('#nav-menu .menu-label').allInnerTexts();
  await page.locator('#nav-boards').click();
  await page.locator('#nav-menu').waitFor();
  assert.equal(await page.locator('#nav-boards').getAttribute('aria-expanded'),'true');
  assert.deepEqual((await menuLabels()).sort(),['All boards','Documentation','Shared tools']);
  await page.locator('#nav-menu .menu-item',{hasText:'Documentation'}).click();
  await page.locator('#nav-menu').waitFor({state:'hidden'});
  assert.equal(await page.locator('#board-filter').inputValue(),second.id);
  assert.equal(await page.locator('.task-card').count(),2);
  assert.equal(await page.locator('#nav-count').innerText(),'2');
  await page.locator('#nav-plans').click();
  assert.deepEqual(await menuLabels(),['All mini projects','Readable release notes']); // only that board's mini projects
  await page.keyboard.press('Escape');
  await page.locator('#nav-menu').waitFor({state:'hidden'});
  assert.equal(await page.locator('#nav-plans').getAttribute('aria-expanded'),'false');
  await page.locator('#nav-boards').click(); await page.locator('#nav-menu .menu-item',{hasText:'All boards'}).click();
  assert.equal(await page.locator('#board-filter').inputValue(),'');
  await page.locator('#nav-plans').click(); await page.locator('#nav-menu .menu-item',{hasText:'A complete handoff'}).click();
  assert.equal(await page.locator('.task-card').count(),1);
  await page.locator('#nav-tasks').click();
  assert.equal(await page.locator('.task-card').count(),3);
  assert.equal(await page.locator('#plan-filter').inputValue(),'');
  // Home clears every filter, including the text one.
  await page.locator('#search').fill('nothing matches this');
  assert.equal(await page.locator('.task-card').count(),0);
  await page.locator('#nav-home').click();
  assert.equal(await page.locator('#search').inputValue(),'');
  assert.equal(await page.locator('.task-card').count(),3);
  // Sections that are not features of this tool say so, instead of doing nothing.
  for (const name of ['Roadmap','Knowledge','Reports','Settings']) {
    await page.locator('.nav-item',{hasText:name}).click();
    assert.match(await page.locator('#global-notice').innerText(),new RegExp(`^${name} is not part of AgileVow yet`));
  }
  // Every status keeps its own tint: the colour is how a column is told from its neighbour at a glance.
  const tints = await page.$$eval('.column[data-status]',columns => columns.map(c => getComputedStyle(c).getPropertyValue('--tint')));
  assert.equal(new Set(tints).size,tints.length,'every status column has its own tint');
  passed('The sidebar navigates boards and mini projects like the filters do, Home clears them, unbuilt sections say so, and each status column has its own tint');

  // The "..." menu holds the rarely used controls and the count; it opens, shows them, and closes on Escape.
  await page.locator('#more-button').click();
  await page.locator('#more-panel').waitFor();
  assert(await page.locator('#show-inactive').isVisible());
  assert.match(await page.locator('#board-count').innerText(),/^\d+ tasks?/);
  assert.equal(await page.locator('#more-button').getAttribute('aria-expanded'),'true');
  await page.keyboard.press('Escape');
  await page.locator('#more-panel').waitFor({state:'hidden'});
  assert.equal(await page.locator('#more-button').getAttribute('aria-expanded'),'false');
  passed('The "..." menu opens with the blocked-column switch and the count, and closes on Escape');

  // The top search finds a mini project, a task, a board or a person and goes there.
  const search = page.locator('#global-search'), options = page.locator('#search-results [role=option]');
  await search.fill('handoff');
  await options.first().waitFor();
  assert.equal(await search.getAttribute('aria-expanded'),'true');
  assert((await page.locator('#search-results .result-title').allInnerTexts()).includes('A complete handoff'));
  await options.filter({hasText:'A complete handoff'}).click();
  assert.equal(await page.locator('#plan-filter').inputValue(),planId);
  assert.equal(await search.inputValue(),'');
  await page.locator('#nav-home').click();
  await search.fill('next step clear');
  await options.first().waitFor();
  assert.equal(await page.locator('#search-results .result-group').first().textContent(),'Tasks');   // grouped, tasks first
  await search.press('ArrowDown'); await search.press('ArrowUp');                // wraps around to the first hit
  await search.press('Enter');
  await page.locator('#task-dialog[open]').waitFor();
  assert.match(await page.locator('#task-title').innerText(),/Make the next step clear/);
  await page.locator('[data-close="task-dialog"]').click();
  await search.fill('zzzz-no-such-thing');
  assert.match(await page.locator('#search-results .menu-empty').innerText(),/Nothing matches/);
  await search.press('Escape');
  await page.locator('#search-results').waitFor({state:'hidden'});
  const owners = (await api('/api/tasks')).tasks.map(tk => tk.owner).filter(Boolean);
  if (owners.length) {                                                          // a person: the text filter above the columns takes their name
    await search.fill(owners[0].slice(0,3));
    await page.locator('#search-results .result-item',{hasText:owners[0]}).first().click();
    assert.equal(await page.locator('#search').inputValue(),owners[0]);
    await page.locator('#nav-home').click();
  }
  passed('The top search finds mini projects, tasks and people, works from the keyboard, and says when nothing matches');

  // Light and dark. The toggle switches, remembers the choice across a reload, and says what it will do next.
  const paper = () => page.evaluate(()=>getComputedStyle(document.body).backgroundColor);
  const lightPaper = await paper();
  assert.equal(await page.locator('#theme-toggle').getAttribute('aria-label'),'Switch to dark theme');
  await page.locator('#theme-toggle').click();
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'dark');
  assert.notEqual(await paper(),lightPaper,'dark must repaint the page');
  assert.equal(await page.locator('#theme-toggle').getAttribute('aria-label'),'Switch to light theme');
  await page.reload(); await page.locator('.task-card').first().waitFor();
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'dark','the choice is remembered');
  await page.screenshot({path:path.join(output,'dark.png'),fullPage:true});
  await page.locator('#theme-toggle').click();
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'light');
  assert.equal(await paper(),lightPaper);
  // Who is acting: the profile chip at the top right takes you to the field that sets it.
  await page.locator('#whoami').click();
  assert.equal(await page.evaluate(()=>document.activeElement.id),'actor');
  passed('The theme toggle switches light and dark and remembers the choice; the profile chip leads to the acting-as field');
  // Retain useful evidence of the board and its detailed card at real viewport sizes.
  await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
  await page.goto(base+'/?task='+taskId);
  await page.locator('#task-dialog').waitFor();
  await page.screenshot({path:path.join(output,'detail.png')});
  // A competing edit must leave the browser draft visible with a conflict response.
  await page.locator('#task-edit-form [name="next_step"]').fill('My unsaved handoff');
  task = (await api('/api/tasks/item?id='+taskId)).task;
  await api('/api/tasks/update',{actor:'another-session',id:taskId,revision:task.revision,next_step:'A competing saved handoff'});
  expectConflict = true;
  await page.getByRole('button',{name:'Save task details',exact:true}).click();
  await page.locator('#task-error').filter({hasText:'stale revision'}).waitFor();
  expectConflict = false; assert.equal(conflicts.length,1,'the competing edit must be refused exactly once');
  assert.equal(await page.locator('#task-edit-form [name="next_step"]').inputValue(),'My unsaved handoff');
  passed('Conflicting edits refresh saved state while preserving the visible draft');
  await page.locator('[data-close="task-dialog"]').click();
  await page.setViewportSize({width:393,height:852});
  await page.locator('#board-filter').selectOption(boardId);
  await page.screenshot({path:path.join(output,'mobile-principles.png'),fullPage:true});
  await page.locator('#board-filter').selectOption(second.id);
  await page.locator('#plan-filter').selectOption(secondPlan);
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1),'Page must not overflow horizontally');
  await page.locator('.task-card').first().click();
  await page.locator('#task-dialog').waitFor();
  assert.equal(await page.locator('#task-dialog').evaluate(el=>el.scrollTop),0,'A newly opened task starts at its heading and status');
  assert(await page.locator('#task-dialog').evaluate(el=>el.getBoundingClientRect().width <= innerWidth-12));
  await page.screenshot({path:path.join(output,'mobile-detail.png')});
  await page.locator('[data-close="task-dialog"]').click();
  const downloadPromise = page.waitForEvent('download'); await page.locator('#export-button').click();
  const downloaded = await downloadPromise; const dest = path.join(output,'archive.json'); await downloaded.saveAs(dest);
  const archive = JSON.parse(fs.readFileSync(dest,'utf8'));
  assert.equal(archive.tasks.length,3); assert(!JSON.stringify(archive).includes('lease_hash'));
  passed('Desktop and 393px mobile render, usable detail dialog, export');

  // ---- Hostile text. Real boards carry keys like `collection-standalone-lightweight-2026-09-18`, pasted URLs and
  // titles with no spaces. None of it may leave its card, push the close button out of its dialog, or widen the page.
  const token = n => 'W'.repeat(n);
  const stress = (await api('/api/boards/create',{actor:'tester',source_key:'stress-'+'k'.repeat(60),title:'A board title that is long enough to need several lines '.repeat(4),
    description:'D'.repeat(150)+' '+'and then ordinary words. '.repeat(6),scope:'scope-'+'s'.repeat(80),
    principles:[{key:'p-token',title:'U'.repeat(100),content:'V'.repeat(180)+' then ordinary words after an unbroken run.'},
                {key:'p-long',title:'A very long principle title that goes on and on and on and on and on and on and on and on',content:'Ordinary prose. '.repeat(40)}]})).board;
  const imported = await api('/api/plans/import',{actor:'tester',board_id:stress.id,source_key:'p-'+'q'.repeat(80),title:'Plan '+'P'.repeat(90),
    description:'desc '+'d'.repeat(120),scope:'x'.repeat(60),kind:'kind-'+'k'.repeat(30),sources:[],tasks:[
      {key:'collection-standalone-lightweight-2026-09-18',title:'Package a lightweight standalone HTML game for sharing',
       outcome:'User refreshes the updated standalone file to confirm the reported small-screen clipping is resolved. Automated local-file browser access remains unavailable.',
       acceptance:['Clipping is gone on a phone-width window.'],priority:1,status:'ready',tags:['html','standalone-share']},
      {key:'t'+'-segment'.repeat(18),title:'Title '+token(110),outcome:'O'.repeat(300)+' tail',acceptance:['A'.repeat(200),'Short one'],priority:0,status:'ready',
       tags:['tag-'+'g'.repeat(30)],next_step:'N'.repeat(150)}]});
  const longKeyTask = imported.tasks[0], hostileTask = imported.tasks[1];
  const digest = hostileTask.understanding_context.principles_sha256;
  const claimed = await api('/api/tasks/action',{op:'claim',id:hostileTask.id,revision:hostileTask.revision,actor:'a'.repeat(100),principles_sha256:digest});
  await api('/api/tasks/update',{actor:'a'.repeat(100),id:hostileTask.id,revision:claimed.task.revision,lease:claimed.lease,
    evidence:[{criterion:0,ref:'docs/brief.md',note:'E'.repeat(200)}]});
  for (const [width,height] of [[1600,900],[1024,800],[768,1024],[393,852]]) {
    await page.setViewportSize({width,height});
    await page.goto(base+'/');
    await page.locator('.task-card').first().waitFor();
    const board = await page.evaluate(findLeaks,{scope:'body',ignore:'dialog'});
    assert.deepEqual(board,[],`board leaks at ${width}px:\n${board.join('\n')}`);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1),`the page itself scrolls sideways at ${width}px`);
    if (width > 600) {
      const widest = await page.$$eval('.toolbar .filter select',selects => Math.max(...selects.map(s => Math.round(s.getBoundingClientRect().width))));
      assert(widest <= 200,`a toolbar select grew to ${widest}px at ${width}px: long board names must not stretch the toolbar`);
    }
    // The exact case from the old board: a long key must not carry its card's text outside the card.
    const spill = await page.$$eval('.task-card',cards=>cards.flatMap(card=>{
      const edge = card.getBoundingClientRect().right;
      return [...card.querySelectorAll('*')].filter(el=>el.getBoundingClientRect().right > edge+1).map(el=>`${card.querySelector('.task-key')?.textContent.slice(0,40)}: ${el.className||el.tagName}`);}));
    assert.deepEqual(spill,[],`card contents leave their card at ${width}px`);
    for (const id of [longKeyTask.id, hostileTask.id]) {
      await page.goto(base+'/?task='+id);
      await page.locator('#task-dialog[open]').waitFor();
      await page.locator('#task-body > *').first().waitFor();
      const dialog = await page.evaluate(findLeaks,{scope:'#task-dialog'});
      assert.deepEqual(dialog,[],`task dialog leaks at ${width}px:\n${dialog.join('\n')}`);
      const reach = await page.evaluate(()=>{ const d = document.querySelector('#task-dialog').getBoundingClientRect(), c = document.querySelector('#task-dialog .close').getBoundingClientRect();
        return {inside: c.left >= d.left-1 && c.right <= d.right+1 && c.top >= d.top-1 && c.bottom <= d.bottom+1, dialogFits: d.left >= -1 && d.right <= innerWidth+1}; });
      assert.deepEqual(reach,{inside:true,dialogFits:true},`the close button must stay inside the dialog at ${width}px`);
    }
    if (width === 393 || width === 1600) await page.screenshot({path:path.join(output,`hostile-${width}.png`)});
  }
  passed('Hostile text (100-character tokens, 160-character keys, long titles) stays inside cards, columns, dialogs and the page at 1600, 1024, 768 and 393px');

  // ---- Text nobody planned for. The wrapping rules above cover the strings we know about; the pinned grid tracks and
  // the shrinkable flex items cover the rest. Prove that: drop a 400-character string that CANNOT wrap into a card, a
  // dialog body, a dialog head and the principles column, and require that nothing around it is pushed out of place.
  const probe = (selector) => page.evaluate(sel => {
    const el = document.createElement('div'); el.id = 'injected-probe'; el.textContent = 'X'.repeat(400);
    el.style.whiteSpace = 'nowrap'; document.querySelector(sel).appendChild(el); }, selector);
  for (const [width,height] of [[1024,800],[393,852]]) {
    await page.setViewportSize({width,height});
    await page.goto(base+'/'); await page.locator('.task-card').first().waitFor();
    const widths = () => page.$$eval('.column',cols=>cols.map(c=>Math.round(c.getBoundingClientRect().width)));
    const before = await widths();
    await probe('.task-card');
    assert.deepEqual(await page.evaluate(()=>{ const card = document.querySelector('.task-card'), edge = card.getBoundingClientRect().right;
      return [...card.children].filter(el=>el.id !== 'injected-probe' && el.getBoundingClientRect().right > edge+1).map(el=>el.className); }),[],
      `an unbreakable string inside a card pushed the card's other content out of it at ${width}px`);
    assert.deepEqual(await widths(),before,`an unbreakable string inside a card resized the columns at ${width}px`);
    await page.goto(base+'/'); await page.locator('#principles-column .principle-group').first().waitFor();
    const principlesWidth = () => page.$eval('#principles-column',el=>Math.round(el.getBoundingClientRect().width));
    const principlesBefore = await principlesWidth();
    await probe('#principles-column .principle-group');
    assert.equal(await principlesWidth(),principlesBefore,`an unbreakable string widened the principles column at ${width}px`);
    for (const target of ['#task-body','#task-dialog .dialog-head > div']) {
      await page.goto(base+'/?task='+longKeyTask.id); await page.locator('#task-dialog[open]').waitFor(); await page.locator('#task-body > *').first().waitFor();
      await probe(target);
      const held = await page.evaluate(()=>{ const dialog = document.querySelector('#task-dialog'), box = dialog.getBoundingClientRect(), close = dialog.querySelector('.close').getBoundingClientRect();
        return {closeInside: close.left >= box.left-1 && close.right <= box.right+1,
                pushed: [...document.querySelectorAll('#task-body > *')].filter(el=>el.id !== 'injected-probe' && el.getBoundingClientRect().right > box.right+1).map(el=>el.tagName.toLowerCase()+'.'+el.className)}; });
      assert.deepEqual(held,{closeInside:true,pushed:[]},`an unbreakable string in ${target} pushed content out of the dialog at ${width}px`);
    }
  }
  passed('An unbreakable string nobody planned for cannot widen a card, the principles column or a dialog, or push the close button out');

  // ---- The Hebrew locale: the strings come from i18n.mjs, which is the only file allowed to hold them.
  const {LOCALES} = await import(pathToFileURL(path.join(repo,'agilevow/web/i18n.mjs')).href);
  await page.setViewportSize({width:1440,height:1050});
  await page.goto(base+'/?lang=he');
  await page.locator('.task-card').first().waitFor();
  assert.deepEqual(await page.evaluate(()=>[document.documentElement.lang,document.documentElement.dir]),['he','rtl']);
  // All boards are shown, so the principles column holds one group (and one edit button) per board.
  const groups = await page.locator('.principle-group').count();
  assert(groups >= 1);
  assert.equal(await page.getByRole('button',{name:LOCALES.he['principles.edit'],exact:true}).count(),groups);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth+1),'Hebrew layout must not scroll sideways');
  const titleStart = await page.evaluate(()=>{ const card = document.querySelector('.task-card'), title = card.querySelector('.task-title'), range = document.createRange();
    range.selectNodeContents(title); return Math.round(range.getClientRects()[0].left - card.getBoundingClientRect().left); });
  assert(titleStart < 30,`English card text must start at the card's left edge inside the Hebrew layout (it started ${titleStart}px in)`);
  await page.screenshot({path:path.join(output,'hebrew.png')});
  await page.goto(base+'/?lang=en');
  assert.deepEqual(await page.evaluate(()=>[document.documentElement.lang,document.documentElement.dir]),['en','ltr']);
  passed('Hebrew locale mirrors the layout (rtl) and English restores it (ltr)');

  assert.equal(errors.length,0,errors.join('\n'));
  passed('No page errors');
  const report = {checks,viewports:[[1440,1050],[393,852],[1600,900],[1024,800],[768,1024]],errors,isolated_data:true,at:new Date().toISOString()};
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({...report,output}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{
  if (browser) await browser.close();
  host.kill('SIGTERM');
  // Leave retained screenshots when no explicit output path was provided; never touch user data.
  if (process.env.TASK_BOARD_REVIEW_DIR) fs.rmSync(temp,{recursive:true,force:true});
});
