# 获客虾卡密授权服务

这是卡密、设备绑定、冻结与 Ed25519 授权签名的独立服务端模板。生产环境应与获客虾本机应用、用户数据分开部署。

## 本地验证

```powershell
cd C:\path\to\live_watch
python -m venv .venv-license
.\.venv-license\Scripts\Activate.ps1
pip install -r licensing_server\requirements.txt
```

生成一次 Ed25519 密钥对。`PRIVATE` 只能保存到服务器环境变量；`PUBLIC` 会写进商业安装包，因此可以公开。

```powershell
python -m licensing_server.keygen
```

把 `licensing_server/.env.example` 复制为仅服务器保存的 `.env`，填入私钥、随机的 `LICENSE_TOKEN_HASH_SECRET` 和 `LICENSE_ADMIN_TOKEN`。不要把真实 `.env` 传回项目、Git 或聊天记录。

启动开发服务：

```powershell
$env:LICENSE_DB_PATH = ".\license_data\licenses.db"
$env:LICENSE_SIGNING_PRIVATE_KEY = "服务器私钥"
$env:LICENSE_TOKEN_HASH_SECRET = "至少32位随机字符串"
$env:LICENSE_ADMIN_TOKEN = "至少32位管理员令牌"
uvicorn --factory licensing_server.app:create_app_from_env --host 127.0.0.1 --port 9077
```

## 管理接口

以下请求只应从管理员电脑或服务器内部运行。`LICENSE_ADMIN_TOKEN` 不会进入客户端。

也可以打开 `https://license.example.com/admin` 使用授权管理台。页面会要求输入管理员令牌；该令牌只保存在当前浏览器内存，刷新或关闭页面即清除。管理台支持创建卡密、查看绑定设备、冻结和解绑。

创建一张一设备、含全部商业功能的卡：

```powershell
$headers = @{ Authorization = "Bearer 管理员令牌" }
$body = @{ product_code = "lead_shrimp"; features = @("basic","lead_radar","export"); max_devices = 1; note = "客户名称" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "https://license.example.com/admin/card-keys" -Headers $headers -ContentType "application/json" -Body $body
```

冻结或解绑设备：

```powershell
$headers = @{ Authorization = "Bearer 管理员令牌" }
Invoke-RestMethod -Method Post -Uri "https://license.example.com/admin/activations/<activation_id>/freeze" -Headers $headers -ContentType "application/json" -Body '{"reason":"退款"}'
Invoke-RestMethod -Method Post -Uri "https://license.example.com/admin/activations/<activation_id>/unbind" -Headers $headers -ContentType "application/json" -Body '{"reason":"客户换电脑"}'
```

## 客户端行为

- 正式安装包内只有服务端 URL、Ed25519 公钥和强制授权开关。
- 激活后，本机保存签名授权包及刷新凭据；卡密本身不会写入本机。
- 客户端启动后和每 6 小时尝试刷新一次。联网设备被服务端冻结后，下次成功刷新即失效。
- 商业版会记录最近可信时间；明显回拨系统时间会暂停商业功能并要求联网刷新。
- 没网时，已签发授权最多继续使用 3 天加 1 天宽限期；这是为了兼顾离线可用与远程控制。可通过服务器环境变量调整。离线设备无法被服务器即时冻结，这是所有本地离线授权的客观边界。


部署时使用 HTTPS 反向代理、只对外暴露必要的 API，并备份授权数据库。生产 `.env` 、数据库和备份均不属于 Git 仓库。
