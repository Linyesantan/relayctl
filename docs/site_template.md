# 站点接入档案模板 (Site Template)

当你接入一个新的上游 AI 中转站时，建议参考本模板进行档案记录。

---

## 站点标识: <site_name>
- **官网地址**: `https://api.example.com` (控制台: `https://api.example.com/console`)
- **系统类型**: New API / One API / 自研面板
- **当前状态**: `api` (全自动管理) 或 `parked` (缺少凭据/已搁置)
- **鉴权方式**: 
  * New API: `Authorization: Bearer <系统访问令牌>` + `New-Api-User: <uid>`
  * One API: `Authorization: Bearer <系统访问令牌>`
- **长期令牌获取路径**: 登录官网控制台 -> 个人设置 -> 生成「系统访问令牌」
- **用户分组与倍率**: 记录该站可用的主要分组（如 `default`, `pro`, `claude` 等）及官方标定倍率
- **支持的核心模型**: 如 `gpt-4o`, `claude-3-5-sonnet`, `deepseek-chat` 等
- **排障与注意事项**:
  * 遇到凭据失效或 401 报错时，直接登录对应官网控制台核验；
  * 严禁通过高频脚本机器死循环探测敏感管理端点（如全量定价 `/api/pricing`）。
