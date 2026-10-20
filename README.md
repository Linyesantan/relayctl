# relayctl

<p align="center">
  <b>多上游 AI 中转站 Key 与余额统一管理工具</b><br>
  开箱即用的轻量化多中转密钥运维控制台（CLI + 双端 Web Dashboard）
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/Web_UI-Mobile%20%26%20Desktop-purple.svg" alt="Responsive Web">
  <img src="https://img.shields.io/badge/Zero_Heavy_Deps-Pure_Python-success.svg" alt="Zero Heavy Deps">
</p>

---

## 💡 为什么需要 relayctl？

现代 AI 开发者往往同时使用多家基于 **New API / One API** 搭建的第三方 AI 中转站（公益站、拼车站、特惠站等）。随着账号逐渐增多，痛点日益明显：
- **查看余额太繁琐**：想查各个站点的剩余额度，必须逐个打开网页、过验证码、手动登录；
- **分组倍率不清晰**：很多站点上线了特惠/降智/纯血等不同倍率分组，每次都要去公告或模型广场翻倍率；
- **Key 管理分散**：想为不同的工具、脚本或客户端新建独立的 Key，必须在各个站点的网页端反复点开“令牌管理”手动创建。

`relayctl` 正是为此而生：**只需将各站点的长效系统访问令牌（System Access Token）集中登记在一个配置文件中，即可在终端或双端 Web 面板上一键统一查看所有站点的余额、分组倍率，并直接对上游 Key 进行创建、修改与启禁运维！**

---

## 🌟 核心特性

- 📱 **双端自适应 Web 控制台**：
  * 单文件内嵌轻量 Web 面板，**无需 Node.js、无需 npm run build、零复杂前端构建**，Python 原生驱动；
  * **移动端优先体验**：手机浏览器自适应触控卡片流与底部抽屉交互，随时随地查余额、查分组与一键开 Key；
  * **桌面端现代仪表盘**：宽屏毛玻璃数据看板，支持即时搜索过滤与多线程并发秒级状态刷新。
- 🔑 **上游 Key 全生命周期管理**：
  * 在终端或 Web 端直接向指定上游站点下发指令，创建新 Key（可自由指定名称、分组、限额或无限额度）；
  * 随时查看已有 Key 列表（默认自动脱敏显示，防止截屏或日常使用泄露）；
  * 在线启用、禁用或修改已有 Key 的额度与所属分组，支持直接删除废弃 Key。
- 📊 **站点与分组倍率直查**：
  * 统一查询各中转站当前生效的用户分组、标定倍率及站长原始描述（如 `pro`、`claude`、`特价` 分组）；
  * 自动化 `probe` 探测站点健康度、系统底层版本及鉴权有效性。
- 💰 **严格纯数字不换算纪律**：
  * 统一按各站点底层 `quota / quota_per_unit` 计算纯数字额度，杜绝虚假汇率换算与虚构货币符号拼接。
- 🛡️ **防高频机器扫描纪律**：
  * 内置智能短期只读缓存，常规非写操作优先读取缓存，避免暴力请求上游敏感管理端点（如全量定价 `/api/pricing`），防止被站长风控误判为恶意爬站。

---

## 🚀 快速上手

### 1. 克隆与安装依赖

```bash
git clone https://github.com/Linyesantan/relayctl.git
cd relayctl
pip install -r requirements.txt
```

建议创建软链接到系统路径，方便全局任意目录调用：
```bash
ln -sf $(pwd)/bin/relayctl ~/.local/bin/relayctl
```

### 2. 初始化配置文件

复制模板文件并锁定权限为 600（保护凭据安全）：
```bash
mkdir -p ~/.config/relayctl
cp sites.example.yaml ~/.config/relayctl/sites.yaml
chmod 600 ~/.config/relayctl/sites.yaml
```

