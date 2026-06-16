"""Admin UI static resources."""
from __future__ import annotations

from aiohttp import web
from .auth import auth_enabled


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zhongzhuan Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d}
header h1{font-size:18px;font-weight:600;color:#58a6ff}
.status-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:12px;font-size:12px}
.status-badge.running{background:#1a3a2a;color:#3fb950}
.status-badge.stopped{background:#3a1a1a;color:#f85149}
nav{display:flex;gap:4px;padding:8px 20px;background:#161b22;border-bottom:1px solid #30363d}
nav a{color:#8b949e;text-decoration:none;padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer}
nav a:hover,nav a.active{color:#e1e4e8;background:#21262d}
main{padding:20px;max-width:1200px;margin:0 auto}
.tab{display:none}
.tab.active{display:block}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;color:#58a6ff;margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #21262d;font-size:13px}
th{color:#8b949e;font-weight:500}
tr:hover{background:#1c2128}
.btn{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer}
.btn:hover{background:#30363d}
.btn.primary{background:#238636;border-color:#238636;color:#fff}
.btn.primary:hover{background:#2ea043}
.btn.danger{background:#da3633;border-color:#da3633;color:#fff}
.btn.danger:hover{background:#f85149}
.btn.small{padding:3px 10px;font-size:12px}
textarea{background:#0d1117;border:1px solid #30363d;color:#e1e4e8;padding:6px 10px;border-radius:6px;font-size:13px;width:100%;min-height:120px;font-family:monospace}
input,select{background:#0d1117;border:1px solid #30363d;color:#e1e4e8;padding:6px 10px;border-radius:6px;font-size:13px;width:100%}
label{font-size:13px;color:#8b949e;display:block;margin-bottom:4px}
.form-group{margin-bottom:12px}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.stat-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;text-align:center}
.stat-card .label{font-size:12px;color:#8b949e}
.stat-card .value{font-size:24px;font-weight:700;color:#58a6ff;margin-top:4px}
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:100;align-items:center;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:20px;min-width:400px;max-width:600px}
.modal h3{font-size:16px;color:#58a6ff;margin-bottom:16px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.health-bar{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.health-bar.good{background:#3fb950}
.health-bar.warn{background:#d29922}
.health-bar.bad{background:#f85149}
.login-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.8);z-index:200;align-items:center;justify-content:center}
.login-overlay.show{display:flex}
.login-box{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:32px;width:360px;text-align:center}
.login-box h2{color:#58a6ff;margin-bottom:20px;font-size:20px}
.login-box .error{color:#f85149;font-size:12px;margin-top:8px}
.token-value{font-family:monospace;font-size:12px;word-break:break-all}
</style>
</head>
<body>
<div class="login-overlay" id="loginOverlay">
  <div class="login-box">
    <h2>Zhongzhuan</h2>
    <div class="form-group"><input id="loginUser" placeholder="用户名"></div>
    <div class="form-group"><input id="loginPass" type="password" placeholder="密码"></div>
    <button class="btn primary" onclick="doLogin()" style="width:100%">登录</button>
    <div class="error" id="loginError"></div>
  </div>
</div>
<header>
  <h1><span style="margin-right:8px">&#9881;</span>Zhongzhuan</h1>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="status-badge" id="svcStatus">...</span>
    <button class="btn" onclick="svcToggle()" id="svcBtn">启动</button>
    <button class="btn" onclick="exportConfig()">导出配置</button>
    <button class="btn" onclick="importConfig()">导入配置</button>
    <button class="btn" onclick="doLogout()" id="logoutBtn" style="display:none">登出</button>
  </div>
</header>
<nav>
  <a class="active" onclick="showTab('overview', event)">总览</a>
  <a onclick="showTab('models', event)">模型</a>
  <a onclick="showTab('keys', event)">Key池</a>
  <a onclick="showTab('groups', event)">分组</a>
  <a onclick="showTab('tokens', event)" id="navTokens" style="display:none">令牌</a>
  <a onclick="showTab('logs', event)">日志</a>
</nav>
<main>
  <div class="tab active" id="tab-overview">
    <div class="stats-grid" id="statsGrid"></div>
    <div class="card"><h2>请求日志</h2><div id="overviewLogs"></div></div>
  </div>
  <div class="tab" id="tab-models">
    <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h2>模型列表</h2><button class="btn primary" onclick="showModelModal()">+ 添加模型</button></div>
    <table><thead><tr><th>名称</th><th>上游地址</th><th>上游模型</th><th>RPM</th><th>TPM</th><th>启用</th><th>操作</th></tr></thead><tbody id="modelTable"></tbody></table></div>
  </div>
  <div class="tab" id="tab-keys">
    <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h2>Key 列表</h2><div style="display:flex;gap:8px"><button class="btn primary" onclick="showKeyModal()">+ 添加 Key</button><button class="btn primary" onclick="showBatchImportModal()">批量导入</button></div></div>
    <table><thead><tr><th>标签</th><th>模型</th><th>Key</th><th>优先级</th><th>启用</th><th>操作</th></tr></thead><tbody id="keyTable"></tbody></table></div>
  </div>
  <div class="tab" id="tab-groups">
    <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h2>分组列表</h2><button class="btn primary" onclick="showGroupModal()">+ 添加分组</button></div>
    <table><thead><tr><th>名称</th><th>策略</th><th>成员</th><th>操作</th></tr></thead><tbody id="groupTable"></tbody></table></div>
  </div>
  <div class="tab" id="tab-tokens">
    <div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><h2>访问令牌</h2><button class="btn primary" onclick="showTokenModal()">+ 创建令牌</button></div>
    <table><thead><tr><th>标签</th><th>令牌值</th><th>启用</th><th>创建时间</th><th>操作</th></tr></thead><tbody id="tokenTable"></tbody></table></div>
  </div>
  <div class="tab" id="tab-logs">
    <div class="card"><h2>请求日志</h2><div id="logsTable"></div></div>
  </div>
</main>
<div class="modal-overlay" id="modal">
  <div class="modal" id="modalContent"></div>
</div>
<script>
const API = "";
let models = [];
let keys = [];
let groups = [];
let loading = 0;
let authToken = localStorage.getItem("zhongzhuan_token") || "";

function showLoading(show) {
  if (show) {
    loading++;
    document.body.style.cursor = "wait";
  } else {
    loading = Math.max(0, loading - 1);
    if (loading === 0) document.body.style.cursor = "";
  }
}

async function api(path, opts = {}) {
  try {
    showLoading(true);
    const headers = {"Content-Type": "application/json"};
    if (authToken) headers["Authorization"] = "Bearer " + authToken;
    const r = await fetch(API + path, {headers, ...opts});
    showLoading(false);
    if (r.status === 401) {
      authToken = "";
      localStorage.removeItem("zhongzhuan_token");
      checkAuth();
      return null;
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({error: {message: r.statusText}}));
      console.error("API error:", path, err);
      return null;
    }
    return r.json();
  } catch(e) {
    console.error("API fetch error:", e);
    return null;
  }
}

async function checkAuth() {
  const s = await api("/api/auth/status");
  if (!s) return;
  if (s.auth_enabled) {
    if (!authToken) {
      document.getElementById("loginOverlay").classList.add("show");
      return;
    }
    const me = await api("/api/auth/me");
    if (!me || !me.username) {
      authToken = "";
      localStorage.removeItem("zhongzhuan_token");
      document.getElementById("loginOverlay").classList.add("show");
      return;
    }
    document.getElementById("logoutBtn").style.display = "";
    document.getElementById("navTokens").style.display = "";
  }
  document.getElementById("loginOverlay").classList.remove("show");
  loadOverview(); loadSvcStatus();
}

async function doLogin() {
  const user = document.getElementById("loginUser").value;
  const pass = document.getElementById("loginPass").value;
  const r = await fetch(API + "/api/auth/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username: user, password: pass})
  });
  if (!r.ok) {
    document.getElementById("loginError").textContent = "用户名或密码错误";
    return;
  }
  const data = await r.json();
  authToken = data.token;
  localStorage.setItem("zhongzhuan_token", authToken);
  document.getElementById("loginError").textContent = "";
  checkAuth();
}

function doLogout() {
  authToken = "";
  localStorage.removeItem("zhongzhuan_token");
  document.getElementById("logoutBtn").style.display = "none";
  document.getElementById("navTokens").style.display = "none";
  checkAuth();
}

function showTab(name, evt) {
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll("nav a").forEach(a => a.classList.remove("active"));
  document.getElementById("tab-" + name).classList.add("active");
  if (evt && evt.target) evt.target.classList.add("active");
  if (name === "overview") loadOverview();
  if (name === "models") loadModels();
  if (name === "keys") { loadModels(); loadKeys(); }
  if (name === "groups") loadGroups();
  if (name === "tokens") loadTokens();
  if (name === "logs") loadLogs();
}

async function loadOverview() {
  const s = await api("/api/stats?range=1h");
  if (!s) return;
  document.getElementById("statsGrid").innerHTML = `
    <div class="stat-card"><div class="label">QPS</div><div class="value">${s.qps}</div></div>
    <div class="stat-card"><div class="label">成功率</div><div class="value">${(s.success_rate*100).toFixed(1)}%</div></div>
    <div class="stat-card"><div class="label">平均延迟</div><div class="value">${s.avg_latency_ms}ms</div></div>
    <div class="stat-card"><div class="label">活跃 Key</div><div class="value">${s.active_keys}</div></div>`;
}

async function loadModels() {
  const d = await api("/api/models");
  models = d?.data || [];
  document.getElementById("modelTable").innerHTML = models.map(m => `
    <tr><td>${m.name}</td><td>${m.upstream_base}</td><td>${m.upstream_model}</td>
    <td>${m.rpm_limit||"不限"}</td><td>${m.tpm_limit||"不限"}</td>
    <td>${m.enabled?"是":"否"}</td>
    <td><button class="btn" onclick="editModel(${m.id})">编辑</button> <button class="btn danger" onclick="delModel(${m.id})">删除</button></td></tr>`).join("");
}

async function delModel(id) { 
  const r = await api("/api/models/" + id, {method:"DELETE"});
  if (r !== null) loadModels(); 
}

function showModelModal(model) {
  const isEdit = !!model;
  document.getElementById("modalContent").innerHTML = `
    <h3>${isEdit ? '编辑模型' : '添加模型'}</h3>
    <div class="form-group"><label>名称</label><input id="f_name" value="${isEdit ? model.name : ''}"></div>
    <div class="form-group"><label>上游地址</label><input id="f_upstream_base" placeholder="https://api.openai.com/v1" value="${isEdit ? model.upstream_base : ''}"></div>
    <div class="form-group"><label>上游模型名</label><input id="f_upstream_model" placeholder="gpt-4o" value="${isEdit ? model.upstream_model : ''}"></div>
    <div class="form-group"><label>RPM限制</label><input id="f_rpm" type="number" value="${isEdit ? model.rpm_limit : 0}"></div>
    <div class="form-group"><label>TPM限制</label><input id="f_tpm" type="number" value="${isEdit ? model.tpm_limit : 0}"></div>
    <div class="form-group"><label>启用</label><select id="f_enabled"><option value="1" ${isEdit && model.enabled ? 'selected' : ''}>是</option><option value="0" ${isEdit && !model.enabled ? 'selected' : ''}>否</option></select></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="saveModel(${isEdit ? model.id : ''})">保存</button></div>`;
  document.getElementById("modal").classList.add("show");
}

function editModel(id) {
  const m = models.find(x => x.id === id);
  if (m) showModelModal(m);
}

async function saveModel(id) {
  const body = {
    name: document.getElementById("f_name").value,
    upstream_base: document.getElementById("f_upstream_base").value,
    upstream_model: document.getElementById("f_upstream_model").value,
    rpm_limit: parseInt(document.getElementById("f_rpm").value)||0,
    tpm_limit: parseInt(document.getElementById("f_tpm").value)||0,
    enabled: document.getElementById("f_enabled").value === "1",
  };
  let r;
  if (id) {
    r = await api("/api/models/" + id, {method:"PUT", body: JSON.stringify(body)});
  } else {
    r = await api("/api/models", {method:"POST", body: JSON.stringify(body)});
  }
  if (r !== null) { closeModal(); loadModels(); }
}

async function loadKeys() {
  const [dk, dm] = await Promise.all([api("/api/keys"), api("/api/models")]);
  if (!dk && !dm) return;
  keys = dk?.data || [];
  models = dm?.data || [];
  const modelMap = {};
  models.forEach(m => modelMap[m.id] = m);

  const groups = new Map();
  for (const m of models) groups.set(m.id, { model: m, keys: [] });
  for (const k of keys) {
    const g = groups.get(k.model_id) || { model: { id: k.model_id, name: "已删除模型#" + k.model_id }, keys: [] };
    g.keys.push(k);
    groups.set(k.model_id, g);
  }

  const collapsed = JSON.parse(localStorage.getItem("keyGroupCollapsed") || "{}");
  if (keys.length === 0 && models.length === 0) {
    document.getElementById("keyTable").innerHTML =
      '<tr><td colspan="6" style="text-align:center;color:#8b949e;padding:24px">还没有 Key,先在「模型」标签添加模型,然后点击 + 添加 Key</td></tr>';
    return;
  }

  const parts = [];
  for (const [mid, g] of groups) {
    const m = g.model;
    const isCollapsed = !!collapsed[mid];
    const arrow = isCollapsed ? '\u25B6' : '\u25BC';
    parts.push(
      '<tr class="group-header" data-model="' + mid + '" onclick="toggleKeyGroup(' + mid + ')" style="cursor:pointer;background:#0d1117">' +
        '<td colspan="6" style="font-weight:600;color:#58a6ff;user-select:none">' +
          '<span class="kg-arrow" style="display:inline-block;width:16px;color:#8b949e">' + arrow + '</span> ' +
          esc(m.name) +
          '<span style="color:#8b949e;font-weight:400;margin-left:8px">' + g.keys.length + ' 个 Key</span>' +
          '<span style="float:right;color:#8b949e;font-weight:400;font-size:12px">' + esc(m.upstream_base||"") + ' \u00B7 ' + esc(m.upstream_model||"") + '</span>' +
        '</td></tr>');
    if (!isCollapsed) {
      if (g.keys.length === 0) {
        parts.push('<tr><td colspan="6" style="text-align:center;color:#8b949e;padding:12px">该模型下还没有 Key</td></tr>');
      } else {
        for (const k of g.keys) {
          parts.push(
            '<tr>' +
              '<td>' + esc(k.label) + '</td>' +
              '<td>' + esc(m.name) + '</td>' +
              '<td><code>' + esc(k.key_masked) + '</code></td>' +
              '<td>' + k.priority + '</td>' +
              '<td>' + (k.enabled?"是":"否") + '</td>' +
              '<td><button class="btn danger" onclick="delKey(' + k.id + ')">删除</button></td>' +
            '</tr>');
        }
      }
    }
  }
  document.getElementById("keyTable").innerHTML = parts.join("");
}

function toggleKeyGroup(modelId) {
  const collapsed = JSON.parse(localStorage.getItem("keyGroupCollapsed") || "{}");
  collapsed[modelId] = !collapsed[modelId];
  localStorage.setItem("keyGroupCollapsed", JSON.stringify(collapsed));
  const header = document.querySelector('tr.group-header[data-model="' + modelId + '"]');
  if (!header) { loadKeys(); return; }
  let nextRow = header.nextElementSibling;
  while (nextRow && !nextRow.classList.contains("group-header")) {
    const toRemove = nextRow;
    nextRow = nextRow.nextElementSibling;
    toRemove.remove();
  }
  header.querySelector(".kg-arrow").textContent = collapsed[modelId] ? '\u25B6' : '\u25BC';
  if (!collapsed[modelId]) {
    const m = (models.find(x => x.id === modelId) || { id: modelId, name: "已删除模型#" + modelId });
    const ks = keys.filter(k => k.model_id === modelId);
    let bodyHtml = '';
    if (ks.length === 0) {
      bodyHtml = '<tr><td colspan="6" style="text-align:center;color:#8b949e;padding:12px">该模型下还没有 Key</td></tr>';
    } else {
      bodyHtml = ks.map(k =>
        '<tr>' +
          '<td>' + esc(k.label) + '</td>' +
          '<td>' + esc(m.name) + '</td>' +
          '<td><code>' + esc(k.key_masked) + '</code></td>' +
          '<td>' + k.priority + '</td>' +
          '<td>' + (k.enabled?"是":"否") + '</td>' +
          '<td><button class="btn danger" onclick="delKey(' + k.id + ')">删除</button></td>' +
        '</tr>'
      ).join("");
    }
    header.insertAdjacentHTML("afterend", bodyHtml);
  }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function delKey(id) { 
  const r = await api("/api/keys/" + id, {method:"DELETE"});
  if (r !== null) loadKeys(); 
}

function showKeyModal() {
  const opts = models.map(m => '<option value="' + m.id + '">' + m.name + '</option>').join("");
  document.getElementById("modalContent").innerHTML = `
    <h3>添加 Key</h3>
    <div class="form-group"><label>模型</label><select id="f_model_id">${opts}</select></div>
    <div class="form-group"><label>标签</label><input id="f_label"></div>
    <div class="form-group"><label>Key值</label><input id="f_key_value" type="password"></div>
    <div class="form-group"><label>优先级</label><input id="f_priority" type="number" value="0"></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="addKey()">保存</button></div>`;
  document.getElementById("modal").classList.add("show");
}

async function addKey() {
  const r = await api("/api/keys", {method:"POST", body:JSON.stringify({
    model_id: parseInt(document.getElementById("f_model_id").value),
    label: document.getElementById("f_label").value,
    key_value: document.getElementById("f_key_value").value,
    priority: parseInt(document.getElementById("f_priority").value)||0,
  })});
  if (r !== null) { closeModal(); loadKeys(); }
}

function showBatchImportModal() {
  const opts = models.map(m => '<option value="' + m.id + '">' + m.name + '</option>').join("");
  document.getElementById("modalContent").innerHTML = `
    <h3>批量导入 Key</h3>
    <div class="form-group"><label>模型</label><select id="f_batch_model_id">${opts}</select></div>
    <div class="form-group"><label>Key列表</label><textarea id="f_batch_keys" placeholder="每行一个 Key，可选格式：label|key_value|priority&#10;例如：&#10;key1|sk-xxx123|0&#10;key2|sk-yyy456|1"></textarea></div>
    <div class="form-group"><label>默认优先级</label><input id="f_batch_priority" type="number" value="0"></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="batchImportKeys()">导入</button></div>`;
  document.getElementById("modal").classList.add("show");
}

async function batchImportKeys() {
  const modelId = parseInt(document.getElementById("f_batch_model_id").value);
  const text = document.getElementById("f_batch_keys").value.trim();
  const defaultPriority = parseInt(document.getElementById("f_batch_priority").value)||0;
  if (!text) { return; }
  
  const lines = text.split('\\n').filter(l => l.trim());
  let success = 0;
  let failed = 0;
  
  for (const line of lines) {
    const parts = line.trim().split('|');
    const keyValue = parts.length >= 2 ? parts[1].trim() : parts[0].trim();
    if (!keyValue) { failed++; continue; }
    const label = parts.length >= 2 ? parts[0].trim() : '';
    const priority = parts.length >= 3 ? parseInt(parts[2].trim())||defaultPriority : defaultPriority;
    
    try {
      const r = await api("/api/keys", {method:"POST", body:JSON.stringify({
        model_id: modelId,
        label: label,
        key_value: keyValue,
        priority: priority,
      })});
      if (r !== null) success++;
      else failed++;
    } catch(e) { failed++; }
  }
  
  closeModal();
  loadKeys();
}

async function loadGroups() {
  const d = await api("/api/groups");
  groups = d?.data || [];
  document.getElementById("groupTable").innerHTML = groups.map(g => `
    <tr><td>${g.name}</td><td>${g.strategy}</td>
    <td>${(g.members||[]).map(x=>"model#"+x.model_id).join(", ")}</td>
    <td><button class="btn danger" onclick="delGroup(${g.id})">删除</button></td></tr>`).join("");
}

async function delGroup(id) {
  const r = await api("/api/groups/" + id, {method:"DELETE"});
  if (r !== null) loadGroups();
}

function showGroupModal() {
  loadModels();
  const opts = models.map(m => `<option value="${m.id}">${m.name}</option>`).join("");
  document.getElementById("modalContent").innerHTML = `
    <h3>添加分组</h3>
    <div class="form-group"><label>名称</label><input id="f_gname"></div>
    <div class="form-group"><label>策略</label><select id="f_strategy"><option value="round_robin">轮询</option><option value="weighted">加权</option><option value="failover">故障转移</option></select></div>
    <div class="form-group"><label>成员模型</label><select id="f_members" multiple style="height:100px">${opts}</select></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="addGroup()">保存</button></div>`;
  document.getElementById("modal").classList.add("show");
}

async function addGroup() {
  const sel = document.getElementById("f_members").selectedOptions;
  const members = Array.from(sel).map(o => ({model_id: parseInt(o.value)}));
  const r = await api("/api/groups", {method:"POST", body:JSON.stringify({
    name: document.getElementById("f_gname").value,
    strategy: document.getElementById("f_strategy").value,
    members,
  })});
  if (r !== null) { closeModal(); loadGroups(); }
}

// Token management
async function loadTokens() {
  const d = await api("/api/tokens");
  const tokens = d?.data || [];
  document.getElementById("tokenTable").innerHTML = tokens.map(t => `
    <tr>
      <td>${esc(t.label)}</td>
      <td><code class="token-value">${esc(t.token)}</code> <button class="btn small" onclick="copyToken('${t.token}')">复制</button></td>
      <td>${t.enabled?"是":"否"} <button class="btn small" onclick="toggleToken(${t.id}, ${!t.enabled})">${t.enabled?"禁用":"启用"}</button></td>
      <td>${new Date(t.created_at*1000).toLocaleString()}</td>
      <td><button class="btn danger" onclick="delToken(${t.id})">删除</button></td>
    </tr>`).join("");
}

function copyToken(token) {
  navigator.clipboard.writeText(token).then(() => alert("已复制"));
}

async function showTokenModal() {
  document.getElementById("modalContent").innerHTML = `
    <h3>创建访问令牌</h3>
    <div class="form-group"><label>标签</label><input id="f_tlabel" placeholder="例如：Trae专用"></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="addToken()">创建</button></div>`;
  document.getElementById("modal").classList.add("show");
}

async function addToken() {
  const label = document.getElementById("f_tlabel").value;
  const r = await api("/api/tokens", {method:"POST", body:JSON.stringify({label})});
  if (r !== null) { closeModal(); loadTokens(); }
}

async function delToken(id) {
  const r = await api("/api/tokens/" + id, {method:"DELETE"});
  if (r !== null) loadTokens();
}

async function toggleToken(id, enabled) {
  await api("/api/tokens/" + id, {method:"PUT", body:JSON.stringify({enabled})});
  loadTokens();
}

async function loadLogs() {
  const d = await api("/api/logs?limit=30");
  if (!d) return;
  const rows = (d.data||[]).map(l => `
    <tr><td>${new Date(l.ts*1000).toLocaleString()}</td><td>${l.model_name}</td>
    <td>${l.status}</td><td>${l.latency_ms}ms</td><td>${l.error||""}</td></tr>`).join("");
  document.getElementById("logsTable").innerHTML = `<table><thead><tr><th>时间</th><th>模型</th><th>状态</th><th>延迟</th><th>错误</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function closeModal() { document.getElementById("modal").classList.remove("show"); }

async function loadSvcStatus() {
  try {
    const s = await api("/api/service/status");
    if (!s) return;
    const badge = document.getElementById("svcStatus");
    const btn = document.getElementById("svcBtn");
    if (s.status === "running") {
      badge.className = "status-badge running"; badge.textContent = "运行中";
      btn.textContent = "停止";
    } else if (s.status === "stopped") {
      badge.className = "status-badge stopped"; badge.textContent = "已停止";
      btn.textContent = "启动";
    } else {
      badge.className = "status-badge"; badge.textContent = s.status;
      btn.textContent = "安装服务";
    }
  } catch(e) {}
}

async function svcToggle() {
  const btn = document.getElementById("svcBtn");
  if (btn.textContent === "启动") await api("/api/service/start", {method:"POST"});
  else if (btn.textContent === "停止") await api("/api/service/stop", {method:"POST"});
  else if (btn.textContent === "安装服务") await api("/api/service/install", {method:"POST"});
  setTimeout(loadSvcStatus, 1000);
}

async function exportConfig() {
  try {
    const headers = {};
    if (authToken) headers["Authorization"] = "Bearer " + authToken;
    const r = await fetch(API + "/api/export", {headers});
    if (!r.ok) throw new Error(r.statusText);
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "zhongzhuan-export.zip";
    a.click();
  } catch(e) {
    console.error("导出失败", e);
  }
}

function importConfig() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".zip";
  input.onchange = async () => {
    try {
      const headers = {};
      if (authToken) headers["Authorization"] = "Bearer " + authToken;
      const r = await fetch(API + "/api/import", {method:"POST", body: input.files[0], headers});
      if (!r.ok) throw new Error(r.statusText);
      loadModels(); loadKeys(); loadGroups();
    } catch(e) {
      console.error("导入失败", e);
    }
  };
  input.click();
}

checkAuth();
setInterval(loadOverview, 10000);
</script>
</body>
</html>"""


def mount_ui(app: web.Application, ctx) -> None:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")
    app.router.add_get("/", index)
    app.router.add_get("/ui/", index)