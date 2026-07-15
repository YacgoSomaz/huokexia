"""Self-contained same-origin admin console for the licensing server."""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>直播复盘侠 - 授权管理台</title>
<style>
:root{color-scheme:light;font-family:"Microsoft YaHei",system-ui,sans-serif;color:#14213d;background:#f3f7fc}
*{box-sizing:border-box}body{margin:0;min-width:1120px}.top{height:66px;background:#fff;border-bottom:1px solid #e5ecf7;display:flex;align-items:center;justify-content:space-between;padding:0 32px}.brand{font-size:19px;font-weight:800}.brand i{color:#3977ff;font-style:normal;margin-right:10px}.sub,.muted,.footer{font-size:12px;color:#8492aa}.auth{display:flex;align-items:center;gap:9px}.auth input{width:300px}.page{padding:24px 32px;max-width:1580px;margin:auto}.notice{background:#edf5ff;border:1px solid #cfe0ff;border-radius:8px;padding:12px 15px;color:#4d6386;font-size:13px;margin-bottom:16px}.grid{display:grid;grid-template-columns:390px 1fr;gap:16px}.card{background:#fff;border:1px solid #e5ecf7;border-radius:8px;padding:18px;box-shadow:0 4px 15px #1d3f762e}.card h2{font-size:16px;margin:0 0 15px;padding-left:10px;border-left:3px solid #3c8dff}.card h3{font-size:14px;margin:18px 0 9px}.field{margin:12px 0}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}.field span{font-size:12px;color:#8492aa;font-weight:400;margin-left:6px}input,textarea,select{font:inherit;width:100%;padding:9px 10px;border:1px solid #ced9e9;border-radius:6px;outline:none}input:focus,textarea:focus,select:focus{border-color:#4d86ff;box-shadow:0 0 0 3px #4d86ff18}.features{display:flex;flex-wrap:wrap;gap:7px}.features label{border:1px solid #dce6f4;border-radius:15px;padding:5px 8px;font-size:12px;color:#52647f}.features input{width:auto;margin-right:4px}button{border:0;border-radius:6px;background:#3977ff;color:#fff;padding:9px 13px;font:inherit;cursor:pointer}button:hover{background:#2866ee}button.ghost{background:#fff;border:1px solid #cdd9eb;color:#3866b2}button.warn{background:#ed6a5f}button.warn:hover{background:#db554a}button.danger{background:#f44336}button.danger:hover{background:#d93329}button.small{padding:5px 8px;font-size:12px}.issued{display:none;margin-top:13px;border-radius:7px;background:#e7fff1;color:#176342;padding:10px;font-size:13px}.issued code{display:block;color:#132d51;background:#fff;padding:7px;margin-top:6px;border-radius:5px;word-break:break-all}.headrow{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.headrow h2{margin:0}.status{font-size:13px;min-height:19px;color:#5c6f8d;margin-bottom:10px}.status.bad{color:#c23e35}.table-wrap{overflow:auto;max-height:350px;border:1px solid #e7eef8;border-radius:7px}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;background:#f7faff;color:#70809a;font-weight:600;white-space:nowrap}td,th{padding:11px 12px;border-bottom:1px solid #edf1f7;vertical-align:middle}tr:last-child td{border-bottom:0}.tag{display:inline-block;background:#edf3ff;color:#3c68c4;border-radius:12px;padding:3px 7px;font-size:11px;margin:1px}.state-active{color:#1e9b5c;font-weight:700}.state-frozen{color:#d35a4e;font-weight:700}.state-unbound{color:#8a98ad;font-weight:700}.actions{white-space:nowrap}.empty{text-align:center;color:#9aa7ba;padding:32px}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}.hidden{display:none!important}.modal{display:none;position:fixed;inset:0;background:#10203c66;align-items:center;justify-content:center;padding:20px;z-index:20}.modal.open{display:flex}.modal-box{width:min(660px,92vw);background:#fff;border-radius:10px;border:1px solid #dce6f4;box-shadow:0 18px 60px #10203c4d;padding:18px}.modal-title{font-weight:800;margin-bottom:12px}.modal-body{white-space:pre-wrap;line-height:1.75;color:#34435d;background:#f7faff;border-radius:8px;padding:12px}.modal-actions{text-align:right;margin-top:13px}
</style>
</head>
<body>
<header class="top">
  <div><div class="brand"><i>◉</i>直播复盘侠 · 授权管理台</div><div class="sub">发卡、设备绑定、冻结与删除</div></div>
  <div class="auth"><input id="token" type="password" placeholder="输入管理员令牌"><button id="connect" class="ghost">连接管理台</button></div>
</header>
<main class="page">
  <div class="notice">管理员令牌只用于建立 24 小时信任会话；同一 IP、同一浏览器设备指纹、同一信任 Cookie 命中时，无需反复输入。新卡会保存完整卡密，旧卡若没有加密记录会显示“旧卡未保存”。</div>
  <div id="status" class="status">正在检查管理台登录状态...</div>
  <div class="grid">
    <section class="card">
      <h2>创建卡密</h2>
      <div class="field"><label>产品</label><select id="productCode"><option value="live_replay_xia" selected>直播复盘侠</option><option value="lead_shrimp">获客虾</option><option value="wanshan_zimeiti">万山自媒体</option><option value="wanshan_media">万山漫剧</option></select></div>
      <div class="field"><label>授权功能</label><div class="features" id="features"></div></div>
      <div class="field"><label>设备数量 <span>默认一台电脑</span></label><select id="maxDevices"><option value="1">1 台</option><option value="2">2 台</option><option value="3">3 台</option><option value="5">5 台</option></select></div>
      <div id="livePolicy">
        <div class="field"><label>同时监听上限 <span>仅直播复盘侠使用</span></label><select id="maxActiveRooms"><option value="3">3 个</option><option value="5">5 个</option><option value="10" selected>10 个</option><option value="15">15 个</option><option value="20">20 个</option></select></div>
        <div class="field"><label>导出水印</label><div class="features"><label><input id="exportWatermark" type="checkbox" checked>导出文件写入授权标识</label></div></div>
        <div class="field"><label>最低版本 <span>低于此版本可远程要求升级，可留空</span></label><input id="forceUpgradeBelow" maxlength="64" placeholder="例如 1.0.3"></div>
      </div>
      <div class="field"><label>卡密有效期</label><select id="expiresChoice"><option value="minute">1 分钟</option><option value="week">一周</option><option value="month">一个月</option><option value="half_year">半年</option><option value="year" selected>一年</option></select></div>
      <div class="field"><label>备注 <span>默认收在详情按钮里</span></label><textarea id="note" rows="3" maxlength="500" placeholder="例如客户名称、订单号、售后备注"></textarea></div>
      <button id="issue">生成卡密</button>
      <div class="issued" id="issued">请复制并保存这张卡密：<code id="issuedKey"></code><button class="small ghost" id="copyKey">复制卡密</button></div>
    </section>
    <section class="card">
      <div class="headrow"><h2>卡密与设备</h2><div><button class="small ghost" id="copyPublicKey">复制构建公钥</button> <button class="small ghost" id="refresh">刷新列表</button></div></div>
      <div class="muted">卡密列表只展示必要信息；授权功能和自定义备注统一放进详情按钮。</div>
      <h3>卡密列表</h3>
      <div class="table-wrap"><table><thead><tr><th>完整卡密</th><th>产品</th><th>详情</th><th>策略</th><th>已绑/上限</th><th>到期时间</th><th>状态</th><th>操作</th></tr></thead><tbody id="cards"><tr><td colspan="8" class="empty">等待加载</td></tr></tbody></table></div>
      <h3>设备授权记录</h3>
      <div class="table-wrap"><table><thead><tr><th>卡密</th><th>设备编号</th><th>客户端</th><th>最后刷新</th><th>状态</th><th>操作</th></tr></thead><tbody id="activations"><tr><td colspan="6" class="empty">等待加载</td></tr></tbody></table></div>
    </section>
  </div>
  <div class="footer">冻结或删除卡密后，联网客户端会在下一次授权刷新时停止商业功能；离线设备仍受本机签名授权有效期限制。</div>
</main>
<div class="modal" id="detailsModal"><div class="modal-box"><div class="modal-title" id="detailsTitle">卡密详情</div><div class="modal-body" id="detailsBody"></div><div class="modal-actions"><button class="ghost" id="closeDetails">关闭</button></div></div></div>
<script>
const $=id=>document.getElementById(id);let adminToken='';let adminDevice='';
const PRODUCT_NAMES={wanshan_zimeiti:'万山自媒体',wanshan_media:'万山漫剧',lead_shrimp:'获客虾',live_replay_xia:'直播复盘侠'};
const PRODUCT_FEATURES={
  lead_shrimp:[['basic','基础功能'],['lead_radar','评论线索采集'],['export','线索导出']],
  wanshan_zimeiti:[['basic','基础功能'],['topic_radar','热点/选题'],['copywriting','文案生成'],['prompt_templates','提示词模板'],['video_workshop','视频工作台'],['distribution','平台分发'],['analytics','数据分析'],['updates','在线更新']],
  wanshan_media:[['basic','基础功能'],['topic_radar','小说/选题管理'],['copywriting','剧本转换'],['prompt_templates','提示词模板'],['video_workshop','分镜与视频工坊'],['distribution','批量导出'],['analytics','项目数据'],['updates','在线更新']],
  live_replay_xia:[['basic','基础功能'],['live_monitor','直播监听'],['export','数据导出'],['ai_replay','AI复盘'],['short_video_ai','短视频AI'],['lead_radar','AI获客'],['batch','批量操作']]
};
const status=(text,bad=false)=>{const el=$('status');el.textContent=text;el.className='status'+(bad?' bad':'')};
const fmt=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'永久';
async function sha256(text){try{const data=new TextEncoder().encode(text);const buf=await crypto.subtle.digest('SHA-256',data);return [...new Uint8Array(buf)].map(x=>x.toString(16).padStart(2,'0')).join('')}catch(e){return btoa(unescape(encodeURIComponent(text))).replace(/=+$/,'').slice(0,64)}}
async function initDevice(){adminDevice=await sha256([navigator.userAgent,navigator.language,navigator.platform,screen.width+'x'+screen.height,new Date().getTimezoneOffset(),navigator.hardwareConcurrency||''].join('|'))}
async function api(path,opts={}){const headers={...(opts.headers||{}),'X-Admin-Device':adminDevice};if(adminToken)headers.Authorization='Bearer '+adminToken;const r=await fetch(path,{...opts,headers,credentials:'same-origin'});const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.detail||'请求失败');return data}
function selectedProduct(){return $('productCode').value}
function renderFeaturePicker(){const product=selectedProduct();$('features').textContent='';(PRODUCT_FEATURES[product]||[]).forEach(([v,l])=>{const item=document.createElement('label');item.innerHTML='<input type="checkbox" checked value="'+v+'">'+l;$('features').appendChild(item)});$('livePolicy').classList.toggle('hidden',product!=='live_replay_xia');status('已切换到 '+(PRODUCT_NAMES[product]||product)+' 卡密配置。')}
function selectedExpiresAt(){const now=Math.floor(Date.now()/1000);const days={minute:1/1440,week:7,month:30,half_year:183,year:365}[$('expiresChoice').value]||365;return now+Math.round(days*86400)}
function buildPolicy(){if(selectedProduct()!=='live_replay_xia')return {};return {max_active_rooms:Number($('maxActiveRooms').value),export_watermark:$('exportWatermark').checked,force_upgrade_below:$('forceUpgradeBelow').value}}
function cell(text,cls=''){const td=document.createElement('td');td.textContent=String(text??'');if(cls)td.className=cls;return td}
function keyCell(c){const td=document.createElement('td');const key=c.card_key||'';if(key){const code=document.createElement('code');code.className='mono';code.textContent=key;td.appendChild(code);td.appendChild(document.createTextNode(' '));const b=document.createElement('button');b.className='small ghost';b.textContent='复制';b.onclick=async()=>{await navigator.clipboard.writeText(key);status('卡密已复制。')};td.appendChild(b)}else td.textContent='旧卡未保存';return td}
function detailButton(card){const td=document.createElement('td');const btn=document.createElement('button');btn.className='small ghost';btn.textContent='详情';btn.onclick=()=>showDetails(card);td.appendChild(btn);return td}
function policyText(card){const p=card.policy||{};if(card.product_code!=='live_replay_xia')return '标准授权';return '监听 '+(p.max_active_rooms||10)+' 个'+(p.export_watermark?' / 水印':'')+(p.force_upgrade_below?' / 最低 '+p.force_upgrade_below:'')}
function showDetails(card){const p=card.policy||{};$('detailsTitle').textContent=(PRODUCT_NAMES[card.product_code]||card.product_code)+' · 授权详情';$('detailsBody').textContent='授权功能：\n'+(card.features||[]).join('、')+'\n\n授权策略：\n'+policyText(card)+'\n\n最低版本：\n'+(p.force_upgrade_below||'不限制')+'\n\n自定义备注：\n'+(card.note||'无');$('detailsModal').className='modal open'}
function renderCards(cards){const body=$('cards');body.textContent='';if(!cards.length){body.innerHTML='<tr><td colspan="8" class="empty">暂无卡密</td></tr>';return}cards.forEach(c=>{const tr=document.createElement('tr');tr.append(keyCell(c),cell(PRODUCT_NAMES[c.product_code]||c.product_code),detailButton(c),cell(policyText(c)),cell((c.active_devices||0)+' / '+c.max_devices),cell(fmt(c.expires_at)),cell(c.status,c.status==='active'?'state-active':'state-frozen'));const actions=document.createElement('td');actions.className='actions';const del=document.createElement('button');del.className='small danger';del.textContent='删除';del.onclick=()=>deleteCard(c.id);actions.appendChild(del);tr.appendChild(actions);body.appendChild(tr)})}
function renderActivations(rows){const body=$('activations');body.textContent='';if(!rows.length){body.innerHTML='<tr><td colspan="6" class="empty">暂无设备授权记录</td></tr>';return}rows.forEach(a=>{const tr=document.createElement('tr');tr.append(cell((a.key_prefix||'')+'…','mono'),cell((a.device_hash||'').slice(0,16)+'…','mono'),cell(a.app_version||'—'),cell(fmt(a.last_seen_at||a.first_seen_at)),cell(a.status,'state-'+a.status));const actions=document.createElement('td');actions.className='actions';if(a.status==='active'){const freeze=document.createElement('button');freeze.className='small warn';freeze.textContent='冻结';freeze.onclick=()=>changeActivation(a.id,'freeze');actions.appendChild(freeze)}else actions.textContent='—';tr.appendChild(actions);body.appendChild(tr)})}
async function load(){try{status('正在读取授权数据...');const [cards,acts]=await Promise.all([api('/admin/cards'),api('/admin/activations')]);renderCards(cards.cards||[]);renderActivations(acts.activations||[]);status('已连接，数据已刷新。')}catch(e){status(e.message||'连接失败',true)}}
async function login(){adminToken=$('token').value.trim();if(!adminToken)return status('请输入管理员令牌。',true);try{await api('/admin/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:adminToken,device_hash:adminDevice})});$('token').value='';adminToken='';status('登录成功，当前设备 24 小时内可信。');load()}catch(e){status(e.message||'登录失败',true)}}
async function changeActivation(id,action){const reason=prompt('冻结原因（可选）：','')||'';try{await api('/admin/activations/'+encodeURIComponent(id)+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})});status('操作已提交。');load()}catch(e){status(e.message,true)}}
async function deleteCard(id){if(!confirm('确认删除这张卡密？删除后列表不再显示，已绑定设备会被冻结。'))return;try{await api('/admin/cards/'+encodeURIComponent(id),{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'admin console delete'})});status('卡密已删除。');load()}catch(e){status(e.message,true)}}
$('productCode').onchange=renderFeaturePicker;$('connect').onclick=login;$('refresh').onclick=load;
$('issue').onclick=async()=>{const features=[...document.querySelectorAll('#features input:checked')].map(x=>x.value);if(!features.length)return status('至少选择一个授权功能。',true);try{const r=await api('/admin/card-keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_code:selectedProduct(),features,max_devices:Number($('maxDevices').value),expires_at:selectedExpiresAt(),policy:buildPolicy(),note:$('note').value})});$('issuedKey').textContent=r.card_key;$('issued').style.display='block';status('卡密已生成，请立即复制并保存。');load()}catch(e){status(e.message,true)}};
$('copyKey').onclick=async()=>{try{await navigator.clipboard.writeText($('issuedKey').textContent);status('卡密已复制。')}catch(e){status('复制失败，请手动复制。',true)}};
$('copyPublicKey').onclick=async()=>{try{const r=await api('/admin/public-key');await navigator.clipboard.writeText(r.public_key);status('构建公钥已复制。')}catch(e){status(e.message||'复制失败',true)}};
$('closeDetails').onclick=()=>{$('detailsModal').className='modal'};$('detailsModal').onclick=e=>{if(e.target===$('detailsModal'))$('detailsModal').className='modal'};
renderFeaturePicker();(async()=>{await initDevice();try{await api('/admin/session');status('已通过信任会话连接管理台。');load()}catch(e){status('请输入管理员令牌连接管理台。')}})();
</script>
</body>
</html>"""
