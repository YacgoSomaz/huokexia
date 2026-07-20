# 获客虾项目交接报告

更新时间：2026-07-20
仓库：`YacgoSomaz/AIhook`
分支：`main`
当前最新提交：`7c6e49b build: add portable Inno public installer`

## 1. 先看结论

当前主线目标是**通用版本地 Web 应用**：任何 Windows 用户安装后即可使用，不加密、不启用账号系统、不要求远端授权。最新安装器由 Inno Setup 生成，内置 Python 运行时和依赖，入口是 `pythonw.exe -m lead_shrimp.launcher`。

AI 已作为独立“AI 内部测试”模块接入运行时，不替代稳定的采集、人工筛选和 Excel 导出。流程固定为“筛选智能体 -> 机会智能体 -> 人工跟进”：第二阶段只接收 `keep=true` 的结果；所有输出先过字段集合、枚举、长度与置信度校验才会回写。AI 不得自动私信或改写人工联系状态。模型配置在本机 `ai_config.json`，绝不能提交。

商业授权代码仍在仓库中，但属于未来商业版路径；不要因为看到 `licensing_server/` 或 `LICENSE_ENFORCE` 就给通用版加激活弹窗。

当前已验证：

- `78 passed`：产品、服务端、AI 结构化结果和前端契约测试。
- 通用版 staging 安全扫描通过。
- 内置运行时可以导入 `fastapi`、`playwright` 和 `lead_shrimp.app`。
- `release/LeadShrimpSetup_0.1.6.exe` 已在本机生成，约 62 MB；Inno 编译、安全扫描和 staging `/api/health` 检查通过。本次通用包不启用商业授权、账号系统、加密和运行时完整性校验，运行依赖本机 Edge。
- 启动器新增端口冲突回退和服务就绪探测；不再固定在服务尚未启动时打开 `8922`。
- 已验证 `staging\LeadShrimp\python\pythonw.exe -m lead_shrimp.launcher` 可以正常启动；Uvicorn 已关闭默认控制台日志配置，规避无 stdout 环境崩溃。

仍需真实抖音账号验收：不同账号、不同作品的评论分页完整度、发布时间字段稳定性和验证页面恢复流程。没有真实验收数据时，不要宣称“抓全了”。

## 2. 产品边界

产品只处理用户有权访问的公开页面和用户自己完成登录后的会话。不得绕过验证码、风控、登录限制或访问控制；遇到验证应停下来提示用户操作。产品不会自动私信、关注、发布内容，也不会主动寻找非公开手机号等隐私信息。

主流程：

```text
启动本地 launcher
  -> 127.0.0.1:8922 FastAPI
  -> 登录窗口完成抖音登录并保存会话
  -> 添加主页/视频监控对象
  -> 解析作品元数据（发布时间、点赞、评论、置顶）
  -> 人工选择作品和评论数量
  -> 采集评论、过滤作者、按 comment_id 去重
  -> 人工筛选高价值线索、导出选中 Excel
  -> （可选内部测试）AI 两阶段分析、人工查看建议
```

## 3. 文件职责与定位入口

