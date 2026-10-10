
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let CFG={}, SELREPO=null, TAGS=[], sPage=1, tPage=1, CHK=new Set(), SRC='mirror';
const esc=s=>String(s==null?'':s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
const fmt=b=>b>1073741824?(b/1073741824).toFixed(2)+' GB':b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';
const nfmt=n=>n>1e8?(n/1e8).toFixed(1)+'亿':n>1e4?(n/1e4).toFixed(1)+'万':String(n);
const api=async(p,o)=>{const r=await fetch(p,o);const j=await r.json(); if(j.error) throw new Error(j.error); return j;};
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});

function toggle(id){const e=$('#'+id); e.style.display = e.style.display==='none'?'':'none'}

async function loadCfg(){
  const d=await api('/api/config'); CFG=d.config;
  $('#c_api').value=CFG.api_base; $('#c_reg').value=CFG.registry; $('#c_mode').value=CFG.registry_mode;
  SRC = CFG.use_mirror!==false ? 'mirror' : 'official';
  paintSource();
  $('#c_proxy').value=CFG.proxy; $('#c_user').value=CFG.username; $('#c_pass').value=CFG.password;
  $('#c_pscope').value=CFG.proxy_scope||'auto';
  $('#c_os').value=CFG.os; $('#c_arch').value=CFG.arch; $('#c_var').value=CFG.variant||'';
  $('#c_gz').value=CFG.gzip?'1':'0'; $('#c_out').value=CFG.outdir;
  $('#c_insecure').checked=!!CFG.insecure; $('#c_suffix').checked=!!CFG.tag_suffix;
  $('#c_retries').value=CFG.retries; $('#c_rdelay').value=CFG.retry_delay;
  $('#outDir').textContent=CFG.outdir;
  drawArch(); drawSrc();
}

function sourceToggle(){
  $('#mirrorFields').style.display = (SRC==='mirror')?'':'none';
}
function paintSource(){
  $('#srcMirror').classList.toggle('active', SRC==='mirror');
  $('#srcOfficial').classList.toggle('active', SRC==='official');
  sourceToggle();
}
function setSource(v){ SRC=v; paintSource(); saveCfg(); }

function collectCfg(){return {
  api_base:$('#c_api').value.trim(),
  registry:$('#c_reg').value.trim(),
  registry_mode:$('#c_mode').value,
  use_mirror: SRC==='mirror',
  proxy:$('#c_proxy').value.trim(),
  proxy_scope:$('#c_pscope').value,
  username:$('#c_user').value.trim(),
  password:$('#c_pass').value,
  os:$('#c_os').value, arch:$('#c_arch').value, variant:$('#c_var').value,
  gzip:$('#c_gz').value==='1',
  outdir:$('#c_out').value.trim(),
  insecure:$('#c_insecure').checked,
  tag_suffix:$('#c_suffix').checked,
  retries:(parseInt($('#c_retries').value||'0',10)||0),
  retry_delay:(parseFloat($('#c_rdelay').value||'0')||0)
};}

async function saveCfg(close){
  try{
    const d=await post('/api/config',collectCfg());
    CFG=d.config; $('#cfgMsg').textContent='已保存 '+new Date().toLocaleTimeString();
    drawArch(); drawSrc();
    if(close) toggle('setBox');
  }catch(e){ $('#cfgMsg').textContent='保存失败: '+e.message; }
}

// 设置项 change 即保存，不再有「改了没点保存」的坑
function setupAutosave(){
  ['c_api','c_reg','c_mode','c_proxy','c_pscope','c_user','c_pass',
   'c_os','c_arch','c_var','c_gz','c_out','c_retries','c_rdelay'].forEach(id=>{
    const e=$('#'+id); if(e) e.addEventListener('change',()=>saveCfg());
  });
  ['c_insecure','c_suffix'].forEach(id=>{
    const e=$('#'+id); if(e) e.addEventListener('change',()=>saveCfg());
  });
}

function drawArch(){
  const v=CFG.variant?('/'+CFG.variant):'';
  const t=CFG.os+'/'+CFG.arch+v;
  $$('#curArch').forEach(e=>e.textContent=t);
}
function drawSrc(){
  const on=CFG.use_mirror!==false;
  const label=on ? (CFG.registry||'docker.1ms.run')+'（镜像站）' : 'registry-1.docker.io（官方）';
  $('#srcChip').textContent='拉取源：'+label;
  $('#curSrc').textContent=label;
}

