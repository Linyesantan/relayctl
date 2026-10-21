---
name: relayctl
description: 安全接入和管理 New API、One API 及自定义中转站，验证余额与 Key，并按需分发。
---

# relayctl

处理站点接入时必须严格按以下七步执行，不得跳步。

## 七步流程

1. **识别面板**
   - 先执行：`relayctl probe-url <地址>`。
   - 只探测公开端点；不要在这一步提交账号、令牌或 Key。

2. **取得凭据**
   - 根据探测到的面板类型告诉用户去哪里取得管理令牌与 UID，或在用户明确授权并提供账号后自行登录获取。
   - **New API**：进入 `个人设置 → 系统访问令牌` 获取长期管理令牌；UID 在个人设置或用户信息中查看。
   - **One API / 自定义面板**：按对应面板的个人设置、令牌管理或管理员页面指引；无法确认时先询问，不猜接口。
   - 优先让用户把令牌放入环境变量，并使用 `--token-env`；不要把令牌写进对话或命令输出。

3. **非交互登记站点并补齐文档**
   - 使用 `relayctl site add --name <名称> --base <API地址> --panel <类型> --adapter <适配器> ...`，禁止依赖交互输入。
   - 必须同时填写登录方式、令牌获取路径和注意事项：
     - `--login-method '<登录方式>'`
     - `--token-howto '<令牌获取路径>'`
     - `--notes '<注意事项>'`
   - 凭据优先使用 `--token-env <环境变量名>`；需要 UID 时加 `--user-id <UID>`。

4. **验证并激活**
   - 依次执行 `relayctl probe <站点> --refresh`、`relayctl balance <站点> --refresh`、`relayctl keys <站点> --refresh`。
   - 三项验证成功后执行 `relayctl site set <站点> --status active`。
   - 鉴权失败先修正凭据；不支持的能力进入第 5 步，不伪造成功结果。

5. **不支持 Key 管理时接入脚本**
   - 依次执行：`relayctl script new <站点>` → 编写生成的脚本 → `relayctl script test <站点>`。
   - 测试通过后确认站点为 `adapter=script`。
   - 必须通过 `--script-howto` 或站点配置的 `docs.script_howto` 记录爬取来源、登录前提和维护注意事项；不得记录完整 Cookie、Key 或令牌。

6. **分类、刷新与分发**
   - 询问用户站点分类 `tags` 与刷新间隔 `refresh_interval`，再用 `relayctl site set <站点> --tags ... --refresh-interval ...` 写入。
   - 用户需要时，先预演再用 `relayctl dist export` 分发到 **CC Switch** 或 **New API**；确认目标站点、应用和筛选标签。

7. **安全红线**
   - 不打印完整 Key、访问令牌、Cookie 或密码，只显示脱敏值。
   - 不把 `$RELAYCTL_HOME`、站点配置、缓存或分发文件提交到 git。
   - 不高频探测；默认读取缓存，只有明确需要时才用 `--refresh`。
   - 破坏性操作必须先执行 `--dry-run`，展示影响范围并取得用户确认后再执行；删除还必须使用显式确认参数。

## 常用命令速查

```bash
relayctl probe-url https://example.com
relayctl site add --name demo --base https://example.com --panel newapi --adapter newapi \
  --token-env DEMO_ACCESS_TOKEN --user-id 123 \
  --login-method '控制台登录' \
  --token-howto '个人设置 → 系统访问令牌' \
  --notes '长期令牌；不要提交配置' --status parked
relayctl probe demo --refresh
relayctl balance demo --refresh
relayctl groups demo --refresh
relayctl keys demo --refresh
relayctl key add demo --name agent --group default --quota 10 --dry-run
relayctl site set demo --status active --tags team,primary --refresh-interval 30m
relayctl script new demo
relayctl script test demo --json-envelope
relayctl import sites sites.yaml --dry-run --json
relayctl import keys keys.txt --dry-run --json
relayctl dist list --json
relayctl dist export --target ccswitch --app all --tag team --dry-run
relayctl dist export --target newapi --site target-site --tag team --dry-run
relayctl doctor --json
relayctl schema --kind all --json
```

所有普通命令均可使用 `--json` 获取 `relayctl/v2` 信封；旧自动化需要原始 `data` 时使用 `--json-raw`。`script test` 为兼容既有严格 JSON 契约，迁移到信封时使用 `--json-envelope`。

## 退出码

| 退出码 | 含义 | 处理方式 |
|---:|---|---|
| 0 | 成功 | 继续下一步 |
| 1 | 一般失败 | 查看脱敏错误，修正后重试 |
| 2 | 鉴权失败 | 更新令牌、UID、Cookie 或账号凭据 |
| 3 | 当前面板或适配器不支持 | 使用脚本适配或调整流程 |
| 4 | 参数或配置错误 | 修正命令参数、YAML、权限或 Web 配置 |

在 JSON 信封中，失败时 `error.code` 必须与进程退出码相同。
