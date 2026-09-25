const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const markup=fs.readFileSync('web/index.html','utf8');
async function until(check){for(let i=0;i<100;i++){if(check())return;await new Promise(r=>setTimeout(r,10));}throw Error('Update UI timed out');}
(async()=>{
 let status={phase:'idle',busy:false,supported:true,current_version:'0.3.1'}, calls=[], offline=false;
 function page(){
  const dom=new JSDOM(markup,{url:'http://nas:17863',runScripts:'outside-only'}),w=dom.window;
  w.__polls=[];w.confirm=()=>true;w.setTimeout=(fn,ms)=>{if(ms===3000||ms===15000)w.__polls.push(fn);return 0;};w.clearTimeout=()=>{};
  w.fetch=async(path,options)=>{
   calls.push({path,options});
   if(offline)throw Error('restarting');
   let value;
   if(path==='/api/update/status')value=status;
   else if(path==='/api/update')value={available:true,version:'v0.3.2'};
   else if(path==='/api/update/apply'){status={...status,phase:'installing',busy:true,target:'v0.3.2',message:'適用中'};value=status;}
   else value={log:'installer log'};
   return {ok:true,json:async()=>structuredClone(value)};
  };
  w.eval(fs.readFileSync('web/update.js','utf8'));
  return dom;
 }
 let dom=page();let d=dom.window.document;
 try{
  await until(()=>!d.getElementById('check-update').disabled);
  d.getElementById('check-update').click();await until(()=>!d.getElementById('apply-update').disabled);
  assert.match(d.getElementById('apply-update').textContent,/v0.3.2/);
  assert.equal(d.querySelector('#update-message a'),null);
  d.getElementById('apply-update').click();d.getElementById('apply-update').click();
  await until(()=>d.getElementById('update-progress').textContent.includes('インストール中'));
  const posts=calls.filter(c=>c.path==='/api/update/apply');assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0].options.body),{version:'v0.3.2'});
  assert.equal(posts[0].options.headers['X-SelfCare-Request'],'1');
  offline=true;await dom.window.__polls.shift()();
  assert.match(d.getElementById('update-progress').textContent,/再接続/);
  assert.equal(calls.filter(c=>c.path==='/api/update/apply').length,1);
  offline=false;await dom.window.__polls.shift()();
  assert.match(d.getElementById('update-progress').textContent,/インストール中/);
  dom.window.close();dom=page();d=dom.window.document;
  await until(()=>d.getElementById('update-progress').textContent.includes('インストール中'));
  assert.equal(d.getElementById('check-update').disabled,true);
  assert.equal(calls.filter(c=>c.path==='/api/update/apply').length,1);
  // Reload after restart reads completion from the backend.
  status={...status,phase:'success',busy:false,current_version:'0.3.2',message:'完了'};
  dom.window.close();dom=page();d=dom.window.document;
  await until(()=>!d.getElementById('reload-update').hidden);
  // A previous success must not hide the next available release's button.
  status={...status,target:'v0.3.1',current_version:'0.3.1'};
  d.getElementById('check-update').click();await until(()=>!d.getElementById('apply-update').disabled);
  assert.equal(d.getElementById('apply-update').hidden,false);
  console.log('Update UI: explicit apply, no download link, double-click guard, persisted restart state: passed');
 }finally{dom.window.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
