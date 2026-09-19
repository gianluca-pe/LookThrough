// Run against backup_safety_demo.py; argument is its printed disposable directory.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const directory = process.argv[2];
assert.ok(directory && directory.includes('/lookthrough-backup-demo-'));
const dir = fs.mkdtempSync('/tmp/lt-backup-chrome-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking',
   '--disable-component-update', '--no-default-browser-check', '--remote-debugging-port=9346',
   `--user-data-dir=${dir}`, 'about:blank'], {stdio: 'ignore'});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let ws;
(async () => {
  let targets;
  for (let n = 0; n < 60; n++) {
    try { targets = await (await fetch('http://127.0.0.1:9346/json/list')).json(); break; }
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
  const base = 'http://127.0.0.1:5196';
  const keyboard = async (key, code, value, text) => {
    await call('Input.dispatchKeyEvent', {type: 'rawKeyDown', key, code, windowsVirtualKeyCode: value});
    if (text) await call('Input.dispatchKeyEvent', {type: 'char', key, text, windowsVirtualKeyCode: value});
    await call('Input.dispatchKeyEvent', {type: 'keyUp', key, code, windowsVirtualKeyCode: value});
  };
  const submit = async selector => {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    await keyboard('Enter', 'Enter', 13, '\r');
  };
  const upload = async name => {
    const document = await call('DOM.getDocument');
    const input = await call('DOM.querySelector', {nodeId: document.root.nodeId, selector: '#backup_file'});
    await call('DOM.setFileInputFiles', {nodeId: input.nodeId, files: [directory + '/' + name + '.json']});
    await submit('form[action="/backup/preview"] input[type=submit]');
  };
  const tables = async () => (await (await fetch(base + '/backup/export')).json()).tables;
  const original = await tables();
  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
  for (const scripts of [true, false]) {
    await call('Emulation.setScriptExecutionDisabled', {value: !scripts});
    await call('Emulation.setDeviceMetricsOverride', {width: scripts ? 1400 : 390, height: 1000, deviceScaleFactor: 1, mobile: false});
    await call('Page.navigate', {url: base + '/settings'});
    await waitFor("document.readyState === 'complete' && !!document.querySelector('#backup_file')");
    await upload('invalid');
    await waitFor("document.body.innerText.includes('Allocation role weights must total 100%')");
    await waitFor("document.activeElement.id === 'backup_file'");
    assert.equal(await evaluate("document.activeElement.getAttribute('aria-invalid')"), 'true');
    assert.ok(await evaluate("!!document.querySelector('a[href=\"#backup_file\"]')"));
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    assert.deepEqual(await tables(), original);
    const shot = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
    fs.writeFileSync(`/tmp/lt-backup-${scripts ? 'js' : 'nojs'}-invalid.png`, Buffer.from(shot.data, 'base64'));
    await upload('valid');
    await waitFor("document.body.innerText.includes('The active database is still unchanged')");
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    assert.deepEqual(await tables(), original);
    await submit('form[action="/backup/restore"] input[type=submit]');
    await waitFor("document.body.innerText.includes('Confirm before restoring the backup')");
    await waitFor("document.activeElement.id === 'confirm_restore'");
    assert.deepEqual(await tables(), original);
    await keyboard(' ', 'Space', 32, ' ');
    assert.equal(await evaluate("document.querySelector('#confirm_restore').checked"), true);
    await submit('form[action="/backup/restore"] input[type=submit]');
    await waitFor("document.body.innerText.includes('Backup restored')");
    assert.deepEqual(await tables(), original);
  }
  console.log('Backup browser checks passed: invalid upload, linked error/focus, retry, preview, required confirmation, keyboard restore, JS/no-JS, narrow layout, exact table round trip.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  if (ws) ws.close();
  chrome.kill();
});
