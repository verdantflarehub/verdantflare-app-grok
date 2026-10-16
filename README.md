# VerdantFlare Grok Channel

## 1. 实现状态

独立 Grok 兼容网关账号管理及概览／日志页面。账号加密、组织隔离、明确默认、启停归档和凭据原子轮换已接线；登记分别保存 SuperGrok Plus 与 Grok Bot 周窗口、Build／聊天分项、额外 USD 积分及自动充值登记状态。截图数值不作为新账号默认值；登记自动充值不修改供应商设置。

产品事实源为设计仓库 `docs/design/app/grok/README.md` 与 `docs/design/contracts/channel-provider-v1.md`。尚未发布。图片生成／单图编辑执行监听已接线，使用持久意图和加密结果；Image 实际切换、视频执行、旧任务迁移、跨媒体预留、Network 代理、价格结算和 etcd 尚待完成。健康接口声明图片适配，不声明视频适配或上游已验证。

## 2. 服务与配置

入口 `python -m grok_channel.server`，管理端口 8000 仅供 Studio 内部访问，执行端口 8001 仅供内部 Image MCP。`GROK_STATE_DB` 指定数据库；凭据从 `GROK_MANAGEMENT_TOKEN`、`GROK_EXECUTION_TOKEN`、`GROK_ENCRYPTION_KEY` 读取；两个监听必须使用不同且至少 32 字符的令牌；服务持有状态卷独占锁。`GROK_IMAGE_MODELS` 指定既有白名单中的图片模型，默认 `grok-imagine-image-2.0`。

`GROK_ALLOWED_API_ORIGINS` 是管理员批准的兼容网关 HTTPS origin 白名单，不含路径。连接基地址规范化为 `/v1`，不提供官方 xAI 默认地址。只读更新尝试兼容网关 `/v1/usage`，接口不可读或返回不符合结构时保留已有观测和人工登记。返回网关单位独立保存，不用网关计划名推导会员等级或两个窗口。

`GROK_DASHBOARD_DIR` 指向已校验摘要的页面包。目前代理仅接受明确直连，客户端禁止环境代理继承与重定向。部署和页面验收在开发集群进行，本机不启动服务或容器。

## 3. 轻量检查与构建

检查使用 `python -m unittest discover -s tests`、`node --test tests/dashboard.test.mjs`、`node --check dashboard/dashboard.mjs` 和 `python -m compileall -q grok_channel`。测试使用临时数据库、模拟响应和合成账号，不触发媒体生成。

`.github/workflows/grok-channel.yml` 在 release 分支构建非 root 镜像及页面制品，版本 0.1.0。私有远端仓库已创建并确认所需组织 Registry Secrets 对仓库可用；首次源码推送、Actions 和部署尚未完成。正式发行后，设计仓库 `scripts/appdashboard/prepare_channel.py --app-id grok` 校验实际制品与独立核实的提交／镜像摘要，只生成管理登记片段，不部署或安装。
