'use strict';
const WELLNESS_FIELDS=['height_cm','age','sex','goal_weight_kg','goal_date','bp_sys_low','bp_sys_high','bp_dia_low','bp_dia_high','bmi_low','bmi_high','plan_text'];
async function refreshWellness(){
 const userId=$('current-user').value;
 $('wellness-empty').hidden=!!userId;$('wellness-content').hidden=!userId;
 if(!userId){wellnessProfile=null;wellnessProfileUser=null;renderBmi();renderCharts();return;}
 const [summary,meals,energy]=await Promise.all([api('/api/wellness/summary?user_id='+encodeURIComponent(userId)),api('/api/wellness/meals?user_id='+encodeURIComponent(userId)),api('/api/wellness/energy?user_id='+encodeURIComponent(userId))]);
 if(userId!==$('current-user').value)return;
 wellnessProfile=summary.profile;wellnessProfileUser=userId;renderBmi();const form=$('wellness-form');
 for(const key of WELLNESS_FIELDS)form.elements[key].value=summary.profile[key]??'';
 renderCharts();renderEnergy(energy,summary);
 const rows={'現在の体重':summary.latest_weight==null?'—':summary.latest_weight+' kg',
  'BMI（身長から算出）':summary.bmi??'身長・体重を設定してください',
  '推定基礎代謝':summary.estimated_bmr_kcal==null?'身長・年齢・計算条件・体重を設定してください':summary.estimated_bmr_kcal+' kcal/日',
  '直近の血圧':summary.latest_pressure?`${summary.latest_pressure.systolic} / ${summary.latest_pressure.diastolic} mmHg`:'—',
  '7日以上の体重変化':summary.weight_change_7d_kg==null?'比較できる記録がありません':`${summary.weight_change_7d_kg>0?'+':''}${summary.weight_change_7d_kg} kg`,
  '目標':summary.goal_weight_kg==null?'未設定':`${summary.goal_weight_kg} kg${summary.profile.goal_date?'（'+summary.profile.goal_date+'まで）':''}`,
  '目標への週あたりの変化':summary.weekly_change_needed_kg==null?'—':`${summary.weekly_change_needed_kg} kg/週の減量`};
 $('wellness-summary').replaceChildren();for(const [label,value] of Object.entries(rows))$('wellness-summary').append(element('dt',label),element('dd',String(value)));
 $('wellness-notices').replaceChildren();for(const notice of summary.notices)$('wellness-notices').append(element('p',notice,'notice'));if(!summary.notices.length)$('wellness-notices').append(element('p','現在の設定で追加の見直し通知はありません。','notice'));
 $('meal-list').replaceChildren();for(const m of meals.slice(0,20)){const row=element('div',undefined,'meal-row');row.append(element('span',dateText(m.created_at)+'　'+m.note+(m.calories===null?'':'　約'+m.calories+' kcal')));const remove=element('button','削除','danger');remove.type='button';remove.addEventListener('click',guarded(async()=>{if(!confirm('この食事メモを削除しますか？'))return;await api('/api/wellness/meals',{user_id:userId,id:m.id},'DELETE');await refreshWellness();message('食事メモを削除しました');}));row.append(remove);$('meal-list').append(row);}
}
$('wellness-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('wellness-form'),profile={};
 for(const key of WELLNESS_FIELDS){const v=f.elements[key].value;profile[key]=['height_cm','age','goal_weight_kg'].includes(key)?(v?Number(v):null):key.startsWith('bp_')||key.startsWith('bmi_')?Number(v):v;}
 await api('/api/wellness/profile',{user_id,profile});await refreshWellness();message('健康設定と計画を保存しました');
}));
$('meal-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('meal-form');await api('/api/wellness/meals',{user_id,meal:{note:f.elements.note.value,calories:f.elements.calories.value?Number(f.elements.calories.value):null}});f.reset();await refreshWellness();message('食事メモを保存しました');
}));
$('coach-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('coach-form');const result=await api('/api/ai/consult',{user_id,mode:f.elements.mode.value,question:f.elements.question.value});
 $('coach-answer').textContent=result.answer;$('coach-answer').hidden=false;$('coach-plan').hidden=false;message('相談への回答を受け取りました');
}));
$('coach-plan').addEventListener('click',()=>{const field=$('wellness-form').elements.plan_text;field.value=$('coach-answer').textContent.slice(0,8000);field.focus();message('計画欄へコピーしました。内容を確認して保存してください');});
async function refreshAiSettings(){const config=await api('/api/ai/settings');const f=$('ai-form');for(const key of ['provider','base_url','model'])f.elements[key].value=config[key];$('ai-status').textContent=config.key_configured?'APIキーを保存済み':'この接続先のAPIキーは未設定です';$('coach-provider').textContent='相談先: '+config.provider+(config.provider==='openrouter'?'（外部サービス）':'（ローカル）');}
$('ai-form').addEventListener('submit',guarded(async()=>{const f=$('ai-form');await api('/api/ai/settings',{provider:f.elements.provider.value,base_url:f.elements.base_url.value.trim(),model:f.elements.model.value.trim()});await refreshAiSettings();message('AIの接続先を保存しました');}));
$('ai-key-form').addEventListener('submit',guarded(async()=>{const f=$('ai-key-form');await api('/api/ai/key',{provider:$('ai-form').elements.provider.value,key:f.elements.key.value});f.reset();await refreshAiSettings();message('APIキーを更新しました');}));
$('ai-form').elements.provider.addEventListener('change',()=>{const f=$('ai-form');const p=f.elements.provider.value;if(p==='qnapassistant')f.elements.base_url.value='http://127.0.0.1:11435/v1';else if(p==='openrouter')f.elements.base_url.value='https://openrouter.ai/api/v1';else f.elements.base_url.value='';f.elements.model.value=p==='openrouter'?'openai/gpt-4o-mini':'auto';});
refreshAiSettings().catch(e=>message(e.message,true));

