// DOM integration test against an isolated real HTTP server. No NAS data is used.
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const data = fs.mkdtempSync(path.join(os.tmpdir(),'selfcare-ui-'));
const child = spawn(process.env.PYTHON || 'python3',['webapp.py','--port','18863','--data-dir',data],{stdio:['ignore','pipe','pipe']});
const base='http://127.0.0.1:18863';
async function until(check){for(let i=0;i<100;i++){if(await check())return;await new Promise(r=>setTimeout(r,50));}throw Error('UI operation timed out');}
(async()=>{let dom;try{
 await until(async()=>{try{return (await fetch(base+'/api/status')).ok;}catch{return false;}});
 dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:base,runScripts:'outside-only'});
 const w=dom.window,d=w.document;
 w.fetch=(url,init)=>fetch(base+url,init);w.setInterval=()=>0;w.confirm=()=>true;w.HTMLElement.prototype.scrollIntoView=()=>{};
 w.eval(fs.readFileSync('web/app.js','utf8'));
 await until(()=>d.getElementById('message').textContent.includes('利用者を追加'));
 const submit=id=>d.getElementById(id).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 d.getElementById('user-form').elements.name.value='DOM Test';submit('user-form');
 await until(()=>d.getElementById('message').textContent==='利用者を追加しました');
 d.querySelector('[data-page="records"]').click();
 const form=d.getElementById('record-form');
 form.elements.measured_at.value='2026-09-25T07:30:12';form.elements.systolic.value='130';form.elements.diastolic.value='80';form.elements.pulse.value='72';submit('record-form');
 await until(()=>d.getElementById('message').textContent.includes('1件保存'));
 await until(()=>d.getElementById('history').textContent.includes('最高血圧 130'));
 assert.match(d.getElementById('latest-bp').textContent,/130 \/ 80/);
 d.getElementById('history').querySelector('button').click();form.elements.pulse.value='74';submit('record-form');
 await until(()=>d.getElementById('message').textContent==='記録を更新しました');
 await until(()=>d.getElementById('history').textContent.includes('脈拍 74'));
 d.querySelector('[data-page="devices"]').click();
 const device=d.getElementById('device-form');device.elements.name.value='Test cuff';device.elements.address.value='AA:BB:CC:DD:EE:FF';
 d.getElementById('bindings').querySelector('select').selectedIndex=1;submit('device-form');
 await until(()=>d.getElementById('message').textContent==='機器設定を保存しました');
 assert.match(d.getElementById('device-list').textContent,/Test cuff/);
 assert.equal((await (await fetch(base+'/api/devices')).json())[0].transport,'homehub');
 const actualFetch=w.fetch;
 w.fetch=(url,init)=>url==='/api/jobs'&&init?.method==='POST'&&JSON.parse(init.body).action==='scan'?Promise.resolve(new Response(JSON.stringify({id:'scan-test',state:'queued'}),{headers:{'Content-Type':'application/json'}})):url==='/api/jobs'&&(!init||!init.method)?Promise.resolve(new Response(JSON.stringify([{
  id:'scan-test',action:'scan',state:'done',created_at:new Date().toISOString(),message:'2台検出しました',
  result:JSON.stringify({adapter:'hci1',transport:'homehub',devices:[
   {name:'BLEsmart_0001000B123456',address:'11:22:33:44:55:66',rssi:-40},
   {name:'名前なし',address:'11:22:33:44:55:77',rssi:-65}]})
 }]),{headers:{'Content-Type':'application/json'}})):actualFetch(url,init);
 d.getElementById('scan').click();
 await until(()=>d.querySelectorAll('#scan-results .scan-result').length===2);
 const candidates=d.querySelectorAll('#scan-results .scan-result');
 assert.equal(candidates.length,2);
 assert.equal(candidates[0].querySelector('select').value,'HBF-228T');
 assert.equal(candidates[1].querySelector('select').value,'');
 assert.equal(candidates[0].querySelectorAll('select')[1].value,(await (await fetch(base+'/api/users')).json())[0].id);
 candidates[0].querySelector('button').click();
 await until(()=>d.getElementById('message').textContent.includes('登録してペアリングを開始しました'));
 const registered=(await (await fetch(base+'/api/devices')).json()).find(x=>x.address==='11:22:33:44:55:66');
 assert.equal(registered.model,'HBF-228T');assert.equal(registered.adapter,'hci1');
 assert.equal(registered.bindings['1'],(await (await fetch(base+'/api/users')).json())[0].id);
 assert.equal(registered.auto_sync,true);
 console.log('UI records, discovery → model/user selection → registration and pairing queued: passed');
}finally{dom?.window.close();child.kill('SIGTERM');await new Promise(r=>child.once('exit',r));fs.rmSync(data,{recursive:true,force:true});}})().catch(e=>{console.error(e);process.exitCode=1;});
