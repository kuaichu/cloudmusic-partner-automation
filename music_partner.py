# -*- coding: utf-8 -*-
"""
网易云音乐合伙人自动评分脚本
每天自动为音乐合伙人任务中的歌曲评分，无需人工干预。

用法:
  python music_partner.py              # 使用默认配置文件
  python music_partner.py --config xxx.json  # 指定配置文件
"""

import os
import sys
import json
import time
import hashlib
import random
import logging
import argparse
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler

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
    to_16,
)

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.environ.get("MUSIC_PARTNER_LOG_FILE", os.path.join(ROOT_DIR, "music_partner.log"))
STATE_FILE = os.environ.get("MUSIC_PARTNER_STATE_FILE", os.path.join(ROOT_DIR, "music_partner_state.json"))
REQUEST_TIMEOUT = (5.0, 20.0)
GET_ATTEMPTS = 2

log = logging.getLogger("music_partner")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    log.addHandler(stream_handler)
    log.addHandler(file_handler)


class TaskStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NO_WORK = "NO_WORK"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class PartnerError(RuntimeError):
    """可安全展示的业务错误基类。"""


class RequestFailure(PartnerError):
    """网络、HTTP 或 JSON 解析失败。"""


class UnknownResponse(PartnerError):
    """响应成功到达，但结构不足以判断结果。"""


_STATE_THREAD_LOCK = threading.RLock()


def _safe_message(data: dict) -> str:
    return redact_sensitive(data.get("message") or data.get("msg") or "未知错误")[:160]


@contextmanager
def _state_file_lock():
    """同进程锁，并在标准库支持时增加跨进程文件锁。"""
    lock_path = f"{STATE_FILE}.lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with _STATE_THREAD_LOCK:
        lock_file = open(lock_path, "a+b")
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

