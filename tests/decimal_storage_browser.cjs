// Run against decimal_storage_demo.py; disposable databases only.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const dir = fs.mkdtempSync('/tmp/lt-decimal-chrome-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking',
   '--disable-component-update', '--no-default-browser-check', '--remote-debugging-port=9347',
   `--user-data-dir=${dir}`, 'about:blank'], {stdio: 'ignore'});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let ws;
(async () => {
  let targets;
  for (let n = 0; n < 60; n++) {
    try { targets = await (await fetch('http://127.0.0.1:9347/json/list')).json(); break; }
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
  const base = 'http://127.0.0.1:5197';
  const keyboard = async (key, code, value, text) => {
    await call('Input.dispatchKeyEvent', {type: 'rawKeyDown', key, code, windowsVirtualKeyCode: value});
    if (text) await call('Input.dispatchKeyEvent', {type: 'char', key, text, windowsVirtualKeyCode: value});
    await call('Input.dispatchKeyEvent', {type: 'keyUp', key, code, windowsVirtualKeyCode: value});
  };
  const submit = async selector => {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    await keyboard('Enter', 'Enter', 13, '\r');
  };
  const navigate = async path => {
    await call('Page.navigate', {url: base + path});
    await waitFor("document.readyState === 'complete'");
  };
  const shot = async name => {
    const result = await call('Page.captureScreenshot', {format:'png',captureBeyondViewport:true});
    fs.writeFileSync('/tmp/lt-decimal-' + name + '.png',Buffer.from(result.data,'base64'));
  };
  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  for (const scripts of [true,false]) {
    const name = scripts ? 'Legacy.sqlite3' : 'LegacyNoJS.sqlite3';
    await call('Emulation.setScriptExecutionDisabled',{value:!scripts});
    await call('Emulation.setDeviceMetricsOverride',{width:scripts?1400:390,height:1000,deviceScaleFactor:1,mobile:false});
    await navigate('/');
    await waitFor("document.querySelectorAll('.database-card').length === 2");
    await evaluate(`[...document.querySelectorAll('.database-card')].find(c=>c.innerText.includes(${JSON.stringify(name)})).querySelector('button').focus()`);
    await keyboard('Enter','Enter',13,'\r');
    await waitFor("document.body.innerText.includes('before/after record')");
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
    await shot(scripts?'upgrade-desktop':'upgrade-nojs');
    await submit('#submit');
    await waitFor("document.body.innerText.includes('Your recovery copy is saved')");
    await evaluate(`[...document.querySelectorAll('.database-card')].find(c=>c.innerText.includes(${JSON.stringify(name)})).querySelector('button').focus()`);
    await keyboard('Enter','Enter',13,'\r');
    await waitFor("location.pathname.includes('/d/') && !!document.querySelector('.dataset-menu')");
    const prefix = await evaluate("location.pathname.split('/').slice(0,3).join('/')");
    const tables = async () => (await (await fetch(base+prefix+'/backup/export')).json()).tables;
    const before = await tables();
    assert.equal(before.postings.find(r=>r.posting_kind==='instrument').quantity_delta,'100000.010000');
    assert.ok(before.decimal_conversions.length);
    await navigate(prefix+'/activity/new?type=buy');
    await waitFor("!!document.querySelector('#quantity')");
    await evaluate(`document.querySelector('#effective_date').value='2026-09-19';document.querySelector('#account_id').value='1';document.querySelector('#instrument_id').value='1';document.querySelector('#quantity').value='1.0000001';document.querySelector('#unit_price').value='1.234567';document.querySelector('#fee_amount').value='0';`);
    await submit('#post_trade');
    await waitFor("document.body.innerText.includes('Use at most 6 decimal places')");
    await waitFor("document.activeElement.id === 'quantity'");
    assert.equal(await evaluate("document.querySelector('#quantity').value"),'1.0000001');
    assert.ok(await evaluate(`!!document.querySelector('a[href="#quantity"]')`));
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
    assert.deepEqual(await tables(),before);
    await shot(scripts?'error-desktop':'error-nojs');
    await evaluate("document.querySelector('#quantity').value='1.123456'");
    await submit('#post_trade');
    await waitFor("document.body.innerText.includes('Buy recorded') || document.body.innerText.includes('buy recorded')");
    assert.ok(await evaluate("document.body.innerText.includes('1.387')"));
    const after = await tables();
    assert.ok(after.postings.some(r=>r.quantity_delta==='1.123456' && r.unit_price==='1.234567'));
    assert.ok(after.postings.some(r=>r.posting_kind==='cash' && r.cash_amount_delta==='-1.387'));
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
    await shot(scripts?'success-desktop':'success-nojs');
  }
  console.log('Decimal browser checks passed: staged upgrade, rounding disclosure, conversion audit in export, six-decimal units/price, three-decimal OMR settlement, linked errors and focus, retained input, keyboard retry, JS/no-JS, desktop/390px/reduced motion.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  if (ws) ws.close();
  chrome.kill();
});
