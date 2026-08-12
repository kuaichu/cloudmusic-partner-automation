# CloudMusic Partner Automation

一个面向 Windows 的网易云音乐合伙人每日评分任务自动化工具。它支持多账号、Cookie 获取、随机操作间隔、Windows 计划任务，以及可选的本地 Web 控制台。

> [!IMPORTANT]
> 本项目是非官方工具，与网易云音乐或其关联公司无关。接口和页面规则可能随时变化。请仅用于你自己的账号，并自行判断是否符合相关服务条款。Cookie 等同于登录凭据，请勿上传、分享或写入公开日志。

## 功能

- 自动完成基础评定和拓展评定
- 支持在一个配置文件中管理多个账号
- 从 Chrome、Edge、Brave 或 Chromium 提取 Cookie
- 支持扫码登录和手动粘贴 Cookie
- 通过 Windows 任务计划程序每日运行
- 提供仅监听本机的可选 Web 控制台
- 对日志中的 Cookie、CSRF Token 等敏感内容进行脱敏

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 已开通网易云音乐合伙人权限的账号

## 安装

```powershell
git clone https://github.com/<你的用户名>/cloudmusic-partner-automation.git
cd cloudmusic-partner-automation
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止激活虚拟环境，可以直接使用 `.venv\Scripts\python.exe` 执行后续命令。

## 配置 Cookie

最简单的方式是让工具读取本机浏览器中已经登录的网易云音乐账号：

```powershell
python get_cookie.py
```

也可以选择扫码或手动输入：

```powershell
python get_cookie.py --qrcode
python get_cookie.py --manual
```

程序会将凭据写入 `copartner_ck.json`。该文件已被 Git 忽略，不要强制提交。需要手工配置时，可复制 `copartner_ck.json.example` 并按示例填写。

## 运行

```powershell
python music_partner.py
```

指定其他配置文件：

```powershell
python music_partner.py --config D:\secure\copartner_ck.json
```

程序会依次处理配置中的账号，并在评分操作之间随机等待。账号越多，完整运行所需时间越长。

## 设置每日任务

在管理员 PowerShell 中运行：

```powershell
.\setup_task.ps1
```

默认每天 09:00 执行。可指定时间或卸载任务：

```powershell
.\setup_task.ps1 -Hour 9 -Minute 30
.\setup_task.ps1 -Uninstall
```

## 本地 Web 控制台

```powershell
python web_app.py
```

然后访问 <http://127.0.0.1:8765>。控制台默认只监听本机。除非你理解网络暴露风险并配置了防火墙与允许的 Host，否则不要启用远程监听，更不要将控制台直接暴露到公网。

## 配置格式

`MUSIC_COPARTNER` 数组中的每一项代表一个账号：

```json
{
  "MUSIC_COPARTNER": [
    {
      "cookie": "MUSIC_U=...; __csrf=...",
      "delay": {
        "basic": [15, 20],
        "interact": [1, 3],
        "extra": [15, 20]
      }
    }
  ]
}
```

间隔单位为秒，程序会在每个区间内随机取值。建议保留合理间隔，避免高频请求。

## 测试

测试使用模拟请求，不需要真实账号，也不会访问网易云接口：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m unittest discover -s tests -v
```

## 目录说明

```text
music_partner.py             主程序
get_cookie.py                Cookie 获取工具
web_app.py                   可选的本地控制台
setup_task.ps1               Windows 计划任务安装脚本
copartner_ck.json.example    配置示例
tests/                       离线回归测试
research/                    协议分析和历史逆向资料
```

`research/` 中的文件不参与程序运行，仅用于排查上游接口变化。

## 常见问题

**提示配置文件不存在**  
先运行 `python get_cookie.py`，或将示例配置复制为 `copartner_ck.json`。

**浏览器 Cookie 提取失败**  
关闭正在运行的浏览器后重试，或者改用 `--qrcode`、`--manual`。部分浏览器版本可能调整本地 Cookie 的加密方式。

**接口返回未授权或任务不可用**  
确认 Cookie 没有过期、账号已开通合伙人权限，并重新获取 Cookie。上游接口变化也可能导致暂时不可用。

## 安全说明

- 不要提交 `copartner_ck.json`、日志、HAR 抓包或二维码图片。
- 不要把 Cookie 发送给他人；泄漏后应立即退出相关登录会话并重新登录。
- Web 控制台仅适合可信的本机环境。
- 提交 Issue 时请删除账号、Cookie、CSRF Token、用户 ID 和本地绝对路径。

## 许可证

代码以 [MIT License](LICENSE) 发布。上游服务、网页资源、商标及历史分析材料仍归各自权利人所有。
