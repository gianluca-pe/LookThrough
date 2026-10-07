const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const artifacts = fs.mkdtempSync('/tmp/lt-retirement-targets-');
const repository = path.resolve(__dirname, '..');
const fixture = spawn(path.join(repository, '.venv/bin/python'), ['tests/retirement_demo.py', '--state', 'targets', '--port', '5189'], {cwd: repository, stdio: ['ignore', 'pipe', 'pipe']});
const log = fs.createWriteStream(path.join(artifacts, 'fixture.log'));
fixture.stdout.pipe(log);
fixture.stderr.pipe(log);
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
  '--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking',
  '--disable-component-update', '--no-default-browser-check', '--remote-debugging-port=9345',
  `--user-data-dir=${path.join(artifacts, 'chrome')}`, 'about:blank',
], {stdio: 'ignore'});
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const checks = [];
let socket;

(async () => {
  let targets;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      targets = await (await fetch('http://127.0.0.1:9345/json/list')).json();
      if ((await fetch('http://127.0.0.1:5189/retirement')).ok) break;
    } catch {}
    await delay(100);
  }
  assert.ok(targets);
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise(resolve => socket.addEventListener('open', resolve, {once: true}));
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!message.id) return;
    const request = pending.get(message.id);
    pending.delete(message.id);
    message.error ? request.reject(message.error) : request.resolve(message.result);
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const identifier = ++sequence;
    pending.set(identifier, {resolve, reject});
    socket.send(JSON.stringify({id: identifier, method, params}));
  });
  const evaluate = async expression => {
    const result = await call('Runtime.evaluate', {expression, returnByValue: true});
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const waitFor = async expression => {
    for (let attempt = 0; attempt < 600; attempt++) {
      if (await evaluate(expression)) return;
      await delay(100);
    }
    throw Error('Timed out: ' + expression);
  };
  const navigate = async url => {
    await call('Page.navigate', {url});
    await waitFor("document.readyState==='complete' && !!document.querySelector('main')");
  };
  const submit = async identifier => {
    await evaluate(`window.targetPending=true;document.getElementById('${identifier}').focus()`);
    await call('Input.dispatchKeyEvent', {type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
    await call('Input.dispatchKeyEvent', {type: 'char', text: '\r', key: 'Enter', windowsVirtualKeyCode: 13});
    await call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
    await waitFor("!window.targetPending && document.readyState==='complete'");
  };
  const screenshot = async filename => fs.writeFileSync(path.join(artifacts, filename), Buffer.from((await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true})).data, 'base64'));
  await call('Page.enable');
  await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', {width: 1400, height: 1000, deviceScaleFactor: 1, mobile: false});
  await call('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
  const base = 'http://127.0.0.1:5189';
  await navigate(base + '/retirement');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '40');
  assert.match(await evaluate("document.getElementById('capacity-flexible').innerText"), /240\.00/);
  await evaluate("document.getElementById('flexible_amount').value='120.001'");
  await submit('review_lifestyle');
  assert.equal(await evaluate('document.activeElement.id'), 'flexible_amount');
  assert.equal(await evaluate("document.querySelector('#lifestyle-form .error-summary a').getAttribute('href')"), '#flexible_amount');
  await evaluate("document.getElementById('flexible_amount').value='120.01';document.getElementById('flexible_amount').dispatchEvent(new Event('input',{bubbles:true}))");
  assert.equal(await evaluate("document.getElementById('retirement-review-status').hidden"), false);
  await submit('review_lifestyle');
  assert.equal(await evaluate("JSON.parse(document.querySelector('[data-series=spending]').dataset.flexible)[0]"), 120.01);
  assert.equal(await evaluate("JSON.parse(document.querySelector('[data-series=capital]').dataset.capital).at(-1)"), 459.97);
  await waitFor("typeof Chart!=='undefined' && !!Chart.getChart(document.querySelector('[data-series=spending] canvas'))");
  assert.equal(await evaluate("Chart.getChart(document.querySelector('[data-series=spending] canvas')).options.animation"), false);
  assert.equal(await evaluate("document.querySelectorAll('[id]').length===new Set([...document.querySelectorAll('[id]')].map(element=>element.id)).size"), true);
  const draftUrl = await evaluate("document.querySelector('.retirement-details a').href");
  await screenshot('desktop.png');
  await evaluate("document.getElementById('flexible_amount').dispatchEvent(new Event('input',{bubbles:true}))");
  assert.equal(await evaluate("document.getElementById('save_plan').disabled"), true);
  assert.equal(await evaluate("document.querySelector('[data-reviewed-action]').getAttribute('aria-disabled')"), 'true');
  await submit('review_lifestyle');
  await submit('save_plan');
  await navigate(base + '/retirement');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '120.01');
  checks.push('Exact annual target, separate capacity, keyboard validation, review/save/reopen and reduced motion');
  const experimentUrl = await evaluate("document.querySelector('a[href*=monte-carlo]').href");
  await navigate(experimentUrl);
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '120.01');
  assert.match(await evaluate("document.getElementById('mc-allocation-total').innerText"), /Total: 100%.*allocation complete/);
  const setAllocation = async values => {
    await evaluate(`['equity_percent','income_percent','liquidity_percent','alternatives_percent'].forEach((identifier,index)=>{const field=document.getElementById(identifier);field.value=${JSON.stringify(values)}[index];field.dispatchEvent(new Event('input',{bubbles:true}))})`);
  };
  await setAllocation(['50', '30', '10', '8']);
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='under'");
  assert.match(await evaluate("document.getElementById('mc-allocation-total').innerText"), /Total: 98%.*2% remaining/);
  await setAllocation(['50', '30', '10', '13']);
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='over'");
  assert.match(await evaluate("document.getElementById('mc-allocation-total').innerText"), /Total: 103%.*3% over/);
  await setAllocation(['33.333333', '33.333333', '33.333333', '0']);
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='under'");
  assert.match(await evaluate("document.getElementById('mc-allocation-total').innerText"), /99\.999999%.*0\.000001% remaining/);
  await setAllocation(['50.1', '30.2', '10.3', '9.4']);
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='complete'");
  await evaluate("document.getElementById('equity_percent').value='';document.getElementById('equity_percent').dispatchEvent(new Event('input',{bubbles:true}))");
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='invalid'");
  assert.equal(await evaluate("document.getElementById('mc-allocation-total').dataset.total"), undefined);
  await evaluate("window.mcLiveFetch=window.fetch;window.fetch=()=>Promise.reject(new TypeError('Synthetic preview connection loss'))");
  await setAllocation(['0', '0', '100', '0']);
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='unavailable'");
  assert.equal(await evaluate("document.getElementById('run_comparison').disabled"), false);
  await evaluate("window.fetch=window.mcLiveFetch;window.mcPreviewCalls=0;window.fetch=(...argumentsList)=>{window.mcPreviewCalls++;return window.mcLiveFetch(...argumentsList)}");
  await setAllocation(['50', '30', '10', '8']);
  await setAllocation(['0', '0', '100', '0']);
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='complete'");
  assert.equal(await evaluate('window.mcPreviewCalls'), 1);
  await evaluate("window.fetch=window.mcLiveFetch;window.mcPreviewPending=[];window.fetch=()=>new Promise(resolve=>window.mcPreviewPending.push(resolve))");
  await setAllocation(['50', '30', '10', '8']);
  await waitFor('window.mcPreviewPending.length===1');
  await setAllocation(['0', '0', '100', '0']);
  await waitFor('window.mcPreviewPending.length===2');
  await evaluate("window.mcPreviewPending[1](new Response(JSON.stringify({state:'complete',total:'100',difference:'0',message:'Total: 100% — allocation complete.'}),{headers:{'Content-Type':'application/json'}}))");
  await waitFor("document.getElementById('mc-allocation-total').dataset.state==='complete'");
  await evaluate("window.mcPreviewPending[0](new Response(JSON.stringify({state:'under',total:'98',difference:'2',message:'Total: 98% — 2% remaining.'}),{headers:{'Content-Type':'application/json'}}))");
  await delay(400);
  assert.equal(await evaluate("document.getElementById('mc-allocation-total').dataset.state"), 'complete');
  await evaluate("window.fetch=window.mcLiveFetch;delete window.mcLiveFetch");
  assert.equal(await evaluate("document.getElementById('mc-allocation-total').getAttribute('role')"), 'status');
  assert.equal(await evaluate("document.getElementById('mc-allocation-total').getAttribute('aria-live')"), 'polite');
  await call('Emulation.setDeviceMetricsOverride', {width: 390, height: 1000, deviceScaleFactor: 1, mobile: false});
  assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'), true);
  await screenshot('allocation-live-narrow.png');
  await call('Emulation.setDeviceMetricsOverride', {width: 1400, height: 1000, deviceScaleFactor: 1, mobile: false});
  checks.push('Live exact totals, under/over/complete/incomplete states, debouncing, stale-response protection, connection recovery and narrow allocation feedback');
  await call('Emulation.setScriptExecutionDisabled', {value: true});
  await setAllocation(['50', '30', '10', '8']);
  await submit('run_comparison');
  assert.match(await evaluate("document.getElementById('mc-allocation-total').innerText"), /Total: 98%.*2% remaining/);
  assert.equal(await evaluate('document.activeElement.id'), 'equity_percent');
  await setAllocation(['0', '0', '100', '0']);
  await evaluate("document.getElementById('flexible_amount').value='300'");
  await submit('run_comparison');
  assert.match(await evaluate('document.body.innerText'), /Annual Flexible tested:[\s\S]*300\.00/);
  const simulationUrl = await evaluate("document.getElementById('mc-report-link').href");
  await navigate(base + '/retirement');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '120.01');
  await evaluate("document.getElementById('flexible_amount').value='300'");
  await submit('review_lifestyle');
  assert.equal(await evaluate("document.getElementById('planned-lifestyle').dataset.lifestyleStatus"), 'flexible_shortfall');
  await submit('save_plan');
  assert.equal(await evaluate('document.activeElement.id'), 'acknowledge_failure');
  assert.equal(await evaluate("document.querySelector('#adoption-form .error-summary a').getAttribute('href')"), '#acknowledge_failure');
  await evaluate("document.getElementById('acknowledge_failure').checked=true");
  await submit('save_plan');
  await navigate(base + '/retirement');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '300');
  assert.equal(await evaluate("document.getElementById('planned-lifestyle').dataset.lifestyleStatus"), 'flexible_shortfall');
  await submit('use_maximum');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '240');
  await navigate(base + '/retirement');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '300');
  checks.push('No-JavaScript Monte Carlo above capacity, no adoption, acknowledged unsupported target and explicit maximum draft');
  await evaluate("document.getElementById('flexible_amount').value='0'");
  await submit('review_lifestyle');
  assert.equal(await evaluate("JSON.parse(document.querySelector('[data-series=spending]').dataset.flexible)[0]"), 0);
  await call('Emulation.setDeviceMetricsOverride', {width: 390, height: 1000, deviceScaleFactor: 1, mobile: false});
  assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'), true);
  await screenshot('narrow.png');
  await navigate(draftUrl);
  assert.match(await evaluate('document.body.innerText'), /Planned Flexible[\s\S]*120\.01/);
  assert.match(await evaluate('document.body.innerText'), /459\.97/);
  assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'), true);
  await call('Emulation.setDeviceMetricsOverride', {width: 816, height: 1056, deviceScaleFactor: 1, mobile: false});
  fs.writeFileSync(path.join(artifacts, 'lifestyle-report.pdf'), Buffer.from((await call('Page.printToPDF', {printBackground: true})).data, 'base64'));
  await navigate(simulationUrl);
  assert.match(await evaluate('document.body.innerText'), /300\.00/);
  checks.push('Zero target, 390px layout, pinned report/table and Monte Carlo report agreement');
  await navigate(base + '/retirement');
  const olderReview = await evaluate("document.getElementById('save-review').value");
  await evaluate("document.getElementById('flexible_amount').value='120'");
  await submit('review_lifestyle');
  await submit('save_plan');
  await evaluate(`document.getElementById('save-review').value=${JSON.stringify(olderReview)}`);
  await evaluate("const acknowledgement=document.createElement('input');acknowledgement.type='hidden';acknowledgement.name='acknowledge_failure';acknowledgement.value='y';document.getElementById('adoption-form').append(acknowledgement)");
  await submit('save_plan');
  assert.match(await evaluate('document.body.innerText'), /changed since this review/);
  assert.equal(await evaluate("document.getElementById('acknowledge_failure').checked"), false);
  await navigate(base + '/retirement');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '120');
  await evaluate("document.querySelector('#affordability-form details, .retirement-assumptions details').open=true;document.getElementById('core_amount').value='80'");
  await submit('project');
  assert.equal(await evaluate("document.getElementById('flexible_amount').value"), '120');
  assert.match(await evaluate("document.getElementById('capacity-flexible').innerText"), /220\.00/);
  await navigate(base + '/retirement');
  assert.equal(await evaluate("document.getElementById('core_amount').value"), '60');
  checks.push('Dirty inputs disable adoption until review; stale-tab adoption rejected; assumption edits preserve chosen target without saving');
  const record = {passed: true, command: 'node tests/retirement_targets_browser.cjs', fixture: 'tests/retirement_demo.py --state targets --port 5189', checks, artifacts};
  fs.writeFileSync(path.join(artifacts, 'assertions.json'), JSON.stringify(record, null, 2));
  console.log(JSON.stringify(record, null, 2));
})().catch(error => {
  fs.writeFileSync(path.join(artifacts, 'assertions.json'), JSON.stringify({passed: false, checks, error: String(error), artifacts}, null, 2));
  console.error(error, artifacts);
  process.exitCode = 1;
}).finally(() => {
  if (socket) socket.close();
  chrome.kill();
  fixture.kill('SIGINT');
});
