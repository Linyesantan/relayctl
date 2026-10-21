# 站点接入档案模板（v2 Site Template）

接入新的上游 AI 中转站时，按本模板把站点的「可复制配置」与「档案说明」一起落下来。
配置文件放 `$RELAYCTL_HOME/sites/<name>.yaml`（0600），下面四、五两节里的说明内容
直接写进该文件的 `docs` 四个字段，这样 `relayctl site show` 与 `doctor` 都能看到。

---

## 1. 站点文件

```yaml
name: <name>
base: https://api.example.com
console_url: https://panel.example.com
panel: newapi                    # newapi / oneapi / custom
adapter: newapi                  # newapi / oneapi / script
script: null                     # adapter=script 时填 scripts/<name>.py
auth:
  type: access_token             # access_token / cookie / password / none
  access_token: ''
  access_token_env: <SITE>_ACCESS_TOKEN
  user_id: null                  # New API 的 UID
  username: ''
  password: ''
  password_env: ''
  cookie: ''
docs:
  login_method: ''
  token_howto: ''
  script_howto: ''
  notes: ''
proxy: ''                        # 空=继承全局 / none / auto / http://host:port
refresh_interval: 30m            # 空=继承全局
tags: []
status: active                   # active / parked
parked_reason: ''
```

等价的非交互命令（不要在命令行里传明文令牌）：

```bash
export <SITE>_ACCESS_TOKEN='...'
relayctl site add \
  --name <name> \
  --base https://api.example.com \
  --console-url https://panel.example.com \
  --panel newapi --adapter newapi \
  --auth-type access_token \
  --token-env <SITE>_ACCESS_TOKEN \
  --user-id 12345 \
  --login-method '<登录/进站方式>' \
  --token-howto '<长期令牌获取路径>' \
  --script-howto '' \
  --notes '<排障备注>' \
  --tags <标签> --refresh-interval 30m --status active
```

---

## 2. `docs` 四个字段

| 字段 | CLI 参数 | 写什么 |
|---|---|---|
| `login_method` | `--login-method` | 登录 / 进站方式（控制台账号密码、SSO、邀请码…） |
| `token_howto` | `--token-howto` | 长期令牌获取路径（如「控制台 → 个人设置 → 系统访问令牌」） |
| `script_howto` | `--script-howto` | `adapter=script` 时记录脚本的爬取来源、登录前提与维护注意事项 |
| `notes` | `--notes` | 其它排障备注（风控限制、频率上限、已知坑） |

四个字段必须齐全，不需要内容的写空字符串。**严禁**在 `docs` 里记录完整 Cookie、
Key 或令牌——它们会随 `site show` / 配置导出一起流出。

---

## 3. 鉴权方式

- **New API**：`Authorization: Bearer <系统访问令牌>` + `New-Api-User: <uid>`
- **One API**：`Authorization: Bearer <系统访问令牌>`
- **自研面板**：按 `script` 适配器注入的环境变量取凭据

---

## 4. 脚本适配器（`adapter: script`）

当标准适配器覆盖不到该站点时：

```bash
relayctl script new <name>            # 生成模板并切换 adapter=script
relayctl script test <name>           # 7 个动作逐个体检
relayctl script test <name> --action probe
relayctl script test <name> --json-envelope
```

协议：`<script> <action> [--json-args '<json>']`

- 动作：`probe` / `balance` / `groups` / `list_keys` / `create_key` / `update_key` / `delete_key`
- stdout：恰好一个 JSON 文档；退出码 `0` 成功 / `1` 失败 / `2` 鉴权失败 / `3` 不支持
- 宿主只注入：`RELAYCTL_SITE` / `RELAYCTL_BASE` / `RELAYCTL_TOKEN` / `RELAYCTL_COOKIE` /
  `RELAYCTL_USER_ID` / `RELAYCTL_PROXY`
- 未实现的动作由基类自动以退出码 3 应答，`script test` 记为 `unsupported` 而不是 `failed`

把「从哪里取数据、依赖什么登录前提、多久会失效」写进 `docs.script_howto`。

---

## 5. 站点档案（记在配置之外的地方）

- **用户分组与倍率**：该站可用分组（如 `default` / `pro` / `claude`）与官方标定倍率
- **支持的核心模型**：如 `gpt-4o`、`claude-3-5-sonnet`、`deepseek-chat`
- **排障与注意事项**：
  * 凭据失效或 401 报错时，直接登录站点控制台核验；
  * 严禁高频、无间隔地探测敏感管理端点（如全量定价 `/api/pricing`）；
  * 默认读缓存，只有明确需要时才 `--refresh`。

---

## 6. 接入后复核

```bash
relayctl doctor                     # 权限 / 配置 / 凭据引用 / 脚本 / 缓存
relayctl probe <name> --refresh
relayctl balance <name> --refresh
relayctl groups <name> --refresh
relayctl keys <name> --refresh
relayctl site set <name> --status active
```

验证通过再置为 `status: active`；暂时没有凭据的站点保持 `status: parked`
并在 `parked_reason` 里写明原因。
