/* Read-only view of persisted Apple Health samples. */
(() => {
  const $ = id => document.getElementById(id);
  const names = {steps:['歩数','歩'], walking_running_distance:['距離','km'], active_energy:['アクティブエネルギー','kcal'], heart_rate:['心拍数','bpm'], oxygen_saturation:['血中酸素飽和度','%'], sleep:['睡眠','']};
  const stages = {awake:'覚醒',in_bed:'ベッドにいた時間',asleep_unspecified:'睡眠',core:'コア',deep:'深い',rem:'レム'};
  const colors = {awake:'#e6a343',in_bed:'#89949c',asleep_unspecified:'#738aca',core:'#489bc5',deep:'#6358b8',rem:'#b767ba'};
  const node = (tag,text) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const fmt = n => Number(n.toFixed(2)).toLocaleString('ja-JP');
  const time = t => new Date(t).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  function bounds(day) {const a=new Date(day+'T00:00:00');const b=new Date(a);b.setDate(b.getDate()+1);return [+a,+b];}
  function union(intervals) {let end=-Infinity,total=0;for(const [a,b] of intervals.sort((x,y)=>x[0]-y[0])){total+=Math.max(0,b-Math.max(a,end));end=Math.max(end,b);}return total;}
  function summarize(rows,a,b) {
    const out={};
    for(const key of Object.keys(names)) {
      const list=rows.filter(r=>r.metric===key && (key==='sleep'?Date.parse(r.end_at)>a:Date.parse(r.start_at)>=a) && Date.parse(r.start_at)<b);
      let value='—';
      if(list.length) {
        if(key==='sleep') {const ms=union(list.filter(r=>!['awake','in_bed'].includes(r.value)).map(r=>[Math.max(a,Date.parse(r.start_at)),Math.min(b,Date.parse(r.end_at))]));value=`${Math.floor(ms/3600000)}時間${Math.floor(ms/60000)%60}分`;}
        else if(['heart_rate','oxygen_saturation'].includes(key)){const vs=list.map(r=>r.value);value=`${fmt(vs.reduce((x,y)=>x+y,0)/vs.length)} ${names[key][1]}（平均）`;}
        else value=fmt(list.reduce((s,r)=>s+r.value,0)/(key==='walking_running_distance'?1000:1))+' '+names[key][1];
      }
      out[key]={list,value};
    }
    return out;
  }
  // Export pure calculations for regression tests, without any network side effects.
  if(typeof module!=='undefined' && module.exports) {module.exports={bounds,union,summarize};return;}
  if(!$('ah-view'))return;
  let rows=[],range=[],serial=0;
  function svg(tag,attrs,text){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;return e;}
  function plot(target,list,key) {
    if(!list.length){target.append(node('p','この日の記録はありません'));return;}
    const [a,b]=range,W=600,H=200,x=t=>45+(t-a)/(b-a)*535;
    const g=svg('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':names[key][0]+'の時刻別記録'});
    const sleeping=key==='sleep',labels=Object.keys(stages),values=list.map(r=>r.value);
    const low=sleeping?0:Math.min(...values)-1,high=sleeping?6:Math.max(...values)+1;
    const y=v=>160-(v-low)/(high-low)*130;
    for(let i=0;i<=4;i++){const t=a+(b-a)*i/4;g.append(svg('text',{x:x(t),y:190,'text-anchor':i===4?'end':'start',fill:'currentColor','font-size':12},new Date(t).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'})));}
    if(!sleeping)for(let i=0;i<3;i++){const v=low+(high-low)*i/2;g.append(svg('text',{x:2,y:y(v),fill:'currentColor','font-size':11},fmt(v)));}
    const points=[];
    for(const r of list){const t=Date.parse(r.start_at),label=`${time(r.start_at)}〜${time(r.end_at)} ${sleeping?stages[r.value]:fmt(r.value)+' '+names[key][1]}`;
      const e=sleeping?svg('rect',{x:x(Math.max(a,t)),y:10+labels.indexOf(r.value)*24,width:Math.max(1,x(Math.min(b,Date.parse(r.end_at)))-x(Math.max(a,t))),height:18,fill:colors[r.value]}):svg('circle',{cx:x(t),cy:y(r.value),r:2.5,fill:'var(--accent, #16867d)'});
      e.append(svg('title',{},label));g.append(e);
      points.push({x:x(Math.max(a,t)),label:time(r.start_at)+'〜'+time(r.end_at),fields:[{label:names[key][0],value:sleeping?stages[r.value]:r.value,unit:names[key][1],y:sleeping?10+labels.indexOf(r.value)*24:y(r.value),color:sleeping?colors[r.value]:'#16867d'}]});
    }
    target.append(g);
    if(window.attachChartCursor)window.attachChartCursor(target,g,points.sort((a,b)=>a.x-b.x),{viewWidth:W,top:5,bottom:165});
    if(sleeping)target.append(node('p',labels.map(k=>stages[k]).join(' ／ ')+'（上から順）'));
  }
  function render(){
    const source=$('ah-source').value,selected=rows.filter(r=>r.source===source),stats=summarize(selected,...range);
    const cards=$('ah-cards');cards.replaceChildren();
    for(const [key,{list,value}] of Object.entries(stats)){
      const card=node('article');card.className='card';card.append(node('h3',names[key][0]),node('strong',value),node('p',`${list.length}件`));
      if(['heart_rate','oxygen_saturation','sleep'].includes(key)){const graph=node('div');graph.className='ah-plot';plot(graph,list,key);card.append(graph);}
      const detail=node('details');detail.append(node('summary','記録の詳細'));const body=node('div');detail.append(body);let shown=0;
      const more=node('button','さらに100件');more.type='button';
      function page(){more.remove();for(const r of list.slice(shown,shown+100)){body.append(node('p',`${time(r.start_at)}〜${time(r.end_at)}：${key==='sleep'?stages[r.value]:fmt(r.value/(key==='walking_running_distance'?1000:1))+' '+names[key][1]}`));}shown+=100;if(shown<list.length)body.append(more);}
      more.addEventListener('click',page);detail.addEventListener('toggle',()=>{if(detail.open && !shown)page();});card.append(detail);cards.append(card);
    }
  }
  async function load(){
    const ticket=++serial;rows=[];$('ah-cards').replaceChildren();$('ah-source').replaceChildren();
    const user=$('ah-user').value,day=$('ah-day').value;
    if(!user||!day){$('ah-status').textContent='利用者と日付を選択してください';return;}
    range=bounds(day);const localRange=[...range];$('ah-status').textContent='読み込み中…';
    try {
      const all=[];let total=0;
      do {
        const p=new URLSearchParams({user_id:user,since:new Date(localRange[0]).toISOString(),until:new Date(localRange[1]).toISOString(),limit:'1000',offset:String(all.length)});
        const response=await fetch('/api/apple-health/samples?'+p);const data=await response.json();if(!response.ok)throw Error(data.error||'取得に失敗しました');if(ticket!==serial)return;
        total=data.total;if(total>50000)throw Error('この日は50,000件を超えるため表示できません。合計は表示しません。');
        if(!data.samples.length && all.length<total)throw Error('取得中に記録が変わりました。再読み込みしてください。');all.push(...data.samples);
      }while(all.length<total);
      rows=all;const sources=[...new Set(rows.map(r=>r.source))];for(const s of sources){const option=node('option',s==='unknown'?'記録元不明（unknown）':s);option.value=s;$('ah-source').append(option);}
      $('ah-status').textContent=`${rows.length}件取得・${new Date().toLocaleTimeString('ja-JP')} 更新。表示時刻：${Intl.DateTimeFormat().resolvedOptions().timeZone}`;render();
    }catch(e){if(ticket===serial){rows=[];$('ah-cards').replaceChildren();$('ah-status').textContent=e.message;}}
  }
  const today=new Date();$('ah-day').value=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
  $('ah-refresh').addEventListener('click',load);$('ah-user').addEventListener('change',load);$('ah-day').addEventListener('change',load);$('ah-source').addEventListener('change',render);
  fetch('/api/users').then(async r=>{if(!r.ok)throw Error('利用者を取得できません');return r.json();}).then(users=>{for(const u of users){const o=node('option',u.name);o.value=u.id;$('ah-user').append(o);}if(users.length===1)$('ah-user').value=users[0].id;load();}).catch(e=>{$('ah-status').textContent=e.message;});
})();
