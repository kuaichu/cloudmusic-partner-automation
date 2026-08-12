# -*- coding: utf-8 -*-
"""
网易云音乐自动获取 Cookie

方法1 (推荐): 从 Chrome/Edge 浏览器直接提取 cookie
  python get_cookie.py                 # 自动从浏览器提取
  python get_cookie.py --browser edge  # 指定浏览器

方法2: 扫码登录 (备用)
  python get_cookie.py --qrcode

方法3: 手动粘贴 cookie
  python get_cookie.py --manual
"""

import sys
import os
import json
import time
import hashlib
import logging
import argparse
import getpass
import tempfile
from http.cookies import SimpleCookie
from urllib.parse import urlparse

import requests
from netease_utils import (
    IV,
    MODULUS,
    NONCE,
    PUBKEY,
    aes_encrypt,
    random_key,
    redact_sensitive,
    rsa_encrypt,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
QR_KEY_URL = "https://music.163.com/api/login/qrcode/unikey"
QR_CHECK_URL = "https://music.163.com/api/login/qrcode/client/login"
QR_CHECK_WEAPI_URL = "https://music.163.com/weapi/login/qrcode/client/login"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = (5.0, 20.0)
GET_ATTEMPTS = 2


log = logging.getLogger("get_cookie")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")


def encrypt_payload(data: dict) -> dict:
    text = json.dumps(data)
    rkey = random_key(16)
    params = aes_encrypt(aes_encrypt(text, NONCE, IV), rkey, IV)
    enc_sec_key = rsa_encrypt(rkey, PUBKEY, MODULUS)
    return {"params": params, "encSecKey": enc_sec_key}


class CookieRequestError(RuntimeError):
    pass


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    label: str,
    *,
    expect_json: bool = True,
    **kwargs,
) -> tuple[requests.Response, dict | None]:
    """统一处理 timeout、HTTP 状态、JSON 解析、有限 GET 重试和脱敏日志。"""
    method = method.upper()
    attempts = GET_ATTEMPTS if method == "GET" else 1
    kwargs["timeout"] = REQUEST_TIMEOUT
    request = getattr(session, method.lower())
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = request(url, **kwargs)
            final_url = getattr(response, "url", None) or url
            if urlparse(final_url).scheme.lower() != "https":
                raise CookieRequestError(f"{label} 拒绝非 HTTPS 响应")
            response.raise_for_status()
            if not expect_json:
                return response, None
            try:
                data = response.json()
            except ValueError as exc:
                raise CookieRequestError(f"{label} 返回非 JSON") from exc
            if not isinstance(data, dict):
                raise CookieRequestError(f"{label} 响应结构未知")
            return response, data
        except CookieRequestError:
            raise
        except requests.RequestException as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", "unknown")
            log.warning(
                "%s 请求失败 [status=%s attempt=%d/%d]: %s",
                label,
                status,
                attempt,
                attempts,
                redact_sensitive(exc),
            )
            if attempt >= attempts or (isinstance(status, int) and status < 500):
                break
    raise CookieRequestError(f"{label} 请求失败") from last_error


# ---------------------------------------------------------------------------
# Cookie 处理
# ---------------------------------------------------------------------------

def format_cookie_from_session(cookie_str: str, session: requests.Session) -> str:
    cookie_keys = {}
    for name, value in session.cookies.items():
        cookie_keys[name] = value
    if cookie_str:
        for item in cookie_str.split("; "):
            if "=" in item:
                k, v = item.split("=", 1)
                cookie_keys[k] = v
    return "; ".join(f"{k}={v}" for k, v in cookie_keys.items())


def cookie_from_set_cookie_header(header: str) -> str:
    if not header:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:
        return ""
    return "; ".join(f"{key}={morsel.value}" for key, morsel in cookie.items())


def cookie_from_response(response: requests.Response) -> str:
    """合并响应头和响应 Cookie 容器中的 Cookie。"""
    header_cookie = cookie_from_set_cookie_header(response.headers.get("Set-Cookie", ""))
    response_cookie = "; ".join(f"{key}={value}" for key, value in response.cookies.items())
    return "; ".join(value for value in (header_cookie, response_cookie) if value)


