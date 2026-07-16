# 获客虾（LeadShrimp）

获客虾是一个本地 Web 应用，用于在用户完成抖音登录后，读取公开主页作品和指定作品评论，整理评论线索，按时间/日期筛选并导出给客服跟进。

仓库名是 `AIhook`，本地历史目录可能仍叫 `Alhook`。产品展示名为“获客虾”。

## 当前交付

当前默认交付是**通用版 Inno 安装包**：不加密、不启用账号系统、不接入远端卡密鉴权。安装包内置 Python 和项目依赖，安装后通过本地 Web 页面使用。

已生成的本地安装包：

```text
C:\Users\q2414\Desktop\Alhook\release\LeadShrimpSetup_0.1.2.exe
```

通用版源代码和依赖会随安装包部署，定位目标是“任何 Windows 用户可安装运行”，不是代码保护方案。商业版授权和 Nuitka 构建仍保留，但不属于当前通用版发布入口。

## 使用方式

要求：Windows、Microsoft Edge，以及可访问抖音的网络环境。

安装后启动“获客虾”，打开 `http://127.0.0.1:8922`。首次采集时点击登录，在独立浏览器窗口完成抖音登录；登录态会保存到本机数据目录，后续采集优先复用。只有需要登录或遇到平台验证时才需要用户介入。

开发环境启动：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
$env:PYTHONPATH = (Get-Location).Path
python -m lead_shrimp.launcher --port 8922
```

数据目录固定为 `%LOCALAPPDATA%\LeadShrimp\data`，包含登录态缓存、监控对象、作品缓存、评论线索和导出文件。不要删除或提交该目录中的真实数据。

## 当前功能

- 添加抖音主页或视频作为监控对象。
- 先解析主页作品，再人工选择作品采集评论，不默认全量抓取。
- 缓存主播和作品；缓存超过一天时在界面提醒可能有新作品或数据。
- 展示作品封面、标题、点赞数、评论数、发布时间和置顶状态。
- 每条作品可选择评论抓取上限：100、300、500、1000、2000。
- 评论按最新评论/最早评论排序，支持关键词、日期范围和时间范围筛选。
- 不采集作品作者自己的评论；按评论 ID 去重，不会因文本相同误删不同用户。
- 支持逐条选择、当前结果全选、按日期加入选择、滑动连续选择。
- 只导出选中的高价值评论，CSV 由后端生成。
- 浏览器采集尽量在后台运行；检测到登录失效或验证页面时，再提示用户处理。

## 目录速览

```text
lead_shrimp/
  app.py                         FastAPI 路由、本地 API、错误转换
  launcher.py                    端口检查、单实例启动、打开本地 Web
  frontend.html                  单页前端、Tab、筛选、批量选择、导出
  build/build_public_release.ps1 通用版 staging、依赖和 Inno 构建
  build/lead_shrimp_public.iss   通用版安装器配置
  tests/                         产品与构建契约测试
pipeline/
  comment_leads.py               评论采集、评论持久化、去重、作者过滤
  short_video.py                 主页作品解析、发布时间/统计、作品缓存
  browser_cookies.py             登录态缓存和会话复用
  config.py                      本机数据目录和路径配置
  export.py                      旧项目导出模块，仅按引用使用
licensing_server/                商业版卡密服务模板，不是通用版必需项
packaging/build/check_release.py 安装包敏感内容扫描
PROJECT_HANDOFF.md              下一个 AI 必读的完整交接
BUG_TRIAGE.md                    常见问题定位顺序
CHANGELOG.md                     面向开发和发布的变更记录
```

## 测试

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest lead_shrimp\tests licensing_server\tests -q
```

最近一次通用版构建验证：54 项测试通过；内置运行时可导入 FastAPI、Playwright 和项目模块；安全扫描通过。

## 构建通用安装包

要求安装 Inno Setup 6、Python 和项目依赖。推荐双击：

```text
lead_shrimp\build\一键打包通用版.bat
```

或执行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\lead_shrimp\build\build_public_release.ps1 -Version 0.1.0
```

脚本会清理 staging、复制 Python 运行时、安装 `requirements.txt`、复制业务源码、执行安全扫描，再调用 Inno Setup 生成 `release\LeadShrimpSetup_<版本>.exe`。`-SkipInstaller` 可只生成并验证 staging。

## 商业版说明

`build_commercial_release.ps1` 和 `lead_shrimp.iss` 仍用于未来的商业版。它们涉及 Nuitka、服务端授权和卡密配置，当前通用版不要调用。私钥、管理员令牌、真实卡密、Cookie、用户数据、授权数据库和构建产物不得提交 Git。

## 继续开发前

请依次阅读 [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md)、[BUG_TRIAGE.md](BUG_TRIAGE.md) 和 [CHANGELOG.md](CHANGELOG.md)。修改采集、缓存、筛选或导出时，先补回归测试，再改实现。
