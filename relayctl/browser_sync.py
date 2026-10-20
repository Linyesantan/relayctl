import subprocess
import json
import time
import os
import sys
from .core import load_config, STATE_DIR, secure_write_json, mask_key

def cdp_call(cmd, *args):
    full_cmd = ["/home/lunaadmin/bin/browser-cdp", cmd] + list(args)
    res = subprocess.run(full_cmd, capture_output=True, text=True)
    try:
        return json.loads(res.stdout)
    except Exception:
        return {"ok": False, "raw": res.stdout, "err": res.stderr}

def eval_in_tab(tab_id, js_code):
    res = cdp_call("eval", tab_id, js_code)
    if res.get("ok"):
        return res.get("result")
    return None

LOGIN_CHECK_JS = """(() => {
    const href = location.href || '';
    const urlLogin = /(\\/|^)(login|signin|sign-in|sign_in|auth)(\\?|#|\\/|$)/i.test(href);
    const pwdField = !!document.querySelector('input[type=password]');
    return { href: href, url_login: urlLogin, password_field: pwdField };
})()"""


def is_login_page(tab_id):
    """导航后判断是否被踢到登录页，避免未登录却报「已获取」"""
    info = eval_in_tab(tab_id, LOGIN_CHECK_JS)
    if not isinstance(info, dict):
        return False, {}
    return bool(info.get("url_login") or info.get("password_field")), info


def fetch_site_data(name, site_cfg, tab_id):
    base_url = site_cfg.get("base", "").rstrip("/")
    ch_id = site_cfg.get("local_channel")
    
    # 支持自定义路径配置（经典版 /console/topup, /console/token；新版 /wallet, /keys）
    wallet_path = site_cfg.get("wallet_path") or ("/console/topup" if name == "kuaipao" else "/wallet")
    keys_path = site_cfg.get("keys_path") or ("/console/token" if name == "kuaipao" else "/keys")
    
    print(f"[*] 正在拉取: {name} ({base_url}) ...")
    
    # 1. 访问钱包/充值页面提取余额
    cdp_call("navigate", tab_id, f"{base_url}{wallet_path}")
    time.sleep(2.5)

    # 未登录会被重定向到 /login，此时页面里没有余额，不能报「已登录 / 已获取」
    logged_out, info = is_login_page(tab_id)
    if logged_out:
        return {
            "name": name,
            "balance": "未登录",
            "channel_id": ch_id,
            "keys_masked": [],
            "key_count": 0,
            "status": f"未登录（停留在 {info.get('href', '登录页')}）"
        }

    balance_text = eval_in_tab(tab_id, """(() => {
        const text = document.body.innerText;
        const matches = text.match(/(当前余额|账户余额|余额|剩余配额|普通余额|统一额度)[^\\n]*\\n*([¥$]?[0-9\\.\\-]+[\\s]*(credits|USD|CNY)?)/i);
        if (matches) return matches[0].replace(/\\n+/g, ' ');
        const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
        for (let i = 0; i < lines.length; i++) {
            if (['余额', '当前余额', '账户余额', '统一额度'].includes(lines[i]) && i + 1 < lines.length) {
                return lines[i] + ': ' + lines[i+1];
            }
        }
        return "已登录";
    })()""")

    # 2. 访问 API Keys 页面提取 Key
    cdp_call("navigate", tab_id, f"{base_url}{keys_path}")
    time.sleep(2.5)

    logged_out, info = is_login_page(tab_id)
    if logged_out:
        return {
            "name": name,
            "balance": "未登录",
            "channel_id": ch_id,
            "keys_masked": [],
            "key_count": 0,
            "status": f"未登录（停留在 {info.get('href', '登录页')}）"
        }

    # 异步剪贴板拦截：await 约 500ms 后再恢复 writeText，确保抓到异步复制内容
    keys_data = eval_in_tab(tab_id, """(async () => {
        let captured = [];
        const origWrite = navigator.clipboard.writeText;
        navigator.clipboard.writeText = (text) => {
            if (text && text.startsWith('sk-') && !text.includes('*')) {
                captured.push(text);
            }
            return Promise.resolve();
        };

        // 监听 copy 事件
        const copyHandler = (e) => {
            if (e.clipboardData) {
                const text = e.clipboardData.getData('text');
                if (text && text.startsWith('sk-') && !text.includes('*')) {
                    captured.push(text);
                }
            }
        };
        document.addEventListener('copy', copyHandler);

        try {
            // 精确查找表格行内的复制专用按钮
            const rows = Array.from(document.querySelectorAll("table tbody tr, [role=row], .border, .rounded-xl"));
            for (const r of rows) {
                const copyBtn = r.querySelector('button[title*="复制"], button[aria-label*="复制"], .lucide-copy')?.closest('button');
                if (copyBtn) {
                    copyBtn.click();
                }
            }
            // 等待 500ms 捕获异步剪贴板写入
            await new Promise(resolve => setTimeout(resolve, 500));
        } finally {
            navigator.clipboard.writeText = origWrite;
            document.removeEventListener('copy', copyHandler);
        }

        // 读取页面上现有的只读 input（New API popover 输入框）
        const popInputs = Array.from(document.querySelectorAll('input[readonly], table input'));
        for (const inp of popInputs) {
            if (inp.value && inp.value.startsWith('sk-') && !inp.value.includes('*')) {
                captured.push(inp.value);
            }
        }
        
        return {
            keys: Array.from(new Set(captured)),
            rowCount: document.querySelectorAll('table tbody tr, [role=row]').length
        };
    })()""") or {}

    raw_keys = keys_data.get("keys", [])
    # 脱敏后存入摘要：不写明文 key 到 state，只写打码后前 4 后 4
    masked_keys = [mask_key(k) for k in raw_keys]

    row_count = keys_data.get("rowCount") or 0
    # Key 数必须是实际抓到的 Key 数，不能用页面行数（行数含表头/空行，会虚高）
    key_count = len(masked_keys)
    status = "已获取" if key_count else f"已登录但页面未抓到 Key（表格行 {row_count}）"

    return {
        "name": name,
        "balance": balance_text,
        "channel_id": ch_id,
        "keys_masked": masked_keys,
        "key_count": key_count,
        "row_count": row_count,
        "status": status
    }