def validate_cookie(cookie: str) -> bool:
    values = {
        part.split("=", 1)[0].strip(): part.split("=", 1)[1].strip()
        for part in cookie.split(";")
        if "=" in part
    }
    return bool(values.get("__csrf")) and bool(values.get("MUSIC_U") or values.get("MUSIC_A"))


def save_config(cookie: str, config_path: str):
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("现有配置无法解析，已保留原文件") from exc
        if not isinstance(config, dict):
            raise ValueError("现有配置顶层必须是 JSON 对象")

    accounts = config.get("MUSIC_COPARTNER")
    if accounts is not None and not isinstance(accounts, list):
        raise ValueError("现有 MUSIC_COPARTNER 必须是数组")
    if not accounts:
        accounts = [{}]
    if not isinstance(accounts[0], dict):
        raise ValueError("现有首个帐号配置必须是对象")
    accounts[0]["cookie"] = cookie
    config["MUSIC_COPARTNER"] = accounts

    config_dir = os.path.dirname(os.path.abspath(config_path))
    os.makedirs(config_dir, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_dir,
            prefix=".copartner_ck.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = f.name
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, config_path)
    except Exception:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise
    log.info("Cookie 已写入配置文件: %s", os.path.basename(config_path))


# ---------------------------------------------------------------------------
# 方法1: 从浏览器提取 cookie（推荐）
# ---------------------------------------------------------------------------



def extract_from_browser(browser: str = "chrome") -> tuple[str, dict] | None:
    """从浏览器提取 music.163.com 的 cookie（使用 browser-cookie3）"""
    try:
        import browser_cookie3
    except ImportError:
        log.error("缺少 browser-cookie3 库，请运行: pip install browser-cookie3")
        return None

    # browser-cookie3 支持的浏览器
    browser_funcs = {
        "chrome": browser_cookie3.chrome,
        "edge": browser_cookie3.edge,
        "brave": browser_cookie3.brave,
        "chromium": browser_cookie3.chromium,
    }

    if browser not in browser_funcs:
        log.error("不支持的浏览器: %s。支持: %s", browser, ", ".join(browser_funcs))
        return None

    try:
        log.info("正在从 %s 读取 cookie...", browser)
        cj = browser_funcs[browser](domain_name="music.163.com")
    except Exception as e:
        log.error("读取 cookie 失败: %s", redact_sensitive(e))
        log.error("请关闭 %s 浏览器后重试，或用 --manual 手动粘贴", browser)
        return None

    if not cj:
        log.warning("浏览器中没有找到 music.163.com 的 cookie")
        log.warning("请先在 %s 中打开 https://music.163.com 并登录", browser)
        return None

    cookie_parts = [f"{c.name}={c.value}" for c in cj]
    cookie_str = "; ".join(cookie_parts)

    has_csrf = "__csrf" in cookie_str
    has_music_u = "MUSIC_U" in cookie_str or "MUSIC_A" in cookie_str

    log.info("提取到 %d 个 cookie (csrf=%s, MUSIC_U=%s)",
             len(cookie_parts), "存在" if has_csrf else "缺失",
             "存在" if has_music_u else "缺失")

    if not has_csrf or not has_music_u:
        log.warning("Cookie 可能不完整，请确认已在浏览器登录 music.163.com")

    user_info = {"nickname": f"浏览器 ({browser})", "userId": ""}
    return cookie_str, user_info


def find_available_browsers() -> list[str]:
    """查找系统中安装了哪些支持的浏览器"""
    try:
        import browser_cookie3
        available = []
        for name in ["chrome", "edge", "brave", "chromium"]:
            try:
                cj = getattr(browser_cookie3, name)()
                if cj:
                    available.append(name)
            except Exception:
                pass
        return available
    except ImportError:
        return []


# ---------------------------------------------------------------------------
# 二维码登录（备用）
# ---------------------------------------------------------------------------

def generate_qr_image(url: str, path: str):
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(path)
        return True
    except ImportError:
        return False


def print_qr_ascii(url: str):
    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


