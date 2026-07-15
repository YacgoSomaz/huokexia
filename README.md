# Alhook / 获客虾

获客虾是一个独立的本地桌面 Web 应用：在用户自己完成抖音登录后，采集主页或视频评论，保留评论原文与话术线索，进行筛选并导出 CSV。它从直播复盘项目中拆出，**不包含**直播录制、AI 复盘、直播工作台或短视频 AI 页面。

> 仓库名为 `Alhook`，对用户展示的产品名为“获客虾（LeadShrimp）”。

## 快速启动（开发版）

要求：Windows、Python 3.11+、Microsoft Edge，以及可访问抖音的网络环境。

```powershell
git clone https://github.com/YacgoSomaz/Alhook.git
cd Alhook
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m playwright install msedge
```

随后双击 [`lead_shrimp/启动获客虾.bat`](lead_shrimp/启动获客虾.bat)，或执行：

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m lead_shrimp.launcher --port 8922
```

打开 `http://127.0.0.1:8922`。首次使用请在“打开登录窗口”中完成抖音登录；登录 Cookie 只用于浏览器访问抖音，不是待采集的账号数据。

本机数据固定保存在 `%LOCALAPPDATA%\LeadShrimp\data`，不会读取或写入其他产品的数据目录。

## 核心能力

- 主页作品与指定视频评论采集；支持继续补采。
- 评论按稳定评论 ID 去重；不同用户或不同文本的评论不会因主页重复采集而被丢弃。
- 评论内容、昵称、地区、时间、点赞数与线索标签展示；CSV 导出。
- 本地“快速排错”：区分未完成抖音登录、无可用 Cookie、没有采集结果与正常可继续采集。
- 商业包卡密激活、单设备绑定、服务端 Ed25519 签名授权包与本机加密缓存。

## 测试

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest lead_shrimp\tests licensing_server\tests -q
```

## 商业构建

先跑功能测试，再运行 [`lead_shrimp/build/一键打包获客虾.bat`](lead_shrimp/build/一键打包获客虾.bat)，输入版本号。该流程会使用 Nuitka 编译业务代码、执行产物敏感扫描，并用 Inno Setup 生成安装包。

它需要 Python、Nuitka、Inno Setup 6 与商业授权**公钥**。私钥、管理员令牌、真实卡密、数据库、证书与用户数据绝不能进入本仓库或安装包。首次 Nuitka 构建较慢是正常现象。

## 目录

```text
lead_shrimp/       产品 API、前端、启动脚本、安装包脚本与测试
pipeline/          获客所需的精简采集/浏览器/授权公共模块
licensing_server/  卡密和设备授权服务端（部署模板，不含生产配置）
packaging/build/   商业构建产物安全扫描脚本
PROJECT_HANDOFF.md 面向下一个开发者/AI 的详细交接报告
```

## 继续开发前必读

请先阅读 [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md)。其中记录了架构、授权协议、运行数据边界、已验证内容、未完成事项和发布清单。
