import requests
import json
import os
import time
from .core import load_config, STATE_DIR, secure_write_json

def check_single_site(name, site_cfg):
    base_url = site_cfg.get("base", "").rstrip("/")
    proxy_val = site_cfg.get("proxy")
    parked_reason = site_cfg.get("parked_reason") or "缺少凭据"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    proxies = {"http": proxy_val, "https": proxy_val} if proxy_val else None
    
    start = time.time()
    try:
        # 尝试访问根路径或 status
        test_url = f"{base_url}/api/status" if site_cfg.get("adapter") == "newapi" else base_url
        r = requests.get(test_url, headers=headers, proxies=proxies, timeout=10)
        elapsed = time.time() - start
        code = r.status_code
        text = r.text
        
        status = "仍受限"
        detail = parked_reason
        
        if code == 522 or "Error code 522" in text:
            status = "仍宕机"
            detail = "Cloudflare 522 源站仍未响应"
        elif "正在进行安全验证" in text or ("Cloudflare" in text and "Ray ID" in text and code in [403, 503]):
            status = "盾拦截中"
            detail = "Cloudflare 质询页依然生效，需人工在浏览器过盾"
        elif code == 200:
            if site_cfg.get("adapter") == "newapi" and ("system_name" in text or "version" in text):
                status = "服务在线"
                detail = f"服务健康运行，{parked_reason}"
            else:
                status = "页面正常"
                detail = f"返回 200 OK，{parked_reason}"
        elif code == 401:
            status = "接口在线"
            detail = f"HTTP 401，需要有效登录凭据 ({parked_reason})"
        else:
            detail = f"HTTP {code} | {parked_reason}"
            
        return {
            "name": name,
            "adapter": site_cfg.get("adapter", "-"),
            "code": code,
            "status": status,
            "latency": f"{elapsed:.2f}s",
            "detail": detail
        }
    except requests.exceptions.Timeout:
        return {
            "name": name,
            "adapter": site_cfg.get("adapter", "-"),
            "code": "超时",
            "status": "连接超时",
            "latency": ">10s",
            "detail": "请求超时，可能网络不通或源站阻断"
        }
    except Exception as e:
        return {
            "name": name,
            "adapter": site_cfg.get("adapter", "-"),
            "code": "错误",
            "status": "连接异常",
            "latency": "-",
            "detail": str(e)[:30]
        }

def run_parked_check():
    print("=== 开始执行 parked 受限站点自动化健康巡检 ===")
    cfg = load_config()
    sites = cfg.get("sites", {})
    
    # P1 - 8: 全部从 sites.yaml 动态过滤出 tier == 'parked' 的站点
    parked_sites = {k: v for k, v in sites.items() if v.get("tier") == "parked"}
    if not parked_sites:
        print("[✓] 当前没有处于 parked 状态的站点。")
        return

    results = []
    for name, s_cfg in parked_sites.items():
        res = check_single_site(name, s_cfg)
        results.append(res)
        
    cache_path = os.path.join(STATE_DIR, "parked_check_summary.json")
    secure_write_json(cache_path, results)

    print("\n" + "="*85)
    print(f"{'站点名称':<14} | {'适配器':<12} | {'状态':<10} | {'响应耗时':<8} | {'巡检诊断详情'}")
    print("-" * 85)
    for r in results:
        print(f"{r['name']:<14} | {r['adapter']:<12} | {r['status']:<10} | {r['latency']:<8} | {r['detail']}")
    print("="*85)
    print(f"[*] 巡检结果已安全缓存至 (600): {cache_path}\n")

if __name__ == "__main__":
    run_parked_check()