def login_qrcode() -> tuple[str, dict] | None:
    """扫码登录"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Referer": "https://music.163.com/",
        "Origin": "https://music.163.com",
    })
    script_dir = os.path.dirname(os.path.abspath(__file__))
    qr_png = os.path.join(script_dir, "qrcode.png")
    if os.path.exists(qr_png):
        fd, alternate_qr = tempfile.mkstemp(prefix=".qrcode.", suffix=".png", dir=script_dir)
        os.close(fd)
        os.remove(alternate_qr)
        qr_png = alternate_qr
    try:
        request_json(session, "GET", "https://music.163.com/", "登录页", expect_json=False)

        ts = int(time.time() * 1000)
        _, data = request_json(
            session,
            "GET",
            f"{QR_KEY_URL}?type=1&t={ts}",
            "二维码初始化",
            headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
        )
        if data.get("code") != 200:
            log.error("二维码初始化失败 [code=%s]", data.get("code"))
            return None
        unikey = data.get("unikey")
        if not isinstance(unikey, str) or not unikey:
            log.error("二维码初始化响应缺少 key")
            return None
        qr_url = f"https://music.163.com/login?codekey={unikey}"

        shown = False
        if print_qr_ascii(qr_url):
            print()
            log.info("请用网易云音乐 App 扫描上方二维码")
            shown = True
        if generate_qr_image(qr_url, qr_png):
            try:
                os.startfile(qr_png)
                log.info("二维码图片已打开")
                shown = True
            except Exception:
                log.info("二维码图片已生成")
                shown = True
        if not shown:
            log.error("无法显示二维码，请安装 qrcode 后重试")
            return None

        log.info("等待扫码...")
        start_time = time.time()
        tip = ""
        last_code = None

        def check_qr_status(check_type: int) -> tuple[dict, str]:
            check_ts = int(time.time() * 1000)
            response, payload = request_json(
                session,
                "GET",
                f"{QR_CHECK_URL}?key={unikey}&type={check_type}&t={check_ts}",
                "二维码状态",
                headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
            )
            return payload, cookie_from_response(response)

        def check_qr_status_weapi() -> tuple[dict, str]:
            payload = encrypt_payload({"key": unikey, "type": 3})
            response, response_data = request_json(
                session,
                "POST",
                QR_CHECK_WEAPI_URL,
                "二维码状态兼容检查",
                data=payload,
                headers={
                    "User-Agent": UA,
                    "Referer": "https://music.163.com/",
                    "Origin": "https://music.163.com",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            return response_data, cookie_from_response(response)

        while time.time() - start_time < 300:
            result = None
            response_cookie = ""
            for check in (lambda: check_qr_status(1), lambda: check_qr_status(3), check_qr_status_weapi):
                try:
                    candidate, candidate_cookie = check()
                except CookieRequestError as exc:
                    log.debug("二维码状态检查失败: %s", redact_sensitive(exc))
                    continue
                if result is None or candidate.get("code") in (802, 803, 800):
                    result = candidate
                    response_cookie = candidate_cookie
                if candidate.get("code") == 803:
                    break

            if result is None:
                time.sleep(2)
                continue

            code = result.get("code")
            if code != last_code:
                log.info("二维码状态码: %s", code)
                last_code = code

            if code == 800:
                log.error("二维码已过期")
                return None
            if code == 803:
                cookie_str = format_cookie_from_session(
                    "; ".join(x for x in [result.get("cookie", ""), response_cookie] if x),
                    session,
                )
                if "__csrf" not in cookie_str or ("MUSIC_U" not in cookie_str and "MUSIC_A" not in cookie_str):
                    try:
                        request_json(
                            session,
                            "GET",
                            "https://music.163.com/",
                            "登录态刷新",
                            expect_json=False,
                            headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
                        )
                        cookie_str = format_cookie_from_session(cookie_str, session)
                    except CookieRequestError:
                        pass
                log.info(
                    "扫码凭据字段检查: csrf=%s, MUSIC_U=%s, MUSIC_A=%s",
                    "存在" if "__csrf" in cookie_str else "缺失",
                    "存在" if "MUSIC_U" in cookie_str else "缺失",
                    "存在" if "MUSIC_A" in cookie_str else "缺失",
                )
                user_info = {
                    "nickname": result.get("nickname", ""),
                    "userId": result.get("account", {}).get("id", ""),
                    "avatarUrl": result.get("avatarUrl", ""),
                }
                log.info("扫码成功!")
                return cookie_str, user_info
            if code == 802 and tip != "scanned":
                log.info("已扫码，请在手机上确认...")
                tip = "scanned"

            time.sleep(2)

        log.error("等待扫码超时")
        return None
    except CookieRequestError as exc:
        log.error("扫码登录失败: %s", redact_sensitive(exc))
        return None
    finally:
        try:
            if os.path.exists(qr_png):
                os.remove(qr_png)
        except OSError as exc:
            log.warning("二维码临时图片清理失败: %s", redact_sensitive(exc))


# ---------------------------------------------------------------------------
# 方法3: 手动粘贴 cookie
# ---------------------------------------------------------------------------

def manual_input() -> tuple[str, dict] | None:
    """用户手动粘贴 cookie"""
    print("\n请从浏览器复制完整的 Cookie 字符串，然后粘贴到这里。")
    print("获取方法: F12 → Application → Cookies → music.163.com → 逐个复制或从 Network 面板复制")
    print()
    cookie = getpass.getpass("Cookie (不会回显): ").strip()
    if not cookie:
        log.error("Cookie 不能为空")
        return None
    if "__csrf" not in cookie:
        log.warning("Cookie 中缺少 __csrf 字段，但会继续保存")
    return cookie, {"nickname": "手动输入", "userId": ""}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="网易云音乐自动获取 Cookie")
    parser.add_argument("--browser", "-b", default=None, help="浏览器: chrome, edge, brave, chromium")
    parser.add_argument("--qrcode", "-q", action="store_true", help="扫码登录 (备用)")
    parser.add_argument("--manual", "-m", action="store_true", help="手动粘贴 cookie")
    parser.add_argument("--output", "-o", default=None, help="配置文件路径")
    parser.add_argument("--test", "-t", action="store_true", help="获取后执行只读身份/权限检查")
    parser.add_argument("--no-test", action="store_true", help="获取后不询问只读检查")
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("--phone", "-p", "--email", "-e") for arg in argv):
        parser.error("手机号/邮箱登录当前无法可靠验证，已禁用；请使用 --qrcode、--manual 或 --browser")
    args = parser.parse_args(argv)

    config_path = args.output or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "copartner_ck.json"
    )

    # 确定登录方式
    result = None

    if args.manual:
        result = manual_input()
    elif args.qrcode:
        result = login_qrcode()
    else:
        # 默认: 从浏览器提取
        if args.browser:
            browsers = [args.browser]
        else:
            browsers = find_available_browsers()

        if not browsers:
            log.error("未找到已安装的浏览器 (Chrome/Edge/Brave/Chromium)")
            log.info("你可以: python get_cookie.py --qrcode  (扫码登录)")
            log.info("      或: python get_cookie.py --manual   (手动粘贴)")
            return 1

        log.info("检测到浏览器: %s", ", ".join(browsers))

        for browser in browsers:
            result = extract_from_browser(browser)
            if result:
                break

        if not result:
            log.warning("从浏览器提取失败，尝试扫码登录...")
            result = login_qrcode()

    if result is None:
        return 1

    cookie_str, user_info = result
    if not validate_cookie(cookie_str):
        log.error("获取到的 Cookie 缺少必要认证字段，未修改现有配置")
        return 1
    log.info("获取成功!")
    account_id = "acct-" + hashlib.sha256(cookie_str.encode("utf-8")).hexdigest()[:8]
    log.info("  帐号标识: %s", account_id)

    # 可选测试
    run_test = args.test
    if not run_test and not args.no_test:
        yn = input("\n是否执行只读身份/权限检查? (y/n): ").strip().lower()
        run_test = (yn == "y")

    if run_test:
        print()
        from music_partner import MusicPartner
        try:
            result = MusicPartner(cookie_str, quiet=True).check_access()
            log.info(
                "只读检查通过 [account=%s identity=%s task_access=%s]",
                account_id,
                "ok" if result.get("user_name") else "unknown",
                "ok" if result.get("daily_task_access") else "failed",
            )
        except Exception as exc:
            log.error("只读检查失败 [account=%s]: %s", account_id, redact_sensitive(exc))
            return 1

    # 只有凭据结构校验和可选只读检查都通过后才替换配置。
    try:
        save_config(cookie_str, config_path)
    except Exception as exc:
        log.error("保存配置失败，原配置保持不变: %s", redact_sensitive(exc))
        return 1
    print(f"\nCookie 已保存到: {os.path.basename(config_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