| 文件 | 负责什么 | 出问题先看哪里 |
| --- | --- | --- |
| `lead_shrimp/app.py` | FastAPI 路由、后台线程、前端可见错误 | 对照 API 路由和 `_run_blocking`，不要在事件循环中直接跑 Playwright |
| `lead_shrimp/launcher.py` | 端口、单实例、本地浏览器启动 | 检查端口占用、工作目录、`LEADSHRIMP_ASSET_DIR` |
| `lead_shrimp/frontend.html` | Tab、作品选择、评论筛选、拖动选择、导出 | 先查状态对象、API 请求和 `render*` 函数 |
| `pipeline/comment_leads.py` | 评论采集、登录状态、评论落盘、作者过滤 | 采集失败看 `capture_video_comments`、响应监听和 `_page_needs_verification` |
| `pipeline/lead_ai.py` | 两阶段 AI 分析、JSON 解析、Schema 校验、结果回写 | 先看 `SCREENING_KEYS`/`OPPORTUNITY_KEYS` 和 `analyze_stored_leads`，不要放宽未知字段 |
| `pipeline/short_video.py` | 主页解析、作品元数据、主页缓存 | 作品少/日期缺失看 `_render_profile_events_locked`、`_videos_from_aweme_list`、`_enrich_profile_videos` |
| `pipeline/browser_cookies.py` | Cookie 持久化和会话复用 | 看 `shared_status`、`store_shared_jar`、`auto_refresh` |
| `pipeline/config.py` | `%LOCALAPPDATA%\LeadShrimp\data` 路径 | 先确认数据目录，不要误读旧项目目录 |
| `pipeline/export.py` | 历史导出能力 | 评论线索 Excel 主入口在 `comment_leads.export_leads_xlsx` |
| `AI_INPUT_OUTPUT_SPEC.md` | AI 输入、输出、合规边界与前端映射契约 | 改提示词、字段或 AI UI 前必须先对照此文件 |
| `AI_BUSINESS_CONTEXT_DRAFT.json` | 昆明房产场景的初始业务上下文 | 上线前由业务方确认，不可把草案当成通用规则 |
| `AI_AGENT_PROMPT_TUNING_V3.md` | 筛选/机会智能体的提示词和规则 | 调整意图判断时同步更新示例与测试 |
| `SCREENING_JSON_EXAMPLES_V3.json` | 筛选智能体合法与反例输出 | Schema 回归和提示词对齐依据 |
| `OPPORTUNITY_JSON_EXAMPLES_V3.json` | 机会智能体合法与反例输出 | Schema 回归和提示词对齐依据 |
| `lead_shrimp/build/build_public_release.ps1` | 通用 staging 和 Inno 调用 | 先用 `-SkipInstaller`，再检查扫描结果 |
| `lead_shrimp/build/lead_shrimp_public.iss` | 通用安装器文件、快捷方式、卸载保留策略 | 检查 `{app}\python\pythonw.exe` 和 `-m lead_shrimp.launcher` |
| `packaging/build/check_release.py` | 发布目录安全扫描 | Python 运行时目录是供应商内容，业务源码仍必须扫描 |
| `licensing_server/` | 未来商业版服务端 | 通用版不要依赖它 |

## 4. API 清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/` | 返回前端 |
| `GET` | `/api/license/status` | 授权状态；通用版应保持可用，不代表必须激活 |
| `POST` | `/api/comment-leads/login` | 打开抖音登录窗口并更新状态 |
| `GET` | `/api/comment-leads/monitors` | 监控对象和缓存作品 |
| `DELETE` | `/api/comment-leads/monitors/{id}` | 删除监控对象，保留历史评论线索 |
| `GET` | `/api/comment-leads/diagnosis` | 登录和采集诊断 |
| `POST` | `/api/comment-leads/monitors` | 添加主页或视频监控 |
| `POST` | `/api/comment-leads/profile-videos` | 解析或读取缓存作品 |
| `GET` | `/api/comment-leads/leads` | 评论线索列表、搜索和状态筛选 |
| `POST` | `/api/comment-leads/run` | 采集选中作品评论 |
| `GET/POST` | `/api/comment-leads/export` | 导出全部或指定 `lead_ids` |
| `GET` | `/api/comment-leads/internal-test/overview` | AI 内部测试概览、评论人聚合、巡检记录 |
| `POST` | `/api/comment-leads/internal-test/run-daily-scan` | 立即刷新对标账号并采集有变化作品的评论 |
| `POST` | `/api/comment-leads/internal-test/schedule` | 保存本机每日巡检时间与开关 |
| `POST` | `/api/comment-leads/internal-test/analyze` | 对最多 50 条已选线索执行两阶段 AI 分析 |

## 5. 缓存、登录与数据

数据根目录由 `pipeline/config.py` 管理，通常为 `%LOCALAPPDATA%\LeadShrimp\data`。重要文件包括：

- `comment_leads.json`：监控对象和评论线索。
- `short_video_profiles.json`：按 `sec_uid` 缓存主页作品和元数据。
- `ai_config.json`：仅本机模型连接配置，可能含 API 密钥，已由 `.gitignore` 排除。
- 登录态相关缓存：由 `browser_cookies.py` 管理。
- 导出目录：后端生成中文 Excel 后保存。

主页作品缓存当前 TTL 是 6 小时；界面以超过 1 天作为“可能有新作品/数据”的提醒阈值。刷新后优先恢复监控对象中的 `cached_videos`，只有缓存不足或强制刷新时才重新访问主页。

评论抓取由前端传入每条作品的数量上限，当前选项为 100/300/500/1000/2000。评论按稳定 ID 去重；作者评论通过作者 `sec_uid` 过滤。增量和分页完整度仍必须用真实账号验收，不要只看 UI 数量。

作品变化标记：`pipeline.comment_leads.annotate_profile_video_changes(current_videos, previous_videos)` 按 `id/aweme_id/video_id/url` 建立快照索引。刷新后写入每条作品的 `is_new` 和 `comment_increase` 字段；前端作品卡片分别渲染“新视频”和红色 `+数量`。选择作品页的 `workCompetitorFilter` 用监控对象 ID 切换账号，默认读取该账号 `cached_videos`，对标账号页的“采集作品”用于强制刷新。