async function testCfg(){
  $('#testMsg').textContent='测试中…';
  try{
    const d=await post('/api/test',collectCfg());
    $('#testMsg').innerHTML=d.ok?'<span style="color:#3fb950">连通 OK</span> '+esc(d.detail):'<span style="color:#f85149">失败</span> '+esc(d.detail);
  }catch(e){$('#testMsg').innerHTML='<span style="color:#f85149">'+esc(e.message)+'</span>';}
}

async function doSearch(p){
  const q=$('#q').value.trim(); if(!q) return;
  sPage=p; $('#sMsg').textContent='搜索中…';
  try{
    const d=await api('/api/search?q='+encodeURIComponent(q)+'&page='+p);
    $('#sMsg').textContent=`命中 ${d.count}，第 ${d.page} 页`;
    $('#sRows').innerHTML=d.results.map(r=>`<tr data-ref="${esc(r.pull_ref)}">
      <td class="mono">${esc(r.pull_ref)} ${r.official?'<span class="chip on">官方</span>':''}</td>
      <td class="dim">★${nfmt(r.stars)}<br>${nfmt(r.pulls)}</td>
      <td class="dim">${esc(r.desc.slice(0,110))}</td>
      <td><button class="mini" onclick="pickRepo('${esc(r.pull_ref)}')">选</button></td></tr>`).join('');
    $('#moreS').style.display = d.results.length ? '' : 'none';
  }catch(e){$('#sRows').innerHTML=`<tr><td colspan="4" style="color:#f85149">${esc(e.message)}</td></tr>`; $('#sMsg').textContent='';}
}
function pickRepo(ref,keep){
  SELREPO=ref; CHK.clear(); $('#tRepo').textContent='→ '+ref; if(!keep) $('#tFilter').value=''; loadTags(1);
  $$('#sRows tr').forEach(tr=>tr.classList.toggle('sel', tr.dataset.ref===ref));
  const box=document.querySelector('#tRows').closest('.scroll'); if(box) box.scrollTop=0;
}
const VRE=/^v?(\d+(?:\.\d+){1,3})(?![\d.])/;
const aliasTag=n=>!VRE.test(n);
const vkey=n=>{const m=n.match(VRE); if(!m) return -1; const p=m[1].split('.').map(Number);
  while(p.length<3)p.push(0); return p[0]*1e6+p[1]*1e3+p[2]};
function sortByVersion(list){
  return list.slice().sort((a,b)=>{
    const av=vkey(a.tag), bv=vkey(b.tag);
    if((av<0)!==(bv<0)) return av<0?1:-1;
    if(av!==bv) return bv-av;
    return a.tag.localeCompare(b.tag);
  });
}

