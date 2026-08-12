# -*- coding: utf-8 -*-
"""
Decode NetEase Cloud Music eapi requests/responses from a HAR file.

Usage:
  python decode_eapi_har.py capture.har
  python decode_eapi_har.py capture.har --filter work/evaluate
"""

import argparse
import base64
import gzip
import json
from urllib.parse import parse_qs

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


EAPI_KEY = b"e82ckenh8dichen8"


def decrypt_eapi_bytes(data: bytes) -> str:
    """Decrypt AES-128-ECB eapi bytes and return UTF-8 text."""
    plain = unpad(AES.new(EAPI_KEY, AES.MODE_ECB).decrypt(data), AES.block_size)
    return plain.decode("utf-8", errors="replace")


def decode_har_content(content: dict) -> bytes:
    """Decode HAR content text into raw bytes."""
    text = content.get("text", "")
    if content.get("encoding") == "base64":
        return base64.b64decode(text)
    return text.encode("utf-8")


def maybe_gunzip(data: bytes) -> bytes:
    """Reqable usually stores the decrypted response body, but keep gzip fallback."""
    try:
        return gzip.decompress(data)
    except OSError:
        return data


def extract_params(post_text: str) -> str:
    """Return the form field named params from application/x-www-form-urlencoded text."""
    parsed = parse_qs(post_text, keep_blank_values=True)
    values = parsed.get("params")
    return values[0] if values else ""


def print_entry(index: int, entry: dict) -> None:
    request = entry.get("request", {})
    response = entry.get("response", {})
    url = request.get("url", "")
    method = request.get("method", "")
    status = response.get("status", "")

    print("=" * 100)
    print(f"[{index}] {method} {url}")
    print(f"status: {status}")

    post_text = request.get("postData", {}).get("text", "")
    params = extract_params(post_text)
    if params:
        print("\nrequest plaintext:")
        try:
            print(decrypt_eapi_bytes(bytes.fromhex(params)))
        except Exception as exc:
            print(f"<request decrypt failed: {exc}>")

    content = response.get("content", {})
    if content.get("text"):
        print("\nresponse plaintext:")
        try:
            raw = maybe_gunzip(decode_har_content(content))
            print(decrypt_eapi_bytes(raw))
        except Exception as exc:
            print(f"<response decrypt failed: {exc}>")


def main() -> None:
    parser = argparse.ArgumentParser(description="Decode NetEase Cloud Music eapi HAR entries")
    parser.add_argument("har", help="HAR file path")
    parser.add_argument("--filter", default="", help="Only show entries whose URL contains this text")
    args = parser.parse_args()

    with open(args.har, "r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har.get("log", {}).get("entries", [])
    shown = 0
    for i, entry in enumerate(entries, 1):
        url = entry.get("request", {}).get("url", "")
        if "interface3.music.163.com/eapi/" not in url:
            continue
        if args.filter and args.filter not in url:
            continue
        shown += 1
        print_entry(i, entry)

    if not shown:
        print("No matching eapi entries found.")


if __name__ == "__main__":
    main()
