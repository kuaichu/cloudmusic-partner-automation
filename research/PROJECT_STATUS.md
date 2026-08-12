# 网易云音乐合伙人自动评分 — 项目状态

## 目标

自动化网易云音乐合伙人的每日评分任务，规则：
- 基础评定 5 首 = +8 分
- 拓展评定每首 +1 分，上限 +15 分
- 每日总分上限 23 分

## 已完成功能

### 1. Cookie 自动提取 (`get_cookie.py`)

从 Chrome/Edge 浏览器直接读取 `music.163.com` 的登录 cookie，使用 `browser-cookie3` 库解密。

```bash
python get_cookie.py           # 自动检测 Chrome，提取 cookie 写入 copartner_ck.json
python get_cookie.py --manual  # 手动粘贴 cookie
```

### 2. 基础评定 — 5 首 / +8 分 (`music_partner.py`)

完美工作。流程：

1. 获取每日任务 → `GET https://interface.music.163.com/api/music/partner/daily/task/get`
2. 遍历 `works[]` 数组中的未完成歌曲
3. 提交评分 → `POST https://interface.music.163.com/weapi/music/partner/work/evaluate`

**加密方式：weapi（已验证正确）**

```python
# 双重 AES-128-CBC + RSA 混淆
# 第一层：AES-CBC(key=NONCE, iv=IV)  
# 第二层：AES-CBC(key=随机16位字符串, iv=IV)
# encSecKey：随机key的RSA混淆
```

关键常量：
```
MODULUS = '00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725...'
PUBKEY  = '010001'
NONCE   = '0CoJUm6Qyw8W8jud'
IV      = '0102030405060708'
```

评分请求 payload（加密前）：
```json
{
  "taskId": "281541098",
  "workId": "8610460",
  "score": "3",
  "tags": "3-A-1",
  "customTags": "%5B%5D",
  "comment": "",
  "syncYunCircle": "true",
  "csrf_token": "xxx"
}
```

提交为 form data：`params=<加密后>&encSecKey=<RSA加密后>`

### 3. 拓展歌曲列表获取

```python
# POST weapi 加密
POST https://interface.music.163.com/weapi/music/partner/extra/wait/evaluate/work/list
payload: {"csrf_token": "xxx"}
```

返回 15 首拓展评定歌曲，结构同基础评定。**列表获取完美工作。**

---

## 最新进展

### 拓展评定提交 — 已跑通

2026-05-18 21:29 已通过受控请求验证，拓展评定接口返回：

```text
恭喜你完成评分，获得1分
```

最终确认的关键点：
- `work/evaluate` 仍是 weapi，不是 eapi
- 提交 payload 需要 `source: "mp-music-partner"`
- `csrf_token` 需要同时进入 query 和加密 payload
- H5 fetch 封装会在加密前执行 `obj2str`，把嵌套对象 `extraScore` 转成 JSON 字符串

Python 侧已在 `_encrypt_params()` 中模拟该行为：

```json
"extraScore": "{\"3\":4,\"2\":4,\"1\":4}"
```

而不是：

```json
"extraScore": {"3":4,"2":4,"1":4}
```

注意：验证时 `daily/task/get` 的 `integral` 未立即体现拓展分，但 `extra/wait/evaluate/work/list` 已能看到 completed 增加，接口本身返回了成功 toast。

### 拓展评定提交 — 已确认不是 eapi 主链路

2026-05-18 抓包确认：
- `https://interface3.music.163.com/eapi/mix/orpheus/convert` 只是打开 H5 页面的 Orpheus 转换请求，不是评分提交
- 真正拓展评分提交仍是 `https://interface.music.163.com/weapi/music/partner/work/evaluate`
- 提交评分前，H5 会先调用 `https://interface.music.163.com/weapi/partner/resource/interact/report`
- 成功评分响应包含 `evaluateRes: true`、`curScore: 1`、`successToast: "恭喜你完成评分，获得1分"`

已在 `music_partner.py` 中新增：
- `report_resource_interact()`：上报拓展资源互动
- `rate_extra_work()`：使用拓展评分 payload 单独提交，不再复用基础评分 payload

拓展评定最终 payload 形态：
```json
{
  "taskId": 281541098,
  "workId": 8621356,
  "score": 4,
  "tags": "",
  "customTags": "[]",
  "comment": "",
  "extraResource": true,
  "syncYunCircle": false,
  "syncComment": true,
  "extraScore": "{\"1\":4,\"2\":4,\"3\":4}",
  "source": "mp-music-partner",
  "csrf_token": "xxx"
}
```

如果 `supportExtraEvaTypes` 非空，`extraScore` 会按类型填充，且在加密前转成 JSON 字符串：
```json
"{\"1\":4,\"2\":4,\"3\":4}"
```

### eapi 加密结论

eapi 仍可能用于 App/H5 入口、配置和消息接口，但不是拓展评分提交主链路。

正确 eapi key 已确认：
```text
e82ckenh8dichen8
```

之前尝试的 `e82ee39a31ba0a5c` 是错误 key。