async function loadTags(p){
  if(!SELREPO) return;
  tPage=p;
  const ord=$('#tOrder').value, base=`/api/tags?repo=${encodeURIComponent(SELREPO)}&filter=${encodeURIComponent($('#tFilter').value.trim())}`;
  try{
    if(ord==='version_desc'){
      $('#tMsg').textContent='读取版本（默认载入最近 3 页，按版本号排序）…';
      const rs=await Promise.all([1,2,3].map(pg=>api(base+`&page=${pg}&ordering=last_updated`).catch(()=>null)));
      const ok=rs.filter(r=>r&&r.results);
      const cnt=(ok[0]||{}).count||0;
      TAGS=[].concat(...ok.map(r=>r.results));
      const nAlias=TAGS.filter(t=>aliasTag(t.tag)).length;
      TAGS=sortByVersion(TAGS);
      $('#tMsg').textContent=`共 ${cnt} 个 tag · 已载入 ${TAGS.length} 个并按版本号排序（其中别名 ${nAlias} 个沉底）；要看更早的版本用过滤框，如 1.27`;
      $('#moreT').style.display='none';
    }else{
      $('#tMsg').textContent='读取版本…';
      const d=await api(base+`&page=${p}&ordering=${ord}`);
      TAGS = p===1? d.results : TAGS.concat(d.results);
      $('#tMsg').textContent=`共 ${d.count} 个 tag，已载入 ${TAGS.length}`;
      $('#moreT').style.display = d.results.length ? '' : 'none';
    }
    renderTags();
  }catch(e){$('#tRows').innerHTML=`<tr><td colspan="4" style="color:#f85149">${esc(e.message)}</td></tr>`; $('#tMsg').textContent='';}
}
function renderTags(){
  const fa=$('#tArch').value;
  $('#tRows').innerHTML=TAGS.map((t,i)=>{
    const chips=t.platforms.filter(p=>!fa||p.arch===fa).map(p=>
      `<span class="chip ${p.arch===CFG.arch&&(p.variant||'')===(CFG.variant||'')?'on':''}" title="${p.os}/${p.arch}${p.variant?'/'+p.variant:''}">${p.arch}${p.variant?'/'+p.variant:''} ${fmt(p.size)}</span>`).join('');
    const has=t.platforms.some(p=>p.arch===CFG.arch && (!CFG.variant || (p.variant||'')===CFG.variant));
    return `<tr><td><input type="checkbox" style="width:auto" data-i="${i}" ${CHK.has(t.tag)?'checked':''} ${has?'':'disabled'}></td>
      <td class="mono">${esc(t.tag)}${aliasTag(t.tag)?' <span class="hint">别名</span>':''}</td><td class="dim">${esc(t.updated.slice(0,10))}</td>
      <td>${chips||'<span class="dim">无跨平台信息</span>'}
        ${has?'':`<span class="chip" style="color:#f85149">无 ${esc(CFG.arch)}</span>`}
        <button class="mini" style="margin-left:6px" ${has?'':'disabled'} onclick="queueOne(${i})">拉取 ${esc(CFG.arch)}</button></td></tr>`;
  }).join('')||'<tr><td colspan="4" class="hint">没有匹配的 tag</td></tr>';
  $$('#tRows input[type=checkbox]').forEach(c=>c.onchange=()=>{const tg=TAGS[+c.dataset.i].tag; c.checked?CHK.add(tg):CHK.delete(tg); $('#selMsg').textContent=CHK.size?`已勾选 ${CHK.size} 个` : ''});
}
function split(ref){ // 返回 [registry_host, repository]；host 为 null 表示 Docker Hub
  ref=ref.replace(/^library\//,'');
  let host=null, r=ref;
  if(ref.includes('/') && (ref.split('/')[0].includes('.')||ref.split('/')[0].includes(':'))){host=ref.split('/')[0]; r=ref.split('/').slice(1).join('/')}
  else r = ref.includes('/')?ref:('library/'+ref);
  if(CFG.registry_mode==='all') host=null;
  return [host, r];
}
function item(tag){const [host,repo]=split(SELREPO); return {repo, tag, display:SELREPO.replace(/^library\//,''),
  os:CFG.os, arch:CFG.arch, variant:CFG.variant||'', host_override:host||''};}
async function queueOne(i){ await push([item(TAGS[i].tag)]); }
async function queueChecked(){
  if(!CHK.size) return alert('先勾选 tag（没有对应架构的会置灰）');
  const items=TAGS.filter(t=>CHK.has(t.tag)).map(t=>item(t.tag));
  CHK.clear(); await push(items);
}
async function push(items){
  const d=await post('/api/queue',{items});
  $('#qMsg').textContent=`已入队 ${d.ids.length} 个`+(d.note?('，'+d.note):'');
  tick();
}
async function parseCompose(){
  const p=$('#bCompose').value.trim(); if(!p) return alert('填 compose 文件路径');
  try{const d=await api('/api/compose?path='+encodeURIComponent(p));
    $('#bList').value=d.images.join('\n'); $('#bMsg').textContent=`读到 ${d.images.length} 个镜像`;}
  catch(e){$('#bMsg').innerHTML='<span style="color:#f85149">'+esc(e.message)+'</span>'}
}
async function queueBatch(){
  const lines=$('#bList').value.split('\n').map(s=>s.trim()).filter(s=>s&&!s.startsWith('#'));
  if(!lines.length) return alert('列表是空的');
  const items=[];
  for(const line of lines){
    let ref=line, tag='latest';
    if(ref.split('/').pop().includes(':')){const i=ref.lastIndexOf(':'); tag=ref.slice(i+1); ref=ref.slice(0,i)}
    const [host,repo]=split(ref);
    items.push({repo, tag, display:ref.replace(/^library\//,''), os:CFG.os, arch:CFG.arch, variant:CFG.variant||'', host_override:host||''});
  }
  await push(items);
}
async function tick(){
  try{
    const d=await api('/api/jobs');
    $('#jobs').innerHTML=d.jobs.map(j=>{
      const p=j.prog, pct=p&&p.total?Math.min(100,Math.round(p.done*100/p.total)):0;
      const active=j.status==='queued'||j.status==='running';
      return `<div class="job ${active?'':'settled'}">
        <div class="jhead"><b>${esc(j.title)}</b> <span class="tag ${j.status}">${j.status}</span>
          ${j.result?`<span class="hint">${esc(j.result.file)} · ${fmt(j.result.bytes)}</span>`:''}
          <span class="jsp"></span>
          ${active?`<button class="mini" onclick="cancelOne('${j.id}')">取消</button>`:''}
          ${!active?`<button class="mini" onclick="retryOne('${j.id}')">重试</button>`:''}
        </div>
        ${p?`<div class="hint">层 ${p.layer}/${p.layers} · ${fmt(p.done)}/${fmt(p.total)} · ${fmt(p.speed)}/s</div><div class="bar"><i style="width:${pct}%"></i></div>`:''}
        ${j.result?`<div class="hint">${esc(j.result.platform)} · 镜像名 ${esc(j.result.tag_in_archive)} · sha256 ${esc(j.result.sha256.slice(0,12))}…</div>`:''}
        ${j.error?`<div class="err">${esc(j.error)}</div>`:''}
        ${j.log.length?`<details class="jlog"><summary>日志</summary><pre>${esc(j.log.slice(-30).join('\n'))}</pre></details>`:''}
      </div>`;
    }).join('')||'<span class="hint">暂无任务</span>';
    $('#retryAllBtn').style.display = d.jobs.some(j=>j.status==='failed') ? '' : 'none';
    $('#cancelAllBtn').style.display = d.jobs.some(j=>j.status==='queued'||j.status==='running') ? '' : 'none';
    $('#clearDoneBtn').style.display = d.jobs.some(j=>j.status!=='queued'&&j.status!=='running') ? '' : 'none';
    const f=await api('/api/files');
    $('#files').innerHTML=f.files.map(x=>`<div class="row frow">
      <span class="fname" title="${esc(x.name)}">${esc(x.name)}</span>
      <span class="dim">${fmt(x.bytes)}</span>
      <span class="actions">
        <a href="/api/download?f=${encodeURIComponent(x.name)}">下载</a>
        <button class="mini" onclick="copyLoad('${esc(x.name)}')">复制 load 命令</button>
        <a href="#" onclick="del('${esc(x.name)}');return false">删除</a>
      </span></div>`).join('')||'<span class="hint">无</span>';
  }catch(e){}
}
async function clearDone(){ const d=await post('/api/clear',{}); $('#qMsg').textContent=`已清除 ${d.n} 个已结束任务`; tick(); }
let _toast;
function flash(msg){
  if(!_toast){_toast=document.createElement('div'); _toast.className='toast'; document.body.appendChild(_toast);}
  _toast.textContent=msg; _toast.classList.add('show');
  clearTimeout(_toast._t); _toast._t=setTimeout(()=>_toast.classList.remove('show'),2200);
}
async function copyLoad(name){
  const cmd='docker load -i '+name;
  try{ await navigator.clipboard.writeText(cmd); flash('已复制：'+cmd); }
  catch(e){
    const ta=document.createElement('textarea'); ta.value=cmd; document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); flash('已复制：'+cmd); }catch(_){ prompt('复制：', cmd); }
    ta.remove();
  }
}
async function cancelOne(id){ await post('/api/cancel',{id}); $('#qMsg').textContent='已请求取消'; tick(); }
async function cancelAll(){ const d=await post('/api/cancel',{}); $('#qMsg').textContent=`已取消 ${d.n} 个排队/运行中的任务`; tick(); }
async function retryOne(id){ await post('/api/retry',{id}); $('#qMsg').textContent='已重试'; tick(); }
async function retryAll(){ const d=await post('/api/retry',{}); $('#qMsg').textContent=`已重试 ${d.n} 个失败任务`; tick(); }
async function del(f){if(!confirm('删除 '+f+' ?'))return; await post('/api/delete',{file:f}); tick();}

loadCfg().then(()=>{
  setupAutosave();
  const P=new URLSearchParams(location.search);
  if(P.get('set')) toggle('setBox');
  (async()=>{
    if(P.get('q')){ $('#q').value=P.get('q'); await doSearch(1); }
    if(P.get('repo')){ if(P.get('filter')) $('#tFilter').value=P.get('filter'); pickRepo(P.get('repo'),true); }
  })();
});
tick(); setInterval(tick,1200);
