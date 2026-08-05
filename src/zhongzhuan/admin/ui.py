"""Admin UI static resources — GitHub Dark + 侧边栏 + ECharts 看板。

设计参考：new-api (Semi Design Dark) + Portkey Dashboard。
- 左侧固定侧边栏 240px
- 顶部 KPI 卡片网格
- ECharts 数据看板（请求趋势、模型分布、Token 堆叠）
- GitHub Dark 配色
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zhongzhuan Admin</title>
<script src="/ui/static/echarts.min.js"></script>
<style>
:root{
  --bg:#0d1117; --bg-card:#161b22; --bg-hover:#1c2128; --bg-input:#0d1117;
  --border:#30363d; --border-muted:#21262d;
  --text:#e6edf3; --text-muted:#7d8590; --text-subtle:#484f58;
  --accent:#2f81f7; --accent-hover:#1f6feb;
  --success:#3fb950; --warning:#d29922; --danger:#f85149; --orange:#f0883e;
  --sidebar-w:240px;
  --radius:6px; --radius-lg:10px;
  --shadow:0 1px 3px rgba(0,0,0,0.3);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,sans-serif;
  background:var(--bg); color:var(--text); font-size:14px; line-height:1.5;
  -webkit-font-smoothing:antialiased;
}
/* ---- 登录遮罩 ---- */
.login-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:200;align-items:center;justify-content:center}
.login-overlay.show{display:flex}
.login-box{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:32px 40px;width:360px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.5)}
.login-box .logo{font-size:24px;font-weight:700;color:var(--accent);margin-bottom:8px;letter-spacing:1px}
.login-box .subtitle{color:var(--text-muted);font-size:12px;margin-bottom:24px}
.login-box .error{color:var(--danger);font-size:12px;margin-top:8px;min-height:16px}
/* ---- 布局 ---- */
.layout{display:flex;min-height:100vh}
.sidebar{
  width:var(--sidebar-w); background:var(--bg-card); border-right:1px solid var(--border);
  position:fixed; top:0; left:0; bottom:0; display:flex; flex-direction:column; z-index:50;
}
.sidebar-brand{padding:18px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sidebar-brand .icon{width:28px;height:28px;border-radius:6px;background:linear-gradient(135deg,var(--accent),#6f3ff5);display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#fff}
.sidebar-brand .name{font-size:16px;font-weight:700;color:var(--text)}
.sidebar-brand .ver{font-size:11px;color:var(--text-muted);margin-left:auto}
.sidebar-nav{flex:1;overflow-y:auto;padding:8px 0}
.nav-section{padding:8px 16px 4px;font-size:11px;color:var(--text-subtle);text-transform:uppercase;letter-spacing:0.5px;font-weight:600}
.nav-item{
  display:flex;align-items:center;gap:10px;padding:8px 16px;color:var(--text-muted);
  cursor:pointer;font-size:13px;border-left:2px solid transparent;transition:all 0.15s;
}
.nav-item:hover{background:var(--bg-hover);color:var(--text)}
.nav-item.active{background:var(--bg-hover);color:var(--accent);border-left-color:var(--accent)}
.nav-item .icon{width:16px;text-align:center;font-size:14px}
.nav-item .badge{margin-left:auto;background:var(--accent);color:#fff;font-size:10px;padding:1px 6px;border-radius:10px;font-weight:600}
.sidebar-footer{padding:12px 16px;border-top:1px solid var(--border);font-size:11px;color:var(--text-subtle)}
/* ---- 主内容 ---- */
.main{flex:1;margin-left:var(--sidebar-w);display:flex;flex-direction:column;min-width:0}
.topbar{
  height:52px;background:var(--bg-card);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;z-index:40;
}
.topbar h1{font-size:15px;font-weight:600;color:var(--text)}
.topbar .actions{display:flex;align-items:center;gap:10px}
.status-pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:500}
.status-pill.running{background:rgba(63,185,80,0.15);color:var(--success)}
.status-pill.stopped{background:rgba(248,81,73,0.15);color:var(--danger)}
.status-pill .dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.content{padding:24px;flex:1;max-width:1400px;width:100%}
/* Tab 默认隐藏：只有 showTab() 显式显示当前页，避免刷新后多页内容堆叠 */
.tab{display:none}
.tab.active{display:block}
/* ---- 卡片 / 表格 ---- */
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.card-header h2{font-size:14px;color:var(--text);font-weight:600}
.card-header .actions{display:flex;gap:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 12px;color:var(--text-muted);font-weight:500;border-bottom:1px solid var(--border);font-size:12px;text-transform:uppercase;letter-spacing:0.3px}
td{padding:10px 12px;border-bottom:1px solid var(--border-muted)}
tr:hover td{background:var(--bg-hover)}
tr.group-header td{background:var(--bg-input);font-weight:600;color:var(--accent);cursor:pointer;user-select:none}
/* ---- 按钮 ---- */
.btn{background:var(--bg-card);color:var(--text);border:1px solid var(--border);padding:6px 14px;border-radius:var(--radius);font-size:13px;cursor:pointer;font-family:inherit;transition:all 0.15s;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--bg-hover);border-color:#484f58}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:var(--accent-hover)}
.btn.danger{background:transparent;border-color:var(--danger);color:var(--danger)}
.btn.danger:hover{background:var(--danger);color:#fff}
.btn.success{background:transparent;border-color:var(--success);color:var(--success)}
.btn.success:hover{background:var(--success);color:#fff}
.btn.small{padding:3px 10px;font-size:12px}
.btn.ghost{background:transparent;border-color:transparent;color:var(--text-muted)}
.btn.ghost:hover{background:var(--bg-hover);color:var(--text)}
/* ---- 表单 ---- */
label{font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;font-weight:500}
input,select,textarea{background:var(--bg-input);border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:var(--radius);font-size:13px;width:100%;font-family:inherit;transition:border-color 0.15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,129,247,0.15)}
textarea{min-height:100px;resize:vertical;font-family:ui-monospace,Consolas,monospace}
.form-group{margin-bottom:12px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-row-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.form-hint{font-size:11px;color:var(--text-subtle);margin-top:4px}
/* ---- KPI 卡片 ---- */
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px}
.kpi-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:18px 20px;position:relative;overflow:hidden}
.kpi-card .label{font-size:12px;color:var(--text-muted);font-weight:500}
.kpi-card .value{font-size:28px;font-weight:700;color:var(--text);margin-top:6px;line-height:1.1}
.kpi-card .delta{font-size:11px;color:var(--text-subtle);margin-top:4px}
.kpi-card .value.accent{color:var(--accent)}
.kpi-card .value.success{color:var(--success)}
.kpi-card .value.warning{color:var(--warning)}
.kpi-card .value.danger{color:var(--danger)}
.kpi-card .icon{position:absolute;right:16px;top:16px;font-size:20px;opacity:0.4}
/* ---- 图表 ---- */
.chart-grid{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}
.chart-grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.chart-box{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px}
.chart-box h3{font-size:13px;color:var(--text);font-weight:600;margin-bottom:12px}
.chart{width:100%;height:280px}
.chart.tall{height:320px}
/* ---- 模态框 ---- */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.65);z-index:100;align-items:center;justify-content:center;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:24px;min-width:440px;max-width:720px;max-height:90vh;overflow-y:auto;box-shadow:0 12px 36px rgba(0,0,0,0.5)}
.modal h3{font-size:16px;color:var(--text);margin-bottom:16px;font-weight:600}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:20px;padding-top:16px;border-top:1px solid var(--border-muted)}
/* ---- 标签 / 徽章 ---- */
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:500}
.tag.fallback{background:rgba(240,136,62,0.15);color:var(--orange)}
.tag.custom{background:rgba(125,133,144,0.15);color:var(--text-muted)}
.tag.ok{background:rgba(63,185,80,0.15);color:var(--success)}
.tag.err{background:rgba(248,81,73,0.15);color:var(--danger)}
.tag.warn{background:rgba(210,153,34,0.15);color:var(--warning)}
.health-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.health-dot.good{background:var(--success)}
.health-dot.warn{background:var(--warning)}
.health-dot.bad{background:var(--danger)}
.token-value{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all;color:var(--accent)}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:var(--bg-input);padding:1px 6px;border-radius:3px;color:var(--accent)}
/* ---- 空状态 ---- */
.empty{text-align:center;color:var(--text-muted);padding:32px 16px;font-size:13px}
/* ---- 测试结果 ---- */
.test-result{margin-top:12px;padding:12px;border-radius:var(--radius);font-size:12px;font-family:ui-monospace,monospace}
.test-result.ok{background:rgba(63,185,80,0.1);border:1px solid rgba(63,185,80,0.3);color:var(--success)}
.test-result.fail{background:rgba(248,81,73,0.1);border:1px solid rgba(248,81,73,0.3);color:var(--danger)}
/* ---- 进度条 ---- */
.progress{height:6px;background:var(--bg-input);border-radius:3px;overflow:hidden;margin-top:4px}
.progress-bar{height:100%;background:var(--accent);border-radius:3px;transition:width 0.3s}
.progress-bar.warn{background:var(--warning)}
.progress-bar.danger{background:var(--danger)}
/* ---- 响应式 ---- */
@media (max-width:1100px){.kpi-grid{grid-template-columns:repeat(2,1fr)}.chart-grid,.chart-grid-2{grid-template-columns:1fr}}
@media (max-width:768px){.sidebar{transform:translateX(-100%)}.main{margin-left:0}.kpi-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<!-- 登录遮罩 -->
<div class="login-overlay" id="loginOverlay">
  <div class="login-box">
    <div class="logo">Zhongzhuan</div>
    <div class="subtitle">API 中转代理 · 管理后台</div>
    <div class="form-group"><input id="loginUser" placeholder="用户名" autocomplete="username"></div>
    <div class="form-group"><input id="loginPass" type="password" placeholder="密码" autocomplete="current-password"></div>
    <button class="btn primary" onclick="doLogin()" style="width:100%;justify-content:center">登录</button>
    <div class="error" id="loginError"></div>
  </div>
</div>

<div class="layout">
  <!-- 侧边栏 -->
  <aside class="sidebar">
    <div class="sidebar-brand">
      <div class="icon">Z</div>
      <div class="name">Zhongzhuan</div>
      <div class="ver" id="sideVer"></div>
    </div>
    <nav class="sidebar-nav">
      <div class="nav-section">监控</div>
      <div class="nav-item active" data-tab="dashboard" onclick="showTab('dashboard')"><span class="icon">&#9632;</span>仪表盘</div>
      <div class="nav-section">资源</div>
      <div class="nav-item" data-tab="models" onclick="showTab('models')"><span class="icon">&#9650;</span>模型管理</div>
      <div class="nav-item" data-tab="keys" onclick="showTab('keys')"><span class="icon">&#9755;</span>Key 池</div>
      <div class="nav-item" data-tab="groups" onclick="showTab('groups')"><span class="icon">&#9638;</span>分组策略</div>
      <div class="nav-section">访问控制</div>
      <div class="nav-item" data-tab="tokens" onclick="showTab('tokens')" id="navTokens" style="display:none"><span class="icon">&#9673;</span>访问令牌</div>
      <div class="nav-section">运维</div>
      <div class="nav-item" data-tab="logs" onclick="showTab('logs')"><span class="icon">&#9776;</span>请求日志</div>
    </nav>
    <div class="sidebar-footer">
      <div id="sideSvcStatus" style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
        <span class="health-dot good"></span><span style="color:var(--text-muted)">服务运行中</span>
      </div>
      <div>Zhongzhuan Admin</div>
    </div>
  </aside>

  <!-- 主内容 -->
  <div class="main">
    <header class="topbar">
      <h1 id="pageTitle">仪表盘</h1>
      <div class="actions">
        <span class="status-pill" id="svcStatus"><span class="dot"></span>...</span>
        <button class="btn small" onclick="svcToggle()" id="svcBtn">启动</button>
        <button class="btn small" onclick="exportConfig()">导出</button>
        <button class="btn small" onclick="importConfig()">导入</button>
        <button class="btn small ghost" onclick="doLogout()" id="logoutBtn" style="display:none">登出</button>
      </div>
    </header>

    <div class="content">
      <!-- 仪表盘 -->
      <div class="tab" id="tab-dashboard">
        <div class="kpi-grid" id="kpiGrid"></div>
        <div class="chart-grid">
          <div class="chart-box"><h3>请求趋势（近 7 天）</h3><div id="chartTrend" class="chart tall"></div></div>
          <div class="chart-box"><h3>模型分布</h3><div id="chartPie" class="chart tall"></div></div>
        </div>
        <div class="chart-grid-2">
          <div class="chart-box"><h3>Token 消耗（输入/输出）</h3><div id="chartTokens" class="chart"></div></div>
          <div class="chart-box"><h3>成本趋势</h3><div id="chartCost" class="chart"></div></div>
        </div>
      </div>

      <!-- 模型管理 -->
      <div class="tab" id="tab-models">
        <div class="card" id="fallbackCard">
          <div class="card-header"><h2>兜底上游（OpenCode Free）</h2></div>
          <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
            <div class="form-group" style="margin:0">
              <label>启用兜底上游</label>
              <select id="fbEnabled" style="width:80px"><option value="1">是</option><option value="0">否</option></select>
            </div>
            <div class="form-group" style="margin:0;flex:1;min-width:240px">
              <label>降权系数 <span id="fbPenaltyVal" style="color:var(--accent)">0.1</span> <span style="color:var(--text-subtle);font-size:11px">(0.01=极低优先级,1.0=同等)</span></label>
              <input id="fbPenalty" type="range" min="0.01" max="1.0" step="0.01" value="0.1" oninput="document.getElementById('fbPenaltyVal').textContent=this.value" style="padding:0">
            </div>
            <button class="btn primary" onclick="saveFallbackConfig()">保存配置</button>
            <button class="btn" id="refreshFallbackBtn" onclick="refreshFallback()">刷新兜底模型</button>
          </div>
          <div id="fbInfo" style="margin-top:12px;color:var(--text-muted);font-size:12px"></div>
        </div>
        <div class="card">
          <div class="card-header">
            <h2>模型列表</h2>
            <div class="actions"><button class="btn primary" onclick="showModelModal()">+ 添加模型</button></div>
          </div>
          <table><thead><tr><th>名称</th><th>上游地址</th><th>上游模型</th><th>协议</th><th>RPM</th><th>TPM</th><th>别名</th><th>类型</th><th>启用</th><th>操作</th></tr></thead>
          <tbody id="modelTable"></tbody></table>
        </div>
      </div>

      <!-- Key 池 -->
      <div class="tab" id="tab-keys">
        <div class="card">
          <div class="card-header">
            <h2>Key 列表</h2>
            <div class="actions">
              <button class="btn" onclick="testAllKeys()">测试全部</button>
              <button class="btn primary" onclick="showKeyModal()">+ 添加 Key</button>
              <button class="btn primary" onclick="showBatchImportModal()">批量导入</button>
            </div>
          </div>
          <table><thead><tr><th>标签</th><th>模型</th><th>Key</th><th>优先级</th><th>启用</th><th>连通性</th><th>操作</th></tr></thead>
          <tbody id="keyTable"></tbody></table>
        </div>
      </div>

      <!-- 分组策略 -->
      <div class="tab" id="tab-groups">
        <div class="card">
          <div class="card-header">
            <h2>分组列表</h2>
            <div class="actions"><button class="btn primary" onclick="showGroupModal()">+ 添加分组</button></div>
          </div>
          <table><thead><tr><th>名称</th><th>策略</th><th>成员</th><th>操作</th></tr></thead>
          <tbody id="groupTable"></tbody></table>
        </div>
      </div>

      <!-- 访问令牌 -->
      <div class="tab" id="tab-tokens">
        <div class="card">
          <div class="card-header">
            <h2>访问令牌</h2>
            <div class="actions"><button class="btn primary" onclick="showTokenModal()">+ 创建令牌</button></div>
          </div>
          <table><thead><tr><th>标签</th><th>令牌值</th><th>配额</th><th>白名单</th><th>有效期</th><th>启用</th><th>操作</th></tr></thead>
          <tbody id="tokenTable"></tbody></table>
        </div>
      </div>

      <!-- 请求日志 -->
      <div class="tab" id="tab-logs">
        <div class="card">
          <div class="card-header"><h2>请求日志</h2><div class="actions"><button class="btn small" onclick="loadLogs()">刷新</button></div></div>
          <div id="logsTable"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- 模态框 -->
<div class="modal-overlay" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modalContent"></div>
</div>

<script>
const API = "";
let models = [], keys = [], groups = [], authToken = localStorage.getItem("zhongzhuan_token") || "";
let loading = 0;
let charts = {};
let testResults = {}; // key_id -> {ok, latency, error}

function showLoading(show) {
  if (show) { loading++; document.body.style.cursor = "wait"; }
  else { loading = Math.max(0, loading - 1); if (loading === 0) document.body.style.cursor = ""; }
}

async function api(path, opts = {}) {
  try {
    showLoading(true);
    const headers = {"Content-Type": "application/json"};
    if (authToken) headers["Authorization"] = "Bearer " + authToken;
    const r = await fetch(API + path, {headers, ...opts});
    showLoading(false);
    if (r.status === 401) {
      authToken = ""; localStorage.removeItem("zhongzhuan_token"); checkAuth(); return null;
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({error: {message: r.statusText}}));
      console.error("API error:", path, err);
      return null;
    }
    return r.json();
  } catch(e) {
    console.error("API fetch error:", e);
    showLoading(false);
    return null;
  }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function fmtNum(n) {
  if (n == null) return "0";
  if (n >= 1000000) return (n/1000000).toFixed(2) + "M";
  if (n >= 1000) return (n/1000).toFixed(1) + "K";
  return String(n);
}

function fmtCost(c) {
  if (!c) return "¥0.00";
  return "¥" + c.toFixed(4);
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", {hour12: false});
}

// ---- 认证 ----
async function checkAuth() {
  const s = await api("/api/auth/status");
  if (!s) return;
  if (s.auth_enabled) {
    if (!authToken) { document.getElementById("loginOverlay").classList.add("show"); return; }
    const me = await api("/api/auth/me");
    if (!me || !me.username) {
      authToken = ""; localStorage.removeItem("zhongzhuan_token");
      document.getElementById("loginOverlay").classList.add("show"); return;
    }
    document.getElementById("logoutBtn").style.display = "";
    document.getElementById("navTokens").style.display = "flex";
  }
  document.getElementById("loginOverlay").classList.remove("show");
  document.getElementById("sideVer").textContent = s.version || "";
  // 刷新/登录后始终默认展示仪表盘，避免所有 tab 内容堆叠
  showTab("dashboard");
  loadSvcStatus();
}

async function doLogin() {
  const user = document.getElementById("loginUser").value;
  const pass = document.getElementById("loginPass").value;
  const r = await fetch(API + "/api/auth/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({username: user, password: pass})
  });
  if (!r.ok) { document.getElementById("loginError").textContent = "用户名或密码错误"; return; }
  const data = await r.json();
  authToken = data.token;
  localStorage.setItem("zhongzhuan_token", authToken);
  document.getElementById("loginError").textContent = "";
  checkAuth();
}

function doLogout() {
  authToken = ""; localStorage.removeItem("zhongzhuan_token");
  document.getElementById("logoutBtn").style.display = "none";
  document.getElementById("navTokens").style.display = "none";
  checkAuth();
}

// ---- Tab 切换 ----
const titles = {dashboard:"仪表盘", models:"模型管理", keys:"Key 池", groups:"分组策略", tokens:"访问令牌", logs:"请求日志"};
function showTab(name) {
  document.querySelectorAll(".tab").forEach(t => { t.style.display = "none"; t.classList.remove("active"); });
  document.querySelectorAll(".nav-item").forEach(a => a.classList.remove("active"));
  const tab = document.getElementById("tab-" + name);
  if (tab) { tab.style.display = "block"; tab.classList.add("active"); }
  document.querySelectorAll('.nav-item[data-tab="' + name + '"]').forEach(a => a.classList.add("active"));
  document.getElementById("pageTitle").textContent = titles[name] || name;
  window.scrollTo(0, 0);
  if (name === "dashboard") loadOverview();
  if (name === "models") { loadModels(); loadFallbackStatus(); }
  if (name === "keys") { loadModels(); loadKeys(); }
  if (name === "groups") loadGroups();
  if (name === "tokens") loadTokens();
  if (name === "logs") loadLogs();
}

// ---- 仪表盘 ----
async function loadOverview() {
  // 实时 KPI
  const s = await api("/api/stats?range=1h");
  if (s) {
    document.getElementById("kpiGrid").innerHTML = `
      <div class="kpi-card"><div class="label">QPS (近1小时)</div><div class="value accent">${s.qps}</div><div class="delta">总请求 ${fmtNum(s.total_requests||0)}</div><div class="icon">&#9650;</div></div>
      <div class="kpi-card"><div class="label">成功率</div><div class="value success">${(s.success_rate*100).toFixed(1)}%</div><div class="delta">活跃 Key ${s.active_keys||0}</div><div class="icon">&#10003;</div></div>
      <div class="kpi-card"><div class="label">平均延迟</div><div class="value">${s.avg_latency_ms}ms</div><div class="delta">P50 延迟</div><div class="icon">&#8635;</div></div>
      <div class="kpi-card"><div class="label">错误数 (近1h)</div><div class="value danger">${(s.top_errors||[]).reduce((a,e)=>a+e.count,0)}</div><div class="delta">${(s.top_errors||[]).map(e=>e.status+":"+e.count).join(", ")||"无错误"}</div><div class="icon">&#9888;</div></div>`;
  }
  // 用量统计（7 天）
  const u = await api("/api/stats/usage?days=7");
  if (u) renderCharts(u);
}

function renderCharts(u) {
  const dark = {backgroundColor:"transparent", textStyle:{color:"#7d8590", fontFamily:"inherit"}, grid:{left:48, right:24, top:30, bottom:32}};
  const axisLine = {lineStyle:{color:"#30363d"}};
  const axisLabel = {color:"#7d8590", fontSize:11};

  // 1. 请求趋势折线
  let c1 = charts.trend || echarts.init(document.getElementById("chartTrend"));
  c1.setOption({
    ...dark,
    tooltip:{trigger:"axis", backgroundColor:"#161b22", borderColor:"#30363d", textStyle:{color:"#e6edf3"}},
    xAxis:{type:"category", data:(u.daily||[]).map(d=>d.date), axisLine, axisLabel},
    yAxis:{type:"value", axisLine, axisLabel, splitLine:{lineStyle:{color:"#21262d"}}},
    series:[{name:"请求数", type:"line", smooth:true, data:(u.daily||[]).map(d=>d.requests), itemStyle:{color:"#2f81f7"}, areaStyle:{color:"rgba(47,129,247,0.15)"}}],
    legend:{show:false}
  });
  charts.trend = c1;

  // 2. 模型分布饼图
  let c2 = charts.pie || echarts.init(document.getElementById("chartPie"));
  const pieData = (u.by_model||[]).slice(0, 8).map(m => ({name:m.model_name||"unknown", value:m.requests}));
  c2.setOption({
    ...dark,
    tooltip:{trigger:"item", backgroundColor:"#161b22", borderColor:"#30363d", textStyle:{color:"#e6edf3"}},
    legend:{type:"scroll", orient:"vertical", right:8, top:"center", textStyle:{color:"#7d8590", fontSize:11}},
    series:[{
      type:"pie", radius:["40%","70%"], center:["38%","50%"],
      data:pieData, label:{show:false},
      color:["#2f81f7","#3fb950","#d29922","#f85149","#a371f7","#f0883e","#79c0ff","#56d4de"]
    }]
  });
  charts.pie = c2;

  // 3. Token 堆叠柱状图
  let c3 = charts.tokens || echarts.init(document.getElementById("chartTokens"));
  c3.setOption({
    ...dark,
    tooltip:{trigger:"axis", backgroundColor:"#161b22", borderColor:"#30363d", textStyle:{color:"#e6edf3"}},
    legend:{data:["输入","输出"], textStyle:{color:"#7d8590", fontSize:11}, top:0},
    xAxis:{type:"category", data:(u.daily||[]).map(d=>d.date), axisLine, axisLabel},
    yAxis:{type:"value", axisLine, axisLabel, splitLine:{lineStyle:{color:"#21262d"}}},
    series:[
      {name:"输入", type:"bar", stack:"tok", data:(u.daily||[]).map(d=>d.tokens_in), itemStyle:{color:"#2f81f7"}},
      {name:"输出", type:"bar", stack:"tok", data:(u.daily||[]).map(d=>d.tokens_out), itemStyle:{color:"#3fb950"}}
    ]
  });
  charts.tokens = c3;

  // 4. 成本趋势
  let c4 = charts.cost || echarts.init(document.getElementById("chartCost"));
  c4.setOption({
    ...dark,
    tooltip:{trigger:"axis", backgroundColor:"#161b22", borderColor:"#30363d", textStyle:{color:"#e6edf3"}, valueFormatter:v=>"¥"+Number(v).toFixed(4)},
    xAxis:{type:"category", data:(u.daily||[]).map(d=>d.date), axisLine, axisLabel},
    yAxis:{type:"value", axisLine, axisLabel, splitLine:{lineStyle:{color:"#21262d"}}},
    series:[{name:"成本", type:"line", smooth:true, data:(u.daily||[]).map(d=>d.cost), itemStyle:{color:"#d29922"}, areaStyle:{color:"rgba(210,153,34,0.2)"}}],
    legend:{show:false}
  });
  charts.cost = c4;
}

// ---- 模型管理 ----
let clientPresetOptions = []; // [{key,label}] 内置预设，前端在头加"不模拟"、尾加"自定义"

async function ensurePresetOptions() {
  if (clientPresetOptions.length > 0) return;
  const r = await api("/api/models/client-preset-options");
  if (r && Array.isArray(r.presets)) clientPresetOptions = r.presets;
}

function presetBadge(m) {
  if (!m.client_preset) return "";
  if (m.client_preset === "custom") return ' <span class="tag custom">自定义模拟</span>';
  const p = clientPresetOptions.find(x => x.key === m.client_preset);
  return ' <span class="tag" style="background:rgba(59,130,246,0.15);color:#60a5fa">模拟' + esc(p ? p.label.split(" ")[0] : m.client_preset) + '</span>';
}

async function loadModels() {
  await ensurePresetOptions();
  const d = await api("/api/models");
  models = d?.data || [];
  // 自定义模型在前，兜底模型分组永远在最后，且默认折叠
  const custom = models.filter(m => !m.is_fallback);
  const fb = models.filter(m => m.is_fallback);
  const fbCollapsed = localStorage.getItem("fbModelsCollapsed") !== "0";
  const arrow = fbCollapsed ? "\u25B6" : "\u25BC";
  let html = "";
  if (custom.length > 0) {
    html += custom.map(m => `
      <tr><td><strong>${esc(m.name)}</strong>${presetBadge(m)}</td><td>${esc(m.upstream_base)}</td><td>${esc(m.upstream_model)}</td>
      <td><code>${m.protocol||"openai"}</code></td>
      <td>${m.rpm_limit||"不限"}</td><td>${m.tpm_limit||"不限"}</td>
      <td>${m.aliases? '<code>'+esc(m.aliases)+'</code>' : '<span style="color:var(--text-subtle)">-</span>'}</td>
      <td><span class="tag custom">自定义</span></td>
      <td>${m.enabled? '<span class="health-dot good"></span>是' : '<span class="health-dot bad"></span>否'}</td>
      <td><button class="btn small" onclick="editModel(${m.id})">编辑</button> <button class="btn small danger" onclick="delModel(${m.id})">删除</button></td></tr>`).join("");
  }
  // 兜底模型分组（永远在最后，可折叠）
  if (fb.length > 0) {
    html += '<tr class="group-header" onclick="toggleFbModels()">' +
      '<td colspan="10"><span style="display:inline-block;width:16px;color:var(--text-muted)">' + arrow + '</span> ' +
      '<span class="tag fallback">兜底</span> <strong>内置兜底模型</strong>' +
      '<span style="color:var(--text-muted);font-weight:400;margin-left:8px">' + fb.length + ' 个</span>' +
      '<span style="float:right;color:var(--text-subtle);font-weight:400;font-size:12px">OpenCode Free 自动同步</span>' +
      '</td></tr>';
    if (!fbCollapsed) {
      html += fb.map(m => `
      <tr><td><strong>${esc(m.name)}</strong></td><td>${esc(m.upstream_base)}</td><td>${esc(m.upstream_model)}</td>
      <td><code>${m.protocol||"openai"}</code></td>
      <td>${m.rpm_limit||"不限"}</td><td>${m.tpm_limit||"不限"}</td>
      <td>${m.aliases? '<code>'+esc(m.aliases)+'</code>' : '<span style="color:var(--text-subtle)">-</span>'}</td>
      <td><span class="tag fallback">兜底</span></td>
      <td>${m.enabled? '<span class="health-dot good"></span>是' : '<span class="health-dot bad"></span>否'}</td>
      <td><button class="btn small" onclick="editModel(${m.id})">编辑</button></td></tr>`).join("");
    }
  }
  document.getElementById("modelTable").innerHTML = html || '<tr><td colspan="10" class="empty">还没有模型,点击右上角添加</td></tr>';
}

function toggleFbModels() {
  // 当前折叠则展开(0)，当前展开则折叠(1)
  const collapsed = localStorage.getItem("fbModelsCollapsed") !== "0";
  localStorage.setItem("fbModelsCollapsed", collapsed ? "0" : "1");
  loadModels();
}

async function refreshFallback() {
  const btn = document.getElementById("refreshFallbackBtn");
  const info = document.getElementById("fbInfo");
  if (btn.disabled) return;
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "刷新中...";
  info.textContent = "正在从 OpenCode Free 拉取并同步兜底模型，请稍候...";
  try {
    const r = await api("/api/fallback/refresh", {method:"POST"});
    if (r === null) {
      info.innerHTML = '<span style="color:var(--danger)">刷新失败，请查看服务日志或稍后重试。</span>';
      return;
    }
    await Promise.all([loadModels(), loadFallbackStatus()]);
    info.innerHTML += ' | <span style="color:var(--success)">刚刚同步 <strong>' + r.synced + "</strong> 个模型</span>";
    alert("已同步 " + r.synced + " 个 OpenCode Free 兜底模型:\\n" + (r.models||[]).join(", "));
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function loadFallbackStatus() {
  const s = await api("/api/fallback/status");
  if (!s) return;
  document.getElementById("fbEnabled").value = s.enabled ? "1" : "0";
  document.getElementById("fbPenalty").value = s.fallback_penalty;
  document.getElementById("fbPenaltyVal").textContent = s.fallback_penalty;
  document.getElementById("fbInfo").innerHTML =
    "上游: <code>" + esc(s.upstream_base) + "</code> | 前缀: <code>" + esc(s.model_prefix) +
    "</code> | 已有兜底模型: <strong>" + s.fallback_model_count + "</strong> 个";
}

async function saveFallbackConfig() {
  const body = {
    enabled: document.getElementById("fbEnabled").value === "1",
    fallback_penalty: parseFloat(document.getElementById("fbPenalty").value),
  };
  const r = await api("/api/fallback/config", {method:"PUT", body:JSON.stringify(body)});
  if (r !== null) alert("兜底配置已保存:\\n启用=" + (r.enabled?"是":"否") + "\\n降权系数=" + r.fallback_penalty);
}

async function delModel(id) {
  if (!confirm("确认删除此模型?绑定到此模型的 Key 也会被删除。")) return;
  const r = await api("/api/models/" + id, {method:"DELETE"});
  if (r !== null) loadModels();
}

function showModelModal(model) {
  const isEdit = !!model;
  const preset = isEdit ? (model.client_preset || "") : "";
  // 下拉选项：头"不模拟" + 中间内置预设(按 list_presets 顺序) + 尾"自定义"
  const presetOpts = ['<option value=""' + (preset === "" ? " selected" : "") + '>不模拟</option>']
    .concat(clientPresetOptions.map(p => '<option value="' + esc(p.key) + '"' + (preset === p.key ? " selected" : "") + '>' + esc(p.label) + '</option>'))
    .concat(['<option value="custom"' + (preset === "custom" ? " selected" : "") + '>自定义</option>']);

  // 自定义头初始行：编辑模式解析 model.custom_headers, 否则两空行
  let customRows = [];
  if (isEdit && model.custom_headers) {
    try { customRows = JSON.parse(model.custom_headers) || []; } catch (e) { customRows = []; }
  }
  if (customRows.length === 0) customRows = [{name:"", value:""}, {name:"", value:""}];

  document.getElementById("modalContent").innerHTML = `
    <h3>${isEdit ? "编辑模型" : "添加模型"}</h3>
    <div class="form-group"><label>名称 <span style="color:var(--text-subtle)">(客户端请求时使用的模型名)</span></label><input id="f_name" value="${isEdit ? esc(model.name) : ""}"></div>
    <div class="form-row">
      <div class="form-group"><label>上游地址</label><input id="f_upstream_base" placeholder="https://api.openai.com/v1" value="${isEdit ? esc(model.upstream_base) : ""}"></div>
      <div class="form-group"><label>上游模型名</label><input id="f_upstream_model" placeholder="gpt-4o" value="${isEdit ? esc(model.upstream_model) : ""}"></div>
    </div>
    <div class="form-group"><label>上游完整地址覆盖 <span style="color:var(--text-subtle)">(留空自动拼接,可填路径或完整URL)</span></label><input id="f_upstream_path_override" placeholder="/openai/v1/chat/completions" value="${isEdit ? esc(model.upstream_path_override||"") : ""}"></div>
    <div class="form-group"><label>模型别名 <span style="color:var(--text-subtle)">(逗号分隔,客户端用别名请求时也会路由到此模型)</span></label><input id="f_aliases" placeholder="gpt-4, gpt4, chatgpt" value="${isEdit ? esc(model.aliases||"") : ""}"></div>
    <div class="form-row-3">
      <div class="form-group"><label>上游协议</label><select id="f_protocol"><option value="openai" ${isEdit && model.protocol === "openai" ? "selected" : ""}>OpenAI</option><option value="anthropic" ${isEdit && model.protocol === "anthropic" ? "selected" : ""}>Anthropic</option></select></div>
      <div class="form-group"><label>RPM 限制</label><input id="f_rpm" type="number" value="${isEdit ? model.rpm_limit : 0}"></div>
      <div class="form-group"><label>TPM 限制</label><input id="f_tpm" type="number" value="${isEdit ? model.tpm_limit : 0}"></div>
    </div>
    <div class="form-group">
      <label>客户端模拟 <span style="color:var(--text-subtle)">(模拟特定客户端指纹以走限免通道,"自定义"在最后)</span></label>
      <select id="f_client_preset" onchange="toggleClientPreset()">${presetOpts.join("")}</select>
    </div>
    <div id="f_preset_hint" style="display:none;padding:8px 12px;background:var(--bg-elevated);border-radius:6px;color:var(--text-subtle);font-size:13px;margin-bottom:12px"></div>
    <div id="f_custom_headers_wrap" style="display:none;margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <label style="margin:0">自定义请求头</label>
        <button type="button" class="btn small" onclick="addCustomHeaderRow()">+ 添加 Header</button>
      </div>
      <div id="f_custom_headers" style="display:flex;flex-direction:column;gap:6px"></div>
      <div style="color:var(--text-subtle);font-size:12px;margin-top:6px">支持模板变量：<code>{{uuid}}</code>（每请求生成新 UUID）。禁止 Authorization/Host/Content-Length 等受控头。</div>
    </div>
    <div class="form-group"><label>启用</label><select id="f_enabled"><option value="1" ${isEdit && model.enabled ? "selected" : ""}>是</option><option value="0" ${isEdit && !model.enabled ? "selected" : ""}>否</option></select></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="saveModel(${isEdit ? model.id : ""})">保存</button></div>`;
  document.getElementById("modal").classList.add("show");

  // 渲染自定义头初始行 + 触发条件展示
  renderCustomHeaderRows(customRows);
  toggleClientPreset();
}

function renderCustomHeaderRows(rows) {
  const wrap = document.getElementById("f_custom_headers");
  if (!wrap) return;
  wrap.innerHTML = rows.map((r, i) => `
    <div class="form-row" data-row="${i}" style="gap:6px;align-items:center">
      <input placeholder="Header 名称,如 X-Client-Name" value="${esc(r.name||"")}" style="flex:1" oninput="validateCustomHeaderName(this)">
      <input placeholder="值,如 workbuddy 或 {{uuid}}" value="${esc(r.value||"")}" style="flex:1">
      <button type="button" class="btn small danger" onclick="removeCustomHeaderRow(this)">删除</button>
    </div>`).join("");
}

function addCustomHeaderRow() {
  const wrap = document.getElementById("f_custom_headers");
  const i = wrap.children.length;
  const div = document.createElement("div");
  div.className = "form-row";
  div.style.cssText = "gap:6px;align-items:center";
  div.dataset.row = i;
  div.innerHTML = `<input placeholder="Header 名称,如 X-Client-Name" value="" style="flex:1" oninput="validateCustomHeaderName(this)">
    <input placeholder="值,如 workbuddy 或 {{uuid}}" value="" style="flex:1">
    <button type="button" class="btn small danger" onclick="removeCustomHeaderRow(this)">删除</button>`;
  wrap.appendChild(div);
}

function removeCustomHeaderRow(btn) {
  btn.closest('[data-row]').remove();
}

function validateCustomHeaderName(input) {
  const v = input.value.trim().toLowerCase();
  if (["authorization","host","content-length","transfer-encoding","connection"].includes(v)) {
    input.style.borderColor = "var(--danger)";
    input.title = "不允许设置受控头: " + input.value;
  } else {
    input.style.borderColor = "";
    input.title = "";
  }
}

function toggleClientPreset() {
  const sel = document.getElementById("f_client_preset");
  if (!sel) return;
  const v = sel.value;
  const hint = document.getElementById("f_preset_hint");
  const wrap = document.getElementById("f_custom_headers_wrap");
  if (v === "") {
    if (hint) hint.style.display = "none";
    if (wrap) wrap.style.display = "none";
  } else if (v === "custom") {
    if (hint) hint.style.display = "none";
    if (wrap) wrap.style.display = "block";
  } else {
    const p = clientPresetOptions.find(x => x.key === v);
    if (hint) {
      hint.style.display = "block";
      hint.textContent = p ? ('将自动注入 ' + (p.label) + ' 的指纹头（User-Agent、X-Client-Name、X-Request-ID 等，含 {{uuid}} 动态变量）') : "";
    }
    if (wrap) wrap.style.display = "none";
  }
}

function editModel(id) {
  const m = models.find(x => x.id === id);
  if (m) showModelModal(m);
}

async function saveModel(id) {
  const clientPreset = document.getElementById("f_client_preset").value;
  let customHeadersJson = "";
  if (clientPreset === "custom") {
    // 收集自定义头：过滤 name 空行
    const rows = document.querySelectorAll('#f_custom_headers [data-row]');
    const headers = [];
    rows.forEach(row => {
      const inputs = row.querySelectorAll('input');
      const name = inputs[0].value.trim();
      const value = inputs[1].value;
      if (name) headers.push({name, value});
    });
    customHeadersJson = JSON.stringify(headers);
  } else if (id) {
    // 编辑模式下保留已有的 custom_headers（方便切换不丢失）
    const m = models.find(x => x.id === id);
    if (m) customHeadersJson = m.custom_headers || "";
  }
  const body = {
    name: document.getElementById("f_name").value.trim(),
    upstream_base: document.getElementById("f_upstream_base").value.trim(),
    upstream_model: document.getElementById("f_upstream_model").value.trim(),
    upstream_path_override: document.getElementById("f_upstream_path_override").value.trim(),
    aliases: document.getElementById("f_aliases").value.trim(),
    protocol: document.getElementById("f_protocol").value,
    rpm_limit: parseInt(document.getElementById("f_rpm").value)||0,
    tpm_limit: parseInt(document.getElementById("f_tpm").value)||0,
    enabled: document.getElementById("f_enabled").value === "1",
    client_preset: clientPreset,
    custom_headers: customHeadersJson,
  };
  if (!body.name || !body.upstream_base) { alert("名称和上游地址不能为空"); return; }
  let r;
  if (id) r = await api("/api/models/" + id, {method:"PUT", body: JSON.stringify(body)});
  else r = await api("/api/models", {method:"POST", body: JSON.stringify(body)});
  if (r !== null) { closeModal(); loadModels(); }
}

// ---- Key 池 ----
async function loadKeys() {
  const [dk, dm] = await Promise.all([api("/api/keys"), api("/api/models")]);
  if (!dk && !dm) return;
  keys = dk?.data || [];
  models = dm?.data || [];
  const modelMap = {};
  models.forEach(m => modelMap[m.id] = m);

  // 兜底模型是内置模型，Key 池不显示它们
  const visibleModels = models.filter(m => !m.is_fallback);
  const groupsMap = new Map();
  for (const m of visibleModels) groupsMap.set(m.id, {model: m, keys: []});
  for (const k of keys) {
    const ownerModel = modelMap[k.model_id];
    if (ownerModel && ownerModel.is_fallback) continue; // 兜底模型的 Key 也不显示
    const g = groupsMap.get(k.model_id) || {model: {id: k.model_id, name: "已删除模型#" + k.model_id}, keys: []};
    g.keys.push(k);
    groupsMap.set(k.model_id, g);
  }

  const collapsed = JSON.parse(localStorage.getItem("keyGroupCollapsed") || "{}");
  if (visibleModels.length === 0) {
    document.getElementById("keyTable").innerHTML = '<tr><td colspan="7" class="empty">还没有模型,先在「模型管理」添加模型,然后点击 + 添加 Key</td></tr>';
    return;
  }

  const parts = [];
  for (const [mid, g] of groupsMap) {
    const m = g.model;
    const isCollapsed = !!collapsed[mid];
    const arrow = isCollapsed ? "\\u25B6" : "\\u25BC";
    parts.push(
      '<tr class="group-header" data-model="' + mid + '" onclick="toggleKeyGroup(' + mid + ')">' +
        '<td colspan="7">' +
          '<span class="kg-arrow" style="display:inline-block;width:16px;color:var(--text-muted)">' + arrow + '</span> ' +
          esc(m.name) +
          '<span style="color:var(--text-muted);font-weight:400;margin-left:8px">' + g.keys.length + ' 个 Key</span>' +
          '<span style="float:right;color:var(--text-subtle);font-weight:400;font-size:12px">' + esc(m.upstream_base||"") + ' · ' + esc(m.upstream_model||"") + '</span>' +
        '</td></tr>');
    if (!isCollapsed) {
      if (g.keys.length === 0) {
        parts.push('<tr><td colspan="7" class="empty">该模型下还没有 Key</td></tr>');
      } else {
        for (const k of g.keys) {
          const tr = testResults[k.id];
          let connCell = '<span style="color:var(--text-subtle)">未测试</span>';
          if (tr) {
            if (tr.ok) connCell = '<span class="tag ok">OK ' + tr.latency + 'ms</span>';
            else connCell = '<span class="tag err">失败</span>';
          }
          parts.push(
            '<tr>' +
              '<td>' + esc(k.label) + '</td>' +
              '<td>' + esc(m.name) + '</td>' +
              '<td><code>' + esc(k.key_masked) + '</code></td>' +
              '<td>' + k.priority + '</td>' +
              '<td>' + (k.enabled? '<span class="health-dot good"></span>是' : '<span class="health-dot bad"></span>否') + '</td>' +
              '<td>' + connCell + '</td>' +
              '<td><button class="btn small" onclick="testKey(' + k.id + ')">测试</button> <button class="btn small danger" onclick="delKey(' + k.id + ')">删除</button></td>' +
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
  loadKeys();
}

async function delKey(id) {
  if (!confirm("确认删除此 Key?")) return;
  const r = await api("/api/keys/" + id, {method:"DELETE"});
  if (r !== null) loadKeys();
}

async function testKey(id) {
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = "测试中..."; }
  const r = await api("/api/keys/" + id + "/test", {method:"POST"});
  if (btn) { btn.disabled = false; btn.textContent = "测试"; }
  if (!r) return;
  testResults[id] = {ok: r.ok, latency: r.latency_ms, error: r.error};
  loadKeys();
  // 显示详情
  if (r.ok) {
    alert("连通性测试通过\\n\\n模型: " + r.model + "\\n延迟: " + r.latency_ms + "ms\\nURL: " + r.url);
  } else {
    alert("连通性测试失败\\n\\n状态码: " + r.status + "\\n错误: " + r.error + "\\nURL: " + r.url);
  }
}

async function testAllKeys() {
  const allKeys = keys.slice();
  if (allKeys.length === 0) { alert("没有可测试的 Key"); return; }
  if (!confirm("将测试全部 " + allKeys.length + " 个 Key,可能产生少量请求费用,继续?")) return;
  testResults = {};
  let okCount = 0, failCount = 0;
  for (const k of allKeys) {
    const r = await api("/api/keys/" + k.id + "/test", {method:"POST"});
    if (r) {
      testResults[k.id] = {ok: r.ok, latency: r.latency_ms, error: r.error};
      if (r.ok) okCount++; else failCount++;
    }
    loadKeys();
  }
  alert("测试完成\\n\\n成功: " + okCount + " 个\\n失败: " + failCount + " 个");
}

function showKeyModal() {
  // 兜底模型是内置模型，不给它们添加 Key
  const opts = models.filter(m => !m.is_fallback).map(m => '<option value="' + m.id + '">' + esc(m.name) + '</option>').join("");
  document.getElementById("modalContent").innerHTML = `
    <h3>添加 Key</h3>
    <div class="form-group"><label>模型</label><select id="f_model_id">${opts}</select></div>
    <div class="form-group"><label>标签</label><input id="f_label" placeholder="例如:主账号"></div>
    <div class="form-group"><label>Key 值</label><input id="f_key_value" type="password" placeholder="sk-..."></div>
    <div class="form-group"><label>优先级</label><input id="f_priority" type="number" value="0"><div class="form-hint">数字越大优先级越高</div></div>
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
  // 兜底模型是内置模型，批量导入同样只针对自定义模型
  const opts = models.filter(m => !m.is_fallback).map(m => '<option value="' + m.id + '">' + esc(m.name) + '</option>').join("");
  document.getElementById("modalContent").innerHTML = `
    <h3>批量导入 Key</h3>
    <div class="form-group"><label>模型</label><select id="f_batch_model_id">${opts}</select></div>
    <div class="form-group"><label>Key 列表</label><textarea id="f_batch_keys" placeholder="每行一个 Key,可选格式: label|key_value|priority\\n例如:\\nkey1|sk-xxx123|0\\nkey2|sk-yyy456|1"></textarea></div>
    <div class="form-group"><label>默认优先级</label><input id="f_batch_priority" type="number" value="0"></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="batchImportKeys()">导入</button></div>`;
  document.getElementById("modal").classList.add("show");
}

async function batchImportKeys() {
  const modelId = parseInt(document.getElementById("f_batch_model_id").value);
  const text = document.getElementById("f_batch_keys").value.trim();
  const defaultPriority = parseInt(document.getElementById("f_batch_priority").value)||0;
  if (!text) return;
  const lines = text.split("\\n").filter(l => l.trim());
  let success = 0, failed = 0;
  for (const line of lines) {
    const parts = line.trim().split("|");
    const keyValue = parts.length >= 2 ? parts[1].trim() : parts[0].trim();
    if (!keyValue) { failed++; continue; }
    const label = parts.length >= 2 ? parts[0].trim() : "";
    const priority = parts.length >= 3 ? parseInt(parts[2].trim())||defaultPriority : defaultPriority;
    try {
      const r = await api("/api/keys", {method:"POST", body:JSON.stringify({model_id: modelId, label, key_value: keyValue, priority})});
      if (r !== null) success++; else failed++;
    } catch(e) { failed++; }
  }
  closeModal(); loadKeys();
  alert("导入完成: 成功 " + success + " 个, 失败 " + failed + " 个");
}

// ---- 分组 ----
async function loadGroups() {
  const [dg, dm] = await Promise.all([api("/api/groups"), api("/api/models")]);
  groups = dg?.data || [];
  models = dm?.data || [];
  const modelMap = {};
  models.forEach(m => modelMap[m.id] = m.name);
  document.getElementById("groupTable").innerHTML = groups.length === 0
    ? '<tr><td colspan="4" class="empty">还没有分组</td></tr>'
    : groups.map(g => `
      <tr><td><strong>${esc(g.name)}</strong></td><td><code>${esc(g.strategy)}</code></td>
      <td>${(g.members||[]).map(x => esc(modelMap[x.model_id] || ("model#"+x.model_id)) + '<span style="color:var(--text-subtle);font-size:11px">(w'+(x.weight||1)+',o'+(x.ord||0)+')</span>').join(", ") || '<span style="color:var(--text-subtle)">无</span>'}</td>
      <td><button class="btn small" onclick="editGroup(${g.id})">编辑</button> <button class="btn small danger" onclick="delGroup(${g.id})">删除</button></td></tr>`).join("");
}

async function delGroup(id) {
  if (!confirm("确认删除此分组?")) return;
  const r = await api("/api/groups/" + id, {method:"DELETE"});
  if (r !== null) loadGroups();
}

function editGroup(id) {
  const g = groups.find(x => x.id === id);
  if (g) showGroupModal(g);
}

function showGroupModal(group) {
  const isEdit = !!group;
  const initMembers = isEdit ? (group.members||[]).map(x => ({model_id:x.model_id, weight:x.weight||1, ord:x.ord||0})) : [];
  const memberMap = {};
  initMembers.forEach(m => memberMap[m.model_id] = m);
  const strat = isEdit ? group.strategy : "round_robin";
  const memberRows = models.map(m => {
    const sel = memberMap[m.id];
    const checked = !!sel;
    const w = sel ? sel.weight : 1;
    const o = sel ? sel.ord : 0;
    const tag = m.is_fallback ? ' <span class="tag fallback">兜底</span>' : "";
    return `<tr>
      <td><input type="checkbox" data-mid="${m.id}" class="gmem-chk" ${checked?"checked":""}></td>
      <td>${esc(m.name)}${tag}</td>
      <td><input type="number" data-mid="${m.id}" class="gmem-w" value="${w}" min="1" style="width:60px" ${checked?"":"disabled"}></td>
      <td><input type="number" data-mid="${m.id}" class="gmem-o" value="${o}" min="0" style="width:60px" ${checked?"":"disabled"}></td>
    </tr>`;
  }).join("");
  document.getElementById("modalContent").innerHTML = `
    <h3>${isEdit ? "编辑分组" : "添加分组"}</h3>
    <div class="form-group"><label>名称 <span style="color:var(--text-subtle)">(作为下游可调用的模型名)</span></label><input id="f_gname" value="${isEdit ? esc(group.name) : ""}"></div>
    <div class="form-group"><label>策略</label><select id="f_strategy"><option value="round_robin" ${strat==="round_robin"?"selected":""}>轮询</option><option value="weighted" ${strat==="weighted"?"selected":""}>加权</option><option value="failover" ${strat==="failover"?"selected":""}>故障转移</option></select>
      <div class="form-hint">加权用权重,故障转移用顺序(小优先)</div></div>
    <div class="form-group"><label>成员模型 <span style="color:var(--text-subtle)">(勾选加入分组)</span></label>
      <table style="width:100%"><thead><tr><th style="width:40px">加入</th><th>模型</th><th style="width:70px">权重</th><th style="width:70px">顺序</th></tr></thead><tbody>${memberRows}</tbody></table>
    </div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="saveGroup(${isEdit ? group.id : ""})">保存</button></div>`;
  document.getElementById("modal").classList.add("show");
  document.querySelectorAll(".gmem-chk").forEach(c => {
    c.addEventListener("change", () => {
      const mid = c.dataset.mid;
      const wEl = document.querySelector('.gmem-w[data-mid="'+mid+'"]');
      const oEl = document.querySelector('.gmem-o[data-mid="'+mid+'"]');
      if (wEl) wEl.disabled = !c.checked;
      if (oEl) oEl.disabled = !c.checked;
    });
  });
}

async function saveGroup(id) {
  const checks = document.querySelectorAll(".gmem-chk");
  const members = [];
  checks.forEach(c => {
    if (c.checked) {
      const mid = parseInt(c.dataset.mid);
      const wEl = document.querySelector('.gmem-w[data-mid="'+mid+'"]');
      const oEl = document.querySelector('.gmem-o[data-mid="'+mid+'"]');
      members.push({model_id: mid, weight: parseInt(wEl?.value)||1, ord: parseInt(oEl?.value)||0});
    }
  });
  members.sort((a,b) => a.ord - b.ord);
  const body = {
    name: document.getElementById("f_gname").value.trim(),
    strategy: document.getElementById("f_strategy").value,
    members,
  };
  if (!body.name) { alert("名称不能为空"); return; }
  let r;
  if (id) r = await api("/api/groups/" + id, {method:"PUT", body:JSON.stringify(body)});
  else r = await api("/api/groups", {method:"POST", body:JSON.stringify(body)});
  if (r !== null) { closeModal(); loadGroups(); }
}

// ---- 访问令牌 ----
async function loadTokens() {
  const d = await api("/api/tokens");
  const tokens = d?.data || [];
  document.getElementById("tokenTable").innerHTML = tokens.length === 0
    ? '<tr><td colspan="7" class="empty">还没有访问令牌</td></tr>'
    : tokens.map(t => {
      let quotaCell;
      if (t.quota_tokens < 0) {
        quotaCell = '<span style="color:var(--text-muted)">无限</span>';
      } else {
        const pct = t.quota_tokens > 0 ? (t.used_tokens / t.quota_tokens * 100) : 0;
        const barClass = pct >= 90 ? "danger" : (pct >= 70 ? "warn" : "");
        quotaCell = '<div>' + fmtNum(t.used_tokens) + ' / ' + fmtNum(t.quota_tokens) + ' <span style="color:var(--text-subtle);font-size:11px">(' + pct.toFixed(1) + '%)</span></div><div class="progress"><div class="progress-bar ' + barClass + '" style="width:' + Math.min(100, pct) + '%"></div></div>';
      }
      const expiry = t.expires_at > 0 ? fmtTime(t.expires_at) : '<span style="color:var(--text-muted)">永久</span>';
      const wl = t.model_whitelist ? '<code>' + esc(t.model_whitelist) + '</code>' : '<span style="color:var(--text-subtle)">全部</span>';
      return `<tr>
        <td>${esc(t.label)}</td>
        <td><code class="token-value">${esc(t.token)}</code> <button class="btn small ghost" onclick="copyToken(${t.id})">复制</button></td>
        <td style="min-width:160px">${quotaCell}</td>
        <td>${wl}</td>
        <td>${expiry}</td>
        <td>${t.enabled? '<span class="health-dot good"></span>是' : '<span class="health-dot bad"></span>否'}</td>
        <td><button class="btn small" onclick="editToken(${t.id})">编辑</button> <button class="btn small ${t.enabled? "warning":"success"}" onclick="toggleToken(${t.id}, ${!t.enabled})">${t.enabled?"禁用":"启用"}</button> <button class="btn small danger" onclick="delToken(${t.id})">删除</button></td>
      </tr>`;
    }).join("");
}

function copyToken(id) {
  api("/api/tokens/" + id + "/reveal", {method:"POST"}).then(r => {
    if (r === null || !r.token) { alert("复制失败：无法获取完整 Key（历史令牌可能未留存原始 Key）"); return; }
    copyText(r.token);
  });
}

function copyText(text) {
  const done = () => alert("已复制到剪贴板");
  const fallback = () => {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) { done(); return; }
      alert("复制失败：浏览器不允许自动复制，请手动复制：\\n\\n" + text);
    } catch (e) {
      alert("复制失败：请手动复制：\\n\\n" + text);
    }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(fallback);
  } else {
    fallback();
  }
}

function showTokenModal() {
  document.getElementById("modalContent").innerHTML = `
    <h3 style="display:flex;align-items:center;gap:8px">创建访问令牌
      <span class="tag" style="background:rgba(47,129,247,0.15);color:var(--accent)">下游 API Key</span>
    </h3>
    <div class="form-group"><label>标签</label>
      <input id="f_tlabel" placeholder="例如：Trae 专用" autocomplete="off">
      <div class="form-hint">用于识别该令牌的用途，仅后台可见</div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Token 配额</label>
        <div style="display:flex;gap:6px;margin-bottom:6px">
          <button type="button" class="btn small qpick" data-q="-1">无限</button>
          <button type="button" class="btn small qpick" data-q="10000">1万</button>
          <button type="button" class="btn small qpick" data-q="100000">10万</button>
          <button type="button" class="btn small qpick" data-q="custom">自定义</button>
        </div>
        <input id="f_quota" type="number" value="-1">
        <div class="form-hint">达到配额后该令牌将拒绝请求</div>
      </div>
      <div class="form-group"><label>有效期</label>
        <div style="display:flex;gap:6px;margin-bottom:6px">
          <button type="button" class="btn small epick" data-d="0">永久</button>
          <button type="button" class="btn small epick" data-d="7">7天</button>
          <button type="button" class="btn small epick" data-d="30">30天</button>
          <button type="button" class="btn small epick" data-d="90">90天</button>
        </div>
        <input id="f_expires" type="number" value="0">
        <div class="form-hint">从创建时刻起的有效天数，0 = 永久</div>
      </div>
    </div>
    <div class="form-group"><label>模型白名单</label>
      <input id="f_whitelist" placeholder="留空 = 允许全部" autocomplete="off">
      <div class="form-hint">逗号分隔，例如 <code>gpt-4o,claude-3-opus</code>；留空表示不限制</div>
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="addToken()" id="tokenCreateBtn">创建令牌</button>
    </div>`;
  document.getElementById("modal").classList.add("show");
  // 快捷选项联动
  document.querySelectorAll(".qpick").forEach(b => b.addEventListener("click", () => {
    const v = b.dataset.q;
    document.getElementById("f_quota").value = v === "custom" ? "" : v;
  }));
  document.querySelectorAll(".epick").forEach(b => b.addEventListener("click", () => {
    document.getElementById("f_expires").value = b.dataset.d;
  }));
}

async function addToken() {
  const btn = document.getElementById("tokenCreateBtn");
  if (!btn || btn.disabled) return;
  btn.disabled = true; btn.textContent = "创建中...";
  const r = await api("/api/tokens", {method:"POST", body:JSON.stringify({
    label: document.getElementById("f_tlabel").value,
    quota_tokens: parseInt(document.getElementById("f_quota").value)||-1,
    expires_days: parseInt(document.getElementById("f_expires").value)||0,
    model_whitelist: document.getElementById("f_whitelist").value.trim(),
  })});
  btn.disabled = false; btn.textContent = "创建令牌";
  if (r === null) return;
  // 创建成功：内嵌展示完整 Key，一次性，可复制
  document.getElementById("modalContent").innerHTML = `
    <h3 style="color:var(--success)">✓ 令牌已创建</h3>
    <div style="background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);padding:12px;margin-bottom:8px">
      <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">完整 Key（仅显示这一次，请妥善保存）</div>
      <div class="token-value" id="newTokenVal" style="font-size:13px">${esc(r.token)}</div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:4px">
      <button class="btn primary" onclick="copyText(document.getElementById('newTokenVal').textContent)">复制 Key</button>
      <button class="btn" onclick="closeModal()">完成</button>
    </div>
    <div class="form-hint">该 Key 已保存为安全加密，之后列表中只会显示掩码；需要时点击「复制」即可取回。</div>`;
  loadTokens();
}

function editToken(id) {
  const t = (keys || []) && null; // tokens 不在全局,从 DOM 不好取,这里简单 reload 后弹窗
  // 简化:直接显示编辑模态框,让用户重新设置
  document.getElementById("modalContent").innerHTML = `
    <h3>编辑令牌 #${id}</h3>
    <div class="form-group"><label>标签</label><input id="e_tlabel"></div>
    <div class="form-row">
      <div class="form-group"><label>Token 配额</label><input id="e_quota" type="number" value="-1"><div class="form-hint">-1 = 无限</div></div>
      <div class="form-group"><label>有效期 (天,从现在起)</label><input id="e_expires" type="number" value="0"><div class="form-hint">0 = 永久/不变</div></div>
    </div>
    <div class="form-group"><label>模型白名单</label><input id="e_whitelist" placeholder="留空 = 允许全部"></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="saveEditToken(${id})">保存</button></div>`;
  // 加载现有数据
  api("/api/tokens").then(d => {
    const t = (d?.data||[]).find(x => x.id === id);
    if (t) {
      document.getElementById("e_tlabel").value = t.label || "";
      document.getElementById("e_quota").value = t.quota_tokens;
      document.getElementById("e_whitelist").value = t.model_whitelist || "";
    }
  });
  document.getElementById("modal").classList.add("show");
}

async function saveEditToken(id) {
  const body = {
    label: document.getElementById("e_tlabel").value,
    quota_tokens: parseInt(document.getElementById("e_quota").value)||-1,
    expires_days: parseInt(document.getElementById("e_expires").value)||0,
    model_whitelist: document.getElementById("e_whitelist").value.trim(),
  };
  const r = await api("/api/tokens/" + id, {method:"PUT", body:JSON.stringify(body)});
  if (r !== null) { closeModal(); loadTokens(); }
}

async function delToken(id) {
  if (!confirm("确认删除此令牌?")) return;
  const r = await api("/api/tokens/" + id, {method:"DELETE"});
  if (r !== null) loadTokens();
}

async function toggleToken(id, enabled) {
  await api("/api/tokens/" + id, {method:"PUT", body:JSON.stringify({enabled})});
  loadTokens();
}

// ---- 日志 ----
async function loadLogs() {
  const d = await api("/api/logs?limit=50");
  if (!d) return;
  const rows = (d.data||[]).map(l => {
    const statusClass = l.status >= 200 && l.status < 300 ? "ok" : (l.status >= 500 ? "err" : "warn");
    return `<tr>
      <td style="white-space:nowrap">${fmtTime(l.ts)}</td>
      <td>${esc(l.model_name)}</td>
      <td><span class="tag ${statusClass}">${l.status}</span></td>
      <td>${l.latency_ms}ms</td>
      <td>${l.tokens_in||0} / ${l.tokens_out||0}</td>
      <td>${l.translated? '<span class="tag warn">译</span>' : ""}</td>
      <td style="color:var(--text-muted);max-width:300px;overflow:hidden;text-overflow:ellipsis">${esc(l.error||"")}</td>
    </tr>`;
  }).join("");
  document.getElementById("logsTable").innerHTML = `<table><thead><tr><th>时间</th><th>模型</th><th>状态</th><th>延迟</th><th>Token (入/出)</th><th>翻译</th><th>错误</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ---- 服务控制 ----
function closeModal() { document.getElementById("modal").classList.remove("show"); }

async function loadSvcStatus() {
  try {
    const s = await api("/api/service/status");
    if (!s) return;
    const badge = document.getElementById("svcStatus");
    const btn = document.getElementById("svcBtn");
    const side = document.getElementById("sideSvcStatus");
    btn.disabled = false;
    if (s.status === "running") {
      badge.className = "status-pill running"; badge.innerHTML = '<span class="dot"></span>运行中';
      btn.textContent = s.control_supported === false ? "当前进程" : "停止";
      btn.disabled = s.control_supported === false;
      side.innerHTML = '<span class="health-dot good"></span><span style="color:var(--success)">服务运行中</span>';
    } else if (s.status === "stopped") {
      badge.className = "status-pill stopped"; badge.innerHTML = '<span class="dot"></span>已停止';
      btn.textContent = "启动";
      side.innerHTML = '<span class="health-dot bad"></span><span style="color:var(--danger)">服务已停止</span>';
    } else {
      badge.className = "status-pill"; badge.innerHTML = '<span class="dot"></span>' + s.status;
      btn.textContent = "安装服务";
      side.innerHTML = '<span class="health-dot warn"></span><span style="color:var(--text-muted)">' + s.status + '</span>';
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
  } catch(e) { console.error("导出失败", e); alert("导出失败: " + e.message); }
}

function importConfig() {
  const input = document.createElement("input");
  input.type = "file"; input.accept = ".zip";
  input.onchange = async () => {
    try {
      const headers = {};
      if (authToken) headers["Authorization"] = "Bearer " + authToken;
      const r = await fetch(API + "/api/import", {method:"POST", body: input.files[0], headers});
      if (!r.ok) throw new Error(r.statusText);
      loadModels(); loadKeys(); loadGroups();
      alert("导入成功");
    } catch(e) { console.error("导入失败", e); alert("导入失败: " + e.message); }
  };
  input.click();
}

// ---- 启动 ----
window.addEventListener("resize", () => {
  Object.values(charts).forEach(c => c && c.resize && c.resize());
});

checkAuth();
setInterval(loadOverview, 30000);
</script>
</body>
</html>"""


def mount_ui(app: web.Application, ctx) -> None:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")

    static_dir = Path(__file__).resolve().parent / "static"
    app.router.add_static("/ui/static/", static_dir)
    app.router.add_get("/", index)
    app.router.add_get("/ui/", index)
