# relayctl

多上游 AI 中转站 Key 与余额的统一管理工具（CLI + Web + MCP）。

本文档全部命令均对应 `bin/relayctl` 的真实子命令；仓库内直接运行时把 `relayctl` 换成
`python bin/relayctl` 即可。示例站点名一律用 `demo` 等占位，地址一律用 `example.com`，
不含任何真实站点、路径或凭据。

- [产品功能](#1-产品功能)
- [目录结构](#2-目录结构)
- [站点文件完整示例](#3-站点文件完整示例)
- [安装与快速开始](#4-安装与快速开始)
- [批量导入](#5-批量导入import)
- [缓存与自动刷新](#6-缓存与自动刷新)
- [站点与 Key 管理](#7-站点与-key-管理)
- [脚本协议](#8-脚本协议)
- [分发与导出](#9-分发与导出dist)
- [JSON 契约与退出码](#10-json-契约与退出码)
- [MCP](#11-mcp)
- [Web 面板](#12-web-面板)
- [AI Skill 接入](#13-ai-skill-接入)
- [从 v1 迁移](#14-从-v1-迁移)
- [安全说明](#15-安全说明)
- [开发与测试](#16-开发与测试)

---

## 1. 产品功能

**看得见**

| 能力 | 命令 | 说明 |
|---|---|---|
| 站点状态总表 | `relayctl status` | 所有站点的余额、用户分组、Key 数、状态诊断汇总 |
| 余额详情 | `relayctl balance <site>` | 单站额度 / 已用 / 配额底数 / 站点标注 |
| 分组倍率 | `relayctl groups <site>` | 单站可用模型分组与标定倍率 |
| Key 列表 | `relayctl keys <site>` | 单站 Key 列表（默认脱敏） |
| 健康度探测 | `relayctl probe <site\|all>` | 识别面板类型、版本、鉴权有效性 |
| 公开端点识别 | `relayctl probe-url <地址>` | 只探测公开端点，不鉴权、不请求定价 |
| 配置体检 | `relayctl doctor` | 目录权限、配置解析、凭据引用、脚本、缓存、Web 配置检查 |

**管得住**

| 能力 | 命令 | 说明 |
|---|---|---|
| 站点登记 | `relayctl site add/set/rm/show/list/auth` | 非交互登记，含 `docs` 四字段与标签、刷新间隔 |
| Key 生命周期 | `relayctl key add/set/rm` | 在上游新建 / 改额 / 启禁 / 删除 Key，支持 `--dry-run` |
| 自定义面板 | `relayctl script new/test` | 脚本适配器，7 个动作逐个体检 |
| 批量导入 | `relayctl import sites/keys` | txt / csv / json / yaml（keys 另含 ccswitch / newapi） |
| 分发与导出 | `relayctl dist list/tag/assign/rm/export` | 分发库登记 + CC Switch / New API / env / json / csv 导出 |

**自动化**

| 能力 | 命令 | 说明 |
|---|---|---|
| 只读缓存 | `cache.*` + `--cached` / `--refresh` | 默认读缓存，避免高频打上游管理端点 |
| 后台刷新 | `relayctl daemon` + `docs/relayctl.service` | systemd user 常驻定时刷新，含抖动与失败退避 |
| 机器契约 | `--json` / `--json-raw` / `--json-envelope` | 稳定的 `relayctl/v2` 信封 |
| 协议描述 | `relayctl schema --kind site\|script\|dist\|all` | JSON Schema（draft 2020-12） |
| MCP 服务 | `relayctl mcp` | 零依赖 stdio JSON-RPC，供 AI 客户端调用 |
| Web 控制台 | `relayctl web` | 零前端构建的移动端 / 桌面端面板 |
| AI Skill | `skill/relayctl/SKILL.md` | 七步接入流程，供 AI 客户端加载 |

---

## 2. 目录结构

### 仓库结构

```
relayctl/
├── bin/relayctl              # CLI 入口（可直接 python bin/relayctl ...）
├── relayctl/                 # 核心库
│   ├── adapters/             # newapi / oneapi / script 适配器
│   ├── cache.py              # 缓存读写、时效判定、文本脱敏
│   ├── config.py             # v2 配置层（全局 config.yaml + 每站文件）
│   ├── distribution.py       # 分发库登记与导出调度
│   ├── doctor.py             # 体检项
│   ├── errors.py             # 异常类型与退出码
│   ├── exporters.py          # CC Switch / New API / env / json / csv
│   ├── importers.py          # 多格式导入与指纹去重
│   ├── mcp.py                # stdio JSON-RPC MCP 服务
│   ├── migrate.py            # v1 sites.yaml → v2 sites/<name>.yaml
│   ├── models.py             # 规范化与脱敏
│   ├── paths.py              # 运行时布局、原子写、文件锁
│   ├── probe.py              # 公开端点探测
│   ├── scheduler.py          # 后台刷新调度
│   ├── schema.py             # 配置与协议 JSON Schema
│   ├── script_adapter.py     # script 适配器与脚本模板
│   ├── script_base.py        # 脚本协议基类（零依赖，可复制进脚本）
│   └── web.py                # Web 控制台
├── docs/
│   ├── relayctl.service      # systemd user 单元示例
│   └── site_template.md      # v2 站点档案模板（含 docs 与 script）
├── skill/relayctl/SKILL.md   # AI Skill 接入说明
├── sites.example.yaml        # v2 站点文件示例（含 docs 四字段）
├── tests/                    # pytest 用例
└── requirements.txt
```

### 运行时布局（`$RELAYCTL_HOME`，默认 `~/.config/relayctl`）

```
$RELAYCTL_HOME/
├── config.yaml            # 全局配置（0600）
├── sites/<name>.yaml      # 每站一个文件，隔离加载（0600）
├── scripts/<name>.py      # 站点脚本（0750）
├── cache/<name>.json      # 每站缓存
├── dist/keys.yaml         # 分发 Key 库（0600）
└── logs/                  # 日志
```

目录一律 0700，文件一律 0600，写入全部走「临时文件 → chmod → `os.replace`」原子写。
用 `RELAYCTL_HOME` 可整体重定向（测试与多环境隔离都靠它）。

---

## 3. 站点文件完整示例

一站点一文件，路径为 `$RELAYCTL_HOME/sites/<name>.yaml`。下面就是
`site add` 写出的完整形态，也是 [`sites.example.yaml`](sites.example.yaml) 的内容：

```yaml
name: demo                              # 站点名，同时是文件名
base: https://api.example.com           # API 地址，必须是 http(s)://
console_url: https://panel.example.com  # 控制台地址（可选）
panel: newapi                           # 面板类型: newapi / oneapi / custom
adapter: newapi                         # 适配器: newapi / oneapi / script
script: null                            # adapter=script 时的脚本路径
auth:
  type: access_token                    # access_token / cookie / password / none
  access_token: ''                      # 明文令牌：留空，改用 access_token_env
  access_token_env: DEMO_ACCESS_TOKEN   # 从环境变量读令牌（明文不落盘）
  user_id: 12345                        # New API 的 UID（可为 null）
  username: ''
  password: ''
  password_env: ''
  cookie: ''
docs:                                   # 四字段必须齐全，允许空字符串
  login_method: 控制台账号密码登录
  token_howto: 控制台 → 个人设置 → 系统访问令牌
  script_howto: ''
  notes: 长期令牌，不要提交到 git
proxy: ''                               # 空=继承全局；none / auto / http://host:port
refresh_interval: 30m                   # 空=继承全局；支持 30s / 30m / 2h / 1d
tags:
  - team
  - primary
status: active                          # active / parked
parked_reason: ''
```

`adapter: script` 时额外给出的形态：

```yaml
adapter: script
script: scripts/demo.py
docs:
  login_method: 控制台账号密码登录
  token_howto: 控制台 → 个人设置 → 系统访问令牌
  script_howto: 脚本由 relayctl 以 <script> <action> 调用；凭据只从环境变量读取
  notes: 自研面板，仅支持余额查询
```

### 全局配置 `$RELAYCTL_HOME/config.yaml`

```yaml
version: 2
proxy: none                 # none / auto / http://host:port
refresh_interval: 30m
cache:
  enabled: true
  mode: auto                # auto: 有缓存就用 / refresh: 强制实时 / cached: 只读缓存
  ttl: 30m
  serve_stale: true         # 实时失败且有旧缓存时返回旧数据并标记 stale
script:
  timeout: 15               # 脚本适配器单次调用超时（秒）
scheduler:
  max_workers: 4
  jitter: 0.1               # 成功间隔 ±10% 抖动
  backoff_initial: 60       # 失败退避初值（秒）
  backoff_factor: 2
  backoff_max: 1800
web:
  host: 127.0.0.1
  port: 8088
  access_password: ''       # 空 = 不启用 Web 访问口令
  scheduler_enabled: true
```

### 分发库示例 `$RELAYCTL_HOME/dist/keys.yaml`

由 `import keys` 与 `dist tag/assign` 维护，不要手改：

```yaml
version: 1
keys:
  - id: a1b2c3d4e5f60718
    key: sk-*******************
    source_site: demo
    base: https://api.example.com
    tags:
      - team
    models: []
    groups: []
    assigned_to: ''
    notes: 导入自清单
    status: active
```

---

## 4. 安装与快速开始

```bash
git clone https://github.com/Linyesantan/relayctl.git
cd relayctl
pip install -r requirements.txt
```

全局调用（二选一）：

```bash
ln -sf "$(pwd)/bin/relayctl" ~/.local/bin/relayctl
# 或不建软链，直接在仓库内用
python bin/relayctl --help
```

### 第 1 步：识别面板

```bash
relayctl probe-url https://api.example.com
```

只探测公开端点，不鉴权、不请求定价。

### 第 2 步：登记站点

令牌放进环境变量，用 `--token-env` 引用（明文不落盘）：

```bash
export DEMO_ACCESS_TOKEN='...'

relayctl site add \
  --name demo \
  --base https://api.example.com \
  --console-url https://panel.example.com \
  --panel newapi \
  --adapter newapi \
  --auth-type access_token \
  --token-env DEMO_ACCESS_TOKEN \
  --user-id 12345 \
  --login-method '控制台账号密码登录' \
  --token-howto '控制台 → 个人设置 → 系统访问令牌' \
  --notes '长期令牌，不要提交到 git' \
  --tags team,primary \
  --refresh-interval 30m \
  --status active
```

交互录入令牌（无回显）用 `relayctl site auth demo`。
凭据还没到手时先登记档案：`--status parked --parked-reason '等待生成长期令牌'`，
拿到凭据验证通过后再 `relayctl site set demo --status active`。

### 第 3 步：验证

```bash
relayctl probe demo --refresh
relayctl balance demo --refresh
relayctl groups demo --refresh
relayctl keys demo --refresh
```

### 第 4 步：日常使用

```bash
relayctl status                 # 全站汇总
relayctl status --cached        # 只读缓存，零网络请求
relayctl doctor                 # 体检（默认绝不联网）
relayctl doctor --check-connectivity   # 显式联网检查
```

---

## 5. 批量导入（`import`）

两个子命令：`import sites`（登记站点）与 `import keys`（写入分发库）。
来源可以是文件路径，也可以是 `-`（标准输入）。

### 通用参数

| 参数 | 说明 |
|---|---|
| `<file>` | 导入文件路径，`-` 表示标准输入 |
| `--dry-run` | 只探测与预览，**绝不写盘** |
| `--format` | `import sites`: `auto/txt/csv/json/yaml`；`import keys`: 另含 `ccswitch/newapi` |
| `--json` | 输出 JSON（站点导入绝不含明文令牌；Key 导入只含脱敏值） |

### `import sites`

```bash
# txt：每行 `名称 地址 令牌 [UID]`，# 开头为注释
printf 'demo https://api.example.com <ACCESS_TOKEN> 12345\n' > sites.txt
relayctl import sites sites.txt --dry-run --json
relayctl import sites sites.txt --json

# csv：有表头按字段名匹配，无表头按 名称,地址,令牌,UID 顺序
relayctl import sites sites.csv --format csv --dry-run

# json / yaml：支持 {sites: {name: {...}}} 或 [{...}] 等容器
relayctl import sites sites.yaml --format yaml --dry-run

# 标准输入
cat sites.txt | relayctl import sites - --dry-run
```

站点导入会先 `probe-url` 再用候选鉴权 `probe`：两者都成功 → `status: active`，
否则 `status: parked` 并写入 `parked_reason`。单条失败不影响其它站。

字段名支持中英文别名，例如 `名称/站点名`、`地址/网址`、`令牌/密钥`、`用户ID`。

### `import keys`

```bash
# txt：一行一个 sk- 开头的 Key，后面可跟 站点 标签 备注
printf 'sk-EXAMPLE00 demo team 导入自清单\n' > keys.txt
relayctl import keys keys.txt --dry-run --json
relayctl import keys keys.txt --json

# csv / json / yaml
relayctl import keys keys.csv --format csv --dry-run

# 直接吃 CC Switch 导出或 New API 渠道导出
relayctl import keys ccswitch.json --format ccswitch --dry-run
relayctl import keys channels.json --format newapi --dry-run

# 标准输入
cat keys.txt | relayctl import keys - --dry-run
```

Key 按 `sha256` 前 16 位取指纹作为 `id` 去重，`base` 规范化后自动匹配已登记站点。

---

## 6. 缓存与自动刷新

### 读取口径

| 场景 | 用法 |
|---|---|
| 默认 | `relayctl status`：有缓存直接用，不发网络请求 |
| 强制实时 | `relayctl status --refresh` |
| 只读缓存 | `relayctl status --cached`：任何情况下零网络请求 |

对应 `config.yaml` 的 `cache.mode`（`auto` / `refresh` / `cached`）。
实时拉取失败且存在旧缓存时返回旧数据并标记 `stale`（`cache.serve_stale`）。

### 后台刷新 daemon

```bash
relayctl daemon            # 常驻：按各站 refresh_interval 定时刷新
relayctl daemon --once     # 只刷新当前到期的站点后退出（适合放进 cron）
```

刷新间隔：站点级 `refresh_interval` 覆盖全局；成功后加 ±`jitter` 抖动，
失败按 `backoff_initial × backoff_factor^n` 退避至 `backoff_max`。
每站同一时刻最多 1 个刷新任务，全局并发由 `scheduler.max_workers` 控制。

### systemd（user 单元）

仓库内提供单元文件 [`docs/relayctl.service`](docs/relayctl.service)，
它用 `%h/.local/bin/relayctl` 与 `RELAYCTL_HOME=%h/.config/relayctl`：

```bash
mkdir -p ~/.config/systemd/user
cp docs/relayctl.service ~/.config/systemd/user/relayctl.service
systemctl --user daemon-reload
systemctl --user enable --now relayctl
systemctl --user status relayctl
journalctl --user -u relayctl -f
systemctl --user stop relayctl
systemctl --user disable relayctl
```

退出登录仍要保持常驻时，先开启 lingering：

```bash
loginctl enable-linger "$USER"
```

单元里的 `UMask=0077` 保证 `RELAYCTL_HOME` 下的凭据 / 缓存 / 日志保持 0600/0700。

---

## 7. 站点与 Key 管理

### 站点

```bash
relayctl site list                       # 列出所有站点
relayctl site show demo                  # 单站配置（凭据脱敏）
relayctl site show demo --json           # JSON 输出

relayctl site set demo --status active --tags team,primary --refresh-interval 30m
relayctl site set demo --proxy none
relayctl site set demo --set proxy=none  # 直接改顶层字段
relayctl site set demo --drop-script     # 清除脚本配置

relayctl site rm demo --yes              # 删除站点配置（破坏性，需 --yes）

relayctl site auth demo                  # 交互式录入长期令牌（无回显）
```

站点级 `proxy` 与 `refresh_interval` 留空即继承全局。

### Key

```bash
# 新建（先预演再执行）
relayctl key add demo --name agent --group default --quota 10 --dry-run
relayctl key add demo --name agent --group default --quota 10
relayctl key add demo --name agent --group default --unlimited --models gpt-4o,claude-3-5-sonnet

# 修改
relayctl key set demo 123 --group pro --dry-run
relayctl key set demo 123 --enable
relayctl key set demo 123 --disable
relayctl key set demo 123 --quota 20
relayctl key set demo 123 --unlimited

# 删除（破坏性，需 --yes）
relayctl key rm demo 123 --yes
```

`status: parked` 的站点不参与任何上游操作，只保留档案。

---

## 8. 脚本协议

标准适配器（newapi / oneapi）覆盖不到的自研面板，用脚本适配。

### 生成与验证

```bash
relayctl script new demo                  # 生成模板并把站点切到 adapter=script
relayctl script new demo --force          # 覆盖已有脚本
relayctl script test demo                 # 7 个动作逐个体检
relayctl script test demo --action probe  # 只测一个动作
relayctl script test demo --json-envelope # 严格 JSON 信封输出
```

`script test` 把「未实现」记为 `unsupported`（退出码 3）而不是 `failed`，
所以模板刚生成时全部 `unsupported` 属于正常。

### 协议

```
<script> <action> [--json-args '<json>']
```

- `action`：`probe` / `balance` / `groups` / `list_keys` / `create_key` / `update_key` / `delete_key`
- `stdout`：恰好一个 JSON 文档，多余输出视为协议违规
- 退出码：`0` 成功 / `1` 一般失败 / `2` 鉴权失败 / `3` 不支持该动作

宿主只注入这些环境变量，脚本拿不到其它站点配置：

```
RELAYCTL_SITE  RELAYCTL_BASE  RELAYCTL_TOKEN
RELAYCTL_COOKIE  RELAYCTL_USER_ID  RELAYCTL_PROXY
```

### 可直接复制的模板要点

```python
#!/usr/bin/env python3
"""站点脚本：继承基类后只实现自己支持的动作。"""
from relayctl.script_base import ScriptAdapterBase, run_main


class DemoScript(ScriptAdapterBase):
    # 凭据只从注入的环境变量读，绝不写死在脚本里
    def probe(self):
        return {
            "site": self.site,
            "type": "custom",
            "system_name": "自研面板",
            "version": "",
            "auth_ok": bool(self.token),
            "quota_per_unit": 500000,
            "quota_display_type": "USD",
        }

    def balance(self):
        return {"balance": "0.0000", "used": "0.0000", "raw_quota": 0,
                "raw_used": 0, "quota_per_unit": 500000,
                "display_type": "USD", "user_group": "default"}

    def groups(self):
        return [{"name": "default", "desc": "默认分组", "ratio": 1.0}]

    def list_keys(self):
        return [{"id": 1, "name": "k1", "group": "default", "status": 1,
                 "unlimited": False, "remain": 1.0, "used": 0.0,
                 "key": "sk-...", "models": []}]

    # create_key / update_key / delete_key 不实现即可：
    # 基类会自动以退出码 3 应答，script test 记为 unsupported。


if __name__ == "__main__":
    run_main(DemoScript())
```

脚本文件放在 `$RELAYCTL_HOME/scripts/<name>.py`，权限 0750。
接口细节写在站点的 `docs.script_howto` 里（用 `--script-howto` 登记），
不要写完整 Cookie、Key 或令牌。

---

## 9. 分发与导出（`dist`）

分发库只做「登记 + 打标 + 归属 + 单向导出」：不做负载均衡 / 计费 / 用户系统，
也不与上游做自动双向同步。`dist rm` 只删本地登记，不影响上游真实 Key。

### 登记

```bash
relayctl dist list                    # 默认只显示 id 与脱敏 Key
relayctl dist list --tag team
relayctl dist list --site demo

relayctl dist tag a1b2c3d4e5f60718 --add team,prod --dry-run
relayctl dist tag a1b2c3d4e5f60718 --add team
relayctl dist tag a1b2c3d4e5f60718 --rm prod

relayctl dist assign a1b2c3d4e5f60718 --to '同事A' --note '用于代码助手' --dry-run
relayctl dist assign a1b2c3d4e5f60718 --to '同事A'

relayctl dist rm a1b2c3d4e5f60718 --yes --dry-run
relayctl dist rm a1b2c3d4e5f60718 --yes
```

`id` 可以只写唯一前缀；前缀有歧义时报错，绝不猜测。

### 导出

```bash
# CC Switch：生成 claude / codex 的一键导入配置
relayctl dist export --target ccswitch --app claude --tag team --dry-run
relayctl dist export --target ccswitch --app all --out ./ccswitch.json

# New API：把 Key 作为渠道推送到已登记站点（默认试运行，需显式 --yes）
relayctl dist export --target newapi --site demo --tag team --dry-run
relayctl dist export --target newapi --site demo --channel-type 1 --yes

# 通用
relayctl dist export --target env --out ./keys.env
relayctl dist export --target json --dry-run
relayctl dist export --target csv --out ./keys.csv
```

导出相关参数：

| 参数 | 说明 |
|---|---|
| `--target` | `ccswitch` / `newapi` / `env` / `json` / `csv`（必填） |
| `--tag` | 只导出带该标签的 Key |
| `--site` | `newapi`：推送到哪个已登记站点；其它目标：按来源站点过滤 |
| `--app` | `ccswitch` 的应用：`claude` / `codex` / `all`（默认 `all`） |
| `--channel-type` | New API 渠道 type：`1` openai / `14` anthropic / `24` gemini / `57` codex |
| `--out` | 导出文件路径，写出即 0600；**绝不写 stdout** |
| `--dry-run` | 只预览，绝不写盘、绝不写上游 |
| `--yes` | `newapi` 实际写入的确认（默认试运行） |
| `--reveal` | 显式要求把明文 Key 打到 stdout（默认只显示脱敏） |

`--out` 写出的文件默认含明文且权限 0600；不加 `--out` 时 stdout 只有脱敏值。

---

## 10. JSON 契约与退出码

### 三个开关

| 开关 | 输出 |
|---|---|
| `--json` | `relayctl/v2` 信封（少数历史命令额外保留只读顶层别名，新调用方应只读 `data`） |
| `--json-envelope` | 强制输出 `relayctl/v2` 信封（覆盖严格旧 JSON 兼容） |
| `--json-raw` | 只输出 `data`，不含信封 |

除 `mcp` 外所有命令都支持这三个开关；`mcp` 的 stdout 专用于 JSON-RPC 帧。

### 信封

成功：

```json
{
  "schema": "relayctl/v2",
  "ok": true,
  "command": "dist list",
  "data": {
    "count": 1,
    "items": [
      {
        "id": "a1b2c3d4e5f60718",
        "key_masked": "***",
        "source_site": "demo",
        "base": "https://api.example.com",
        "tags": ["team"],
        "models": [],
        "groups": [],
        "assigned_to": "",
        "notes": "导入自清单",
        "status": "active"
      }
    ]
  },
  "meta": {"exit_code": 0, "version": "2.0.0"},
  "warnings": [],
  "error": null
}
```

失败：

```json
{
  "schema": "relayctl/v2",
  "ok": false,
  "command": "balance",
  "data": null,
  "meta": {"exit_code": 2, "version": "2.0.0"},
  "warnings": [],
  "error": {"code": 2, "message": "鉴权失败: ..."}
}
```

契约要点：

- 始终存在 7 个保留字段：`schema` / `ok` / `command` / `data` / `meta` / `warnings` / `error`。
- `data` 是规范契约；少数历史命令（`status`、`site list`、`site show`、`script test`、
  `probe-url`、`import sites`、`import keys`、`dist list`、`dist tag`、`dist assign`、
  `dist rm`、`dist export`、`daemon`）额外保留同名顶层只读别名，新代码只读 `data`。
- `meta.exit_code` 与 `error.code` 都等于进程退出码；`ok == (exit_code == 0)`。
- 任何输出都经过脱敏：明文凭据字段被剔除，`sk-` 等凭据片段被替换为 `***`。

### 退出码

| 退出码 | 含义 | 常见来源 |
|---:|---|---|
| 0 | 成功 | — |
| 1 | 一般失败 | 上游不可达、脚本异常、被中断 |
| 2 | 鉴权失败 | HTTP 401、`AUTH_*`、令牌 / UID / Cookie 失效 |
| 3 | 站点或适配器不支持该操作 | 脚本未实现该动作、适配器未实现该能力 |
| 4 | 参数或配置问题 | 参数缺失、YAML 非法、权限不符、`doctor` 存在失败项 |

脚本侧沿用同一套退出码，但只产出 0/1/2/3（未实现的动作一律 3）；
4 只由宿主在参数 / 配置错误时产生。

---

## 11. MCP

`relayctl mcp` 是零依赖的 stdio JSON-RPC 服务，每行一个帧：

```bash
relayctl mcp                        # 只注册只读与非破坏性工具
relayctl mcp --allow-destructive    # 额外注册 key_rm / site_rm
relayctl mcp --allow-reveal         # 额外注册 dist_export_reveal
```

默认注册的工具：`status`、`balance`、`groups`、`keys`、`key_add`、`import_sites`、
`import_keys`、`dist_export`、`site_add`、`doctor`。

破坏性工具（`--allow-destructive`）要求入参带 `confirm: true`：
`key_rm`（删上游 Key）、`site_rm`（删本地站点配置）。

客户端配置示例（把占位路径换成你机器上的实际位置）：

```json
{
  "mcpServers": {
    "relayctl": {
      "command": "<仓库绝对路径>/bin/relayctl",
      "args": ["mcp"],
      "env": {"RELAYCTL_HOME": "<你的运行时目录>"}
    }
  }
}
```

MCP 服务永远只返回脱敏结构，错误文本也走统一凭据遮盖。

---

## 12. Web 面板

```bash
relayctl web                        # 默认 127.0.0.1:8088
relayctl web --host 127.0.0.1 --port 8088
```

- 零前端构建：单文件内嵌页面，Python 原生驱动，不需要 Node.js。
- 移动端 / 桌面端自适应：卡片流 + 触控抽屉 / 宽屏看板，支持即时搜索过滤。
- 并发刷新：`ThreadPoolExecutor` 并发拉取各站状态，页面内可起后台刷新调度线程。
- 访问口令：`config.yaml` 的 `web.access_password` 非空时启用登录页；
  口令与会话校验都用 `hmac.compare_digest` 常量时间比较，会话 Cookie 为随机令牌且
  `HttpOnly; SameSite=Strict`，写操作另校验 CSRF。
- 默认只绑 `127.0.0.1`；`doctor` 会检查 Web host 是否为本地回环地址。
- 页面与接口一律只返回脱敏 Key。

---

## 13. AI Skill 接入

仓库内提供 [`skill/relayctl/SKILL.md`](skill/relayctl/SKILL.md)，供 AI 客户端按 Skill 机制加载。
把整个 `skill/relayctl/` 目录复制到客户端的 skills 目录即可：

```bash
cp -r skill/relayctl <你的客户端 skills 目录>/relayctl
```

它约束 AI 按七步接入，不得跳步：

1. `relayctl probe-url <地址>` 识别面板（只探测公开端点，不提交凭据）
2. 取得凭据，优先 `--token-env`，不把令牌写进对话
3. `relayctl site add` 非交互登记，同时写全 `--login-method` / `--token-howto` / `--notes`
4. `probe --refresh` → `balance --refresh` → `keys --refresh` 验证后再 `--status active`
5. 不支持 Key 管理时走 `script new` → 编写 → `script test`
6. 询问 `tags` 与 `refresh_interval` 后写入；分发先 `--dry-run`
7. 安全红线：不打印明文、不提交 `$RELAYCTL_HOME`、不高频探测、破坏性操作先预演

---

## 14. 从 v1 迁移

v1 把所有站点写在单个 `$RELAYCTL_HOME/sites.yaml` 里；v2 拆成 `sites/<name>.yaml`。

```bash
relayctl migrate --dry-run     # 只预览不写盘
relayctl migrate               # 拆分写出
```

迁移要点：

- 旧的 `tier: api|parked` 映射为 `status: active|parked`；为兼容旧适配器，站内仍保留 `tier` 镜像字段。
- 旧 `auth.type` 的 `bearer` / `apikey` 归一化为 `access_token`。
- 缺失的 `docs` 四字段会补为空字符串；读取旧站时会在 `warnings` 里提示。
- 迁移只做拆分与补全，不会把明文令牌写进新文件。

迁移后用 `relayctl doctor` 与 `relayctl site list` 复核。

---

## 15. 安全说明

**凭据**

- 令牌优先用 `--token-env <环境变量名>` / `auth.access_token_env`，明文不落盘；
  由环境变量解析出的明文在写盘前会被清除。
- `site add/set` 的 `--token` / `--password` / `--cookie` 是明文入参，能用环境变量就别用。
- 密钥一律前 4 后 4 脱敏（`sk-abcd...6789`）；JSON 输出会进一步把 `sk-` 片段替换为 `***`。
- `dist export` 默认不输出明文；只有显式 `--reveal` 或写出到 0600 文件时才含明文。
- 错误信息、日志、stderr 都经过凭据遮盖。

**文件权限**

- 运行时目录 0700、文件 0600；`doctor` 会逐项检查并报告。
- 全部写入走原子写（临时文件 → chmod → `os.replace`），读改写用 flock 保护。

**别提交**

`config.yaml`、`sites/`、`cache/`、`dist/`、`logs/` 都在 `.gitignore` 内，
提交前请用 `git status` 复核。分发导出文件含凭据，不要进仓库、不要外传。

**别打上游**

- 默认读缓存，只有明确需要实时数据时才 `--refresh`。
- 严禁高频、无间隔地请求上游管理端点（如全量定价）；后台刷新自带抖动与失败退避。
- 报错先去站点控制台核验，不要在脚本里写死循环探针。

**别猜**

- 站点名只接受字母、数字、点、下划线、连字符，且以字母或数字开头，拒绝路径穿越。
- 分发 Key 的 `id` 前缀有歧义时直接报错。
- 适配器或脚本未实现的能力报「不支持」（退出码 3），不伪造成功结果。

---

## 16. 开发与测试

```bash
# 全量测试
python -m pytest -q

# 单个文件 / 单个用例
python -m pytest tests/test_cli_smoke.py -q
python -m pytest tests/test_script_protocol.py -q -k <关键字>

# 语法编译检查
python3 -m compileall -q bin relayctl tests

# 命令自查
python bin/relayctl --help
python bin/relayctl <子命令> --help

# 协议与契约
python bin/relayctl schema --kind all
python bin/relayctl doctor

# 隔离环境冒烟（不碰真实 ~/.config/relayctl）
RELAYCTL_HOME=/tmp/relayctl-smoke python bin/relayctl doctor
```

运行时依赖只有 `requests` 与 `PyYAML`（见 `requirements.txt`）；
Web 面板、MCP 服务与脚本基类均为零额外依赖（只用标准库）。

---

## 致谢与社区

感谢 [LINUX DO](https://linux.do/) 社区提供开源推广与技术交流平台。

