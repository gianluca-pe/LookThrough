// Headless checks against tests/fx_demo.py on port 5195.
// Disposable synthetic data only; no connection to the owner's running app.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const dir = fs.mkdtempSync('/tmp/lt-fx-chrome-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
 ['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
  '--disable-component-update','--no-default-browser-check','--remote-debugging-port=9345',
  `--user-data-dir=${dir}`,'about:blank'], {stdio:'ignore'});
const delay = ms => new Promise(r=>setTimeout(r,ms));
(async()=>{
 let targets;
 for(let i=0;i<60;i++){try{targets=await(await fetch('http://127.0.0.1:9345/json/list')).json();break;}catch{await delay(150);}}
 assert.ok(targets, 'Chrome started');
 const ws = new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
 await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0;const pending=new Map();
 ws.addEventListener('message',e=>{const msg=JSON.parse(e.data);if(msg.id){const p=pending.get(msg.id);pending.delete(msg.id);msg.error?p.reject(msg.error):p.resolve(msg.result);}});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const waitFor=async expression=>{for(let n=0;n<150;n++){if(await evaluate(expression))return;await delay(200);}throw Error('Timed out: '+expression);};
 const navigate=async url=>{await call('Page.navigate',{url});await waitFor("document.readyState==='complete'");};
 const key=async k=>{await call('Input.dispatchKeyEvent',{type:'rawKeyDown',key:k,code:k,windowsVirtualKeyCode:k==='Tab'?9:13});if(k==='Enter')await call('Input.dispatchKeyEvent',{type:'char',text:'\r',key:'Enter',windowsVirtualKeyCode:13});await call('Input.dispatchKeyEvent',{type:'keyUp',key:k,code:k,windowsVirtualKeyCode:k==='Tab'?9:13});};
 const screenshot=async path=>{await evaluate('window.scrollTo(0,0)');fs.writeFileSync(path,Buffer.from((await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true})).data,'base64'));};
 await call('Page.enable');await call('Runtime.enable');
 await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1050,deviceScaleFactor:1,mobile:false});
 await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});

 const base='http://127.0.0.1:5195';
 await navigate(base+'/settings');await waitFor("!!document.getElementById('fx-settings')");
 assert.match(await evaluate('document.body.innerText'),/No reference set saved yet/);
 await screenshot('/tmp/lt-fx-settings-before.png');
 // Keep JavaScript disabled for all write and recovery flows.
 await call('Emulation.setScriptExecutionDisabled',{value:true});
 await evaluate("document.querySelector('a[href=\"/settings/fx/manual\"]').focus()");await key('Enter');
 await waitFor("location.pathname==='/settings/fx/manual' && !!document.getElementById('rate_USD')");
 await evaluate("document.querySelector('button[type=submit]').focus()");await key('Enter');
 await waitFor("!!document.querySelector('.error-summary')");
 assert.equal(await evaluate('document.activeElement.id'),'source_note');
 assert.ok(await evaluate("!!document.querySelector('a[href=\"#source_note\"]')"));
 await navigate(base+'/settings');await waitFor("!!document.getElementById('fx-settings')");
 await evaluate("document.querySelector('form[action=\"/settings/fx/download\"] button').focus()");await key('Enter');
 await waitFor("document.title.includes('Review FX rates')");
 assert.match(await evaluate('document.body.innerText'),/11 Sep 2026|11 September 2026|11\/09\/2026/);
 assert.equal(await evaluate("document.querySelectorAll('table:first-of-type tbody tr').length>=3"),true);
 await screenshot('/tmp/lt-fx-review.png');
 await evaluate("document.querySelector('form[action=\"/settings/fx/save\"] button').focus()");await key('Enter');
 await waitFor("location.pathname==='/settings' && !!document.querySelector('#fx-settings details')");
 assert.match(await evaluate('document.body.innerText'),/saved in this database/);
 await evaluate("document.querySelector('#fx-settings details summary').focus()");await key('Enter');
 assert.equal(await evaluate("document.querySelector('#fx-settings details').open"),true);
 assert.match(await evaluate("document.getElementById('fx-settings').innerText"),/1\.2/);
 await screenshot('/tmp/lt-fx-settings-desktop.png');
 for(const width of [390,320]) {
   await call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
   await screenshot(`/tmp/lt-fx-settings-${width}.png`);
   assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true,await evaluate("JSON.stringify([...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right})).slice(0,12))"));
 }
 // Repeat the public-table update: no new edition for an unchanged set.
 await evaluate("document.querySelector('form[action=\"/settings/fx/download\"] button').focus()");await key('Enter');
 await waitFor("document.body.innerText.includes('already saved')");
 await evaluate("document.querySelector('form[action=\"/settings/fx/download\"] button').focus()");await key('Enter');
 await waitFor("document.title.includes('FX update unavailable')");
 assert.match(await evaluate('document.body.innerText'),/JSON format has changed/);
 assert.ok(await evaluate("!!document.querySelector('a[href=\"/settings/fx/manual\"]')"));
 await screenshot('/tmp/lt-fx-failure-320.png');
 await evaluate("document.querySelector('a[href=\"/settings/fx/manual\"]').focus()");await key('Enter');
 await waitFor("!!document.getElementById('rate_USD')");
 await evaluate("document.getElementById('source_note').value='Synthetic official reference';document.querySelector('button[type=submit]').focus()");await key('Enter');
 await waitFor("!!document.querySelector('.error-summary a[href=\"#rate_AED\"]')");
 assert.equal(await evaluate('document.activeElement.id'),'rate_AED');
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await screenshot('/tmp/lt-fx-manual-error-320.png');
 await evaluate("document.getElementById('rate_AED').value='4.4';document.getElementById('rate_USD').value='1.21';document.querySelector('button[type=submit]').focus()");await key('Enter');
 await waitFor("document.title.includes('Review FX rates')");
 await evaluate("document.querySelector('form[action=\"/settings/fx/save\"] button').focus()");await key('Enter');
 await waitFor("location.pathname==='/settings' && document.body.innerText.includes('Manual entry / correction')");
 await navigate(base+'/overview?as_of=2026-09-13');await waitFor("!!document.getElementById('ccy')");
 assert.deepEqual(await evaluate("[...document.querySelectorAll('#ccy option')].map(o=>o.value)"),['AED','EUR','USD']);
 await screenshot('/tmp/lt-fx-overview-320.png');
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await navigate(base+'/settings/fx/manual');await waitFor("!!document.getElementById('rate_USD')");
 await evaluate("document.getElementById('rate_USD').value='1.22';document.getElementById('source_note').value='Corrected against source';document.querySelector('button[type=submit]').focus()");await key('Enter');
 await waitFor("!!document.getElementById('replace')");
 await evaluate("document.querySelector('form[action=\"/settings/fx/save\"] button').focus()");await key('Enter');
 await waitFor("document.body.innerText.includes('Confirm replacement')");
 assert.equal(await evaluate('document.activeElement.id'),'replace');
 // Native checkbox keyboard behavior, without JavaScript assistance.
 await call('Input.dispatchKeyEvent',{type:'keyDown',key:' ',code:'Space',windowsVirtualKeyCode:32});
 await call('Input.dispatchKeyEvent',{type:'keyUp',key:' ',code:'Space',windowsVirtualKeyCode:32});
 assert.equal(await evaluate("document.getElementById('replace').checked"),true);
 await evaluate("document.querySelector('form[action=\"/settings/fx/save\"] button').focus()");await key('Enter');
 await waitFor("location.pathname==='/settings' && document.body.innerText.includes('saved in this database')");
 console.log('FX browser checks passed: desktop/390px/320px, no-JS download/review/save, idempotence, provider-format failure, manual new-date entry, linked errors/focus, same-date confirmation, bounded Overview currencies, reduced motion.');
 ws.close();
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{chrome.kill();setTimeout(()=>fs.rmSync(dir,{recursive:true,force:true}),1000);});
