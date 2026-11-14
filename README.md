# 闲鱼虚拟资料自动化

本地优先的审批编排服务，用于生成原创可打印资料、提交飞书审批、在审批通过后调用 Ydisks 闲鱼助手发布商品，并绑定网盘卡密与付款后自动发货规则。

## 安全边界

- 默认 `publish_dry_run=true`，不会调用真实闲鱼发布接口。
- 没有所有者 `open_id` 的明确批准，发布任务不会进入 `APPROVED`；审批卡片可先“提出修改意见”，商品会退回草稿且不会发布。
- 模型只分析修改意见并生成提案；owner 点击“确认方案并重新提交审批”后才生成新版本，之后仍需再次批准。
- 任何标题、描述、价格、页数或素材哈希变化都会使旧审批失效。
- 服务默认只监听 `127.0.0.1`。
- 敏感值存放于 macOS Keychain，不写入仓库或 JSON 配置。
- Ydisks 是未获闲鱼官方授权的社区工具；使用前必须阅读其风险声明并先用小额测试商品验证。

## 快速开始

```bash
uv sync --python 3.12
uv run xianyu-auto init
uv run xianyu-auto generate-samples
uv run xianyu-auto demo
uv run pytest
uv run xianyu-auto serve
```

`demo` 会生成三款样品、建立一个待审批商品、模拟所有者批准并完成 dry-run 发布和发货绑定。它不会连接真实平台。

## 本机部署

```bash
# 1. 初始化、安装并启动两个 LaunchAgent
./scripts/install_local.sh

# 2. 在本机终端逐项录入凭据，内容不会回显
uv run python scripts/setup_credentials.py --configure-runtime

# 3. 检查运行状态、Keychain 配置、Ydisks 和睡眠设置
./scripts/check_runtime.sh
```

服务只监听 `127.0.0.1:8787`。`com.xianyu-auto.orchestrator` 负责 API、飞书长连接和调度，`com.xianyu-auto.backup` 每天 03:30 生成 AES-256 加密备份。卸载脚本只停止常驻任务，不删除数据。

视觉模型可在凭据脚本中选择 OpenAI 兼容服务。当前默认 `glm-4.6v-flash`；若免费档返回限流，自动降级到 `glm-4.5v`。模型只接收商品信息和详情图，不接收飞书或闲鱼凭据。

真实发布前还需要：

- 在 Ydisks 后台扫码绑定一个闲鱼卖家账号，记录该账号的 `cookie_id`。
- 在飞书开放平台创建自建应用，启用机器人、长连接和 `card.action.trigger`，向应用添加机器人并记录你的 `open_id`。
- 交付使用百度网盘开放平台上传，并通过百度官方网盘 MCP 创建分享。系统为每个商品建立固定目录，以后新版本进入同一目录并沿用原链接和提取码。
- 在 Ydisks 后台关闭“自动确认发货”，先人工核对前 3 笔订单。
- 确认 `config.json` 中 `publish_dry_run` 仍为 `true`，完成审批卡片和测试商品演练后才由你手动改为 `false`。

## 长期交付与容量

智谱 AI 只负责图片理解和修改意见分析，不提供文件托管、下载链接或提取码。长期交付使用百度网盘开放平台：

- 首次运行 `uv run python scripts/baidu_oauth.py`，由你本人在百度官方页面完成 OAuth。
- 审批通过后，系统自动上传 PDF 到百度网盘 `/apps/闲鱼`，再调用官方 MCP 的 `file_sharelink_set` 创建目录分享链接和提取码；令牌只存 macOS Keychain。
- 分享结果保存在本机权限为 `0600` 的 `manual-netdisk.json`。同一商品目录后续只上传新文件并复用原链接，不再重复创建分享。
- 官方 `mcp_server` 当前发放 30 天体验令牌且不返回 refresh token；令牌过期后需要重新授权才能为全新目录创建分享，原分享在其 365 天有效期内不受影响。
- 如果 MCP 返回错误，系统不会发布商品或伪造分享链接。可保留 `set_baidu_share.py` 作为人工分享后备路径。
- 存储容量仍受百度账号和开放平台配额限制，不宣称无限空间。

本机打包 `outputs/` 仍可生成清单和 SHA-256：

```bash
uv run python scripts/upload_outputs.py --upload --netdisk baidu --period-days 365
```

不加 `--upload` 时只在本机生成 ZIP、清单和 SHA-256，不访问任何网盘。`--upload --dry-run` 也只生成模拟分享结果。

## 本地测试

```bash
uv run pytest
./scripts/check_runtime.sh
curl -s http://127.0.0.1:8787/api/health
```

测试覆盖审批权限、修改意见回流、模型成功/失败、非法 JSON、低于 ¥1.00 的价格拦截、确认后重生成、重复回调、内容哈希失效、发布超时不重试、重启续跑、每日上限、卡密/规则唯一绑定和三款样例的页数、字体、图片尺寸与 400 道口算题二次校验。

## 常用命令

```bash
uv run xianyu-auto generate-samples
uv run xianyu-auto select
uv run xianyu-auto submit --product-id 1
uv run xianyu-auto decide --approval-id <ID> --approve
uv run xianyu-auto worker-once
uv run xianyu-auto reconcile
uv run xianyu-auto serve
uv run xianyu-auto feishu-listen
```

## 运行目录

默认运行目录为 `~/Library/Application Support/XianyuAuto/`，包含 SQLite、素材和运行日志。可通过 `XIANYU_AUTO_HOME` 覆盖。

## 风险边界

Ydisks 是社区工具，不是闲鱼官方接口，可能触发平台风控或违反平台协议。系统不会绕过验证码、不会使用代理或多账号，也不会在发布超时后自动重试。出现登录墙、验证码、账号警告或多次发货异常时，应停止自动发布并回到“生成草稿 + 人工上架”。

首批资料是程序生成的原创练习页。首版不提供未经逐字校对的笔顺动画或逐笔笔顺库，以免把不确定内容当成教学标准；字帖页明确提示家长用字典或学校材料核对笔顺。

