// Headless checks against tests/retirement_demo.py --state funded --port 5190.
// Disposable synthetic data only; no connection to the owner's running app.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const dir = fs.mkdtempSync('/tmp/lt-mc-chrome-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
 ['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
  '--disable-component-update','--no-default-browser-check','--remote-debugging-port=9341',
  `--user-data-dir=${dir}`,'about:blank'], {stdio:'ignore'});
const delay = ms => new Promise(r=>setTimeout(r,ms));
(async()=>{
 let targets;
 for(let i=0;i<60;i++){try{targets=await(await fetch('http://127.0.0.1:9341/json/list')).json();break;}catch{await delay(150);}}
 assert.ok(targets, 'Chrome started');
 const ws = new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
 await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0;const pending=new Map();
 ws.addEventListener('message',e=>{const msg=JSON.parse(e.data);if(msg.id){const p=pending.get(msg.id);pending.delete(msg.id);msg.error?p.reject(msg.error):p.resolve(msg.result);}});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const waitFor=async expression=>{for(let n=0;n<600;n++){if(await evaluate(expression))return;await delay(200);}throw Error('Timed out: '+expression);};
 const navigate=async url=>{await call('Page.navigate',{url});await waitFor("document.readyState==='complete'");};
 const key=async k=>{await call('Input.dispatchKeyEvent',{type:'rawKeyDown',key:k,code:k,windowsVirtualKeyCode:k==='Tab'?9:13});if(k==='Enter')await call('Input.dispatchKeyEvent',{type:'char',text:'\r',key:'Enter',windowsVirtualKeyCode:13});await call('Input.dispatchKeyEvent',{type:'keyUp',key:k,code:k,windowsVirtualKeyCode:k==='Tab'?9:13});};
 const screenshot=async path=>fs.writeFileSync(path,Buffer.from((await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true})).data,'base64'));
 await call('Page.enable');await call('Runtime.enable');
 await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1050,deviceScaleFactor:1,mobile:false});
 await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 const base='http://127.0.0.1:5190';
 await navigate(base+'/retirement');
 await evaluate("document.getElementById('terminal_legacy_target_amount').value='100000';document.getElementById('annual_savings_amount').value='0';document.getElementById('project').focus()");
 await key('Enter');await waitFor("!!document.querySelector('a[href*=monte-carlo]')");
 const entry=await evaluate("document.querySelector('a[href*=monte-carlo]').href");
 await navigate(entry);await waitFor("!!document.getElementById('mc-form')");
 assert.match(await evaluate('document.body.innerText'),/Illustrative USD profile/);
 await evaluate("document.querySelector('.disclosure summary').focus()");
 await key('Enter');
 assert.match(await evaluate('document.body.innerText'),/Annual volatility[\s\S]*16\.30%/);
 assert.match(await evaluate('document.body.innerText'),/All compound-growth rates remain your inputs/);
 assert.equal(await evaluate("!!document.querySelector('#mc-form a[href^=http]')"),false);
 // The enhanced request retains ordinary server-rendered validation and focus.
 await evaluate("document.getElementById('equity_percent').value='0';document.getElementById('run_comparison').focus()");
 await key('Enter');await waitFor("!!document.getElementById('equity_percent-error') && document.activeElement.id==='equity_percent'");
 // A lost connection restores editable inputs and an actionable message.
 await evaluate("window.mcOriginalFetch=window.fetch;window.fetch=()=>Promise.reject(new TypeError('Synthetic connection loss'));document.getElementById('mc-form').requestSubmit()");
 await waitFor("document.getElementById('mc-run-status').textContent.includes('connection was interrupted')");
 assert.equal(await evaluate("document.getElementById('run_comparison').disabled || document.getElementById('flexible_amount').readOnly"),false);
 await evaluate("window.fetch=window.mcOriginalFetch;delete window.mcOriginalFetch");
 await evaluate("document.getElementById('equity_percent').value='50';document.getElementById('income_percent').value='30';document.getElementById('liquidity_percent').value='10';document.getElementById('alternatives_percent').value='10';document.getElementById('run_comparison').focus()");
 await key('Enter');
 await waitFor("!!document.getElementById('mc-waiting') && !document.getElementById('mc-waiting').hidden");
 assert.equal(await evaluate("document.getElementById('run_comparison').disabled"),true);
 await waitFor("Number(document.querySelector('.mc-progress').getAttribute('aria-valuenow')) > 0");
 const completedBefore=await evaluate("Number(document.querySelector('.mc-progress').getAttribute('aria-valuenow'))");
 assert.equal(await evaluate("getComputedStyle(document.querySelector('.mc-progress span')).animationName"),'none');
 await delay(2200);
 assert.match(await evaluate("document.querySelector('#mc-waiting .hint').textContent"),/\d+ seconds elapsed/);
 assert.ok(await evaluate("Number(document.querySelector('.mc-progress').getAttribute('aria-valuenow'))") > completedBefore);
 assert.equal(await evaluate("document.querySelector('#mc-waiting p').textContent.replaceAll(',','').startsWith(document.querySelector('.mc-progress').getAttribute('aria-valuenow') + ' of ' )"),true);
 assert.equal(await evaluate("document.querySelector('.mc-progress').getBoundingClientRect().bottom <= innerHeight"),true);
 await screenshot('/tmp/lt-mc-waiting.png');
 assert.equal(await evaluate("document.getElementById('mc-run-status').getBoundingClientRect().width"),1);
 assert.equal(await evaluate("!!document.querySelector('#mc-form .error-summary, #mc-form [aria-invalid=true]')"),false);
 if(process.argv.includes('--waiting-only')) { console.log('Streamed progress, validation, connection recovery and waiting presentation passed.'); ws.close(); return; }
 await waitFor("!!document.getElementById('mc-charts') && typeof Chart !== 'undefined' && !!Chart.getChart(document.querySelector('#mc-charts canvas'))");
 const resultUrl=await evaluate('location.href');
 console.log('Full 1,000-path comparison rendered.');
 assert.equal(await evaluate("document.querySelectorAll('#mc-charts canvas').length"),2);
 assert.equal(await evaluate("Chart.getChart(document.querySelector('#mc-charts canvas')).options.animation"),false);
 assert.equal(await evaluate("(()=>{const cs=[...document.querySelectorAll('#mc-charts canvas')].map(c=>Chart.getChart(c));return cs[0].scales.y.min===cs[1].scales.y.min && cs[0].scales.y.max===cs[1].scales.y.max})()"),true);
 assert.equal(await evaluate("(()=>{const c=document.querySelector('#mc-charts canvas');return c.getContext('2d').getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3&&v>0)})()"),true);
 const chart=await evaluate("JSON.parse(document.getElementById('mc-charts').dataset.values)");
 const reportUrl=await evaluate("document.getElementById('mc-report-link').href");
 await evaluate("document.getElementById('mc-year').focus()");
 await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Home',code:'Home',windowsVirtualKeyCode:36});
 await call('Input.dispatchKeyEvent',{type:'keyUp',key:'Home',code:'Home',windowsVirtualKeyCode:36});
 assert.equal(await evaluate("document.getElementById('mc-age').textContent"),'51');
 assert.match(await evaluate("document.getElementById('mc-readout-0').textContent"),/today.*nominal/);
 await screenshot('/tmp/lt-mc-desktop.png');
 await call('Emulation.setDeviceMetricsOverride',{width:390,height:1000,deviceScaleFactor:1,mobile:false});await delay(300);
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await screenshot('/tmp/lt-mc-narrow.png');
 await evaluate("document.getElementById('flexible_amount').value='0';document.getElementById('flexible_amount').dispatchEvent(new Event('input',{bubbles:true}))");
 assert.match(await evaluate("document.getElementById('mc-run-status').textContent"),/previous run/);
 await navigate(reportUrl);await waitFor("!!document.querySelector('.mc-percentiles')");
 for(let i=0;i<2;i++){
  const rows=await evaluate(`([...document.querySelector('[data-allocation="${i}"]').tBodies[0].rows]).map(r=>[...r.cells].slice(1).map(c=>c.innerText.replaceAll(',','')))`);
  for(let j=0;j<rows.length;j++)assert.deepEqual(rows[j].slice(0,3),['p10','median','p90'].map(q=>chart.allocations[i][q][j]));
 }
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 await evaluate("document.getElementById('path').value='2';document.querySelector('.mc-path-form button').focus()");
 await key('Enter');await waitFor("location.search.includes('path=2') && document.body.innerText.includes('Path 2 — annual asset returns')");
 await evaluate("document.getElementById('path').value='0';document.querySelector('.mc-path-form button').focus()");
 await key('Enter');await waitFor("!!document.getElementById('path-error')");
 await waitFor("document.activeElement.id==='path'");
 // Entire comparison and path-selection flow works with JavaScript disabled.
 await call('Emulation.setScriptExecutionDisabled',{value:true});
 await navigate(resultUrl);await waitFor("!!document.getElementById('mc-form')");
 assert.equal(await evaluate("document.getElementById('mc-year').parentElement.hidden"),true);
 await evaluate("document.getElementById('equity_percent').value='55';document.getElementById('run_comparison').focus()");
 await key('Enter');await waitFor("!!document.getElementById('equity_percent-error')");
 assert.equal(await evaluate('document.activeElement.id'),'equity_percent');
 assert.equal(await evaluate("document.querySelector('.error-summary a').getAttribute('href')"),'#equity_percent');
 await evaluate("document.getElementById('equity_percent').value='50';document.getElementById('flexible_amount').value='0';document.getElementById('run_comparison').focus()");
 await key('Enter');await waitFor("!!document.getElementById('mc-results') && document.body.innerText.includes('Annual Flexible tested: USD 0.00')");
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 const coreReport=await evaluate("document.getElementById('mc-report-link').href");
 await navigate(coreReport);await waitFor("!!document.getElementById('path')");
 await evaluate("document.getElementById('path').value='1000';document.querySelector('.mc-path-form button').focus()");
 await key('Enter');await waitFor("document.body.innerText.includes('Path 1000 — annual asset returns')");
 await call('Emulation.setDeviceMetricsOverride',{width:816,height:1056,deviceScaleFactor:1,mobile:false});
 await call('Emulation.setEmulatedMedia',{media:'print'});
 assert.equal(await evaluate("[...document.querySelectorAll('.mc-report table')].every(t=>t.getBoundingClientRect().width<=document.querySelector('main').getBoundingClientRect().width+1)"),true);
 console.log(JSON.stringify({paths:1000,charts:'painted, identical axes and exact table agreement',keyboard:'age and path selection',noJavaScript:'validation focus, zero Flexible comparison and path 1000',narrow:'390px no page overflow',reducedMotion:'disabled animation',screenshots:['/tmp/lt-mc-desktop.png','/tmp/lt-mc-narrow.png']}));
 ws.close();
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>chrome.kill());