$('ai-test').addEventListener('click',guarded(async()=>{
 $('ai-test-result').textContent='接続確認中…（最大5分）';
 try{const r=await api('/api/ai/test',{});$('ai-test-result').textContent='接続成功：'+r.answer;}
 catch(error){$('ai-test-result').textContent=error.message;throw error;}
}));

let energyData=null;
const activityNumbers=['pal','steps','minutes','total_kcal','intake_kcal','target_kcal'];
const planNumbers=['start_weight','goal_weight','pal','deficit_kcal','target_kcal','adaptation_pct'];
const energyValue=(v,unit='')=>v==null?'—':v+unit;
function energyPlot(id,series,unit){
 const container=$(id);container.replaceChildren();
 const points=series.flatMap(s=>s.points).filter(p=>p.value!=null&&Number.isFinite(p.value));
 if(!points.length){container.append(element('p','表示できる記録がありません。','muted'));return;}
 const ns='http://www.w3.org/2000/svg';const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 480 230');svg.setAttribute('role','img');svg.setAttribute('aria-label',series.map(s=>s.name).join('・')+'（'+unit+'）');
 const make=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;};
 const times=points.map(p=>Date.parse(p.date+'T00:00:00Z')),lo=points.reduce((a,p)=>Math.min(a,p.value),Infinity),hi=points.reduce((a,p)=>Math.max(a,p.value),-Infinity);
 const xmin=times.reduce((a,t)=>Math.min(a,t),Infinity),xmax=times.reduce((a,t)=>Math.max(a,t),-Infinity),pad=Math.max((hi-lo)*.1,unit==='kg'?1:100),min=lo-pad,max=hi+pad;
 const x=d=>52+(Date.parse(d+'T00:00:00Z')-xmin)/Math.max(86400000,xmax-xmin)*414,y=v=>190-(v-min)/(max-min)*170;
 for(let i=0;i<4;i++){const v=min+(max-min)*i/3;make('line',{x1:52,x2:466,y1:y(v),y2:y(v),stroke:'currentColor',opacity:.15});make('text',{x:46,y:y(v)+4,'text-anchor':'end',fill:'currentColor','font-size':12},unit==='kg'?v.toFixed(1):Math.round(v));}
 for(const s of series){const usable=s.points.filter(p=>p.value!=null&&Number.isFinite(p.value));if(!usable.length)continue;
 make('polyline',{points:usable.map(p=>x(p.date)+','+y(p.value)).join(' '),fill:'none',stroke:s.color,'stroke-width':2,'stroke-dasharray':s.dash?'5 4':'none'});
 for(const p of usable.filter((_,i)=>i%Math.max(1,Math.ceil(usable.length/400))===0)){const n=make('circle',{cx:x(p.date),cy:y(p.value),r:2.5,fill:s.color});const t=document.createElementNS(ns,'title');t.textContent=p.date+' '+s.name+' '+p.value+' '+unit;n.append(t);}}
 make('text',{x:52,y:217,fill:'currentColor','font-size':12},new Date(xmin).toISOString().slice(0,10));make('text',{x:466,y:217,'text-anchor':'end',fill:'currentColor','font-size':12},new Date(xmax).toISOString().slice(0,10));container.append(svg);
 const legend=element('div',undefined,'energy-legend');for(const s of series){const item=element('span',(s.dash?'┄ ':'● ')+s.name);item.style.color=s.color;legend.append(item);}container.append(legend);
}
function drawEnergy(){
 if(!energyData)return;const data=energyData,range=$('energy-range').value,cutoff=range==='all'?'':new Date(Date.parse(data.today+'T00:00:00Z')-Number(range)*86400000).toISOString().slice(0,10);
 const h=data.history.filter(x=>x.date>=cutoff);energyPlot('energy-chart',[
 {name:'推定基礎代謝',color:'var(--chart-primary)',key:'bmr'},{name:'総消費',color:'var(--chart-secondary)',key:'total_kcal'},
 {name:'摂取実績',color:'var(--energy-intake)',key:'intake_kcal'},{name:'摂取目標',color:'var(--energy-target)',key:'target_kcal',dash:true}
 ].map(s=>({...s,points:h.map(x=>({date:x.date,value:x[s.key]}))})),'kcal/日');
 const p=data.active_plan;const series=[{name:'実測（日平均）',color:'var(--chart-primary)',points:h.filter(x=>x.weight!=null).map(x=>({date:x.date,value:x.weight}))}];
 if(p){series.push({name:'目標の計画線',color:'var(--energy-target)',dash:true,points:[{date:p.start_date,value:p.start_weight},{date:p.goal_date,value:p.goal_weight}]},{name:'代謝変化を含む試算',color:'var(--chart-secondary)',dash:true,points:data.projection.map(x=>({date:x.date,value:x.weight}))});}
 energyPlot('weight-plan-chart',series,'kg');
}
function fillActivity(entry){const f=$('activity-form');f.reset();f.elements.date.value=entry.date;for(const k of [...activityNumbers,'note'])if(entry[k]!=null){if(k==='pal'&&![...f.elements.pal.options].some(o=>o.value===String(entry[k])))f.elements.pal.add(new Option('設定値（'+entry[k]+'）',String(entry[k])));f.elements[k].value=entry[k];}}
function renderEnergy(data,summary){
 energyData=data;const current=data.current,stats=$('energy-today');stats.replaceChildren();
 for(const [label,value]of [['推定基礎代謝',current.bmr],['今日の総消費',current.total_kcal],['今日の摂取目標',current.target_kcal]]){const cell=element('div');cell.append(element('span',label),element('strong',energyValue(value,' kcal')));stats.append(cell);}
 $('energy-basis').textContent='体重の基準日：'+(current.weight_date||'未記録')+'／活動係数 '+current.pal+'。履歴の推定は現在の身体情報で再計算します。入力のない日は活動係数による目安です。';
 $('energy-notices').replaceChildren(...data.notices.map(t=>element('p',t,'notice')));
 fillActivity(data.diaries.find(x=>x.date===data.today)||{date:data.today,pal:current.pal});$('activity-status').textContent='同じ日の保存はその日の活動記録を更新します。';
 const list=$('activity-list');list.replaceChildren();for(const entry of [...data.diaries].reverse().slice(0,90)){const row=element('div',undefined,'energy-log');const h=data.history.find(x=>x.date===entry.date);row.append(element('strong',entry.date),element('span',`歩数 ${energyValue(entry.steps)} · 運動 ${energyValue(entry.minutes,'分')} · 総消費 ${energyValue(h?.total_kcal,' kcal')}（${h?.total_source||'—'}） · 摂取 ${energyValue(entry.intake_kcal,' kcal')} · 収支 ${energyValue(h?.balance_kcal,' kcal')}`),element('span',entry.note));
 const actions=element('div',undefined,'row');const edit=element('button','編集','secondary');edit.type='button';edit.onclick=()=>{fillActivity(entry);$('activity-form').closest('details').open=true;$('activity-form').scrollIntoView({block:'center'});};const remove=element('button','削除','danger');remove.type='button';const userId=$('current-user').value;remove.onclick=guarded(async()=>{if(!confirm(entry.date+'の活動記録を削除しますか？'))return;await api('/api/wellness/activity',{user_id:userId,date:entry.date},'DELETE');await refreshWellness();});actions.append(edit,remove);row.append(actions);list.append(row);}
 if(!data.diaries.length)list.append(element('p','活動記録はまだありません。','muted'));
 const p=data.active_plan,f=$('weight-plan-form');f.reset();f.elements.start_date.value=data.today;f.elements.start_weight.value=summary.latest_weight??'';
 if(p){for(const k of [...planNumbers,'goal_date'])f.elements[k].value=p[k]??'';}else{f.elements.goal_weight.value=summary.goal_weight_kg??'';f.elements.goal_date.value=summary.profile.goal_date||'';}
 // Editing always makes a revision anchored in today's measurements, not a rewrite of an old plan.
 f.elements.start_date.value=data.today;f.elements.start_weight.value=summary.latest_weight??p?.start_weight??'';f.elements.reason.required=!!p;
 for(const k of ['goal_weight_kg','goal_date'])$('wellness-form').elements[k].disabled=!!p;
 const review=$('weekly-review');review.replaceChildren();const elapsed=data.weeks.filter(w=>!w.future),recent=elapsed.slice(-4);const makeWeek=w=>{const row=element('div',undefined,'energy-week');row.append(element('strong',w.date.slice(5)+'〜'+w.end.slice(5)+(w.partial?'（途中）':'')),element('span','平均 '+energyValue(w.actual,' kg')+' / 計画 '+w.planned+' kg'),element('span','目標まで '+energyValue(w.remaining,' kg')+' · 計画差 '+(w.deviation>0?'+':'')+energyValue(w.deviation,' kg')),element('small',w.days+'日分の測定'+(w.days<3?'・少数のため参考':'')));return row;};
 if(!p)review.append(element('p','目標体重と日付を設定すると、週ごとの差を確認できます。','muted'));
 for(const w of recent)review.append(makeWeek(w));if(data.weeks.length>recent.length){const more=element('details');more.append(element('summary','全週の計画と実績'));for(const w of data.weeks)more.append(makeWeek(w));review.append(more);}
 const history=$('plan-history');history.replaceChildren();for(const v of [...data.plans].reverse()){const row=element('div',undefined,'energy-log');row.append(element('strong',dateText(v.created_at)),element('span',`${v.start_date} ${v.start_weight} kg → ${v.goal_date} ${v.goal_weight} kg`),element('span',`活動係数 ${v.pal} · 摂取 ${v.target_kcal==null?'消費−'+v.deficit_kcal:v.target_kcal} kcal · 追加低下仮定 ${v.adaptation_pct}%`),element('span',v.reason||'初回計画'));history.append(row);}drawEnergy();
}
$('energy-range').addEventListener('change',drawEnergy);
$('activity-load').addEventListener('click',()=>{if(!energyData)return;const date=$('activity-form').elements.date.value,entry=energyData.diaries.find(x=>x.date===date);fillActivity(entry||{date,pal:energyData.current.pal});$('activity-status').textContent=entry?'保存済みの記録を読み込みました':'この日は未記録です';});
$('activity-form').addEventListener('submit',guarded(async()=>{const f=$('activity-form'),entry={date:f.elements.date.value,note:f.elements.note.value};for(const k of activityNumbers)entry[k]=f.elements[k].value===''?null:Number(f.elements[k].value);await api('/api/wellness/activity',{user_id:$('current-user').value,entry});await refreshWellness();message('活動記録を保存しました');}));
$('weight-plan-form').addEventListener('submit',guarded(async()=>{const f=$('weight-plan-form'),entry={start_date:f.elements.start_date.value,goal_date:f.elements.goal_date.value,reason:f.elements.reason.value};for(const k of planNumbers)entry[k]=f.elements[k].value===''?null:Number(f.elements[k].value);await api('/api/wellness/weight-plan',{user_id:$('current-user').value,entry});await refreshWellness();message('減量計画と見直し履歴を保存しました');}));
$('plan-rebase').addEventListener('click',()=>{if(!energyData)return;const f=$('weight-plan-form'),weights=energyData.history.filter(x=>x.weight!=null);f.elements.start_date.value=energyData.today;f.elements.start_weight.value=weights.at(-1)?.weight??'';f.elements.reason.focus();});
