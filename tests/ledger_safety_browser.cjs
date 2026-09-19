// Run against ledger_safety_demo.py; argument is its printed disposable DB path.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const database = process.argv[2];
assert.ok(database && database.includes('/lookthrough-ledger-demo-') && database.endsWith('/synthetic.sqlite3'));
const dir = fs.mkdtempSync('/tmp/lt-ledger-chrome-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking',
   '--disable-component-update', '--no-default-browser-check', '--remote-debugging-port=9345',
   `--user-data-dir=${dir}`, 'about:blank'], {stdio: 'ignore'});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let locker, ws;
(async () => {
  let targets;
  for (let n = 0; n < 60; n++) {
    try { targets = await (await fetch('http://127.0.0.1:9345/json/list')).json(); break; }
    catch { await delay(150); }
  }
  assert.ok(targets, 'Chrome started');
  ws = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
  await new Promise(resolve => ws.addEventListener('open', resolve, {once: true}));
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const item = pending.get(message.id); pending.delete(message.id);
      message.error ? item.reject(message.error) : item.resolve(message.result);
    }
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const next = ++id;
    const timeout = setTimeout(() => { pending.delete(next); reject(Error('CDP timeout: ' + method)); }, 10000);
    pending.set(next, {
      resolve: value => { clearTimeout(timeout); resolve(value); },
      reject: error => { clearTimeout(timeout); reject(error); },
    });
    ws.send(JSON.stringify({id: next, method, params}));
  });
  const evaluate = async expression => {
    const result = await call('Runtime.evaluate', {expression, returnByValue: true, timeout: 5000});
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const waitFor = async expression => {
    for (let n = 0; n < 100; n++) {
      if (await evaluate(expression)) return;
      await delay(100);
    }
    throw Error('Timed out: ' + expression);
  };
  const navigate = async () => {
    await call('Page.navigate', {url: 'http://127.0.0.1:5195/activity/new?type=sell'});
    await waitFor("document.readyState === 'complete' && !!document.querySelector('#post_trade') && !document.querySelector('.field-error')");
  };
  const fill = async (mode, day, units) => evaluate(`(() => {
    const values = ${JSON.stringify({effective_date: '2026-02-01', account_id: '1', instrument_id: '1', unit_price: '10', fee_amount: '0'})};
    values.effective_date = ${JSON.stringify(day)}; values.quantity = ${JSON.stringify(units)};
    for (const [name,value] of Object.entries(values)) document.querySelector('[name="'+name+'"]').value = value;
    for (const radio of document.querySelectorAll('[name="quantity_mode"]')) radio.checked = radio.value === ${JSON.stringify(mode)};
    for (const [id,label] of [['account_id','Brokerage'],['instrument_id','Global Fund']]) {
      const visible = document.getElementById(id);
      if (visible.tagName === 'INPUT') visible.value = label;
    }
  })()`);
  const recordWithKeyboard = async () => {
    await evaluate("document.querySelector('#post_trade').focus()");
    await call('Input.dispatchKeyEvent', {type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
    await call('Input.dispatchKeyEvent', {type: 'char', key: 'Enter', text: '\r', windowsVirtualKeyCode: 13});
    await call('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
  };
  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
  for (const scripts of [true, false]) {
    await call('Emulation.setScriptExecutionDisabled', {value: !scripts});
    for (const mode of ['entered', 'entire_holding']) {
      const width = scripts ? 1400 : 390;
      await call('Emulation.setDeviceMetricsOverride', {width, height: 1000, deviceScaleFactor: 1, mobile: false});
      await navigate(); await fill(mode, '2026-02-01', mode === 'entered' ? '50' : '');
      await recordWithKeyboard();
      await waitFor("document.body.innerText.includes('negative holding on 2026-03-01')");
      const field = mode === 'entered' ? 'quantity' : 'quantity_mode';
      await waitFor(`document.activeElement.id === ${JSON.stringify(field)}`);
      assert.equal(await evaluate(`document.activeElement.getAttribute('aria-invalid')`), 'true');
      assert.ok(await evaluate(`!!document.querySelector('a[href="#${field}"]')`));
      assert.equal(await evaluate("document.querySelector('[name=effective_date]').value"), '2026-02-01');
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
      const shot = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
      fs.writeFileSync(`/tmp/lt-ledger-${scripts ? 'js' : 'nojs'}-${mode}.png`, Buffer.from(shot.data, 'base64'));
    }
  }
  // A separate SQLite process holds the write reservation, while the no-JS form
  // must remain usable and preserve the user's input after the failed save.
  await navigate(); await fill('entered', '2026-04-01', '10');
  locker = spawn('.venv/bin/python', ['-u', '-c',
    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); print('locked',flush=True); sys.stdin.readline(); c.rollback(); c.close()", database]);
  await new Promise((resolve, reject) => {
    locker.stdout.once('data', () => resolve());
    locker.once('error', reject);
    locker.once('exit', code => { if (code) reject(Error('Locker failed')); });
  });
  await recordWithKeyboard();
  await waitFor("document.body.innerText.includes('Another save is using this database')");
  await waitFor("document.activeElement.id === 'account_id'");
  assert.equal(await evaluate("document.querySelector('[name=quantity]').value"), '10');
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
  const shot = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
  fs.writeFileSync('/tmp/lt-ledger-busy-nojs.png', Buffer.from(shot.data, 'base64'));
  const released = new Promise(resolve => locker.once('exit', resolve));
  locker.stdin.end('\n'); await released; locker = undefined;
  await recordWithKeyboard();
  await waitFor("document.body.innerText.includes('Sell recorded')");
  assert.ok(await evaluate("document.body.innerText.includes('10')"));
  console.log('Ledger browser checks passed: backdated errors, focus, keyboard submit, JS/no-JS, narrow layout, busy recovery and retry.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  if (locker) locker.kill();
  if (ws) ws.close();
  chrome.kill();
});
