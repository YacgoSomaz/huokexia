# 故障定位手册

先判断问题属于“前端状态、后端接口、缓存数据、浏览器会话、平台返回”哪一层。不要看到页面没数据就直接改前端。

## 快速检查

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest lead_shrimp\tests licensing_server\tests -q
python -m lead_shrimp.launcher --port 8922
```

确认数据目录：`%LOCALAPPDATA%\LeadShrimp\data`。确认端口：`Get-NetTCPConnection -LocalPort 8922 -ErrorAction SilentlyContinue`。

## 页面能打开，但显示未登录

1. 请求 `GET /api/comment-leads/login` 和 `GET /api/comment-leads/diagnosis`。
2. 检查 `pipeline/comment_leads.py` 的 `login_status`、`_login_state_valid`。
3. 检查 `pipeline/browser_cookies.py` 是否有共享 Cookie 缓存。
4. 让用户重新打开登录窗口并完成登录；不要手工复制 Cookie 到聊天或仓库。

## 刷新后作品消失

1. 看 `GET /api/comment-leads/monitors` 是否返回 `cached_videos`。
2. 看监控对象是否有 `works_cached_at`。
3. 查看 `comment_leads.resolve_profile_works` 是否命中缓存，以及 `short_video.resolve_profile` 的缓存判断。
4. 只有缓存缺失、元数据不完整或用户强制刷新时才重新访问主页。

## 作品数量少或发布时间缺失

优先查后端，不要先调整卡片 UI：

1. `short_video._render_profile_events_locked` 是否收到作品 API 响应。
2. `_videos_from_aweme_list` 是否映射了 `create_time/publish_time`、点赞和评论统计。
3. `_enrich_profile_videos` 是否补齐卡片缺少的字段。
4. 页面是否把未知值显示成“待确认”，而不是误认为采集卡死。
5. 记录实际返回的字段名和作品数量，脱敏后补测试。

## 评论采集失败、只有少量评论

1. 调用 `/api/comment-leads/diagnosis` 判断登录、Cookie 或验证状态。
2. 检查 `capture_video_comments` 的响应监听、分页 cursor、`max_comments` 和结束条件。
3. 检查是否误把“当前页数量”当成“总数量”；平台无下一页时应正常结束。
4. 检查 `filter_author_comments` 是否只过滤作者，不要误删其他用户。
5. 检查 `ingest_rows` 是否按 `comment_id` 去重，不能按评论文本去重。
6. 若出现验证页面，保留窗口给用户完成验证，不要尝试绕过。

## 点击后长时间无反馈

检查 `frontend.html` 的状态消息和 `app.py` 的后台线程边界。Playwright/网络操作必须通过 `_run_blocking` 或对应后台任务执行，不能阻塞 FastAPI 事件循环。解析作品应先发送阶段状态，再返回作品列表；真实失败原因应显示在任务状态中。

## 导出不完整或导出错文件

1. 检查前端 `selectedLeadIds` 是否包含用户选择的 ID。
2. 检查 `POST /api/comment-leads/export` 请求体是否传 `lead_ids`。
3. 检查 `list_leads_by_ids` 是否找到了对应记录。
4. 检查 `export_leads_csv` 的字段顺序和 UTF-8 BOM。
5. 不要用“导出全部”替代用户的选择导出。

## 通用安装包构建失败

先运行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\lead_shrimp\build\build_public_release.ps1 -Version 0.1.0 -SkipInstaller
```

按顺序排查：Python 可执行文件、`requirements.txt` 安装、staging 安全扫描、内置运行时导入、Inno Setup 路径。发布扫描失败时不要通过删除规则掩盖业务源码或用户数据中的敏感内容；只有确认是供应商 Python 运行时误报，才调整供应商目录规则。

## 需要收集的脱敏证据

- 提交号和版本号。
- 操作步骤、主页/视频 URL 类型，不记录 Cookie。
- 请求阶段：登录、解析作品、采集评论、写入、导出。
- 期望数量、平台返回数量、最终保存数量。
- 是否出现登录页/验证页。
- 相关测试名称和完整错误摘要。
