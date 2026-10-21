#!/usr/bin/env python3
"""relayctl 零额外依赖 Web 控制台。"""

import hmac
import json
import secrets
import sys
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

from . import cache as rcache
from . import models
from .adapters import get_adapter
from .config import load_config
from .doctor import is_loopback
from .errors import ConfigError

_SCHEDULER_FACTORY = None
_WEB_SCHEDULER = None

MAX_BODY_BYTES = 64 * 1024
SESSION_COOKIE = "relayctl_session"

LOGIN_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>relayctl 登录</title><style>
body{font-family:sans-serif;max-width:28rem;margin:5rem auto;padding:1rem;background:#0f172a;color:#f8fafc}
form{display:grid;gap:.8rem}input,button{font:inherit;padding:.7rem;border-radius:.4rem;border:1px solid #475569}
button{cursor:pointer;background:#0284c7;color:white}</style></head><body>
<h1>relayctl</h1><p>请输入 Web 访问口令。</p>
<form method="post" action="/login"><label for="password">访问口令</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">登录</button></form></body></html>"""

HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>relayctl 控制台</title><style>
:root{color-scheme:dark}body{font-family:system-ui,sans-serif;margin:0;padding:1rem;background:#0f172a;color:#f8fafc}
main{max-width:70rem;margin:auto}.toolbar{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
button,input{font:inherit;padding:.55rem;border-radius:.4rem;border:1px solid #475569}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:.8rem;margin-top:1rem}
.card,.panel{background:#1e293b;border:1px solid #334155;border-radius:.7rem;padding:1rem}.muted{color:#94a3b8}
table{width:100%;border-collapse:collapse}td,th{text-align:left;border-bottom:1px solid #334155;padding:.4rem}
#details{margin-top:1rem;overflow:auto}</style></head><body><main>
<div class="toolbar"><h1>relayctl 控制台</h1><button id="refresh" type="button">刷新状态</button><button id="logout" type="button">退出</button></div>
<p id="message" class="muted">正在加载…</p><section id="sites" class="grid"></section><section id="details" class="panel" hidden></section>
</main><script>
'use strict';
const sitesNode=document.getElementById('sites');
const detailsNode=document.getElementById('details');
const messageNode=document.getElementById('message');
async function request(path, options={}){
  const response=await fetch(path,{credentials:'same-origin',...options});
  const body=await response.json();
  if(!response.ok) throw new Error(body.message||'请求失败');
  return body;
}
function clear(node){while(node.firstChild)node.removeChild(node.firstChild);}
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(className)node.className=className;return node;}
function addButton(parent,label,handler){const button=element('button',label);button.type='button';button.addEventListener('click',handler);parent.appendChild(button);}
function showTable(title,rows,columns){clear(detailsNode);detailsNode.hidden=false;detailsNode.appendChild(element('h2',title));const table=element('table');const head=element('tr');columns.forEach(c=>head.appendChild(element('th',c.label)));const thead=element('thead');thead.appendChild(head);table.appendChild(thead);const tbody=element('tbody');rows.forEach(row=>{const tr=element('tr');columns.forEach(c=>tr.appendChild(element('td',row[c.key]??'-')));tbody.appendChild(tr);});table.appendChild(tbody);detailsNode.appendChild(table);}
async function showKeys(name){try{const body=await request('/api/keys/'+encodeURIComponent(name));showTable(name+' · Key',body.data||[],[{key:'name',label:'名称'},{key:'group',label:'分组'},{key:'key_masked',label:'Key（脱敏）'}]);}catch(error){messageNode.textContent=error.message;}}
async function showGroups(name){try{const body=await request('/api/groups/'+encodeURIComponent(name));showTable(name+' · 分组',body.data||[],[{key:'name',label:'名称'},{key:'ratio',label:'倍率'},{key:'desc',label:'说明'}]);}catch(error){messageNode.textContent=error.message;}}
function renderSites(items){clear(sitesNode);items.forEach(site=>{const card=element('article',undefined,'card');card.appendChild(element('h2',site.name));card.appendChild(element('p','状态：'+site.tier));card.appendChild(element('p','余额：'+site.balance+' '+(site.display_type||'')));card.appendChild(element('p','分组：'+(site.group||'-')));if(site.diagnosis)card.appendChild(element('p',site.diagnosis,'muted'));const actions=element('div','', 'toolbar');addButton(actions,'Key',()=>showKeys(site.name));addButton(actions,'分组',()=>showGroups(site.name));card.appendChild(actions);sitesNode.appendChild(card);});}
async function loadStatus(refresh=false){try{messageNode.textContent='正在加载…';const body=await request('/api/status'+(refresh?'?refresh=1':''));renderSites(body.data||[]);messageNode.textContent='站点数：'+(body.data||[]).length;}catch(error){messageNode.textContent=error.message;}}
document.getElementById('refresh').addEventListener('click',()=>loadStatus(true));
document.getElementById('logout').addEventListener('click',async()=>{try{await request('/logout',{method:'POST'});location.reload();}catch(error){messageNode.textContent=error.message;}});
loadStatus(false);
</script></body></html>"""


def _public(value: Any) -> Any:
    value = models.sanitize(value)
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, str):
        return rcache.redact_text(value)
    return value


class RelayctlWebHandler(BaseHTTPRequestHandler):
    config = None
    sessions = {}
    server_version = "relayctl"
    sys_version = ""

    def _cfg(self):
        return self.config if self.config is not None else load_config()

    def _password(self) -> str:
        return str((self._cfg().get("web") or {}).get("access_password") or "")

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def _send_json(self, data, code=200, extra_headers=None):
        body = json.dumps(_public(data), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        if code == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="relayctl"')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_str, code=200, extra_headers=None):
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        if code == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="relayctl"')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_session(self):
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
        except cookies.CookieError:
            return None, None
        morsel = jar.get(SESSION_COOKIE)
        token = morsel.value if morsel else None
        return token, self.sessions.get(token) if token else None

    def _bearer_valid(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header[7:].strip()
        password = self._password()
        return bool(password) and hmac.compare_digest(supplied, password)

    def _authenticated(self) -> bool:
        if not self._password():
            return True
        if self._bearer_valid():
            return True
        _, session = self._cookie_session()
        return session is not None

    def _require_auth(self, api=True) -> bool:
        if self._authenticated():
            return True
        if api:
            self._send_json({"success": False, "message": "未认证"}, 401)
        else:
            self._send_html(LOGIN_TEMPLATE, 401)
        return False

    def _same_origin_or_csrf(self) -> bool:
        token, session = self._cookie_session()
        del token
        csrf = self.headers.get("X-CSRF-Token", "")
        if session and csrf and hmac.compare_digest(csrf, str(session.get("csrf") or "")):
            return True
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        source = origin or referer
        if source:
            parsed = urllib.parse.urlparse(source)
            host = self.headers.get("Host", "")
            return parsed.scheme in ("http", "https") and parsed.netloc == host
        # 无浏览器来源头时只允许显式 Bearer 客户端；Cookie 会话必须给同源头或 CSRF。
        return self._bearer_valid()

    def _content_length(self) -> Optional[int]:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            length = int(raw)
        except ValueError:
            self._send_json({"success": False, "message": "Content-Length 无效"}, 400)
            return None
        if length < 0:
            self._send_json({"success": False, "message": "Content-Length 无效"}, 400)
            return None
        if length > MAX_BODY_BYTES:
            self._send_json({"success": False, "message": "请求体过大"}, 413)
            return None
        return length

    def _read_json(self):
        length = self._content_length()
        if length is None:
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json({"success": False, "message": "请求体不是合法 JSON"}, 400)
            return None
        if not isinstance(value, dict):
            self._send_json({"success": False, "message": "请求体必须是 JSON 对象"}, 400)
            return None
        return value

    def _read_form(self):
        length = self._content_length()
        if length is None:
            return None
        try:
            text = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError:
            self._send_json({"success": False, "message": "表单编码无效"}, 400)
            return None
        return urllib.parse.parse_qs(text, keep_blank_values=True)

    def _sites(self):
        cfg = self._cfg()
        return cfg, cfg.get("sites", {})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        is_api = path.startswith("/api/")
        if path == "/login":
            self._send_html(LOGIN_TEMPLATE)
            return
        if not self._require_auth(api=is_api):
            return
        if path in ("/", "/index.html"):
            self._send_html(HTML_TEMPLATE)
            return
        if path == "/api/session":
            _, session = self._cookie_session()
            self._send_json({"success": True, "csrf": (session or {}).get("csrf", "")})
            return

        cfg, sites = self._sites()
        if path == "/api/status":
            force_refresh = "refresh" in urllib.parse.parse_qs(parsed.query)
            from concurrent.futures import ThreadPoolExecutor

            def process_site(item):
                name, site = item
                tier = site.get("tier", "parked")
                info = {"name": name, "tier": tier, "balance": "-", "used": "-",
                        "group": "-", "key_count": "-", "display_type": "", "diagnosis": ""}
                if tier != "api":
                    info["diagnosis"] = site.get("parked_reason") or "已搁置"
                    return info
                try:
                    adapter = get_adapter(name, site, cfg)
                    mode = rcache.MODE_REFRESH if force_refresh else rcache.MODE_AUTO
                    balance, _ = rcache.fetch(name, "balance", adapter.balance, mode)
                    keys, _ = rcache.fetch(name, "keys", adapter.list_keys, mode)
                    info.update({"balance": str(balance.get("balance", "-")),
                                 "used": str(balance.get("used", "-")),
                                 "group": balance.get("user_group", "-"),
                                 "display_type": balance.get("display_type", ""),
                                 "key_count": len(keys) if isinstance(keys, list) else "-"})
                except Exception as exc:
                    info["diagnosis"] = "读取异常: " + rcache.sanitize_error(exc)
                return info

            with ThreadPoolExecutor(max_workers=min(len(sites) or 1, 10)) as executor:
                data = list(executor.map(process_site, sites.items()))
            self._send_json({"success": True, "data": data})
            return
        if path.startswith("/api/keys/"):
            name = urllib.parse.unquote(path[len("/api/keys/"):])
            if name not in sites:
                self._send_json({"success": False, "message": "站点未登记"}, 404)
                return
            try:
                data = get_adapter(name, sites[name], cfg).list_keys()
                self._send_json({"success": True, "data": data})
            except Exception:
                self._send_json({"success": False, "message": "读取 Key 失败"}, 500)
            return
        if path.startswith("/api/groups/"):
            name = urllib.parse.unquote(path[len("/api/groups/"):])
            if name not in sites:
                self._send_json({"success": False, "message": "站点未登记"}, 404)
                return
            try:
                self._send_json({"success": True, "data": get_adapter(name, sites[name], cfg).groups()})
            except Exception:
                self._send_json({"success": False, "message": "读取分组失败"}, 500)
            return
        self._send_json({"success": False, "message": "Not Found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            if not self._same_origin_or_csrf():
                self._send_json({"success": False, "message": "跨站请求被拒绝"}, 403)
                return
            form = self._read_form()
            if form is None:
                return
            supplied = (form.get("password") or [""])[0]
            password = self._password()
            if not password or not hmac.compare_digest(str(supplied), password):
                self._send_html(LOGIN_TEMPLATE, 401)
                return
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {"csrf": secrets.token_urlsafe(32)}
            cookie = f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"
            self._send_html("登录成功", 303, {"Set-Cookie": cookie, "Location": "/"})
            return

        if not self._require_auth(api=True):
            return
        if not self._same_origin_or_csrf():
            self._send_json({"success": False, "message": "跨站请求被拒绝"}, 403)
            return
        if path == "/logout":
            token, _ = self._cookie_session()
            if token:
                self.sessions.pop(token, None)
            self._send_json({"success": True}, extra_headers={
                "Set-Cookie": f"{SESSION_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"})
            return
        if path == "/api/key/add":
            body = self._read_json()
            if body is None:
                return
            cfg, sites = self._sites()
            name = str(body.get("site") or "")
            if name not in sites:
                self._send_json({"success": False, "message": "站点未登记"}, 404)
                return
            try:
                result = get_adapter(name, sites[name], cfg).create_key(
                    name=body.get("name"), group=body.get("group"), quota=body.get("quota"),
                    unlimited=bool(body.get("unlimited", False)))
                rcache.invalidate(name, "keys")
                self._send_json({"success": True, "key_id": result.get("id"),
                                 "key_masked": result.get("key_masked")})
            except Exception:
                self._send_json({"success": False, "message": "创建 Key 失败"}, 500)
            return
        self._send_json({"success": False, "message": "Not Found"}, 404)

    def log_message(self, fmt, *args):
        message = rcache.redact_text(fmt % args)
        print(f"[web] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Web 内置后台调度器：随 server 启停，绝不由 Web 请求各自创建
# ---------------------------------------------------------------------------
def scheduler_enabled(cfg: Optional[dict], override: Optional[bool] = None) -> bool:
    if override is not None:
        return bool(override)
    web_cfg = (cfg or {}).get("web") or {}
    return bool(web_cfg.get("scheduler_enabled", True))


def start_background_scheduler(cfg: Optional[dict] = None, enabled: Optional[bool] = None,
                               factory: Any = None) -> Any:
    from .scheduler import Scheduler
    cfg = cfg if cfg is not None else load_config()
    if not scheduler_enabled(cfg, enabled):
        return None
    builder = factory or _SCHEDULER_FACTORY or Scheduler
    scheduler = builder(cfg)
    if scheduler is None:
        return None
    scheduler.start()
    return scheduler


def stop_background_scheduler(scheduler: Any, timeout: float = 5.0):
    if scheduler is None:
        return
    try:
        scheduler.stop(timeout)
    except Exception:
        pass


def run_web(host: str = "127.0.0.1", port: int = 8088,
            config_override: Optional[dict] = None,
            scheduler_enabled: Optional[bool] = None):
    """启动 Web；非回环监听必须先配置非空访问口令。"""
    global _WEB_SCHEDULER
    cfg = config_override or load_config()
    password = str((cfg.get("web") or {}).get("access_password") or "")
    if not is_loopback(host) and not password:
        raise ConfigError("Web 监听非回环地址时必须配置 config.web.access_password")

    RelayctlWebHandler.config = cfg
    RelayctlWebHandler.sessions = {}
    httpd = HTTPServer((host, port), RelayctlWebHandler)
    _WEB_SCHEDULER = start_background_scheduler(cfg, enabled=scheduler_enabled)
    print(f"[*] relayctl Web 控制台已启动: http://{host}:{port}/")
    if password:
        print("    Web 访问口令认证已启用。")
    if _WEB_SCHEDULER is not None:
        print("    内置后台刷新调度已随控制台启动。")
    print("    按 Ctrl+C 停止服务。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] relayctl Web 服务已停止。")
    finally:
        try:
            httpd.server_close()
        finally:
            stop_background_scheduler(_WEB_SCHEDULER)
            _WEB_SCHEDULER = None


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    run_web(port=port)