新增工具：
```bash
python research/decode_eapi_har.py capture.har
python research/decode_eapi_har.py capture.har --filter evaluate
```

用于解码 HAR 中的 eapi 请求和响应。

---

## 历史排查记录

### 曾误判方向：拓展评定提交走 eapi

最初 App 抓包看到：
- 请求发到 `https://interface3.music.163.com/eapi/mix/orpheus/convert`
- 使用 HTTP/2 协议
- Content-Type: `application/x-www-form-urlencoded`
- Body: `params=<HEX编码的加密字符串>`

#### 已尝试的方案

**方案 A — Binaryify 标准 eapi（失败，服务端返回空响应）**

```python
def eapi_encrypt(url_path, payload):
    text = json.dumps(payload, separators=(',', ':'))
    message = f"nobody{url_path}use{text}md5{url_path}"
    key = hashlib.md5(message.encode()).digest()  # 16 bytes
    cipher = AES.new(key, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(text.encode(), AES.block_size))
    return encrypted.hex().upper()
```

**方案 B — 另一AI提供的固定key方案（失败，服务端返回空响应）**

```python
def eapi_encrypt(url_path, payload):
    if 'header' not in payload:
        payload['header'] = {"os": "ios", "appver": "8.9.0"}
    text = json.dumps(payload, separators=(',', ':'))
    message = f"nobody{url_path}use{text}md5forencrypt"
    digest = hashlib.md5(message.encode('utf-8')).hexdigest()
    data = f"{url_path}-36cd479b6b5-{text}-36cd479b6b5-{digest}"
    key = b'e82ee39a31ba0a5c'
    cipher = AES.new(key, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(data.encode('utf-8'), AES.block_size))
    return encrypted.hex().upper()
```

**验证：** 拿到真实抓包数据后用方案B的key解密，结果是乱码而非JSON，说明 key 或算法不对。

#### 真实抓包数据（HAR 格式）

**请求：**
```
POST https://interface3.music.163.com/eapi/mix/orpheus/convert
HTTP/2.0
Content-Type: application/x-www-form-urlencoded
User-Agent: NeteaseMusic/7.2.22.1596693056(7002022);Dalvik/2.1.0 (Linux; U; Android 16; PJZ110 Build/BP2A.250605.015)

params=9F8F324CDCD35D34453B50A0354C007FCFD417BC0915E3DD2D358D1309AF39B5B8751CB86BF9122B5CE60041245FC7CCD98FE7DD31497B9B4F7297F61AB83FC23D537B37E60CB9C4E7ECF3D34379DB74AB96383F65716A96A15B1B8D96A992C59E012789E2F85F5483E83B71406B3F63D62A43C97A98E14911AA831D5747FE2DC0DC0913B7F9F3D0769796F14419A18C65A1253E5ED4A8D978E839992CE0DA8D0DE4D8136E555EAE4168F1B874E5D487C6D1A3C82464E4A3926ECFBD6398671A11EBD64EB7AFA60921402B9402E5D604243805265C94066BECB3C19A117D0418D88A8AAF019E29E93E6404C7D44D2F0407D8A3B821CA0B4A0CADC2772AA4971FEFF0229816F7F46B265FBA7D3151E1D3
```

- params 长度: 544 hex chars = 272 bytes = 17 个 AES 块
- 加密前明文长度: 240-256 字节左右
- 注意: params 值使用了 **HEX 编码（大写）**，不是 base64

**响应：**
```
HTTP 200
Content-Type: text/plain;charset=UTF-8
Content-Encoding: gzip

(gzip压缩的 base64 响应体)
```

#### 已知的 eapi 与 weapi 的差异

| 特性 | weapi | eapi |
|------|-------|------|
| 域名 | `interface.music.163.com` | `interface3.music.163.com` |
| HTTP协议 | HTTP/1.1 | HTTP/2 |
| URL前缀 | `/weapi/` | `/eapi/` |
| 加密 | 双重AES-CBC + RSA | AES-128-ECB |
| 输出编码 | base64 | hex（大写） |
| 密钥 | 动态（随机字符串+RSA） | 待确认 |
| Body格式 | `params=...&encSecKey=...` | `params=...` |

#### 已关闭的问题

1. **eapi 是否是评分主链路** — 已确认不是；评分提交走 `weapi/music/partner/work/evaluate`
2. **评分 payload 是否需要 taskId** — 需要，且 H5 中为数字
3. **评分 URL** — 已确认为 `https://interface.music.163.com/weapi/music/partner/work/evaluate`
4. **500 根因** — `extraScore` 需要按 H5 fetch 封装先 JSON.stringify，再参与 weapi 加密

---

## 现有文件

| 文件 | 说明 |
|------|------|
| `music_partner.py` | 主脚本（基础评定完美工作） |
| `get_cookie.py` | Cookie 提取（Chrome/Edge） |
| `requirements.txt` | Python 依赖 |
| `copartner_ck.json.example` | Cookie 配置模板 |
| `setup_task.ps1` | Windows 定时任务配置 |

运行：
```bash
python get_cookie.py       # 提取 cookie
python music_partner.py    # 自动评分（基础 5 首 + 尝试拓展）
```
