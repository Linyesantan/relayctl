#!/usr/bin/env python3
"""
relayctl Web Dashboard: 适配移动端与桌面端的轻量双端管理面板
零额外依赖 (基于 Python 原生 http.server + requests + pyyaml)
"""

import sys, os, json, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

# 引入核心模块
from .core import load_config
from . import cache as rcache
from .adapters import get_adapter

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>relayctl · 中转管理控制台</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: rgba(30, 41, 59, 0.7);
      --card-border: rgba(255, 255, 255, 0.08);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --primary-hover: #0ea5e9;
      --success: #34d399;
      --warning: #fbbf24;
      --danger: #f87171;
      --radius: 14px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background-color: var(--bg); color: var(--text-main); min-height: 100vh; padding: 16px; -webkit-tap-highlight-color: transparent; }
    .container { max-width: 1200px; margin: 0 auto; }
    
    /* Header */
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
    .brand { display: flex; align-items: center; gap: 10px; }
    .brand-icon { width: 36px; height: 36px; background: linear-gradient(135deg, #38bdf8, #818cf8); border-radius: 10px; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #fff; font-size: 18px; box-shadow: 0 4px 12px rgba(56, 189, 248, 0.3); }
    .brand-title { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
    .brand-subtitle { font-size: 12px; color: var(--text-muted); }
    
    .actions { display: flex; gap: 8px; align-items: center; }
    .btn { background: var(--card-bg); border: 1px solid var(--card-border); color: var(--text-main); padding: 8px 14px; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px; }
    .btn:hover { background: rgba(51, 65, 85, 0.8); border-color: rgba(255, 255, 255, 0.2); }
    .btn-primary { background: linear-gradient(135deg, #0284c7, #2563eb); border: none; color: #fff; }
    .btn-primary:hover { opacity: 0.9; }
    .btn-sm { padding: 4px 10px; font-size: 12px; border-radius: 6px; }
    
    /* Metrics */
    .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
    .metric-card { background: var(--card-bg); border: 1px solid var(--card-border); backdrop-filter: blur(12px); border-radius: var(--radius); padding: 14px; }
    .metric-label { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
    .metric-value { font-size: 24px; font-weight: 700; }
    
    /* Search & Filter Toolbar */
    .toolbar { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
    .search-input { flex: 1; min-width: 200px; background: var(--card-bg); border: 1px solid var(--card-border); color: var(--text-main); padding: 9px 14px; border-radius: 8px; font-size: 13px; outline: none; }
    .search-input:focus { border-color: var(--primary); }
    
    /* Cards Layout (Mobile-first friendly) */
    .cards-container { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
    .site-card { background: var(--card-bg); border: 1px solid var(--card-border); backdrop-filter: blur(12px); border-radius: var(--radius); padding: 16px; display: flex; flex-direction: column; justify-content: space-between; transition: transform 0.2s, box-shadow 0.2s; }
    .site-card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.3); border-color: rgba(255, 255, 255, 0.15); }
    
    .card-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
    .site-name { font-size: 16px; font-weight: 700; color: var(--primary); display: flex; align-items: center; gap: 6px; }
    .badge { font-size: 11px; padding: 2px 8px; border-radius: 12px; font-weight: 600; text-transform: uppercase; }
    .badge-api { background: rgba(52, 211, 153, 0.15); color: var(--success); border: 1px solid rgba(52, 211, 153, 0.3); }
    .badge-parked { background: rgba(251, 191, 36, 0.15); color: var(--warning); border: 1px solid rgba(251, 191, 36, 0.3); }
    .badge-script { background: rgba(129, 140, 248, 0.15); color: #818cf8; border: 1px solid rgba(129, 140, 248, 0.3); }
    
    .card-body { font-size: 13px; margin-bottom: 14px; line-height: 1.6; }
    .info-row { display: flex; justify-content: space-between; margin-bottom: 4px; }
    .info-label { color: var(--text-muted); }
    .info-value { font-weight: 500; }
    .balance-val { color: var(--success); font-weight: 700; font-size: 15px; }
    
    .card-footer { display: flex; gap: 6px; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 12px; flex-wrap: wrap; }
    
    /* Modal / Bottom Sheet */
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); z-index: 1000; display: none; align-items: center; justify-content: center; padding: 16px; }
    .modal-content { background: #1e293b; border: 1px solid var(--card-border); border-radius: var(--radius); max-width: 600px; width: 100%; max-height: 85vh; overflow-y: auto; padding: 20px; box-shadow: 0 20px 40px rgba(0,0,0,0.5); }
    .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
    .modal-title { font-size: 18px; font-weight: 700; }
    .modal-close { background: transparent; border: none; color: var(--text-muted); font-size: 22px; cursor: pointer; }
    
    /* Table inside modal */
    .pure-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
    .pure-table th, .pure-table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.08); }
    .pure-table th { color: var(--text-muted); font-weight: 600; }
    .key-badge { font-family: monospace; background: rgba(0,0,0,0.3); padding: 2px 6px; border-radius: 4px; font-size: 11px; }

    /* Form styles */
    .form-group { margin-bottom: 12px; }
    .form-label { display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
    .form-input { width: 100%; background: #0f172a; border: 1px solid var(--card-border); color: var(--text-main); padding: 8px 12px; border-radius: 6px; font-size: 13px; }
    
    @media (max-width: 640px) {
      body { padding: 10px; }
      .modal-overlay { align-items: flex-end; padding: 0; }
      .modal-content { border-radius: var(--radius) var(--radius) 0 0; max-height: 90vh; }
      .cards-container { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="brand-icon">R</div>
        <div>
          <div class="brand-title">relayctl 控制台</div>
          <div class="brand-subtitle">上游中转统一管理与本地网关同步</div>
        </div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="loadStatus(true)">⚡ 刷新状态</button>
      </div>
    </header>

    <!-- Metrics -->
    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-label">纳管站点总数</div>
        <div class="metric-value" id="stat-total">-</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">活跃 API 站点</div>
        <div class="metric-value" style="color: var(--success);" id="stat-api">-</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">搁置 / 受限站点</div>
        <div class="metric-value" style="color: var(--warning);" id="stat-parked">-</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">监控总余额</div>
        <div class="metric-value" style="color: var(--primary);" id="stat-balance">-</div>
      </div>
    </div>

    <!-- Toolbar -->
    <div class="toolbar">
      <input type="text" id="searchInput" class="search-input" placeholder="搜索站点名称 / 分组 / 模型..." oninput="filterSites()">
    </div>

    <!-- Cards Container -->
    <div class="cards-container" id="cardsContainer">
      <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted);">
        正在加载各中转站点状态...
      </div>
    </div>
  </div>

  <!-- Common Modal -->
  <div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this) closeModal()">
    <div class="modal-content" id="modalContent">
      <div class="modal-head">
        <div class="modal-title" id="modalTitle">详情</div>
        <button class="modal-close" onclick="closeModal()">&times;</button>
      </div>
      <div id="modalBody">加载中...</div>
    </div>
  </div>

  <script>
    let allSites = [];

    async function loadStatus(forceRefresh = false) {
      try {
        const btn = document.querySelector('.btn-primary');
        if (btn) btn.innerText = '刷新中...';
        const res = await fetch('/api/status' + (forceRefresh ? '?refresh=1' : ''));
        const json = await res.json();
        allSites = json.data || [];
        renderMetrics();
        renderSites(allSites);
      } catch (e) {
        alert('加载状态失败: ' + e);
      } finally {
        const btn = document.querySelector('.btn-primary');
        if (btn) btn.innerText = '⚡ 刷新状态';
      }
    }

    function renderMetrics() {
      document.getElementById('stat-total').innerText = allSites.length;
      document.getElementById('stat-api').innerText = allSites.filter(s => s.tier === 'api').length;
      document.getElementById('stat-parked').innerText = allSites.filter(s => s.tier === 'parked').length;
      let totalBal = 0;
      allSites.forEach(s => {
        if (s.balance && !isNaN(parseFloat(s.balance))) {
          totalBal += parseFloat(s.balance);
        }
      });
      document.getElementById('stat-balance').innerText = totalBal.toFixed(2);
    }

    function renderSites(sites) {
      const c = document.getElementById('cardsContainer');
      if (!sites.length) {
        c.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted);">未找到匹配的站点</div>';
        return;
      }
      c.innerHTML = sites.map(s => {
        const isApi = s.tier === 'api';
        const balStr = s.balance !== '-' ? `${s.balance} <span style="font-size:11px;color:var(--text-muted);">${s.display_type||''}</span>` : '-';
        return `
          <div class="site-card">
            <div>
              <div class="card-head">
                <div class="site-name">
                  ${s.name}
                  <span style="font-size: 11px; color: var(--text-muted); font-weight: normal;">(CH: ${s.channel || '-'})</span>
                </div>
                <span class="badge badge-${s.tier}">${s.tier}</span>
              </div>
              <div class="card-body">
                <div class="info-row">
                  <span class="info-label">剩余额度</span>
                  <span class="balance-val">${balStr}</span>
                </div>
                <div class="info-row">
                  <span class="info-label">用户分组</span>
                  <span class="info-value">${s.group || '-'}</span>
                </div>
                <div class="info-row">
                  <span class="info-label">Key 总数</span>
                  <span class="info-value">${s.key_count ?? '-'}</span>
                </div>
                ${s.diagnosis ? `<div style="font-size: 11px; color: var(--warning); margin-top: 6px; background: rgba(251,191,36,0.08); padding: 6px; border-radius: 6px;">${s.diagnosis}</div>` : ''}
              </div>
            </div>
            <div class="card-footer">
              <button class="btn btn-sm" onclick="showKeys('${s.name}')">🔑 密钥列表</button>
              <button class="btn btn-sm" onclick="showGroups('${s.name}')">🏷️ 模型分组</button>
              ${isApi ? `<button class="btn btn-sm" style="color:var(--primary);" onclick="showAddKeyModal('${s.name}')">➕ 新建Key</button>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }

    function filterSites() {
      const q = document.getElementById('searchInput').value.toLowerCase().trim();
      if (!q) {
        renderSites(allSites);
        return;
      }
      const filtered = allSites.filter(s => 
        s.name.toLowerCase().includes(q) || 
        (s.group && s.group.toLowerCase().includes(q)) ||
        (s.diagnosis && s.diagnosis.toLowerCase().includes(q))
      );
      renderSites(filtered);
    }

    function openModal(title, bodyHtml) {
      document.getElementById('modalTitle').innerText = title;
      document.getElementById('modalBody').innerHTML = bodyHtml;
      document.getElementById('modalOverlay').style.display = 'flex';
    }

    function closeModal() {
      document.getElementById('modalOverlay').style.display = 'none';
    }

    async function showKeys(site) {
      openModal(`${site} · API 密钥列表`, '<div style="padding:20px;text-align:center;">正在读取密钥列表...</div>');
      try {
        const res = await fetch(`/api/keys/${site}`);
        const json = await res.json();
        const keys = json.data || [];
        if (!keys.length) {
          document.getElementById('modalBody').innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted);">暂无有效 Key 或无法读取</div>';
          return;
        }
        const html = `
          <table class="pure-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>分组</th>
                <th>剩余额度</th>
                <th>Key (脱敏)</th>
              </tr>
            </thead>
            <tbody>
              ${keys.map(k => `
                <tr>
                  <td><b>${k.name}</b></td>
                  <td>${k.group}</td>
                  <td>${k.remain_quota}</td>
                  <td><span class="key-badge">${k.key}</span></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
        document.getElementById('modalBody').innerHTML = html;
      } catch (e) {
        document.getElementById('modalBody').innerHTML = `<div style="color:var(--danger);">加载失败: ${e}</div>`;
      }
    }

    async function showGroups(site) {
      openModal(`${site} · 可用分组与倍率`, '<div style="padding:20px;text-align:center;">正在查询分组倍率...</div>');
      try {
        const res = await fetch(`/api/groups/${site}`);
        const json = await res.json();
        const groups = json.data || [];
        if (!groups.length) {
          document.getElementById('modalBody').innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted);">未发现分组数据</div>';
          return;
        }
        const html = `
          <table class="pure-table">
            <thead>
              <tr>
                <th>分组名称</th>
                <th>标定倍率</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              ${groups.map(g => `
                <tr>
                  <td><b>${g.name}</b></td>
                  <td style="color:var(--primary); font-weight:600;">${g.ratio}</td>
                  <td style="color:var(--text-muted); font-size:12px;">${g.desc || '-'}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
        document.getElementById('modalBody').innerHTML = html;
      } catch (e) {
        document.getElementById('modalBody').innerHTML = `<div style="color:var(--danger);">加载失败: ${e}</div>`;
      }
    }

    function showAddKeyModal(site) {
      const html = `
        <form onsubmit="submitAddKey(event, '${site}')">
          <div class="form-group">
            <label class="form-label">密钥名称</label>
            <input type="text" id="kName" class="form-input" required placeholder="如: my-new-key">
          </div>
          <div class="form-group">
            <label class="form-label">选择分组 (Group)</label>
            <input type="text" id="kGroup" class="form-input" required placeholder="如: default 或 pro">
          </div>
          <div class="form-group">
            <label class="form-label">额度设置</label>
            <div style="display:flex;gap:10px;align-items:center;">
              <input type="number" step="0.1" id="kQuota" class="form-input" placeholder="输入额度数 (如 5.0)">
              <label style="font-size:12px;display:flex;align-items:center;gap:4px;cursor:pointer;">
                <input type="checkbox" id="kUnlimited" onchange="document.getElementById('kQuota').disabled = this.checked"> 无限额度
              </label>
            </div>
          </div>
          <div style="margin-top:16px;display:flex;justify-content:flex-end;gap:8px;">
            <button type="button" class="btn" onclick="closeModal()">取消</button>
            <button type="submit" class="btn btn-primary">立即创建</button>
          </div>
        </form>
      `;
      openModal(`在上游 [${site}] 新建密钥`, html);
    }

    async function submitAddKey(e, site) {
      e.preventDefault();
      const name = document.getElementById('kName').value.trim();
      const group = document.getElementById('kGroup').value.trim();
      const unlimited = document.getElementById('kUnlimited').checked;
      const quota = document.getElementById('kQuota').value.trim();

      if (!unlimited && (!quota || parseFloat(quota) <= 0)) {
        alert('请指定大于 0 的额度，或勾选“无限额度”！');
        return;
      }

      const body = { site, name, group, unlimited, quota: unlimited ? null : parseFloat(quota) };
      try {
        const res = await fetch('/api/key/add', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)
        });
        const json = await res.json();
        if (json.success) {
          alert('✓ Key 创建成功！' + (json.key ? 'Key: ' + json.key : ''));
          closeModal();
          loadStatus(true);
        } else {
          alert('创建失败: ' + (json.message || '未知错误'));
        }
      } catch (err) {
        alert('请求出错: ' + err);
      }
    }

    // Initial load
    loadStatus();
  </script>
</body>
</html>
"""

class RelayctlWebHandler(BaseHTTPRequestHandler):
    config = None

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_str, code=200):
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url_parts = urllib.parse.urlparse(self.path)
        path = url_parts.path

        if path == "/" or path == "/index.html":
            self._send_html(HTML_TEMPLATE)
            return

        cfg = self.config or load_config()
        sites = cfg.get("sites", {})

        # GET /api/status
        if path == "/api/status":
            query = urllib.parse.parse_qs(url_parts.query)
            force_refresh = "refresh" in query
            data = []
            for s_name, s_cfg in sites.items():
                tier = s_cfg.get("tier", "parked")
                ch = s_cfg.get("local_channel")
                channel_str = ",".join(map(str, ch)) if isinstance(ch, list) else (str(ch) if ch else "-")
                
                info = {
                    "name": s_name,
                    "tier": tier,
                    "channel": channel_str,
                    "balance": "-",
                    "used": "-",
                    "group": "-",
                    "key_count": "-",
                    "display_type": "",
                    "diagnosis": ""
                }

                if tier == "api":
                    try:
                        adapter = get_adapter(s_name, s_cfg, cfg)
                        mode = rcache.MODE_REFRESH if force_refresh else rcache.MODE_AUTO
                        bal_data = rcache.fetch(s_name, "balance", adapter.balance, mode)["data"]
                        info["balance"] = str(bal_data.get("balance", "-"))
                        info["used"] = str(bal_data.get("used", "-"))
                        info["group"] = bal_data.get("user_group", "-")
                        info["display_type"] = bal_data.get("display_type", "")

                        # Key count
                        keys_data = rcache.fetch(s_name, "keys", adapter.list_keys, mode)["data"]
                        info["key_count"] = len(keys_data) if isinstance(keys_data, list) else "-"
                    except Exception as e:
                        info["diagnosis"] = f"读取异常: {e}"
                else:
                    info["diagnosis"] = s_cfg.get("parked_reason") or "已搁置"

                data.append(info)

            self._send_json({"success": True, "data": data})
            return

        # GET /api/keys/{site}
        if path.startswith("/api/keys/"):
            s_name = path[len("/api/keys/"):]
            if s_name not in sites:
                self._send_json({"success": False, "message": "站点未登记"}, 404)
                return
            s_cfg = sites[s_name]
            try:
                adapter = get_adapter(s_name, s_cfg, cfg)
                keys_data = adapter.list_keys()
                self._send_json({"success": True, "data": keys_data})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
            return

        # GET /api/groups/{site}
        if path.startswith("/api/groups/"):
            s_name = path[len("/api/groups/"):]
            if s_name not in sites:
                self._send_json({"success": False, "message": "站点未登记"}, 404)
                return
            s_cfg = sites[s_name]
            try:
                adapter = get_adapter(s_name, s_cfg, cfg)
                groups_data = adapter.groups()
                self._send_json({"success": True, "data": groups_data})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
            return

        self._send_json({"error": "Not Found"}, 404)

    def do_POST(self):
        url_parts = urllib.parse.urlparse(self.path)
        path = url_parts.path

        cfg = self.config or load_config()
        sites = cfg.get("sites", {})

        # POST /api/key/add
        if path == "/api/key/add":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_len).decode("utf-8"))
                s_name = body.get("site")
                k_name = body.get("name")
                k_group = body.get("group")
                k_unlimited = body.get("unlimited", False)
                k_quota = body.get("quota")

                if s_name not in sites:
                    self._send_json({"success": False, "message": "站点未登记"}, 404)
                    return

                adapter = get_adapter(s_name, sites[s_name], cfg)
                res = adapter.create_key(name=k_name, group=k_group, quota=k_quota, unlimited=k_unlimited)
                rcache.invalidate(s_name, "keys")
                self._send_json({"success": True, "key_id": res.get("id"), "key": res.get("key_masked")})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
            return

        self._send_json({"error": "Not Found"}, 404)

def run_web(host: str = "0.0.0.0", port: int = 8088, config_override: Optional[dict] = None):
    """启动 relayctl Web 服务"""
    RelayctlWebHandler.config = config_override or load_config()
    server_address = (host, port)
    httpd = HTTPServer(server_address, RelayctlWebHandler)
    print(f"[*] relayctl Web 控制台已启动: http://{host}:{port}/")
    print("    支持手机浏览器 (触控适配卡片流) 与桌面浏览器 (宽屏仪表盘)")
    print("    按 Ctrl+C 停止服务。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] relayctl Web 服务已停止。")
        httpd.server_close()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    run_web(port=port)
