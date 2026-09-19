// Headless checks against tests/funding_demo.py --state surplus --port 5191.
// Disposable synthetic data only; no connection to the owner's running app.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const dir = fs.mkdtempSync('/tmp/lt-funding-chrome-');
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
 ['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',
  '--disable-component-update','--no-default-browser-check','--remote-debugging-port=9342',
  `--user-data-dir=${dir}`,'about:blank'], {stdio:'ignore'});
const delay = ms => new Promise(r=>setTimeout(r,ms));
(async()=>{
 let targets;
 for(let i=0;i<60;i++){try{targets=await(await fetch('http://127.0.0.1:9342/json/list')).json();break;}catch{await delay(150);}}
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
 const screenshot=async path=>{const clip=await evaluate("(()=>{const r=document.querySelector('[aria-labelledby=funding-heading]').getBoundingClientRect();return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1}})()");fs.writeFileSync(path,Buffer.from((await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip})).data,'base64'));};
 await call('Page.enable');await call('Runtime.enable');
 await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1050,deviceScaleFactor:1,mobile:false});
 await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 const base='http://127.0.0.1:'+(process.argv[2]||'5191');
 const state=process.argv[3]||'surplus';
 await navigate(base+'/overview');
 await waitFor("!!document.getElementById('funding-reserves')");
 const funding=()=>evaluate("document.querySelector('[aria-labelledby=funding-heading]').innerText");
 const original=await funding();
 assert.match(original,/Core spending/);
 assert.match(original,/eligible bucket holdings/);
 assert.match(original,/Flexible spending is excluded/);
 assert.equal(await evaluate("document.getElementById('funding-detail').open"),false);
 assert.match(original,/Annual spending stays constant/);
 assert.match(original,/zero real return/);
 assert.doesNotMatch(original,/Growth remaining|Inflation-adjusted spending/);
 assert.doesNotMatch(original,/Left after year 10/);
 const rows=await evaluate("[...document.querySelectorAll('#funding-reserves tbody tr')].map(r=>r.textContent)");
 assert.match(rows[0],/60,000.00/);
 assert.match(rows[1],/140,000.00/);
 if(state==='surplus') {
  assert.match(original,/cover both periods without Growth withdrawals/);
  assert.match(rows[0],/150,000.00/); assert.match(rows[0],/90,000.00/);
  assert.match(rows[1],/330,000.00/); assert.match(rows[1],/190,000.00/);
 } else if(state==='growth') {
  assert.match(original,/do not cover both periods on their own/);
  assert.match(original,/140,000.00 from Growth/);
  assert.match(rows[0],/Gap/); assert.match(rows[1],/Gap/);
 } else if(state==='partial') {
  assert.match(original,/coverage is incomplete/);
  assert.doesNotMatch(original,/cover both periods without Growth withdrawals/);
 }
 await waitFor("!!Chart.getChart(document.querySelector('[data-chart=bucket-reserves] canvas'))");
 const chart=await evaluate("(()=>{const c=Chart.getChart(document.querySelector('[data-chart=bucket-reserves] canvas'));return {actual:c.data.datasets[0].data,required:c.data.datasets[1].data,painted:c.getDatasetMeta(0).data.every(b=>b.width>0),animation:c.options.animation,maximum:c.options.scales.x.max,unit:document.querySelector('[data-chart=bucket-reserves]').dataset.unit}})()");
 assert.equal(chart.painted,true);assert.equal(chart.animation,false);
 if(state!=='partial')assert.equal(chart.maximum,100);
 const visibleRows=await evaluate("[...document.querySelectorAll('#funding-targets tbody tr')].map(r=>[...r.cells].slice(1,3).map(c=>(c.querySelector('.funding-percent') || c).textContent.replace(/USD|,|%|\\s/g,'')))");
 for(let i=0;i<2;i++)assert.deepEqual(visibleRows[i],[chart.required[i],chart.actual[i]]);
 assert.equal(await evaluate("document.querySelector('[data-chart=bucket-reserves]').getBoundingClientRect().top < document.getElementById('funding-detail').getBoundingClientRect().top"),true);
 // Native disclosures open by keyboard, including without JavaScript.
 await evaluate("document.querySelector('[aria-labelledby=funding-heading] details summary').focus()");
 await key('Enter');
 assert.equal(await evaluate("document.querySelector('[aria-labelledby=funding-heading] details').open"),true);
 await evaluate("document.querySelector('#funding-detail details summary').focus()");await key('Enter');
 assert.match(await funding(),/Applied from all sources/);
 assert.match(await funding(),/Unallocated capital · today's money/);
 await evaluate("document.getElementById('funding-detail').open=false");
 assert.equal(await evaluate("[...document.querySelectorAll('#funding-reserves th')].every(h=>h.hasAttribute('scope'))"),true);
 await evaluate('window.scrollTo(0,0)');
 await screenshot(`/tmp/lt-funding-${state}-desktop.png`);
 await call('Emulation.setDeviceMetricsOverride',{width:390,height:1000,deviceScaleFactor:1,mobile:false});
 await delay(200);
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 assert.equal(await evaluate("[...document.querySelectorAll('#funding-targets td')].every(c=>c.getBoundingClientRect().right<=innerWidth)"),true);
 await screenshot(`/tmp/lt-funding-${state}-narrow.png`);
 const ax=await call('Accessibility.getFullAXTree');
 assert.ok(ax.nodes.some(n=>n.role?.value==='table' && n.name?.value==='Current bucket reserves versus spending targets'));
 assert.ok(ax.nodes.some(n=>n.role?.value==='rowheader' && n.name?.value==='Now'));
 await call('Emulation.setScriptExecutionDisabled',{value:true});
 await navigate(base+'/overview');
 await waitFor("!!document.getElementById('funding-reserves')");
 assert.deepEqual(await evaluate("[...document.querySelectorAll('#funding-reserves tbody tr')].map(r=>r.textContent)"),rows);
 assert.equal(await evaluate("document.querySelector('[data-chart=bucket-reserves]').hidden"),true);
 await evaluate("document.querySelector('[aria-labelledby=funding-heading] details summary').focus()");
 await key('Enter');
 assert.equal(await evaluate("document.querySelector('[aria-labelledby=funding-heading] details').open"),true);
 // Reporting-currency validation and GET recovery retain the selected date.
 await evaluate("document.getElementById('ccy').value='1';document.getElementById('ccy').focus()");
 await key('Enter');await waitFor("!!document.getElementById('ccy-error')");
 assert.equal(await evaluate('document.activeElement.id'),'ccy');
 assert.equal(await evaluate("document.querySelector('#ccy-error a').getAttribute('href')"),'#ccy');
 await evaluate("document.getElementById('ccy').value='USD';document.getElementById('ccy').focus()");
 await key('Enter');await waitFor("!document.getElementById('ccy-error') && location.search.includes('ccy=USD')");
 assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
 assert.deepEqual(await evaluate("[...document.querySelectorAll('#funding-reserves tbody tr')].map(r=>r.textContent)"),rows);
 console.log('Overview: exact reserve rows, keyboard disclosures, no-JavaScript equality and currency-error recovery, 390px layout and reduced-motion presentation passed.');
 ws.close();
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>chrome.kill());
