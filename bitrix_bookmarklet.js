(async()=>{
const C=__CFG__;
if(!window.BX||!BX.bitrix_sessid){alert('Hãy bấm nút này khi đang mở một trang Bitrix24 (đã đăng nhập).');return;}
const box=document.createElement('div');
box.style.cssText='position:fixed;z-index:2147483647;top:16px;right:16px;width:380px;padding:16px;background:white;color:rgb(34,34,34);border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.25);font:14px/1.5 sans-serif;white-space:pre-line';
document.body.appendChild(box);
const say=t=>{box.innerText='📥 Cập nhật Bitrix → Dashboard\n'+t;};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const enc=(fd,k,v)=>{if(v!==null&&typeof v==='object'){for(const i in v)enc(fd,k+'['+i+']',v[i]);}else fd.append(k,v);};
const call=async(m,p)=>{for(let a=0;a<6;a++){const fd=new URLSearchParams();fd.append('sessid',BX.bitrix_sessid());for(const k in p)enc(fd,k,p[k]);const r=await fetch('/rest/'+m+'.json',{method:'POST',body:fd,credentials:'same-origin'});const d=await r.json();if(d.error==='QUERY_LIMIT_EXCEEDED'){await sleep(1500);continue;}if(d.error)throw new Error(m+': '+(d.error_description||d.error));return d;}throw new Error('Bitrix24 đang quá tải, thử lại sau ít phút.');};
const all=async(m,p,label)=>{let out=[],s=0;for(;;){const d=await call(m,Object.assign({},p,{start:s}));out=out.concat(d.result||[]);say('Đang đọc '+label+': '+out.length+(d.total?' / '+d.total:''));if(d.next===undefined)break;s=d.next;await sleep(300);}return out;};
try{
const now=new Date();const f=new Date(now.getFullYear(),now.getMonth()-C.months+1,1);
const from=f.getFullYear()+'-'+String(f.getMonth()+1).padStart(2,'0')+'-01';
const iso=from+'T00:00:00+07:00';
const agg={},stages={};
const add=(u,day,i,v)=>{agg[u]=agg[u]||{};agg[u][day]=agg[u][day]||[0,0,0,0];agg[u][day][i]+=v;};
say('Đang đọc danh sách stage…');
const sts=(await call('crm.status.list',{filter:{ENTITY_ID:'STATUS'}})).result||[];
const code={};const missing=[];
for(const n of C.stages){const s=sts.find(x=>String(x.NAME).trim().toLowerCase()===n.toLowerCase()||String(x.STATUS_ID).toLowerCase()===n.toLowerCase());if(s){code[n]=s.STATUS_ID;}else{missing.push(n);}}
for(const [u,name] of C.users){
const L=await all('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,'>=DATE_CREATE':iso},select:['ID','DATE_CREATE'],order:{ID:'ASC'}},'lead mới của '+name);
L.forEach(x=>add(u,String(x.DATE_CREATE).slice(0,10),0,1));
const D=await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,'>=DATE_CREATE':iso},select:['ID','DATE_CREATE'],order:{ID:'ASC'}},'deal mới của '+name);
D.forEach(x=>add(u,String(x.DATE_CREATE).slice(0,10),1,1));
const W=await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'S','>=CLOSEDATE':iso},select:['ID','CLOSEDATE','OPPORTUNITY'],order:{ID:'ASC'}},'deal thắng của '+name);
W.forEach(x=>{const day=String(x.CLOSEDATE).slice(0,10);add(u,day,2,1);add(u,day,3,parseFloat(x.OPPORTUNITY)||0);});
stages[u]={};
for(const n in code){say('Đang đếm lead theo stage của '+name+': '+n);const d=await call('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,STATUS_ID:code[n]},select:['ID'],start:0});stages[u][n]=d.total||0;await sleep(250);}
}
const data={v:1,at:Date.now(),from:from,portal:location.host,agg:agg,stages:stages,missing:missing,allStages:sts.map(x=>x.NAME)};
const b64=btoa(unescape(encodeURIComponent(JSON.stringify(data)))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
say('✅ Đã đọc xong dữ liệu Bitrix24 từ '+from.split('-').reverse().join('/')+'.\nBấm nút dưới để gửi sang dashboard.');
const btn=document.createElement('button');btn.innerText='📤 Gửi sang dashboard';btn.style.cssText='margin-top:10px;padding:8px 14px;background:rgb(42,120,214);color:white;border:0;border-radius:6px;font-weight:600;cursor:pointer';
btn.onclick=()=>{try{navigator.clipboard.writeText(b64);}catch(e){}window.open(C.url+'?bx='+b64,'_blank');box.remove();};
const x=document.createElement('button');x.innerText='Đóng';x.style.cssText='margin:10px 0 0 10px;padding:8px 14px;background:white;border:1px solid rgb(200,200,200);border-radius:6px;cursor:pointer';x.onclick=()=>box.remove();
box.appendChild(btn);box.appendChild(x);
}catch(e){say('❌ Lỗi: '+e.message);const x=document.createElement('button');x.innerText='Đóng';x.onclick=()=>box.remove();box.appendChild(x);}
})();
