import json
import math
from typing import Dict, Any, List, Optional
from .base import BaseAdapter, NotSupported
from ..core import HttpClient, mask_key, AuthError

class NewAPIAdapter(BaseAdapter):
    NAME = "newapi"

    def __init__(self, name: str, site_cfg: dict, core_cfg: dict):
        super().__init__(name, site_cfg, core_cfg)
        self.client = HttpClient(self.base_url, proxy=self.proxy, auth=self.auth)
        self.quota_per_unit = 500000
        self.quota_display_type = "USD"
        self._quota_unit_known = False
        self._probed = False

    def _check_auth_response(self, r, json_data: Optional[dict] = None):
        """检查响应是否鉴权失败，若是则抛出 AuthError"""
        if r.status_code == 401:
            raise AuthError(f"HTTP 401 Unauthorized: {r.text[:100]}")
        if json_data:
            code = str(json_data.get("code") or json_data.get("errorCode") or "")
            msg = str(json_data.get("message") or "")
            if code.startswith("AUTH_") or "AUTH_" in code or "unauthorized" in msg.lower():
                raise AuthError(f"鉴权失败 ({code}): {msg}")

    def probe(self) -> dict:
        result = {
            "site": self.name,
            "type": "New API",
            "system_name": "未知",
            "version": "未知",
            "auth_ok": False,
            "quota_per_unit": 500000,
            "quota_display_type": "USD"
        }
        # 1. 探测 status
        try:
            r = self.client.get("/api/status", timeout=8)
            if r.status_code == 200:
                data = r.json().get("data", {})
                result["system_name"] = data.get("system_name", "New API")
                result["version"] = data.get("version", "v1.0.0")
                raw_unit = data.get("quota_per_unit")
                if isinstance(raw_unit, (int, float)) and not isinstance(raw_unit, bool) and math.isfinite(raw_unit) and raw_unit > 0:
                    result["quota_per_unit"] = raw_unit
                    self.quota_per_unit = raw_unit
                    self._quota_unit_known = True
                else:
                    result["quota_per_unit"] = 500000
                result["quota_display_type"] = data.get("quota_display_type", "USD")
                self.quota_display_type = result["quota_display_type"]
                self._probed = True
        except Exception as e:
            result["status_error"] = str(e)

        # 2. 探测 auth
        try:
            r = self.client.get("/api/user/self", timeout=8)
            self._check_auth_response(r)
            if r.status_code == 200:
                d = r.json()
                self._check_auth_response(r, d)
                if d.get("success"):
                    result["auth_ok"] = True
                    result["username"] = d.get("data", {}).get("username")
                    result["role"] = d.get("data", {}).get("role")
                    result["user_group"] = d.get("data", {}).get("group")
                else:
                    result["auth_msg"] = d.get("message")
            else:
                result["auth_status"] = r.status_code
        except AuthError as ae:
            result["auth_msg"] = str(ae)
        except Exception as e:
            result["auth_error"] = str(e)

        return result

    def _ensure_probed(self):
        if not self._probed:
            try:
                self.probe()
            except Exception:
                pass

    def _ensure_quota_unit(self):
        self._ensure_probed()
        if not self._quota_unit_known:
            raise NotSupported(
                f"站点 {self.name} 未确认 quota_per_unit，禁止执行额度写操作以避免单位换算错误。"
            )

    def balance(self) -> dict:
        self._ensure_probed()
        r = self.client.get("/api/user/self")
        self._check_auth_response(r)
        
        # 兼容 FastAI 等 /api/v1/user/profile 定制管理端点
        if r.status_code == 404:
            r_alt = self.client.get("/api/v1/user/profile")
            self._check_auth_response(r_alt)
            if r_alt.status_code == 200:
                try:
                    d_alt = r_alt.json()
                    self._check_auth_response(r_alt, d_alt)
                    if d_alt.get("code") == 0 and "data" in d_alt:
                        ud = d_alt["data"]
                        bal_val = ud.get("balance", 0)
                        unit = self.quota_per_unit or 500000
                        return {
                            "balance": f"{bal_val:.4f}",
                            "used": "0.0000",
                            "raw_quota": int(bal_val * unit),
                            "raw_used": 0,
                            "quota_per_unit": unit,
                            "display_type": self.quota_display_type or "USD",
                            "user_group": ud.get("role", "default")
                        }
                except AuthError:
                    raise
                except Exception:
                    pass
            # 一律不猜、不编造余额：端点不可用就明确报 NotSupported
            raise NotSupported(
                f"无法自动读取余额：站点 {self.name} 的 /api/user/self 返回 404，"
                f"/api/v1/user/profile 兜底也不可用。请人工登录站点控制台查看，"
                f"或抓包确认其管理端点后再接入。"
            )

        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        self._check_auth_response(r, d)
        if not d.get("success"):
            raise Exception(d.get("message", "获取余额失败"))
        
        user_data = d.get("data", {})
        quota = user_data.get("quota", 0)
        used_quota = user_data.get("used_quota", 0)
        group = user_data.get("group", "default")
        
        # 统一不换算：金额 = quota / quota_per_unit，只显示纯数字
        unit = self.quota_per_unit or 500000
        val = quota / unit
        used_val = used_quota / unit

        return {
            "balance": f"{val:.4f}",
            "used": f"{used_val:.4f}",
            "raw_quota": quota,
            "raw_used": used_quota,
            "quota_per_unit": unit,
            "display_type": self.quota_display_type or "USD",
            "user_group": group
        }

    def groups(self) -> list:
        groups_list = []
        try:
            r = self.client.get("/api/user/self/groups")
            self._check_auth_response(r)
            if r.status_code == 200:
                d = r.json()
                self._check_auth_response(r, d)
                data = d.get("data", {})
                if isinstance(data, dict):
                    for g_name, val in data.items():
                        if isinstance(val, dict):
                            desc = val.get("desc") or val.get("description") or ""
                            ratio = val.get("ratio", 1)
                        else:
                            desc = ""
                            ratio = val
                        groups_list.append({"name": g_name, "desc": desc, "ratio": ratio})
                elif isinstance(data, list):
                    for g in data:
                        groups_list.append({"name": g.get("name"), "desc": g.get("description", ""), "ratio": g.get("ratio", 1)})
        except AuthError:
            raise
        except Exception:
            pass

        if not groups_list:
            try:
                r = self.client.get("/api/pricing")
                if r.status_code == 200:
                    data = r.json().get("data", {})
                    found = set()
                    for item in (data if isinstance(data, list) else []):
                        g = item.get("group")
                        if g and g not in found:
                            found.add(g)
                            groups_list.append({"name": g, "desc": "来自模型定价", "ratio": 1.0})
            except Exception:
                pass

        if not groups_list:
            groups_list.append({"name": "default", "desc": "默认分组", "ratio": 1.0})
        return groups_list

    def list_keys(self) -> list:
        """带循环翻页与格式兼容的 Key 列表获取"""
        self._ensure_probed()
        unit = self.quota_per_unit or 500000
        all_items = []
        page = 1
        page_param = "p"
        parsed_ok = False
        last_status = None

        # 首先探测是用 p 还是 page
        test_r = self.client.get("/api/token/?p=1&size=50")
        self._check_auth_response(test_r)
        if test_r.status_code != 200 or "application/json" not in test_r.headers.get("Content-Type", ""):
            page_param = "page"
        else:
            try:
                td = test_r.json()
                self._check_auth_response(test_r, td)
                parsed_ok = True
                if not td.get("success"):
                    page_param = "page"
                    parsed_ok = False
            except AuthError:
                raise
            except Exception:
                page_param = "page"

        while True:
            r = self.client.get(f"/api/token/?{page_param}={page}&size=50")
            self._check_auth_response(r)
            if r.status_code != 200:
                # 换另一个参数兜底试一次
                last_status = r.status_code
                alt_param = "page" if page_param == "p" else "p"
                r = self.client.get(f"/api/token/?{alt_param}={page}&size=50")
                self._check_auth_response(r)
                if r.status_code != 200:
                    last_status = r.status_code
                    break

            try:
                d = r.json()
            except Exception:
                last_status = f"非 JSON 响应 (HTTP {r.status_code})"
                break

            self._check_auth_response(r, d)
            parsed_ok = True
            if not d.get("success"):
                last_status = f"业务失败: {d.get('message')}"
                break

            raw_data = d.get("data", [])
            items = []
            total = None
            if isinstance(raw_data, dict):
                items = raw_data.get("items") or raw_data.get("data") or []
                total = raw_data.get("total")
            elif isinstance(raw_data, list):
                items = raw_data

            if not items:
                break

            all_items.extend(items)

            if total is not None and len(all_items) >= total:
                break
            if len(items) < 50:
                break
            page += 1
            if page > 20: # 防止极端死循环
                break

        if not parsed_ok:
            # 定制端点（如 fastai）上 /api/token/ 不可用，别再静默返回空列表假装 0 个 Key
            raise NotSupported(
                f"站点 {self.name} 的 Key 列表接口不可用（{last_status or '未知错误'}），无法自动统计 Key 数。"
            )

        keys = []
        for it in all_items:
            if not isinstance(it, dict):
                continue
            key_val = it.get("key", "")
            remain = "无限" if it.get("unlimited_quota") else f"{it.get('remain_quota', 0) / unit:.4f}"
            used = f"{it.get('used_quota', 0) / unit:.4f}"
            status_str = "启用" if it.get("status") == 1 else "禁用"
            keys.append({
                "id": it.get("id"),
                "name": it.get("name"),
                "group": it.get("group", "default"),
                "status": status_str,
                "remain": remain,
                "used": used,
                "key_masked": mask_key(key_val),
                "_raw_key": key_val
            })
        return keys

    def create_key(self, name: str, group: str, quota: float, models: list = None) -> dict:
        self._ensure_probed()
        unit = self.quota_per_unit or 500000
        # 只有显式的无限额度（quota is None）才算无限；--quota 必须 > 0
        is_unlimited = (quota is None)
        if not is_unlimited and quota <= 0:
            raise ValueError(
                f"额度必须大于 0（收到 {quota}）！只有显式指定 --unlimited 才会创建无限额度 Key。"
            )
        if not is_unlimited:
            self._ensure_quota_unit()
        
        # 记录创建前的 Key 列表用于做差集匹配
        before_keys = []
        before_snapshot_ok = False
        try:
            before_keys = self.list_keys()
            before_snapshot_ok = True
        except Exception:
            pass
        before_ids = {k["id"] for k in before_keys if k.get("id")}

        payload = {
            "name": name,
            "group": group,
            "expired_time": -1,
            "remain_quota": 0 if is_unlimited else int(quota * unit),
            "unlimited_quota": is_unlimited,
            "model_limits_enabled": bool(models),
            "model_limits": ",".join(models) if models else ""
        }
        r = self.client.post("/api/token/", json=payload)
        self._check_auth_response(r)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        self._check_auth_response(r, d)
        if not d.get("success"):
            raise Exception(d.get("message", "新建 Key 失败"))
            
        res_data = d.get("data", {})
        key_id = None
        key_val = ""
        if isinstance(res_data, dict):
            key_id = res_data.get("id")
            key_val = res_data.get("key", "")
        elif isinstance(res_data, int):
            key_id = res_data
            
        # 若创建接口未直接返回 id，通过创建前后列表差集精准匹配新出现的 Key
        if not key_id and before_snapshot_ok:
            try:
                after_keys = self.list_keys()
                new_created = [k for k in after_keys if k.get("id") not in before_ids and k.get("name") == name]
                if len(new_created) == 1:
                    key_id = new_created[0]["id"]
                    key_val = new_created[0]["_raw_key"]
                elif len(new_created) > 1:
                    raise Exception(f"匹配到多个同名且新创建的 Key ({name})，无法唯一确定 ID！")
            except Exception as e:
                if "无法唯一确定" in str(e):
                    raise

        if not key_id:
            raise NotSupported(
                f"站点 {self.name} 创建 Key 后未能可靠确认新 Key ID，拒绝按名称认领已有 Key。"
            )
        return {"id": key_id, "key_masked": mask_key(key_val), "_raw_key": key_val}

    def update_key(self, key_id: int, group: str = None, status: int = None, quota: float = None, name: str = None, unlimited: bool = False) -> dict:
        self._ensure_probed()
        unit = self.quota_per_unit or 500000
        if quota is not None:
            self._ensure_quota_unit()

        def update_status_only():
            sr = self.client.put("/api/token/?status_only=true", json={"id": key_id, "status": status})
            self._check_auth_response(sr)
            if sr.status_code != 200:
                raise Exception(f"HTTP {sr.status_code}: {sr.text[:200]}")
            sd = sr.json()
            self._check_auth_response(sr, sd)
            if not sd.get("success"):
                raise Exception(sd.get("message", "修改 Key 状态失败"))

        # 第 4 项：启用/禁用只改状态时调用 PUT /api/token/?status_only=true
        if status is not None and group is None and quota is None and name is None and not unlimited:
            update_status_only()
            return {"id": key_id, "updated": True, "status_only": True}

        # 完整更新流程：先提取完整原始对象
        payload = None
        r = self.client.get(f"/api/token/{key_id}")
        self._check_auth_response(r)
        if r.status_code == 200:
            try:
                d = r.json()
                self._check_auth_response(r, d)
                if d.get("success") and isinstance(d.get("data"), dict):
                    payload = d["data"]
            except AuthError:
                raise
            except Exception:
                pass

        if not payload:
            try:
                keys = self.list_keys()
                for it in keys:
                    if it.get("id") == key_id:
                        # 尝试通过 GET 再次拉取或者从列表映射
                        break
            except Exception:
                pass

        if not payload:
            raise Exception(f"无法获取 Key ID {key_id} 的原始完整对象，已终止操作以防止损坏 Token 配置！")
            
        if group is not None:
            payload["group"] = group
        # 状态由独立 status_only 接口更新；普通 PUT 携带 status 会在额度校验前使用旧额度。
        payload.pop("status", None)
        if name is not None:
            payload["name"] = name
        # 只有显式的 --unlimited 才允许置为无限；--quota 必须 > 0，否则宁可报错也不静默放大
        if unlimited:
            payload["unlimited_quota"] = True
            payload["remain_quota"] = 0
        elif quota is not None:
            if quota <= 0:
                raise ValueError(
                    f"额度必须大于 0（收到 {quota}）！只有显式指定 --unlimited 才会改为无限额度。"
                )
            payload["unlimited_quota"] = False
            payload["remain_quota"] = int(quota * unit)
                
        r = self.client.put("/api/token/", json=payload)
        self._check_auth_response(r)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        self._check_auth_response(r, d)
        if not d.get("success"):
            raise Exception(d.get("message", "修改 Key 失败"))
        if status is not None:
            try:
                update_status_only()
            except Exception as e:
                raise Exception(f"Key 已完成其他字段更新，但状态更新失败: {e}") from e
        return {"id": key_id, "updated": True}

    def delete_key(self, key_id: int) -> bool:
        r = self.client.delete(f"/api/token/{key_id}")
        self._check_auth_response(r)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        self._check_auth_response(r, d)
        if not d.get("success"):
            raise Exception(d.get("message", "删除 Key 失败"))
        return True

    def reveal_key(self, key_id: int) -> str:
        """提取明文 Key，绝不允许任何含 * 的打码 Key 通过"""
        # 1. POST /api/token/:id/key
        try:
            r = self.client.post(f"/api/token/{key_id}/key")
            self._check_auth_response(r)
            if r.status_code == 200:
                d = r.json()
                self._check_auth_response(r, d)
                if d.get("success"):
                    k = d.get("data")
                    if isinstance(k, dict):
                        k = k.get("key")
                    if k and isinstance(k, str) and "*" not in k:
                        return k
        except AuthError:
            raise
        except Exception:
            pass

        # 2. GET /api/token/:id
        try:
            r = self.client.get(f"/api/token/{key_id}")
            self._check_auth_response(r)
            if r.status_code == 200:
                d = r.json()
                self._check_auth_response(r, d)
                if d.get("success"):
                    k = d.get("data", {}).get("key")
                    if k and isinstance(k, str) and "*" not in k:
                        return k
        except AuthError:
            raise
        except Exception:
            pass

        # 3. 尝试从列表拿
        try:
            keys = self.list_keys()
            for item in keys:
                if item["id"] == key_id:
                    raw = item.get("_raw_key")
                    if raw and "*" not in raw:
                        return raw
        except AuthError:
            raise
        except Exception:
            pass
                    
        raise NotSupported(f"站点 {self.name} 前端打码且未开放明文提取接口，禁止打码 Key 流入同步！请人工在站点复制后同步。")