编辑 `~/.config/relayctl/sites.yaml`，填入各上游中转站的系统访问令牌：
```yaml
sites:
  # 标准 New API 站点示例 (推荐)
  # 凭据获取方式: 登录站点后台 -> 个人设置 -> 生成「系统访问令牌」
  mysite-newapi:
    tier: api                  # 级别: api (全自动管理) 或 parked (已搁置)
    adapter: newapi            # 适配器: newapi 或 oneapi
    base: https://api.example.com
    proxy: auto                # 代理策略: auto (自动探测直连/代理), none (强制直连), 或指定 http://127.0.0.1:7890
    auth:
      access_token: "your-upstream-system-access-token"
      user_id: 12345           # New API 站点通常需要提供 UID 配合 New-Api-User 鉴权

  # 传统 One API 站点示例
  mysite-oneapi:
    tier: api
    adapter: oneapi
    base: https://oneapi.example.com
    proxy: none
    auth:
      access_token: "your-upstream-system-access-token"

  # 暂无凭据或搁置站点示例
  mysite-parked:
    tier: parked
    base: https://parked.example.com
    parked_reason: "等待在个人设置中生成长期系统访问令牌"
```

---

## 📱 双端 Web 控制台

执行以下命令即可启动适配移动端与桌面端的 Web 控制台：

```bash
relayctl web --port 8088
```

- **桌面端访问**：浏览器打开 `http://localhost:8088/`，享受现代极简玻璃拟态仪表盘，支持实时搜索过滤、一键查看密钥、模型分组及在线创建 Key。
- **移动端访问**：手机浏览器连接同一局域网 `http://<局域网IP>:8088/`，自适应卡片流布局与触控抽屉，随时随地掌握各中转站额度与状态。

---

## 💻 核心 CLI 命令速查

| 命令 | 说明 |
|---|---|
| `relayctl status [--json]` | 列出所有上游站点状态汇总表（余额纯数字、用户分组、Key数、健康度诊断） |
| `relayctl balance <site>` | 查看指定站点的精确额度详情（统一不换算，金额 = quota / quota_per_unit） |
| `relayctl groups <site>` | 查看指定站点的可用模型分组及标定倍率 |
| `relayctl keys <site>` | 查看指定站点的全部 Key 列表（脱敏显示，防截屏泄露） |
| `relayctl key add <site> --name X --group G (--quota N\|--unlimited)` | 在上游新建 Key，强制指定额度数值或无限额度 |
| `relayctl key set <site> <id> [--group G] [--enable\|--disable] [--quota N]` | 修改上游已有 Key 的状态、额度或分组 |
| `relayctl key rm <site> <id> --yes` | 删除上游指定 Key（破坏性操作需显式确认） |
| `relayctl probe <site\|all>` | 自动化探测站点网络连通性、系统版本与鉴权有效性 |
| `relayctl site list` | 列出本地配置文件中所有已登记站点 |
| `relayctl site auth <name>` | 交互式录入长期系统访问令牌（安全无回显，验证通过自动晋级） |
| `relayctl site rm <site> --yes` | 移除本地配置文件中的指定站点 |
| `relayctl web [--port 8088]` | 启动双端自适应 Web 控制台 |

---

## 🛡️ 安全底线与隐私规范

1. **凭据安全第一红线**：
   `sites.yaml` 与运行时缓存必须锁定权限为 `600`，绝对禁止提交进 Git 仓库；所有 Key 输出默认进行前4后4脱敏处理。
2. **防高频机器扫描纪律**：
   严禁短时间内连续、无间隔、高并发地请求上游站点的管理接口（如全量定价 `/api/pricing`、全部分组列表等）；常规非写操作优先复用本地短期只读缓存。
3. **故障排查第一原则**：
   遇到任何渠道报错、Key 失效或 401 鉴权异常时，直接登录对应站点官网控制台核验，严禁在本地代码里写死循环探针暴力撞库。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。欢迎提 Issue 与 PR！
