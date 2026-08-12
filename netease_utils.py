"""网易云 Web API 共用的加密与日志脱敏工具。"""

import base64
import codecs
import re
import secrets
import string

from Crypto.Cipher import AES


MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629"
    "ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424"
    "d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
PUBKEY = "010001"
NONCE = "0CoJUm6Qyw8W8jud"
IV = "0102030405060708"


def to_16(key: str) -> bytes:
    """使用空字符将密钥补齐到 16 字节的倍数。"""
    while len(key) % 16 != 0:
        key += "\0"
    return key.encode()


def aes_encrypt(text: str, key: str, iv: str) -> str:
    """执行网易云 Web API 使用的 AES-128-CBC 加密。"""
    block_size = AES.block_size
    padding = block_size - len(text) % block_size
    padded = text + padding * chr(padding)
    cipher = AES.new(to_16(key), AES.MODE_CBC, to_16(iv))
    encrypted = cipher.encrypt(padded.encode())
    return base64.encodebytes(encrypted).decode().replace("\n", "")


def rsa_encrypt(text: str, pubkey: str = PUBKEY, modulus: str = MODULUS) -> str:
    """执行网易云 Web API 使用的 RSA 混淆步骤。"""
    reversed_text = text[::-1]
    value = int(codecs.encode(reversed_text.encode(), "hex_codec"), 16)
    encrypted = value ** int(pubkey, 16) % int(modulus, 16)
    return format(encrypted, "x").zfill(256)


def random_key(length: int = 16) -> str:
    """生成加密请求使用的随机字母数字密钥。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def redact_sensitive(value: object) -> str:
    """清除错误文本中的 URL、Cookie 和认证参数。"""
    text = str(value)
    text = re.sub(r"https?://[^\s'\"<>]+", "<url>", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(\b(?:cookie|set-cookie)\s*[:=]\s*)[^\r\n]+",
        r"\1<redacted>",
        text,
    )
    sensitive_keys = r"cookie|set-cookie|csrf_token|__csrf|music_u|music_a|codekey|unikey|\bkey"
    quoted_pattern = re.compile(
        rf"(?i)(['\"]?(?:{sensitive_keys})['\"]?\s*[:=]\s*)(['\"])(.*?)(\2)"
    )
    text = quoted_pattern.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>{match.group(2)}",
        text,
    )
    text = re.sub(
        rf"(?i)(['\"]?(?:{sensitive_keys})['\"]?\s*[:=]\s*)[^\s,;}}&'\"]+",
        r"\1<redacted>",
        text,
    )
    return text[:500]