def run_managed_browser_sync(manual=False):
    if not manual:
        print("[!] 规则拦截：该操作需要连接已登录 Chrome；按安全规范，严禁任何自动化或后台静默调用 Chrome！")
        print("[!] 如需手动执行，请人工显式附带 --manual 参数：relayctl pull-browser --manual")
        sys.exit(1)

    print("=== [手动激活] 开始执行已登录站点拉取 ===")
    tab_res = cdp_call("open", "about:blank")
    if not tab_res.get("ok"):
        print("[!] 打开 Chrome 标签页失败，请确认 Chrome 正在运行并在 9222 开启 CDP。")
        return
        
    tab_id = tab_res["result"]["id"]
    summary = []
    
    cfg = load_config()
    sites = cfg.get("sites", {})
    
    try:
        for name, s_cfg in sites.items():
            if s_cfg.get("adapter") in ["wusrouter"]:
                continue
            try:
                data = fetch_site_data(name, s_cfg, tab_id)
                summary.append(data)
            except Exception as e:
                summary.append({"name": name, "balance": "拉取异常", "status": str(e)[:30]})
    finally:
        cdp_call("close", tab_id)

    # 权限 600 安全缓存脱敏摘要
    cache_path = os.path.join(STATE_DIR, "browser_managed_summary.json")
    secure_write_json(cache_path, summary)

    print("\n" + "="*85)
    print(f"{'站点名称':<18} | {'本地渠道':<12} | {'当前账户余额':<26} | {'已建Key数':<8} | {'状态'}")
    print("-" * 85)
    for r in summary:
        ch = str(r.get("channel_id", "-"))
        bal = str(r.get("balance", "-"))[:24]
        kc = str(r.get("key_count", len(r.get("keys_masked", []))))
        print(f"{r['name']:<18} | {ch:<12} | {bal:<26} | {kc:<8} | {r['status']}")
    print("="*85)
    print(f"[*] 实时数据已安全脱敏缓存至 (600): {cache_path}\n")

if __name__ == "__main__":
    print("[!] 请用 relayctl pull-browser --manual 执行手动拉取。")
    sys.exit(0)