class MusicPartner:
    """网易云音乐合伙人自动评分"""

    def __init__(self, cookie: str, delay_config: dict | None = None, quiet: bool = False):
        self.session = requests.Session()
        self.csrf = ""
        self.account_id = "acct-" + hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:8]
        self.random_secret = random_key(16)
        self.delay_config = delay_config or {}
        self.quiet = quiet

        # API 地址
        self.task_url = "https://interface.music.163.com/api/music/partner/daily/task/get"
        self.extra_list_url = "https://interface.music.163.com/weapi/music/partner/extra/wait/evaluate/work/list"
        self.user_info_url = "https://music.163.com/api/nuser/account/get"
        self.record_url = "https://interface.music.163.com/api/music/partner/evaluate/record/get"
        self.sign_url = "https://interface.music.163.com/weapi/music/partner/work/evaluate"
        self.interact_url = "https://interface.music.163.com/weapi/partner/resource/interact/report"

        # 评分参数 —— 加入随机化避免风控
        self._score_pool = self._build_score_pool()
        self._tag_pool = ["A", "B", "C"]

        # 请求头
        self.headers = {
            "Accept": "application/json, text/javascript",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://mp.music.163.com",
            "Referer": "https://mp.music.163.com/",
            "X-Requested-With": "com.netease.cloudmusic",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 16; PJZ110 Build/BP2A.250605.015; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/147.0.7727.138 Mobile Safari/537.36 "
                "CloudMusic/0.1.1 NeteaseMusic/7.2.22"
            ),
        }

        # 解析 cookie
        self._parse_cookie(cookie)

    @staticmethod
    def _build_score_pool() -> list[dict]:
        """构造带权重的评分池 —— 模拟真人评分分布：多数 3-4 分，少量 2 分和 5 分"""
        return [
            # (score, tags, weight) — weight 越大越容易被选中
            ("3", "3", 35),   # 35% 概率 3分 + 某标签
            ("4", "4", 40),   # 40% 概率 4分 + 某标签
            ("2", "2", 15),   # 15% 概率 2分
            ("5", "5", 10),   # 10% 概率 5分
        ]

    def _random_rating(self, support_types: list | None = None) -> dict:
        """生成一组随机化的评分参数。

        Returns:
            {"score": str, "tags": str, "extraScore": dict | None}
        """
        # 按权重随机选分档
        pool = self._build_score_pool()
        scores, tags_list, weights = zip(*[(s, t, w) for s, t, w in pool])
        score, tag_prefix = random.choices(
            list(zip(scores, tags_list)),
            weights=weights, k=1
        )[0]

        # 随机选标签类别 A/B/C
        tag_suffix = random.choice(self._tag_pool)
        tags = f"{tag_prefix}-{tag_suffix}-1"

        # 拓展评分用的 extraScore：各维度分数有一定波动
        extra_score = None
        if support_types:
            extra_score = {}
            for t in support_types:
                # 大部分维度跟主分数一致，少量波动 ±1
                base = int(score)
                variation = random.choices([0, -1, 1], weights=[60, 25, 15], k=1)[0]
                dim_score = max(1, min(5, base + variation))
                extra_score[str(t)] = dim_score

        return {"score": score, "tags": tags, "extraScore": extra_score}

    def _delay_range(self, kind: str) -> tuple[float, float]:
        """读取随机延迟范围，避免请求节奏过于机械。"""
        defaults = {
            "basic": (15.0, 20.0),
            "extra": (15.0, 20.0),
            "interact": (1.0, 3.0),
        }
        value = self.delay_config.get(kind, defaults[kind])
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return defaults[kind]

        low, high = float(value[0]), float(value[1])
        if low < 0 or high < 0:
            return defaults[kind]
        return (min(low, high), max(low, high))

    def _sleep_random(self, kind: str, reason: str = "") -> None:
        """按指定类型随机暂停。"""
        low, high = self._delay_range(kind)
        seconds = random.uniform(low, high)
        if reason:
            log.info("等待 %.1f 秒后%s", seconds, reason)
        else:
            log.info("等待 %.1f 秒", seconds)
        time.sleep(seconds)

    def _state_key(self) -> str:
        """按帐号隔离本地状态，避免多帐号互相影响。"""
        return hashlib.sha256(self.csrf.encode("utf-8")).hexdigest()[:16]

    def _today_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _read_state_unlocked(self) -> dict:
        if not os.path.exists(STATE_FILE):
            return {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.error("状态文件已损坏，保留原文件且停止写入: %s", redact_sensitive(exc))
            raise PartnerError("状态文件无法解析") from exc
        except OSError as exc:
            log.error("读取状态文件失败: %s", redact_sensitive(exc))
            raise PartnerError("状态文件读取失败") from exc
        if not isinstance(state, dict):
            log.error("状态文件结构无效，保留原文件且停止写入")
            raise PartnerError("状态文件结构无效")
        return state

    def _write_state_unlocked(self, state: dict) -> None:
        state_dir = os.path.dirname(os.path.abspath(STATE_FILE))
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=state_dir,
                prefix=".music_partner_state.",
                suffix=".tmp",
                delete=False,
            ) as f:
                temp_path = f.name
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, STATE_FILE)
        except Exception as exc:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            log.error("原子写入状态文件失败，原文件保持不变: %s", redact_sensitive(exc))
            raise PartnerError("状态文件写入失败") from exc

    def _load_state(self) -> dict:
        with _state_file_lock():
            return self._read_state_unlocked()

    def _save_state(self, state: dict) -> None:
        with _state_file_lock():
            self._write_state_unlocked(state)

    def _is_extra_no_more_today(self) -> bool:
        state = self._load_state()
        account_state = state.get(self._state_key(), {})
        return account_state.get("extra_no_more_date") == self._today_key()

    def _get_extra_done_today(self) -> int | None:
        state = self._load_state()
        account_state = state.get(self._state_key(), {})
        if account_state.get("extra_no_more_date") != self._today_key():
            return None
        value = account_state.get("extra_done_count")
        return value if isinstance(value, int) else None

    def _mark_extra_no_more_today(self, done_count: int | None = None) -> None:
        with _state_file_lock():
            state = self._read_state_unlocked()
            account_key = self._state_key()
            account_state = state.get(account_key, {})
            if not isinstance(account_state, dict):
                raise PartnerError("账号状态结构无效")
            account_state["extra_no_more_date"] = self._today_key()
            if done_count is not None:
                account_state["extra_done_count"] = done_count
            state[account_key] = account_state
            self._write_state_unlocked(state)

    def _parse_cookie(self, cookie: str):
        """解析 cookie 字符串，设置 session 和 csrf"""
        cookie_dict = {}
        for item in cookie.split("; "):
            if "=" in item:
                k, v = item.split("=", 1)
                cookie_dict[k] = v

        if "__csrf" not in cookie_dict:
            raise ValueError("Cookie 中缺少 __csrf 字段，请检查 cookie 是否完整")

        self.csrf = cookie_dict["__csrf"]
        requests.utils.add_dict_to_cookiejar(self.session.cookies, cookie_dict)
        if not self.quiet:
            log.info("Cookie 解析成功 [account=%s]", self.account_id)

    def _encrypt_params(self, data: dict) -> dict:
        """生成加密后的请求参数"""
        normalized = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                normalized[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                normalized[key] = value

        text = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        params = aes_encrypt(aes_encrypt(text, NONCE, IV), self.random_secret, IV)
        enc_sec_key = rsa_encrypt(self.random_secret, PUBKEY, MODULUS)
        return {"params": params, "encSecKey": enc_sec_key}

    def _request_json(self, method: str, url: str, label: str, **kwargs) -> dict:
        """统一处理 timeout、HTTP 状态、JSON 解析、有限 GET 重试和脱敏日志。"""
        method = method.upper()
        attempts = GET_ATTEMPTS if method == "GET" else 1
        kwargs["timeout"] = REQUEST_TIMEOUT
        kwargs["allow_redirects"] = False
        request = getattr(self.session, method.lower())
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                response = request(url, **kwargs)
                status_code = getattr(response, "status_code", 0)
                if 300 <= status_code < 400:
                    raise RequestFailure(f"{label} 拒绝重定向")
                response.raise_for_status()
                try:
                    data = response.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    log.error(
                        "%s 返回非 JSON [account=%s status=%s]",
                        label,
                        self.account_id,
                        getattr(response, "status_code", "unknown"),
                    )
                    raise RequestFailure(f"{label} 返回非 JSON") from exc
                if not isinstance(data, dict):
                    raise UnknownResponse(f"{label} 响应结构未知")
                return data
            except (RequestFailure, UnknownResponse):
                raise
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", "unknown")
                log.warning(
                    "%s 请求失败 [account=%s status=%s attempt=%d/%d]: %s",
                    label,
                    self.account_id,
                    status,
                    attempt,
                    attempts,
                    redact_sensitive(exc),
                )
                if attempt >= attempts or (isinstance(status, int) and status < 500):
                    break
        raise RequestFailure(f"{label} 请求失败") from last_error

    def _get_user_name(self) -> str:
        """获取当前用户的昵称"""
        try:
            data = self._request_json("GET", self.user_info_url, "用户身份", headers=self.headers)
            profile = data.get("profile")
            if profile is None:
                return "未知用户"
            if not isinstance(profile, dict):
                raise UnknownResponse("用户身份响应结构未知")
            return profile.get("nickname") or "未知用户"
        except PartnerError as exc:
            log.warning("用户身份读取失败 [account=%s]: %s", self.account_id, redact_sensitive(exc))
            return "未知用户"

    def check_access(self) -> dict:
        """只读身份/权限检查；不提交评分或互动。"""
        user_name = self._get_user_name()
        task = self.fetch_task()
        return {
            "user_name": user_name,
            "task_id_present": bool(task.get("id")),
            "daily_task_access": True,
        }

    def fetch_task(self) -> dict:
        """获取每日评分任务

        Returns:
        - code 200: 返回任务数据
        - code 301: cookie 过期
        """
        data = self._request_json(
            "GET",
            self.task_url,
            "每日任务",
            headers={**self.headers, "Referer": "https://mp.music.163.com/"},
        )
        code = data.get("code")

        if code == 301:
            raise RequestFailure(f"登录已过期: {_safe_message(data)}")
        if code != 200:
            if code is None:
                raise UnknownResponse("每日任务响应缺少 code")
            raise RequestFailure(f"获取任务失败: code={code}, message={_safe_message(data)}")

        task = data.get("data")
        if not isinstance(task, dict):
            raise UnknownResponse("每日任务响应缺少 data")
        required_fields = {
            "id": (str, int),
            "works": list,
            "completedCount": (int, float),
            "integral": (int, float),
            "dailyTaskScoreLimit": dict,
        }
        for field, expected_type in required_fields.items():
            if field not in task or not isinstance(task[field], expected_type):
                raise UnknownResponse(f"每日任务响应字段 {field} 结构未知")
        if task["id"] in ("", None):
            raise UnknownResponse("每日任务响应缺少任务 id")
        for item in task["works"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("completed"), bool)
                or not isinstance(item.get("work"), dict)
            ):
                raise UnknownResponse("每日任务 works 条目结构未知")
            if item["work"].get("id") in ("", None):
                raise UnknownResponse("每日任务 work 缺少 id")
        score_limit = task["dailyTaskScoreLimit"]
        for field in ("dailyBasicTaskScore", "dailyMaxExtendEvaluateScore"):
            if field not in score_limit or not isinstance(score_limit[field], (int, float)):
                raise UnknownResponse(f"每日任务计分字段 {field} 结构未知")
            if score_limit[field] < 0:
                raise UnknownResponse(f"每日任务计分字段 {field} 无效")
        return task

    def rate_work(self, task_id: str, work_id: str, support_types: list | None = None) -> TaskStatus:
        """为一首歌评分（基础评定）"""
        rating = self._random_rating(support_types)
        payload = {
            "taskId": task_id,
            "workId": work_id,
            "score": rating["score"],
            "tags": rating["tags"],
            "customTags": "%5B%5D",
            "comment": "",
            "syncYunCircle": "true",
            "csrf_token": self.csrf,
        }
        data = self._encrypt_params(payload)
        url = f"{self.sign_url}?csrf_token={self.csrf}"

        result = self._request_json("POST", url, "基础评分", data=data, headers=self.headers)
        code = result.get("code")
        if code == 200:
            response_data = result.get("data")
            if not isinstance(response_data, dict) or "evaluateRes" not in response_data:
                return TaskStatus.UNKNOWN
            if response_data.get("evaluateRes") is not True:
                return TaskStatus.FAILED
            log.debug("评分参数已生成 [account=%s score=%s tags=%s]", self.account_id, rating["score"], rating["tags"])
            return TaskStatus.SUCCESS
        if code is None:
            return TaskStatus.UNKNOWN
        log.warning("基础评分未成功 [account=%s code=%s message=%s]", self.account_id, code, _safe_message(result))
        return TaskStatus.FAILED

    def fetch_extra_works(self) -> list:
        """获取拓展评定歌曲列表"""
        data = self._request_json(
            "POST",
            self.extra_list_url + f"?csrf_token={self.csrf}",
            "拓展列表",
            data=self._encrypt_params({"csrf_token": self.csrf}),
            headers=self.headers,
        )
        code = data.get("code")
        if code != 200:
            if code is None:
                raise UnknownResponse("拓展列表响应缺少 code")
            raise RequestFailure(f"拓展列表失败: code={code}, message={_safe_message(data)}")
        works = data.get("data")
        if not isinstance(works, list):
            raise UnknownResponse("拓展列表响应 data 不是列表")
        return works

    def fetch_today_record(self) -> dict | None:
        """读取今日评定记录。

        extra/wait/evaluate/work/list 是展示候选列表；真实完成口径以记录接口的
        completeCount/taskIntegral 更可靠。
        """
        data = self._request_json("GET", self.record_url, "今日评定记录", headers=self.headers)
        code = data.get("code")
        if code != 200:
            if code is None:
                raise UnknownResponse("今日评定记录响应缺少 code")
            raise RequestFailure(f"今日评定记录失败: code={code}, message={_safe_message(data)}")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise UnknownResponse("今日评定记录响应缺少 data")
        week_list = payload.get("weekList")
        if week_list is None:
            raise UnknownResponse("今日评定记录响应缺少 weekList")
        if not isinstance(week_list, list):
            raise UnknownResponse("今日评定记录 weekList 结构未知")

        today = datetime.now().strftime("%Y-%m-%d")
        for week in week_list:
            if not isinstance(week, dict):
                raise UnknownResponse("今日评定记录 week 结构未知")
            records = week.get("records") or []
            if not isinstance(records, list):
                raise UnknownResponse("今日评定记录 records 结构未知")
            for record in records:
                if not isinstance(record, dict):
                    raise UnknownResponse("今日评定记录条目结构未知")
                ts = record.get("date")
                if not ts:
                    continue
                record_date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                if record_date == today:
                    return record
        return None

    def report_resource_interact(self, work: dict) -> TaskStatus:
        """上报拓展评定资源互动。

        App 在拓展评分前会先调用该接口；缺少这一步时，评分接口可能返回 200
        但不给拓展积分。
        """
        payload = {
            "workId": work["id"],
            "resourceId": work.get("resourceId"),
            "bizResourceId": "",
            "interactType": "PLAY_END",
            "csrf_token": self.csrf,
        }

        result = self._request_json(
            "POST",
            f"{self.interact_url}?csrf_token={self.csrf}",
            "资源互动上报",
            data=self._encrypt_params(payload),
            headers=self.headers,
        )
        code = result.get("code")
        data = result.get("data")
        if code is None:
            return TaskStatus.UNKNOWN
        if code != 200:
            log.warning("资源互动上报未成功 [account=%s code=%s message=%s]", self.account_id, code, _safe_message(result))
            return TaskStatus.FAILED
        if not isinstance(data, dict) or "interactResult" not in data:
            return TaskStatus.UNKNOWN
        if data.get("interactResult") is True:
            return TaskStatus.SUCCESS
        log.warning("资源互动上报未成功 [account=%s code=%s message=%s]", self.account_id, code, _safe_message(result))
        return TaskStatus.FAILED

    def rate_extra_work(self, task_id: str, work: dict) -> TaskStatus:
        """为一首拓展评定歌曲评分。

        抓包显示拓展评定同样使用 weapi/work/evaluate，但 payload 与基础评定不同：
        分数为 4，标签为空，并且提交前需要 resource/interact/report。

        Returns:
        - "ok": 评分成功
        - "no_more": 服务端明确表示今日无更多可评定歌曲
        - "fail": 其他失败
        """
        interact_status = self.report_resource_interact(work)
        if interact_status is not TaskStatus.SUCCESS:
            return interact_status
        self._sleep_random("interact", "播放结束上报后提交评分")

        support_types = work.get("supportExtraEvaTypes") or [1, 2, 3]
        rating = self._random_rating(support_types)
        extra_score = rating["extraScore"] or {str(t): int(rating["score"]) for t in support_types}
        task_payload = {
            "taskId": int(task_id),
            "workId": work["id"],
            "score": int(rating["score"]),
            "tags": rating["tags"],
            "customTags": "[]",
            "comment": "",
            "extraResource": True,
            "syncYunCircle": False,
            "syncComment": True,
            "extraScore": extra_score,
            "source": "mp-music-partner",
            "csrf_token": self.csrf,
        }

        result = self._request_json(
            "POST",
            f"{self.sign_url}?csrf_token={self.csrf}",
            "拓展评分",
            data=self._encrypt_params(task_payload),
            headers=self.headers,
        )
        code = result.get("code")
        data = result.get("data")
        if code == 200:
            if not isinstance(data, dict) or "evaluateRes" not in data:
                return TaskStatus.UNKNOWN
            if data.get("evaluateRes") is True:
                toast = data.get("successToast")
                if toast:
                    log.info("    %s", redact_sensitive(toast))
                return TaskStatus.SUCCESS
            return TaskStatus.FAILED
        if code == 405 and "没有更多" in str(result.get("message", "")):
            log.info("服务端明确表示今日无更多可评定歌曲")
            return TaskStatus.NO_WORK
        if code is None:
            return TaskStatus.UNKNOWN
        log.warning("拓展评分未成功 [account=%s code=%s message=%s]", self.account_id, code, _safe_message(result))
        return TaskStatus.FAILED

    # 每日积分上限 (基础 8 + 拓展 15 = 23)
    DAILY_CAP = 23

    def _verify_completion(self, basic_target: int, extend_target: int) -> bool:
        """只读复查服务端状态，避免把“提交过”误报成“任务已完成”。"""
        task = self.fetch_task()
        works = task["works"]
        basic_completed = bool(
            task.get("completedCount", 0) >= basic_target
            or (works and all(item.get("completed") for item in works))
        )
        if not works and basic_target == 0:
            basic_completed = True
        if not basic_completed:
            return False
        if extend_target <= 0:
            return True
        record = self.fetch_today_record()
        if not record:
            return False
        complete_count = record.get("completeCount")
        if not isinstance(complete_count, (int, float)):
            raise UnknownResponse("完成复查记录缺少 completeCount")
        extra_done = max(0, complete_count - basic_target)
        return bool(record.get("taskCompleted") and extra_done >= extend_target)

    def run(self) -> TaskStatus:
        try:
            return self._run_impl()
        except UnknownResponse as exc:
            log.error("任务状态无法判断 [account=%s]: %s", self.account_id, redact_sensitive(exc))
            return TaskStatus.UNKNOWN
        except PartnerError as exc:
            log.error("任务执行失败 [account=%s]: %s", self.account_id, redact_sensitive(exc))
            return TaskStatus.FAILED
        except Exception as exc:
            log.error("任务执行异常 [account=%s]: %s", self.account_id, redact_sensitive(exc))
            return TaskStatus.FAILED

    def _run_impl(self) -> TaskStatus:
        """主流程 — 支持多轮评分直到达到每日上限"""
        log.info("=" * 50)
        log.info("网易云音乐合伙人 — 自动评分开始")

        total_success = 0
        total_fail = 0
        no_more_extra = False
        tried_work_ids = set()  # 避免对同一首歌反复尝试

        task_data = self.fetch_task()

        integral = task_data.get("integral", 0)
        completed_count = task_data.get("completedCount", 0)
        works = task_data.get("works", [])
        task_id = task_data.get("id", "")
        rec_resources = task_data.get("recResources", [])

        score_limit = task_data.get("dailyTaskScoreLimit", {})
        basic = score_limit.get("dailyBasicTaskScore", 8)
        extend = score_limit.get("dailyMaxExtendEvaluateScore", 15)
        basic_target = len(works) or 5

        user_name = self._get_user_name()
        log.info("当前帐号: %s | 评定目标: %d分 (基础%d + 拓展%d)", user_name, basic + extend, basic, extend)
        log.info("-" * 50)
        log.info("基础任务状态: %s (%d/%d) | 基础积分: %s",
                 "已完成" if completed_count >= basic_target else "未完成",
                 min(completed_count, basic_target),
                 basic_target,
                 integral)

        # 1. 基础评定
        uncompleted = [w for w in works if not w.get("completed")]
        if uncompleted:
            log.info("--- 基础评定: %d 首 ---", len(uncompleted))
            for i, w in enumerate(uncompleted):
                work = w["work"]
                result = self.rate_work(task_id, work["id"], work.get("supportExtraEvaTypes"))
                tried_work_ids.add(work["id"])
                if result is TaskStatus.SUCCESS:
                    total_success += 1
                    log.info("  [%d/%d] %s (%s) — 评分成功",
                             i + 1, len(uncompleted), work["name"], work["authorName"])
                elif result is TaskStatus.UNKNOWN:
                    log.error("基础评分响应无法判断，停止当前帐号")
                    return TaskStatus.UNKNOWN
                else:
                    total_fail += 1
                    log.error("  [%d/%d] %s (%s) — 评分失败",
                              i + 1, len(uncompleted), work["name"], work["authorName"])
                self._sleep_random("basic", "继续下一首基础评定")

        # 刷新状态
        task_data = self.fetch_task()
        integral = task_data.get("integral", 0)
        rec_resources = task_data.get("recResources", [])

        # 2. 拓展评定 — 从 extra/wait/evaluate/work/list 获取
        extra_works = self.fetch_extra_works()
        extra_done = sum(1 for item in extra_works if item.get("completed"))
        pending_extra = [w for w in extra_works if not w.get("completed")]
        basic_work_count = basic_target
        today_record = self.fetch_today_record()
        record_complete_count = today_record.get("completeCount", 0) if today_record else 0
        record_integral = today_record.get("taskIntegral") if today_record else None
        record_extra_done = max(0, min(extend, record_complete_count - basic_work_count))
        record_completed = bool(today_record and today_record.get("taskCompleted") and record_extra_done >= extend)
        no_more_extra = self._is_extra_no_more_today() or record_completed
        cached_done = self._get_extra_done_today()
        if today_record is not None:
            service_done_count = record_extra_done
        elif cached_done is not None:
            service_done_count = cached_done
        else:
            service_done_count = extra_done
        estimated_score = min(basic + service_done_count, basic + extend)
        if record_completed:
            self._mark_extra_no_more_today(service_done_count)

        if extra_works:
            if no_more_extra:
                total_record_text = (
                    f" | 今日记录总积分: {record_integral}"
                    if record_integral is not None
                    else ""
                )
                if record_integral is not None:
                    log.info("拓展任务状态: 已完成 (%d/%d)%s", service_done_count, extend, total_record_text)
                else:
                    log.info("拓展任务状态: 已完成 (%d/%d)", service_done_count, service_done_count)
                log.info(
                    "接口说明: 拓展列表仍返回%d项，其中%d项标记已评；该列表不作为完成进度",
                    len(extra_works),
                    extra_done,
                )
            else:
                log.info(
                    "拓展任务状态: 进行中 (%d/%d) | 评定积分约 %d/%d",
                    service_done_count,
                    extend,
                    estimated_score,
                    basic + extend,
                )
                log.info(
                    "接口说明: 拓展列表返回%d项，其中%d项标记已评，%d项仍在展示队列",
                    len(extra_works),
                    extra_done,
                    len(pending_extra),
                )

        if no_more_extra:
            log.info("执行策略: 今日评定数量已达上限(%d首)，忽略接口残留队列，跳过提交", basic_work_count + service_done_count)
        elif extra_done < extend:
            if pending_extra:
                log.info("--- 拓展评定: %d 个未评展示项 ---", len(pending_extra))
                for i, ew in enumerate(pending_extra):
                    if extra_done >= extend:
                        log.info("已达到每日拓展积分上限")
                        break

                    work = ew["work"]
                    if work["id"] in tried_work_ids:
                        continue

                    self._sleep_random("extra", "模拟听歌间隔后开始下一首拓展评定")
                    result = self.rate_extra_work(task_id, work)
                    tried_work_ids.add(work["id"])

                    if result is TaskStatus.SUCCESS:
                        total_success += 1
                        extra_done += 1
                        service_done_count = max(service_done_count, extra_done)
                        estimated_score = min(basic + service_done_count, basic + extend)
                        log.info("  [%d/%d] %s (%s) — 拓展评分成功，拓展进度 %d/%d，估算总分 %d/%d",
                                 i + 1, len(pending_extra), work["name"], work["authorName"],
                                 service_done_count, extend, estimated_score, basic + extend)
                    elif result is TaskStatus.NO_WORK:
                        no_more_extra = True
                        service_done_count = extra_done
                        self._mark_extra_no_more_today(service_done_count)
                        log.info("服务端已无更多可评定歌曲，停止拓展评定")
                        break
                    elif result is TaskStatus.UNKNOWN:
                        log.error("拓展评分响应无法判断，停止当前帐号")
                        return TaskStatus.UNKNOWN
                    else:
                        total_fail += 1
                        log.warning("  [%d/%d] %s (%s) — 拓展评分失败",
                                   i + 1, len(pending_extra), work["name"], work["authorName"])
                        log.warning("首个拓展评分失败，停止本轮以避免连续异常请求")
                        break
            else:
                log.info("拓展评定: 无待评分歌曲")

        log.info("-" * 50)
        log.info("运行总结: 成功提交 %d 首 | 异常失败 %d 首", total_success, total_fail)
        if total_fail:
            return TaskStatus.FAILED
        if total_success:
            if self._verify_completion(basic_target, extend):
                return TaskStatus.SUCCESS
            if no_more_extra:
                log.info("服务端明确无更多任务，但尚未确认达到完成目标 [account=%s]", self.account_id)
                return TaskStatus.NO_WORK
            log.error("本轮提交成功但服务端尚未确认任务完成 [account=%s]", self.account_id)
            return TaskStatus.UNKNOWN
        return TaskStatus.NO_WORK


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="网易云音乐合伙人自动评分")
    parser.add_argument(
        "--config", "-c",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "copartner_ck.json"),
        help="配置文件路径 (默认: copartner_ck.json)",
    )
    args = parser.parse_args(argv)

    # 读取配置文件
    config_path = args.config
    if not os.path.exists(config_path):
        log.error("配置文件不存在: %s", os.path.basename(config_path))
        log.error("请将 copartner_ck.json.example 复制为 copartner_ck.json 并填入你的 cookie")
        return 1

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("配置文件读取失败: %s", redact_sensitive(exc))
        return 1
    if not isinstance(config, dict):
        log.error("配置文件结构无效")
        return 1

    # 支持多帐号
    accounts = config.get("MUSIC_COPARTNER", [])
    if not accounts:
        log.error("配置文件中没有找到 MUSIC_COPARTNER 数据")
        return 1

    all_ok = True
    for idx, account in enumerate(accounts):
        if not isinstance(account, dict):
            log.error("第 %d 个帐号配置结构无效", idx + 1)
            all_ok = False
            continue
        cookie = account.get("cookie", "")
        if not cookie:
            log.error("第 %d 个帐号的 cookie 为空，跳过", idx + 1)
            all_ok = False
            continue

        if len(accounts) > 1:
            log.info("\n>>> 处理第 %d/%d 个帐号 <<<", idx + 1, len(accounts))

        try:
            partner = MusicPartner(cookie, account.get("delay"))
            status = partner.run()
            log.info("帐号 %d 结果: %s", idx + 1, status.value)
            if status not in (TaskStatus.SUCCESS, TaskStatus.NO_WORK):
                all_ok = False
        except Exception as e:
            log.error("帐号 %d 执行异常: %s", idx + 1, redact_sensitive(e))
            all_ok = False

    log.info("全部帐号处理完毕")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