AI 内部测试：`comment_leads.run_internal_daily_scan()` 强制刷新启用的对标账号，只对新增作品或评论数增长且大于 0 的作品调用既有采集链路；`run_due_internal_daily_scan()` 由应用启动的守护线程每分钟检查一次，每日最多执行一次，应用关闭时不会运行。`lead_ai.analyze_stored_leads()` 最多接收 50 条人工选中的已入库线索，先写入 `ai.screening`，仅保留项再写入 `ai.opportunity`；低置信度会强制人工复核。当前前端传入的业务上下文仍是房产场景的临时值，正式发布前应增加按对标账号维护、审核和版本化的配置界面。

## 6. 浏览器行为

采集使用 Playwright。正常采集使用屏幕外窗口，不抢用户当前窗口；采集上下文结束后自动关闭。运行时优先使用 Edge，并在 Windows 常见路径兜底；通用包构建时还会把 Chromium 安装到 `python/ms-playwright`，由启动器设置 `PLAYWRIGHT_BROWSERS_PATH`。登录窗口显式指定可见位置和尺寸，避免沿用 Chromium 上次的离屏位置。不要把“无头”理解成“没有登录态”：上下文会加载本地保存的 Cookie。若验证页出现，保持采集窗口可见并提示用户完成验证，不能自动绕过。浏览器全部启动失败时，错误会保留每次启动尝试的异常详情，便于区分缺少浏览器、配置损坏和 Profile 锁定。

应用启动时优先使用 `8922`，若端口被占用则从下一个端口开始寻找可用端口，并把实际端口传给 FastAPI。启动器会轮询不经过授权中间件的 `/api/health`，成功后才打开前端。若 30 秒内仍未就绪，诊断文件写入 `%LOCALAPPDATA%\LeadShrimp\logs\`。`pythonw.exe` 下的模块导入和 Uvicorn 异常会写入同一目录的 `launcher.log` 和 `startup-error.html`。

## 7. 构建发布

通用版：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\lead_shrimp\build\build_public_release.ps1 -Version 0.1.5
```

排错时先执行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\lead_shrimp\build\build_public_release.ps1 -Version 0.1.5 -SkipInstaller
```

该脚本会复制真实 Python 运行时、安装 `requirements.txt`、复制业务源码和前端、清除 `__pycache__`/测试目录、运行发布扫描，之后由 Inno Setup 生成安装包。它不是编译器，源码会随安装包存在，符合当前“先通用交付、不加密”的决定。

商业版入口仍是 `build_commercial_release.ps1`。商业版才涉及 Nuitka、远端卡密和签名授权。任何私钥、管理员令牌、Cookie、真实卡密、用户数据和生产数据库不得进入仓库。

## 8. 测试和接手规则

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest lead_shrimp\tests licensing_server\tests -q
```

本次前端、评论采集和 AI 内部测试回归验证为 `78 passed`。未登录时登录浏览器会先尝试点击登录并切换到扫码登录；入口未渲染时再通过搜索框交互触发页面更新。已有持久化 Profile 登录态时不会再被共享 Cookie 覆盖。删除监控不会删除历史线索；如果需要连历史线索一起清理，应另行设计明确的数据清理操作，不能复用删除监控接口。

接手改动时：

1. 先读根 `README.md`、本文件、`BUG_TRIAGE.md` 和 `CHANGELOG.md`。
2. 修改采集、缓存、排序、导出或错误提示前，先增加回归测试。
3. 先跑局部测试，再跑全量测试和 staging 构建验证。
4. 不删除用户数据、不重置工作区、不覆盖用户未提交改动。
5. 真实平台问题只记录脱敏日志：URL 可保留路径类型，Cookie、Token、个人敏感信息必须移除。

## 9. 待办优先级

- P0：用真实登录账号验收作品分页、发布时间、评论分页和增量采集。
- P1：补充采集任务进度、分页游标和失败阶段的脱敏日志，解决“看起来卡住/失败原因不明”。
- P1：验证通用安装包在干净 Windows、无系统 Python 环境下的安装、升级、卸载和 Edge 检测。
- P1：完成按对标账号维护、审核和版本化 AI 业务上下文的界面，替换当前临时房产上下文。
- P2：为 AI 调用补指数退避、结果缓存、失败队列和审计版本字段；当前仅有严格输出校验。
- P2：继续收敛前端布局和低技术用户操作路径。
- P3：商业版再处理授权、更新服务、代码保护和签名发布；不要提前混入通用版。
