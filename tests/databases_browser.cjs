// Headless checks against tests/databases_demo.py on port 5194.
// Disposable synthetic data only; no connection to the owner's running app.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const dir = fs.mkdtempSync('/tmp/lt-db-chrome-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
 ['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
  '--disable-component-update','--no-default-browser-check','--remote-debugging-port=9344',
  `--user-data-dir=${dir}`,'about:blank'], {stdio:'ignore'});
const delay = ms => new Promise(r=>setTimeout(r,ms));
(async()=>{
 let targets;
 for(let i=0;i<60;i++){try{targets=await(await fetch('http://127.0.0.1:9344/json/list')).json();break;}catch{await delay(150);}}
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
 const screenshot=async path=>fs.writeFileSync(path,Buffer.from((await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true})).data,'base64'));
 await call('Page.enable');await call('Runtime.enable');
 await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1050,deviceScaleFactor:1,mobile:false});
 await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});

 const base=process.argv.includes('--localhost') ? 'http://localhost:5194' : 'http://127.0.0.1:5194';
 await navigate(base+'/');
 await waitFor("document.querySelectorAll('.database-card').length===4");
 assert.equal(await evaluate("document.querySelectorAll('h1').length"),1);
 assert.match(await evaluate('document.body.innerText'),/Choose a database/);
 assert.match(await evaluate('document.body.innerText'),/Ready to open/);
 assert.match(await evaluate('document.body.innerText'),/Unavailable/);
 await screenshot('/tmp/lt-db-desktop.png');
 // Keyboard entry into the populated dataset.
 await evaluate("[...document.querySelectorAll('.database-card')].find(c=>c.innerText.includes('Retirement.sqlite3')).querySelector('button').focus()");
 await key('Enter');
 await waitFor("location.pathname.includes('/d/') && !!document.querySelector('.dataset-menu')");
 await waitFor("document.querySelector('h1').innerText==='Overview'");
 const realPrefix=await evaluate("location.pathname.split('/').slice(0,3).join('/')");
 assert.match(await evaluate("document.querySelector('.dataset-panel').textContent"),/Retirement.sqlite3/);
 assert.equal(await evaluate("[...document.querySelectorAll('.sidebar a')].every(a=>a.pathname.startsWith(location.pathname.split('/').slice(0,3).join('/')) )"),true);

 // Header disclosure: native keyboard access, Escape enhancement and narrow/no-JS fallback.
 assert.equal(await evaluate("!!document.querySelector('.dataset-context')"),false);
 assert.equal(await evaluate("document.querySelector('.dataset-menu').open"),false);
 await evaluate("document.querySelector('.dataset-menu summary').focus()");await key('Enter');
 assert.equal(await evaluate("document.querySelector('.dataset-menu').open"),true);
 assert.match(await evaluate("document.querySelector('.dataset-panel').innerText"),/Current file[\s\S]*Retirement.sqlite3[\s\S]*Change database/);
 await screenshot('/tmp/lt-header-desktop-open.png');
 await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 await call('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 assert.equal(await evaluate("document.querySelector('.dataset-menu').open"),false);
 assert.equal(await evaluate("document.activeElement===document.querySelector('.dataset-menu summary')"),true);
 if(process.argv.includes('--header-only')) {
   await screenshot('/tmp/lt-header-desktop.png');
   await call('Emulation.setScriptExecutionDisabled',{value:true});
   await navigate(base+realPrefix+'/settings');
   await waitFor("!!document.querySelector('.dataset-menu') && document.title.includes('Settings')");
   for(const width of [390,320]) {
     await call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
     await evaluate("document.querySelector('.dataset-menu').open=false;document.querySelector('.dataset-menu summary').focus()");await key('Enter');
     assert.equal(await evaluate("document.querySelector('.dataset-menu').open"),true);
     await screenshot(`/tmp/lt-header-${width}-open.png`);
     assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true, await evaluate("JSON.stringify([...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right})).slice(0,12))"));
     assert.equal(await evaluate("(()=>{const r=document.querySelector('.dataset-panel').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;})()"),true);
     await screenshot(`/tmp/lt-header-${width}-open.png`);
     await key('Tab');
     assert.equal(await evaluate("document.activeElement===document.querySelector('.dataset-panel a')"),true);
     await evaluate("document.querySelector('.dataset-menu summary').focus()");await key('Enter');
     assert.equal(await evaluate("document.querySelector('.dataset-menu').open"),false);
   }
   await evaluate("document.querySelector('.dataset-menu summary').focus()");await key('Enter');await key('Tab');await key('Enter');
   await waitFor("location.pathname==='/' && !!document.querySelector('.database-list')");
   await evaluate("[...document.querySelectorAll('.database-card')].find(c=>c.innerText.includes('Practice.sqlite3')).querySelector('button').focus()");await key('Enter');
   await waitFor("!!document.querySelector('.dataset-menu') && document.body.textContent.includes('Set up your portfolio')");
   assert.equal(await evaluate("document.querySelector('.dataset-label').textContent"),'Practice.sqlite3');
   assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
   console.log('Integrated header: desktop, 390px/320px long names, keyboard opening/switching/closing, Escape focus return, no-JS disclosure and empty-portfolio filename passed.');
   ws.close();return;
 }
 // Existing chart progressive enhancement and report links retain the prefix.
 await waitFor("typeof Chart !== 'undefined'");
 await screenshot('/tmp/lt-db-overview.png');
 await navigate(base+realPrefix+'/retirement');
 await waitFor("!!document.querySelector('h1') && !!document.getElementById('project')");
 await evaluate("document.getElementById('terminal_legacy_target_amount').value='100000';document.getElementById('project').focus()");
 await key('Enter');
 await waitFor("!!document.querySelector('a[href*=monte-carlo]')");
 const mcLink=await evaluate("document.querySelector('a[href*=monte-carlo]').href");
 assert.ok(mcLink.includes(realPrefix));
 await navigate(mcLink);await waitFor("!!document.getElementById('mc-form')");
 assert.ok(await evaluate("document.getElementById('mc-form').action.includes(location.pathname.split('/').slice(0,3).join('/'))"));
 // No-JavaScript chooser, errors, preview, and creation.
 await call('Emulation.setScriptExecutionDisabled',{value:true});
 await navigate(base+'/');
 await evaluate("document.querySelector('.database-new a').focus()");await key('Enter');
 await waitFor("location.pathname==='/databases/new' && document.activeElement.id==='name'");
 assert.equal(await evaluate('document.activeElement.id'),'name');
 await evaluate("document.getElementById('name').value='../wrong';document.querySelector('button[type=submit]').focus()");await key('Enter');
 await waitFor("!!document.getElementById('name-error') && document.activeElement.id==='name'");
 assert.equal(await evaluate('document.activeElement.id'),'name');
 assert.equal(await evaluate("document.querySelector('.error-summary a').getAttribute('href')"),'#name');
 assert.match(await evaluate("document.getElementById('name').getAttribute('aria-describedby')"),/name-error/);
 await evaluate("document.getElementById('name').value='Family practice';document.querySelector('button[type=submit]').focus()");await key('Enter');
 await waitFor("!!document.querySelector('.database-preview')");
 assert.match(await evaluate("document.querySelector('.database-preview').innerText"),/Family practice.sqlite3/);
 await screenshot('/tmp/lt-db-create.png');
 await evaluate("document.querySelector('button[name=confirm]').focus()");await key('Enter');
 await waitFor("!!document.querySelector('.dataset-menu') && document.body.innerText.includes('Set up your portfolio')");
 assert.match(await evaluate("document.querySelector('.dataset-panel').textContent"),/Family practice.sqlite3/);
 const practicePrefix=await evaluate("location.pathname.split('/').slice(0,3).join('/')");
 assert.notEqual(practicePrefix,realPrefix);
 await navigate(base+realPrefix+'/settings');
 await waitFor("!!document.querySelector('.dataset-menu')");
 assert.match(await evaluate("document.querySelector('.dataset-panel').textContent"),/Retirement.sqlite3/);
 // Narrow layout, native help disclosure, and older-schema confirmation.
 await call('Emulation.setDeviceMetricsOverride',{width:390,height:1000,deviceScaleFactor:1,mobile:false});
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await screenshot('/tmp/lt-db-settings-narrow.png');
 await navigate(base+'/');await waitFor("document.querySelectorAll('.database-card').length===5");
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await screenshot('/tmp/lt-db-narrow.png');
 await evaluate("document.querySelector('.database-footer summary').focus()");await key('Enter');
 assert.equal(await evaluate("document.querySelector('.database-footer details').open"),true);
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await evaluate("[...document.querySelectorAll('.database-card')].find(c=>c.innerText.includes('Older.sqlite3')).querySelector('button').focus()");await key('Enter');
 await waitFor("!!document.querySelector('.database-upgrade-steps')");
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await screenshot('/tmp/lt-db-upgrade.png');
 await evaluate("document.querySelector('input[type=submit]').focus()");await key('Enter');
 await waitFor("location.pathname==='/' && document.body.innerText.includes('Your recovery copy is saved at')");
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await screenshot('/tmp/lt-db-upgrade-success-narrow.png');
 console.log('Chooser, prefixed Overview/retirement/Monte Carlo entry, keyboard, no-JS creation/upgrade, linked-error focus, narrow layout and dataset switching passed.');
 ws.close();
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{chrome.kill();setTimeout(()=>fs.rmSync(dir,{recursive:true,force:true}),500);});
