# relayctl

<p align="center">
  <b>上游 AI 中转站统一管理与本地私有网关（New API）双向同步控制器</b><br>
  适配桌面与移动端的轻量化中转管理工具（CLI + Web Dashboard）
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Termux-orange.svg" alt="Platform">
</p>

---

## 🌟 核心特性

- 📱 **双端自适应 Web 控制台**：单文件轻量 Web 面板（零前端重型依赖构建），手机端采用触控卡片流与底部抽屉交互，桌面端呈现宽屏数据仪表盘，随时随地查余额、查分组与管理 Key。
- 🔑 **多中转上游统一编排**：统一纳管几十家中转站的系统级长期访问令牌（System Access Token），摆脱网页繁琐登录。
- 🔄 **本地网关（New API）双向联动**：上游动态开 Key、选分组，一键通过事务回滚管道同步回写至本地私有网关（New API 3001）渠道，自动化完成进货与装配。
- 🛡️ **生产级事务与安全回滚**：更新渠道前自动进行数据库只读一致性快照，若上游 Key 连通性测试未通过，秒级自动回滚至历史有效 Key，绝不破坏本地渠道数据库。
- 🏷️ **多适配器插件化架构**：内置标准 `newapi`、`oneapi` 及自研面板适配器（支持 `api`、`script`、`parked` 三级分层熔断）。
- 💰 **严格纯数字不换算纪律**：统一按站长底层 `quota / quota_per_unit` 计算纯数字额度，杜绝汇率假算与虚假币种拼接。
- ⚡ **防高频机器扫描纪律**：内置智能缓存与按需请求机制，杜绝暴力扫描上游敏感管理端点（如全量定价），防止被上游站长误判为爬虫。

---

## 🏗️ 架构拓扑

```
[上游各中转站] (各大 New API / One API / 自研中转面板)
       ▲                                 ▲
       │ (1. 管理接口: 查余额/分组/开Key)   │ (3. 实际模型推理消费)
       ▼                                 │
[relayctl 控制台 (CLI / Web)]             │
       │                                 │
       ▼ (2. 事务回滚同步: Key & 渠道映射)   │
[本地 New API 网关] (127.0.0.1:3001) ─────┘
       ▲
       │ (4. 本地聚合分流: 自动重试 & 容灾调度)
[客户端 / Agent] (Codex, Claude Code, Pi Agent, SillyTavern 等)
```

---

## 🚀 快速上手

### 1. 克隆与安装依赖

```bash
git clone https://github.com/Linyesantan/relayctl.git
cd relayctl
pip install -r requirements.txt
```

建议创建软链接到系统路径，方便全局调用：
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

编辑 `~/.config/relayctl/sites.yaml`，填入你的本地 New API 管理令牌与各上游中转站的系统访问令牌：
```yaml
local:
  base: http://127.0.0.1:3001
  access_token: "your-local-system-access-token"
  user_id: 1

sites:
  mysite:
    tier: api
    adapter: newapi
    base: https://api.example.com
    proxy: auto
    auth:
      access_token: "upstream-system-access-token"
      user_id: 12345
    local_channel: 1
```

---

## 📱 双端 Web 控制台

执行以下命令即可启动适配移动端与桌面端的 Web 控制台：

```bash
relayctl web --port 8088
```

- **桌面端访问**：浏览器打开 `http://localhost:8088/`，享受现代极简玻璃拟态仪表盘，支持实时搜索过滤、一键查看密钥、模型分组及在线创建 Key。
- **移动端访问**：手机浏览器连接同一局域网 `http://<电脑IP>:8088/`，自适应卡片流布局与触控抽屉，随时随地掌握各中转站额度与状态。

---

## 💻 核心 CLI 命令速查

| 命令 | 说明 |
|---|---|
| `relayctl status [--json]` | 列出所有上游站点状态汇总表（余额纯数字、用户分组、Key数、健康度诊断） |
| `relayctl balance <site>` | 查看指定站点的精确额度详情（统一不换算，金额 = quota / quota_per_unit） |
| `relayctl groups <site>` | 查看指定站点的可用模型分组及标定倍率 |
| `relayctl keys <site>` | 查看指定站点的全部 Key 列表（脱敏显示，防截屏泄露） |
| `relayctl key add <site> --name X --group G (--quota N\|--unlimited) [--sync]` | 在上游新建 Key，可选直接同步至本地网关对应渠道 |
| `relayctl key set <site> <id> [--group G] [--enable\|--disable] [--quota N]` | 修改上游已有 Key 的状态、额度或分组 |
| `relayctl key rm <site> <id> --yes` | 删除上游指定 Key（破坏性操作需显式确认） |
| `relayctl sync <site> --key-id N [--channel M] [--test-model M]` | 将上游 Key 同步至本地 New API 对应渠道（自动执行备份与失败回滚） |
| `relayctl probe <site\|all>` | 自动化探测站点网络连通性、系统版本与鉴权有效性 |
| `relayctl site list` | 列出本地配置文件中所有已纳管站点及其本地渠道映射 |
| `relayctl site add <name> --base <url>` | 交互式添加并自动探测新上游站点系统架构 |
| `relayctl site auth <name>` | 交互式录入长期系统访问令牌（安全无回显） |
| `relayctl web [--port 8088]` | 启动双端自适应 Web 控制台 |

---

## 🛡️ 安全底线与最佳实践

1. **凭据安全第一红线**：
   `sites.yaml` 与运行时缓存必须锁定权限为 `600`，绝不包含在 Git 仓库中；Key 输出默认进行脱敏处理。
2. **防高频机器扫描纪律**：
   严禁短时间内连续密集调用上游敏感管理端点（如全量定价 `/api/pricing`）；所有非写操作优先复用本地短期只读缓存。
3. **本地路由隔离与防意外扣费**：
   本地私有网关建议按 `gemini`、`claude`、`deepseek`、`gpt` 进行分类隔离，严禁无限制跨中转自动故障转移，防止冷门高倍率渠道产生不可预期的超额扣费。

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。欢迎提 Issue 与 PR！
