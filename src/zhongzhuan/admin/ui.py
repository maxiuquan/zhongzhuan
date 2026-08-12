"""Admin UI static resources — 浅色主题 + 侧边栏 + ECharts 看板。

设计参考：one-api（清爽浅色 + 蓝主色）/ new-api（浅色 + 绿主色）。
- 左侧固定侧边栏 240px
- 顶部 KPI 卡片网格
- ECharts 数据看板（请求趋势、模型分布、Token 堆叠）
- 浅灰底 + 白卡片 + 蓝色主色（--accent，想换 new-api 绿改这一行即可）
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
  --bg:#f0f2f5; --bg-card:#ffffff; --bg-hover:#f5f7fa; --bg-input:#ffffff; --bg-elevated:#f5f7fa; --bg-subtle:#f5f7fa;
  --border:#e5e7eb; --border-muted:#eef1f4;
  --text:#1f2d3d; --text-muted:#6b7280; --text-subtle:#9ca3af;
  --accent:#2563eb; --accent-hover:#1d4ed8; --accent-soft:rgba(37,99,235,0.10);
  --success:#10b981; --warning:#f59e0b; --danger:#ef4444; --orange:#fb923c;
  --sidebar-w:240px;
  --radius:6px; --radius-lg:10px;
  --shadow:0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,sans-serif;
  background:var(--bg); color:var(--text); font-size:14px; line-height:1.5;
  -webkit-font-smoothing:antialiased;
}
/* ---- 登录遮罩 ---- */
.login-overlay{display:none;position:fixed;inset:0;background:rgba(15,23,42,0.5);z-index:200;align-items:center;justify-content:center}
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
.sidebar-brand .icon{width:28px;height:28px;border-radius:6px;background:linear-gradient(135deg,var(--accent),#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#fff}
.sidebar-brand .name{font-size:16px;font-weight:700;color:var(--text)}
.sidebar-brand .ver{font-size:11px;color:var(--text-muted);margin-left:auto}
.sidebar-nav{flex:1;overflow-y:auto;padding:8px 0}
.nav-section{padding:8px 16px 4px;font-size:11px;color:var(--text-subtle);text-transform:uppercase;letter-spacing:0.5px;font-weight:600}
.nav-item{
  display:flex;align-items:center;gap:10px;padding:8px 16px;color:var(--text-muted);
  cursor:pointer;font-size:13px;border-left:2px solid transparent;transition:all 0.15s;
}
.nav-item:hover{background:var(--bg-hover);color:var(--text)}
.nav-item.active{background:var(--accent-soft);color:var(--accent);border-left-color:var(--accent)}
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
/* 兜底上游卡片：整体收紧 */
#fallbackCard{padding:14px 16px;margin-bottom:12px}
#fallbackCard .card-header{margin-bottom:10px}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:16px}
.card-header h2{font-size:14px;color:var(--text);font-weight:600;white-space:nowrap}
.card-header .actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.card-header .actions label{margin:0;font-size:12px;color:var(--text-subtle);font-weight:500;white-space:nowrap}
.card-header .actions input,
.card-header .actions select{width:auto;flex:none;min-width:120px;padding:6px 10px;height:34px;font-size:13px}
.card-header .actions .btn{height:34px;padding:6px 14px}
table{width:100%;border-collapse:collapse;font-size:13px}
.exposure-list{max-height:520px;overflow-y:auto;border:1px solid var(--border);border-radius:8px}
.exp-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border-muted)}
.exp-row:last-child{border-bottom:none}
.exp-row:hover{background:var(--bg-hover)}
.exp-row input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
.exp-row .name{font-weight:500}
.exp-row .meta{margin-left:auto;font-size:11px;color:var(--text-subtle)}
.exp-row .tag{font-size:10px;padding:1px 6px;border-radius:10px;margin-left:6px}
.exp-row .tag.fb{background:rgba(251,146,60,0.15);color:var(--orange)}
.exp-row .tag.off{background:var(--bg-input);color:var(--text-subtle)}
th{text-align:left;padding:12px 14px;color:var(--text-muted);font-weight:500;border-bottom:1px solid var(--border);font-size:12px;text-transform:uppercase;letter-spacing:0.3px;white-space:nowrap}
td{padding:12px 14px;border-bottom:1px solid var(--border-muted);vertical-align:top;line-height:1.55;word-break:break-word}
td .tag{vertical-align:middle}
td>strong{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom}
td:last-child{white-space:nowrap}
/* 模型列表：固定行高 + 长文本省略（各单元格自带 .truncate / <strong> 截断） */
#tab-models table{table-layout:fixed}
/* 分组列表：固定列宽 + 成员列省略号 */
#tab-groups table{table-layout:fixed}
.group-row td{height:46px;vertical-align:middle;overflow:hidden}
.group-row td:first-child{white-space:nowrap}
.model-row td{height:46px;vertical-align:middle;overflow:hidden}
.model-row td:first-child{white-space:nowrap}
.truncate{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom}
tr:hover td{background:var(--bg-hover)}
tr.group-header td{background:var(--bg-subtle);font-weight:600;color:var(--accent);cursor:pointer;user-select:none;padding:14px 16px;font-size:13.5px;letter-spacing:0.3px;border-top:8px solid var(--bg);border-bottom:1px solid var(--border)}
tr.group-header:first-child td{border-top:none}
tr.group-header:hover td{background:#eaeef5}
.group-meta{color:var(--text-muted);font-weight:400;font-size:12px;margin-left:6px;display:inline-flex;gap:8px;align-items:center;vertical-align:middle}
.group-meta .pill{display:inline-flex;align-items:center;padding:3px 10px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;font-size:11px;color:var(--text-muted);font-weight:500;white-space:nowrap}
.group-meta .pill strong{color:var(--text);margin-right:4px;font-weight:600}
/* ---- 按钮 ---- */
.btn{background:var(--bg-card);color:var(--text);border:1px solid var(--border);padding:6px 14px;border-radius:var(--radius);font-size:13px;cursor:pointer;font-family:inherit;transition:all 0.15s;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:var(--bg-hover);border-color:#d1d5db}
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
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,0.15)}
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
.modal.lg{max-width:860px;min-width:520px}
.kp-header{margin-bottom:14px}
.kp-header .sub{font-size:12px;color:var(--text-muted);font-weight:400;margin-top:4px}
.kp-toolbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.kp-addform{background:var(--bg-elevated);border:1px solid var(--border-muted);border-radius:8px;padding:14px 16px;margin-bottom:14px}
.kp-table{width:100%;border-collapse:collapse;font-size:13px}
.kp-table th,.kp-table td{padding:9px 10px;border-bottom:1px solid var(--border-muted);text-align:left;vertical-align:middle}
.kp-table th{color:var(--text-muted);font-weight:600;font-size:12px;background:var(--bg-hover)}
.kp-table code{font-size:12px;color:var(--text);background:var(--bg-elevated);padding:2px 6px;border-radius:4px}
.kp-table td:last-child{white-space:nowrap}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:20px;padding-top:16px;border-top:1px solid var(--border-muted)}
/* ---- 标签 / 徽章 ---- */
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:500;white-space:nowrap}
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
      <div class="nav-item" data-tab="exposure" onclick="showTab('exposure')"><span class="icon">&#9783;</span>暴露管理</div>
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
          <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
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
          <div id="fbInfo" style="margin-top:8px;color:var(--text-muted);font-size:12px"></div>
        </div>
        <div class="card">
          <div class="card-header">
            <h2>模型列表</h2>
            <div class="actions">
              <label style="font-size:12px;color:var(--text-subtle);margin-right:2px">分组</label>
              <select id="modelGroupMode" onchange="modelGroupMode=this.value;localStorage.setItem('modelGroupMode',modelGroupMode);renderModelTable()" style="margin-right:8px">
                <option value="upstream">按上游</option>
                <option value="model">按模型</option>
              </select>
              <input id="modelSearch" type="text" placeholder="搜索模型名…" oninput="renderModelTable()" style="margin-right:8px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);min-width:160px">
              <button class="btn primary" onclick="showModelModal()">+ 添加模型</button>
            </div>
          </div>
          <div style="overflow-x:auto;margin:0 -20px;padding:0 20px">
          <table>
            <colgroup>
              <col id="colModelName" style="width:240px">
              <col style="width:240px">
              <col style="width:180px">
              <col style="width:65px">
              <col style="width:60px">
              <col style="width:70px">
              <col style="width:90px">
              <col style="width:65px">
              <col style="width:178px">
            </colgroup>
            <thead><tr><th>名称</th><th>上游地址</th><th>上游模型</th><th>协议</th><th>RPM</th><th>TPM</th><th>类型</th><th>启用</th><th>操作</th></tr></thead>
          <tbody id="modelTable"></tbody></table>
          </div>
        </div>
      </div>

      <!-- Key 池 -->
      <div class="tab" id="tab-keys">
        <div class="card">
          <div class="card-header">
            <h2>Key 列表</h2>
            <div class="actions">
              <input id="keySearch" type="text" placeholder="搜索标签/模型/Key/上游…" oninput="loadKeys()" style="margin-right:8px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);min-width:180px">
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
          <table>
            <colgroup>
              <col id="colGroupName" style="width:200px">
              <col style="width:110px">
              <col id="colGroupMembers" style="width:300px">
              <col style="width:160px">
            </colgroup>
            <thead><tr><th>名称</th><th>策略</th><th>成员</th><th>操作</th></tr></thead>
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

      <!-- 暴露管理 -->
      <div class="tab" id="tab-exposure">
        <div class="card">
          <div class="card-header">
            <h2>暴露管理（Codex 模型发现）</h2>
            <div class="actions">
              <button class="btn small" onclick="exposureSelectAll(true)">全选</button>
              <button class="btn small" onclick="exposureSelectAll(false)">全不选</button>
              <button class="btn small" onclick="loadExposure()">刷新</button>
              <button class="btn primary" onclick="saveExposure()">保存暴露设置</button>
            </div>
          </div>
          <p style="color:var(--text-muted);font-size:12px;margin:4px 0 14px">
            勾选的模型 / 分组会出现在 Codex 的模型下拉（<code>/v1/models</code> 与 <code>/v1/api/codex/models</code>）。取消勾选则立即从 Codex 发现列表隐藏（保存后实时生效，无需重启）。内部聚合分组 <code>mf</code> 始终不暴露。
          </p>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
            <div>
              <h3 style="margin:0 0 8px">模型</h3>
              <input id="exposureModelSearch" class="form-control" placeholder="过滤模型名…" oninput="renderExposure()" style="margin-bottom:8px;width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)">
              <div id="exposureModelList" class="exposure-list"></div>
            </div>
            <div>
              <h3 style="margin:0 0 8px">分组</h3>
              <div id="exposureGroupList" class="exposure-list"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- 模态框 -->
<div class="modal-overlay" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modalContent"></div>
</div>

<!-- 模型 Key 池（仅显示单个模型） -->
<div class="modal-overlay" id="keyPoolModal" onclick="if(event.target===this)closeKeyPool()">
  <div class="modal lg">
    <div class="kp-header" id="modalKeyPoolHeader"></div>
    <div id="modalKeyPoolBody"></div>
  </div>
</div>

<script>
const API = "";
let models = [], keys = [], groups = [], _modelGroupOrder = [], authToken = localStorage.getItem("zhongzhuan_token") || "";
let modelGroupMode = ["upstream","model"].includes(localStorage.getItem("modelGroupMode")) ? localStorage.getItem("modelGroupMode") : "upstream";
let loading = 0;
let charts = {};
let testResults = {}; // key_id -> {ok, latency, error}

// 可见的错误横幅：JS 运行时异常直接显示在页面顶部，便于定位
function showFatal(msg) {
  let el = document.getElementById("fatalBanner");
  if (!el) {
    el = document.createElement("div");
    el.id = "fatalBanner";
    el.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999;background:#ef4444;color:#fff;padding:10px 16px;font-size:13px;font-family:monospace;white-space:pre-wrap;";
    document.body.appendChild(el);
  }
  el.textContent = "⚠️ 页面错误: " + msg;
}
window.addEventListener("error", (e) => showFatal((e.message || "") + " @" + (e.filename || "") + ":" + (e.lineno || "")));
window.addEventListener("unhandledrejection", (e) => showFatal("Promise: " + (e.reason && e.reason.message ? e.reason.message : e.reason)));

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

// 文本宽度测量（用于按最长内容自适应列宽）
const _measureCtx = document.createElement("canvas").getContext("2d");
function measureText(t, px, weight) {
  _measureCtx.font = (weight ? weight + " " : "") + px + 'px system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif';
  return _measureCtx.measureText(t == null ? "" : String(t)).width;
}
function fitColumn(colId, texts, opts) {
  const col = document.getElementById(colId);
  if (!col) return;
  let max = 0;
  for (const t of (texts || [])) { const w = measureText(t, opts.px, opts.weight); if (w > max) max = w; }
  let w = Math.ceil(max) + (opts.pad || 28) + (opts.extra || 0);
  w = Math.max(opts.min || 120, Math.min(opts.max || 480, w));
  col.style.width = w + "px";
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
  if (!s) {
    // 取不到登录状态（网络/接口异常）→ 兜底弹登录窗，避免整页空白
    document.getElementById("loginOverlay").classList.add("show");
    return;
  }
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
const titles = {dashboard:"仪表盘", models:"模型管理", keys:"Key 池", groups:"分组策略", exposure:"暴露管理", tokens:"访问令牌", logs:"请求日志"};
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
  if (name === "exposure") loadExposure();
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
  } else {
    document.getElementById("kpiGrid").innerHTML = `<div class="kpi-card" style="grid-column:1/-1"><div class="label">实时数据</div><div class="value danger">加载失败</div><div class="delta">请按 F12 查看 Console 报错，或尝试重新登录</div></div>`;
  }
  // 用量统计（7 天）
  const u = await api("/api/stats/usage?days=7");
  if (u) {
    try { renderCharts(u); }
    catch (e) { console.error("renderCharts error:", e); }
  } else {
    document.getElementById("chartTrend").innerHTML = '<div class="empty">用量统计加载失败，请刷新重试</div>';
    document.getElementById("chartPie").innerHTML = '';
    document.getElementById("chartTokens").innerHTML = '';
  }
}

function renderCharts(u) {
  const dark = {backgroundColor:"transparent", textStyle:{color:"#6b7280", fontFamily:"inherit"}, grid:{left:48, right:24, top:30, bottom:32}};
  const axisLine = {lineStyle:{color:"#e5e7eb"}};
  const axisLabel = {color:"#6b7280", fontSize:11};

  // 1. 请求趋势折线
  let c1 = charts.trend || echarts.init(document.getElementById("chartTrend"));
  c1.setOption({
    ...dark,
    tooltip:{trigger:"axis", backgroundColor:"#ffffff", borderColor:"#e5e7eb", textStyle:{color:"#1f2d3d"}},
    xAxis:{type:"category", data:(u.daily||[]).map(d=>d.date), axisLine, axisLabel},
    yAxis:{type:"value", axisLine, axisLabel, splitLine:{lineStyle:{color:"#eef1f4"}}},
    series:[{name:"请求数", type:"line", smooth:true, data:(u.daily||[]).map(d=>d.requests), itemStyle:{color:"#2563eb"}, areaStyle:{color:"rgba(37,99,235,0.10)"}}],
    legend:{show:false}
  });
  charts.trend = c1;

  // 2. 模型分布饼图
  let c2 = charts.pie || echarts.init(document.getElementById("chartPie"));
  const pieData = (u.by_model||[]).slice(0, 8).map(m => ({name:m.model_name||"unknown", value:m.requests}));
  c2.setOption({
    ...dark,
    tooltip:{trigger:"item", backgroundColor:"#ffffff", borderColor:"#e5e7eb", textStyle:{color:"#1f2d3d"}},
    legend:{type:"scroll", orient:"vertical", right:8, top:"center", textStyle:{color:"#6b7280", fontSize:11}},
    series:[{
      type:"pie", radius:["40%","70%"], center:["38%","50%"],
      data:pieData, label:{show:false},
      color:["#2563eb","#10b981","#f59e0b","#ef4444","#8b5cf6","#fb923c","#38bdf8","#22d3ee"]
    }]
  });
  charts.pie = c2;

  // 3. Token 堆叠柱状图
  let c3 = charts.tokens || echarts.init(document.getElementById("chartTokens"));
  c3.setOption({
    ...dark,
    tooltip:{trigger:"axis", backgroundColor:"#ffffff", borderColor:"#e5e7eb", textStyle:{color:"#1f2d3d"}},
    legend:{data:["输入","输出"], textStyle:{color:"#6b7280", fontSize:11}, top:0},
    xAxis:{type:"category", data:(u.daily||[]).map(d=>d.date), axisLine, axisLabel},
    yAxis:{type:"value", axisLine, axisLabel, splitLine:{lineStyle:{color:"#eef1f4"}}},
    series:[
      {name:"输入", type:"bar", stack:"tok", data:(u.daily||[]).map(d=>d.tokens_in), itemStyle:{color:"#2563eb"}},
      {name:"输出", type:"bar", stack:"tok", data:(u.daily||[]).map(d=>d.tokens_out), itemStyle:{color:"#10b981"}}
    ]
  });
  charts.tokens = c3;

  // 4. 成本趋势
  let c4 = charts.cost || echarts.init(document.getElementById("chartCost"));
  c4.setOption({
    ...dark,
    tooltip:{trigger:"axis", backgroundColor:"#ffffff", borderColor:"#e5e7eb", textStyle:{color:"#1f2d3d"}, valueFormatter:v=>"¥"+Number(v).toFixed(4)},
    xAxis:{type:"category", data:(u.daily||[]).map(d=>d.date), axisLine, axisLabel},
    yAxis:{type:"value", axisLine, axisLabel, splitLine:{lineStyle:{color:"#eef1f4"}}},
    series:[{name:"成本", type:"line", smooth:true, data:(u.daily||[]).map(d=>d.cost), itemStyle:{color:"#f59e0b"}, areaStyle:{color:"rgba(245,158,11,0.10)"}}],
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
  const sel = document.getElementById("modelGroupMode");
  if (sel) sel.value = modelGroupMode;
  renderModelTable();
  // 预拉取 Key 数据，供「按模型」分组显示每个模型的 Key 数量（仅该模式才重渲染）
  api("/api/keys").then(r => { if (r && r.data) { keys = r.data; if (modelGroupMode === "model") renderModelTable(); } });
}

// 仅重新渲染表格（分组 / 折叠 / 搜索都走这里，不重新拉接口）
function renderModelTable() {
  const custom = models.filter(m => !m.is_fallback);
  const fb = models.filter(m => m.is_fallback);
  const term = (document.getElementById("modelSearch")?.value || "").trim().toLowerCase();
  const matchName = (m) => !term
    || (m.name || "").toLowerCase().includes(term)
    || (m.upstream_base || "").toLowerCase().includes(term)
    || (m.upstream_model || "").toLowerCase().includes(term);

  let html = "";
  if (modelGroupMode === "none") {
    html += custom.filter(matchName).map(m => modelRow(m)).join("");
  } else {
    // 按前缀(默认)或按上游标签聚合为可折叠分组
    const groupsMap = new Map();
    for (const m of custom) {
      if (!matchName(m)) continue;
      const key = modelGroupMode === "tag"
        ? (m.upstream_tag || "未标记")
        : modelGroupMode === "upstream"
          ? (m.name.split('/')[0] || "未设置上游")
        : modelGroupMode === "model"
          ? (m.name.split('/').slice(1).join('/') || m.name)
          : (m.name.split(/[-\/]/)[0] || m.name);
      if (!groupsMap.has(key)) groupsMap.set(key, []);
      groupsMap.get(key).push(m);
    }
    const keysSorted = [...groupsMap.keys()].sort((a, b) => a.localeCompare(b));
    _modelGroupOrder = keysSorted;
    const collapsed = JSON.parse(localStorage.getItem("modelGroupCollapsed") || "{}");
    keysSorted.forEach((gkey, idx) => {
      const list = groupsMap.get(gkey);
      const isCollapsed = !!collapsed[gkey];
      const arrow = isCollapsed ? "\u25b6" : "\u25bc";
      let meta = '<span class="group-meta"><span class="pill"><strong>' + list.length + '</strong> 个模型</span></span>';
      if (modelGroupMode === "model") {
        // 按「模型名」聚合：amd/deepseek-v4-flash 与 p0/deepseek-v4-flash 视为同一模型，仅上游不同
        const upstreams = [...new Set(list.map(m => (m.name.split('/')[0] || "默认")))];
        const kc = keys.filter(k => list.some(mm => mm.id === k.model_id)).length;
        const upStr = esc(upstreams.join(' · '));
        meta = '<span class="group-meta">' +
          '<span class="pill"><strong>' + list.length + '</strong> 个上游</span>' +
          (upstreams.length ? '<span class="pill" title="' + upStr + '">' + upStr + '</span>' : '') +
          '<span class="pill"><strong>' + kc + '</strong> 个 Key</span>' +
          '</span>';
      }
      let addBtn = "";
      if (modelGroupMode === "upstream") {
        const gprefix = (list[0].name.split('/')[0] || "") + "/";
        addBtn = '<button class="btn small primary" style="margin-left:14px;flex-shrink:0" data-prefix="' + esc(gprefix) + '" data-up="' + esc(list[0].upstream_base || "") + '" onclick="event.stopPropagation();addModelToGroupBtn(this)">+ 添加模型</button>';
      }
      html += '<tr class="group-header" onclick="toggleModelGroup(' + idx + ')">' +
        '<td colspan="9"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;width:100%">' +
        '<span style="display:inline-flex;align-items:center;gap:8px;min-width:0">' +
        '<span style="display:inline-block;width:16px;color:var(--text-muted);flex-shrink:0">' + arrow + '</span>' +
        '<strong style="min-width:0">' + esc(gkey) + '</strong></span>' + meta + addBtn + '</div></td></tr>';
      if (!isCollapsed) html += list.map(m => modelRow(m)).join("");
    });
  }

  // 兜底模型分组（永远在最后，可折叠）
  const fbMatch = fb.filter(matchName);
  if (fbMatch.length > 0) {
    const fbCollapsed = localStorage.getItem("fbModelsCollapsed") !== "0";
    const arrow = fbCollapsed ? "\u25b6" : "\u25bc";
    html += '<tr class="group-header" onclick="toggleFbModels()">' +
      '<td colspan="9"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;width:100%">' +
      '<span style="display:inline-flex;align-items:center;gap:8px;min-width:0">' +
      '<span style="display:inline-block;width:16px;color:var(--text-muted);flex-shrink:0">' + arrow + '</span>' +
      '<span class="tag fallback">兜底</span> <strong>内置兜底模型</strong></span>' +
      '<span class="group-meta"><span class="pill"><strong>' + fbMatch.length + '</strong> 个</span></span>' +
      '<span style="margin-left:auto;color:var(--text-subtle);font-size:12px;white-space:nowrap">OpenCode Free 自动同步</span>' +
      '</div></td></tr>';
    if (!fbCollapsed) {
      html += fbMatch.map(m => modelRow(m, true)).join("");
    }
  }
  document.getElementById("modelTable").innerHTML = html || '<tr><td colspan="9" class="empty">' + (term ? '没有匹配的模型' : '还没有模型,点击右上角添加') + '</td></tr>';
  // 名称列按全部模型中最长名称自适应宽度（超出部分由 <strong> 省略号处理）
  fitColumn("colModelName", models.map(m => m.name), {px:13, weight:600, pad:28, extra:40, min:180, max:520});
}

// 单个模型行（isFb=true 时类型列显示「兜底」而非「自定义」，且不显示编辑/删除）
function modelRow(m, isFb) {
  const noteIco = m.note ? ' <span title="' + esc(m.note) + '" style="cursor:help">&#128221;</span>' : '';
  const typeCell = isFb ? '<span class="tag fallback">兜底</span>'
    : '<span class="tag custom">自定义</span>';
  const actions = isFb
    ? '<button class="btn small" onclick="editModel(' + m.id + ')">编辑</button>'
    : '<button class="btn small" onclick="editModel(' + m.id + ')">编辑</button> <button class="btn small" onclick="openModelKeyPool(' + m.id + ')">key池</button> <button class="btn small danger" onclick="delModel(' + m.id + ')">删除</button>';
  return '<tr class="model-row"><td><strong title="' + esc(m.name) + '">' + esc(m.name) + '</strong>' + presetBadge(m) + tagBadge(m) + noteIco + '</td>' +
    '<td><span class="truncate" title="' + esc(m.upstream_base) + '">' + esc(m.upstream_base) + '</span></td>' +
    '<td><span class="truncate" title="' + esc(m.upstream_model) + '">' + esc(m.upstream_model) + '</span></td>' +
    '<td><code>' + (m.protocol || "openai") + '</code></td>' +
    '<td>' + (m.rpm_limit || "不限") + '</td>' +
    '<td>' + (m.tpm_limit || "不限") + '</td>' +
    '<td>' + typeCell + '</td>' +
    '<td>' + (m.enabled ? '<span class="health-dot good"></span>是' : '<span class="health-dot bad"></span>否') + '</td>' +
    '<td>' + actions + '</td></tr>';
}

// 上游标签徽章：官方=蓝, 中转站=橙, 其他(自定义)=灰
function tagBadge(m) {
  if (!m.upstream_tag) return '';
  const t = m.upstream_tag;
  let bg = 'rgba(100,116,139,0.15)', color = '#64748b';
  if (t === '官方') { bg = 'rgba(59,130,246,0.15)'; color = '#60a5fa'; }
  else if (t === '中转站') { bg = 'rgba(245,158,11,0.15)'; color = '#f59e0b'; }
  return ' <span class="tag" style="background:' + bg + ';color:' + color + '">' + esc(t) + '</span>';
}

function toggleModelGroup(idx) {
  const gkey = _modelGroupOrder[idx];
  if (!gkey) return;
  const collapsed = JSON.parse(localStorage.getItem("modelGroupCollapsed") || "{}");
  collapsed[gkey] = !collapsed[gkey];
  localStorage.setItem("modelGroupCollapsed", JSON.stringify(collapsed));
  renderModelTable();
}

function toggleFbModels() {
  // 当前折叠则展开(0)，当前展开则折叠(1)
  const collapsed = localStorage.getItem("fbModelsCollapsed") !== "0";
  localStorage.setItem("fbModelsCollapsed", collapsed ? "0" : "1");
  renderModelTable();
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

function showModelModal(model, prefill) {
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

  const tag = isEdit ? (model.upstream_tag || "") : "";
  const isPresetTag = tag === "官方" || tag === "中转站";
  const tagOpts = [
    '<option value="官方"' + ((!isEdit || tag === "官方") ? " selected" : "") + '>官方</option>',
    '<option value="中转站"' + ((isEdit && tag === "中转站") ? " selected" : "") + '>中转站</option>',
    '<option value="__custom__"' + ((isEdit && tag !== "" && !isPresetTag) ? " selected" : "") + '>自定义</option>',
  ].join("");

  document.getElementById("modalContent").innerHTML = `
    <h3>${isEdit ? "编辑模型" : "添加模型"}</h3>
    <div class="form-group"><label>名称 <span style="color:var(--text-subtle)">(客户端请求时使用的模型名)</span></label><input id="f_name" value="${isEdit ? esc(model.name) : (prefill && prefill.name ? esc(prefill.name) : "")}"></div>
    <div class="form-row">
      <div class="form-group"><label>上游地址</label><input id="f_upstream_base" placeholder="https://api.openai.com/v1" value="${isEdit ? esc(model.upstream_base) : (prefill && prefill.upstream_base ? esc(prefill.upstream_base) : "")}"></div>
      <div class="form-group"><label>上游模型名</label><input id="f_upstream_model" placeholder="gpt-4o" value="${isEdit ? esc(model.upstream_model) : ""}"></div>
    </div>
    <div class="form-group"><label>上游完整地址覆盖 <span style="color:var(--text-subtle)">(留空自动拼接,可填路径或完整URL)</span></label><input id="f_upstream_path_override" placeholder="/openai/v1/chat/completions" value="${isEdit ? esc(model.upstream_path_override||"") : ""}"></div>
    <div class="form-group"><label>模型别名 <span style="color:var(--text-subtle)">(逗号分隔,客户端用别名请求时也会路由到此模型)</span></label><input id="f_aliases" placeholder="gpt-4, gpt4, chatgpt" value="${isEdit ? esc(model.aliases||"") : ""}"></div>
    <div class="form-row">
      <div class="form-group"><label>上游标签 <span style="color:var(--text-subtle)">(官方 / 中转站 / 自定义,用于备注与分组)</span></label><select id="f_upstream_tag" onchange="toggleUpstreamTag()">${tagOpts}</select></div>
      <div class="form-group"><label>备注</label><input id="f_note" placeholder="例如: 主账号,限额高" value="${isEdit ? esc(model.note||"") : ""}"></div>
    </div>
    <div class="form-group" id="f_tag_custom_wrap" style="display:none"><label>自定义标签</label><input id="f_tag_custom" placeholder="例如: 备用渠道A" value="${isEdit && !isPresetTag && tag ? esc(tag) : ""}"></div>
    <div class="form-row-3">
      <div class="form-group"><label>上游协议</label><select id="f_protocol"><option value="openai" ${isEdit && model.protocol === "openai" ? "selected" : ""}>OpenAI</option><option value="anthropic" ${isEdit && model.protocol === "anthropic" ? "selected" : ""}>Anthropic</option><option value="responses" ${isEdit && model.protocol === "responses" ? "selected" : ""}>Responses</option></select></div>
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
    <div class="form-group"><label>上游支持 reasoning_effort <span style="color:var(--text-subtle)">(上游不兼容该参数时报 400 时关闭；关闭后代理自动剥除，请求照常通过)</span></label><select id="f_supports_reasoning_effort"><option value="1" ${(!isEdit || model.supports_reasoning_effort) ? "selected" : ""}>是（默认）</option><option value="0" ${isEdit && !model.supports_reasoning_effort ? "selected" : ""}>否</option></select></div>
    <div class="modal-actions"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="saveModel(${isEdit ? model.id : ""})">保存</button></div>`;
  document.getElementById("modal").classList.add("show");

  // 渲染自定义头初始行 + 触发条件展示
  renderCustomHeaderRows(customRows);
  toggleClientPreset();
  toggleUpstreamTag();
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

function toggleUpstreamTag() {
  const sel = document.getElementById("f_upstream_tag");
  if (!sel) return;
  const wrap = document.getElementById("f_tag_custom_wrap");
  if (wrap) wrap.style.display = (sel.value === "__custom__") ? "block" : "none";
}

function addModelToGroupBtn(btn) {
  const prefix = btn.getAttribute("data-prefix") || "";
  const up = btn.getAttribute("data-up") || "";
  showModelModal(null, { name: prefix, upstream_base: up });
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
  const tagSel = document.getElementById("f_upstream_tag").value;
  const upstream_tag = tagSel === "__custom__" ? (document.getElementById("f_tag_custom").value.trim() || "自定义") : tagSel;
  const note = document.getElementById("f_note").value.trim();
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
    supports_reasoning_effort: document.getElementById("f_supports_reasoning_effort").value === "1",
    upstream_tag: upstream_tag,
    note: note,
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
  const keyTerm = (document.getElementById("keySearch")?.value || "").trim().toLowerCase();
  const keyModelHit = (m) => !keyTerm || (m.name||"").toLowerCase().includes(keyTerm) || (m.upstream_base||"").toLowerCase().includes(keyTerm);
  for (const [mid, g] of groupsMap) {
    const m = g.model;
    const isCollapsed = !!collapsed[mid];
    const arrow = isCollapsed ? "\\u25B6" : "\\u25BC";
    const matched = (!keyTerm || keyModelHit(m)) ? g.keys : g.keys.filter(k => (k.label||"").toLowerCase().includes(keyTerm) || (k.key_masked||"").toLowerCase().includes(keyTerm));
    if (keyTerm && !keyModelHit(m) && matched.length === 0) continue;
    parts.push(
      '<tr class="group-header" data-model="' + mid + '" onclick="toggleKeyGroup(' + mid + ')">' +
        '<td colspan="7">' +
          '<span class="kg-arrow" style="display:inline-block;width:16px;color:var(--text-muted)">' + arrow + '</span> ' +
          esc(m.name) +
          '<span style="color:var(--text-muted);font-weight:400;margin-left:8px">' + matched.length + ' 个 Key</span>' +
          '<span style="float:right;color:var(--text-subtle);font-weight:400;font-size:12px">' + esc(m.upstream_base||"") + ' · ' + esc(m.upstream_model||"") +
          '<button class="btn small primary" style="margin-left:8px" onclick="event.stopPropagation();showKeyModal(' + mid + ')">+ 添加 Key</button></span>' +
        '</td></tr>');
    if (!isCollapsed) {
      if (matched.length === 0) {
        parts.push('<tr><td colspan="7" class="empty">' + (keyTerm ? '该模型下没有匹配的 Key' : '该模型下还没有 Key') + '</td></tr>');
      } else {
        for (const k of matched) {
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
              '<td><button class="btn small" onclick="testKey(' + k.id + ')">测试</button> <button class="btn small" onclick="reprobeKey(' + k.id + ')">重探</button> <button class="btn small danger" onclick="delKey(' + k.id + ')">删除</button></td>' +
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
  if (r !== null) { loadKeys(); refreshPoolIfOpen(); }
}

async function testKey(id, reprobe=false) {
  const btn = event?.target;
  const origText = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "测试中..."; }
  const url = "/api/keys/" + id + "/test" + (reprobe ? "?reprobe=1" : "");
  const r = await api(url, {method:"POST"});
  if (btn) { btn.disabled = false; btn.textContent = origText; }
  if (!r) return;
  testResults[id] = {ok: r.ok, latency: r.latency_ms, error: r.error};
  loadKeys();
  refreshPoolIfOpen();
  // 显示详情（思考等级由系统静默自动探测, 此处不展示）
  const prefix = reprobe ? "重新探测" : "连通性测试";
  if (r.ok) {
    alert(prefix + "通过\\n\\n模型: " + r.model + "\\n延迟: " + r.latency_ms + "ms\\nURL: " + r.url);
  } else {
    alert(prefix + "失败\\n\\n状态码: " + r.status + "\\n错误: " + r.error + "\\nURL: " + r.url);
  }
}

async function reprobeKey(id) {
  await testKey(id, true);
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

function showKeyModal(presetModelId) {
  // 兜底模型是内置模型，不给它们添加 Key
  const opts = models.filter(m => !m.is_fallback).map(m => '<option value="' + m.id + '"' + (presetModelId && m.id === presetModelId ? ' selected' : '') + '>' + esc(m.name) + '</option>').join("");
  document.getElementById("modalContent").innerHTML = `
    <h3>添加 Key</h3>
    <div class="form-group"><label>模型</label><select id="f_model_id" ${presetModelId ? 'disabled' : ''}>${opts}</select></div>
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
      <tr class="group-row"><td><strong class="truncate" title="${esc(g.name)}">${esc(g.name)}</strong></td><td><code>${esc(g.strategy)}</code></td>
      <td><span class="truncate" title="${(g.members||[]).map(x => esc(modelMap[x.model_id] || ("model#"+x.model_id)) + '(w'+(x.weight||1)+',o'+(x.ord||0)+')').join(', ')}">${(g.members||[]).map(x => esc(modelMap[x.model_id] || ("model#"+x.model_id)) + '<span style="color:var(--text-subtle);font-size:11px">(w'+(x.weight||1)+',o'+(x.ord||0)+')</span>').join(", ") || '<span style="color:var(--text-subtle)">无</span>'}</span></td>
      <td><button class="btn small" onclick="editGroup(${g.id})">编辑</button> <button class="btn small danger" onclick="delGroup(${g.id})">删除</button></td></tr>`).join("");
  // 名称列按最长分组名自适应宽度
  fitColumn("colGroupName", groups.map(g => g.name), {px:13, weight:600, pad:28, min:140, max:360});
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
      <span class="tag" style="background:rgba(99,102,241,0.15);color:var(--accent)">下游 API Key</span>
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

function closeKeyPool() { document.getElementById("keyPoolModal").classList.remove("show"); poolModelId = null; }

// ---- 模型专属 Key 池（仅显示单个模型） ----
let poolModelId = null;
let poolKeys = [];

async function openModelKeyPool(modelId) {
  poolModelId = modelId;
  const r = await api("/api/keys");
  poolKeys = (r?.data || []).filter(k => k.model_id === modelId);
  renderModelKeyPool(false);
  document.getElementById("keyPoolModal").classList.add("show");
}

function renderModelKeyPool(showAddForm) {
  const m = models.find(x => x.id === poolModelId);
  if (!m) return;
  document.getElementById("modalKeyPoolHeader").innerHTML =
    'Key 池 · <strong>' + esc(m.name) + '</strong>' +
    '<div class="sub">' + esc(m.upstream_base || "") + ' · ' + esc(m.upstream_model || "") + ' · 共 ' + poolKeys.length + ' 个 Key</div>';
  let html = "";
  if (showAddForm) {
    html +=
      '<div class="kp-addform">' +
        '<div class="form-row">' +
          '<div class="form-group"><label>标签</label><input id="kp_label" placeholder="例如:主账号"></div>' +
          '<div class="form-group"><label>优先级</label><input id="kp_priority" type="number" value="0"></div>' +
        '</div>' +
        '<div class="form-group"><label>Key 值</label><input id="kp_key" type="password" placeholder="sk-..."></div>' +
        '<div class="modal-actions" style="margin-top:12px"><button class="btn" onclick="toggleKpAddForm(false)">取消</button><button class="btn primary" onclick="addKeyToPool()">保存</button></div>' +
      '</div>';
  } else {
    html += '<div class="kp-toolbar"><button class="btn primary" onclick="toggleKpAddForm(true)">+ 添加 Key</button> <button class="btn" onclick="testAllKeysForModel()">测试全部</button></div>';
  }
  if (!showAddForm) {
    if (poolKeys.length === 0) {
      html += '<div class="empty" style="padding:24px 0">该模型下还没有 Key,点击「+ 添加 Key」</div>';
    } else {
      html += '<div style="max-height:50vh;overflow-y:auto"><table class="kp-table"><thead><tr><th>标签</th><th>Key</th><th>优先级</th><th>启用</th><th>连通性</th><th>操作</th></tr></thead><tbody>' +
        poolKeys.map(k => {
          const tr = testResults[k.id];
          let conn = '<span style="color:var(--text-subtle)">未测试</span>';
          if (tr) conn = tr.ok ? '<span class="tag ok">OK ' + tr.latency + 'ms</span>' : '<span class="tag err">失败</span>';
          return '<tr>' +
            '<td>' + esc(k.label) + '</td>' +
            '<td><code>' + esc(k.key_masked) + '</code></td>' +
            '<td>' + k.priority + '</td>' +
            '<td>' + (k.enabled ? '<span class="health-dot good"></span>是' : '<span class="health-dot bad"></span>否') + '</td>' +
            '<td>' + conn + '</td>' +
            '<td><button class="btn small" onclick="testKey(' + k.id + ')">测试</button> <button class="btn small" onclick="reprobeKey(' + k.id + ')">重探</button> <button class="btn small danger" onclick="delKey(' + k.id + ')">删除</button></td>' +
          '</tr>';
        }).join("") + '</tbody></table></div>';
    }
  }
  document.getElementById("modalKeyPoolBody").innerHTML = html;
}

function toggleKpAddForm(show) { renderModelKeyPool(show); }

async function addKeyToPool() {
  const label = document.getElementById("kp_label").value;
  const key_value = document.getElementById("kp_key").value;
  const priority = parseInt(document.getElementById("kp_priority").value) || 0;
  if (!key_value) { alert("请填写 Key 值"); return; }
  const r = await api("/api/keys", {method:"POST", body: JSON.stringify({model_id: poolModelId, label, key_value, priority})});
  if (r !== null) await openModelKeyPool(poolModelId);
}

async function testAllKeysForModel() {
  const list = poolKeys.slice();
  if (list.length === 0) { alert("没有可测试的 Key"); return; }
  if (!confirm("将测试该模型 " + list.length + " 个 Key,可能产生少量请求费用,继续?")) return;
  for (const k of list) {
    const r = await api("/api/keys/" + k.id + "/test", {method:"POST"});
    if (r) testResults[k.id] = {ok: r.ok, latency: r.latency_ms, error: r.error};
    renderModelKeyPool(false);
  }
  alert("测试完成");
}

function refreshPoolIfOpen() {
  if (document.getElementById("keyPoolModal").classList.contains("show") && poolModelId !== null) {
    openModelKeyPool(poolModelId);
  }
}

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

// ---- 暴露管理（Codex 模型发现）----
let exposureModels = [], exposureGroups = [];

async function loadExposure() {
  const d = await api("/api/exposure");
  if (!d) return;
  exposureModels = d.models || [];
  exposureGroups = d.groups || [];
  renderExposure();
}

function renderExposure() {
  const q = (document.getElementById("exposureModelSearch").value || "").trim().toLowerCase();
  const ml = document.getElementById("exposureModelList");
  ml.innerHTML = "";
  for (const m of exposureModels) {
    if (q && !m.name.toLowerCase().includes(q)) continue;
    const row = document.createElement("label");
    row.className = "exp-row";
    let tags = "";
    if (m.is_fallback) tags += '<span class="tag fb">兜底</span>';
    if (!m.enabled) tags += '<span class="tag off">未启用</span>';
    row.innerHTML =
      '<input type="checkbox" data-type="model" data-id="' + m.id + '"' + (m.exposed ? " checked" : "") + ">" +
      '<span class="name">' + esc(m.name) + "</span>" +
      '<span class="meta">' + tags + "</span>";
    ml.appendChild(row);
  }
  if (!ml.children.length) ml.innerHTML = '<div class="exp-row" style="color:var(--text-subtle)">无匹配模型</div>';

  const gl = document.getElementById("exposureGroupList");
  gl.innerHTML = "";
  for (const g of exposureGroups) {
    const row = document.createElement("label");
    row.className = "exp-row";
    row.innerHTML =
      '<input type="checkbox" data-type="group" data-id="' + g.id + '"' + (g.exposed ? " checked" : "") + ">" +
      '<span class="name">' + esc(g.name) + "</span>" +
      '<span class="meta">' + g.members + " 个成员</span>";
    gl.appendChild(row);
  }
  if (!gl.children.length) gl.innerHTML = '<div class="exp-row" style="color:var(--text-subtle)">无分组</div>';
}

function exposureSelectAll(state) {
  document.querySelectorAll('#exposureModelList input[type="checkbox"], #exposureGroupList input[type="checkbox"]')
    .forEach(c => { c.checked = state; });
}

async function saveExposure() {
  const models = {}, groups = {};
  document.querySelectorAll('#exposureModelList input[type="checkbox"]').forEach(c => {
    models[c.getAttribute("data-id")] = c.checked;
  });
  document.querySelectorAll('#exposureGroupList input[type="checkbox"]').forEach(c => {
    groups[c.getAttribute("data-id")] = c.checked;
  });
  const r = await api("/api/exposure", {method:"POST", body: JSON.stringify({models, groups})});
  if (r && r.ok) {
    alert("已保存：" + (r.saved?.models || 0) + " 个模型、" + (r.saved?.groups || 0) + " 个分组的暴露设置");
    loadExposure();
  } else {
    alert("保存失败");
  }
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
