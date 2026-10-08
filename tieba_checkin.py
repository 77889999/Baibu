#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度贴吧自动签到（多账号版）
在原版基础上改造：TIEBA_BDUSS 支持用英文逗号分隔多个 BDUSS，
脚本会依次登录每个账号签到，最后合并成一条 PushPlus 推送。
其余逻辑（接口、签名、节流、脱敏、重试）与原版完全一致。
"""
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ==================== 常量：真实接口 ====================
SIGN_KEY = "tiebaclient!!!"
TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
LIKE_URL = "https://c.tieba.baidu.com/c/f/forum/like"
SIGN_URL = "https://c.tieba.baidu.com/c/c/forum/sign"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/95.0.4638.69 Safari/537.36"
)
CLIENT_ID = "wappc_1534235498291_488"
CLIENT_VERSION = "9.7.8.0"
BJ_TZ = timezone(timedelta(hours=8))
TIMEOUT = 15

MIN_DELAY = float(os.environ.get("TIEBA_MIN_DELAY", "0.3"))
MAX_DELAY = float(os.environ.get("TIEBA_MAX_DELAY", "0.8"))
REST_EVERY = int(os.environ.get("TIEBA_REST_EVERY", "30"))
REST_MIN = float(os.environ.get("TIEBA_REST_MIN", "2"))
REST_MAX = float(os.environ.get("TIEBA_REST_MAX", "4"))
SHOW_NAMES = os.environ.get("TIEBA_LOG_NAMES", "").strip().lower() in ("1", "true", "yes", "on")


def now_bj() -> str:
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_bj()}] {msg}", flush=True)


def fmt_duration(sec: float) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h} 小时 {m} 分 {s} 秒"
    return f"{m} 分 {s} 秒"


# ==================== 脱敏 ====================
_SENSITIVE_KEYS = {
    "bduss", "stoken", "tbs", "token", "cookie", "password",
    "secret", "authorization", "session", "ptoken",
}


def sanitize(value):
    if isinstance(value, dict):
        return {
            k: ("<已脱敏>" if str(k).lower() in _SENSITIVE_KEYS else sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def mask_cred(value: str) -> str:
    if not value:
        return "<未配置>"
    return f"***(len={len(value)})"


def short_fp(name: str) -> str:
    if not name:
        return "--------"
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def tag(name: str, idx: int, total: int) -> str:
    if SHOW_NAMES and name:
        return f"【{name}】({idx}/{total})"
    return f"[{idx}/{total} fp={short_fp(name)}]"


# ==================== HTTP ====================
def http_json(url, data=None, cookie=None):
    headers = {"User-Agent": USER_AGENT}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw), resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(raw), e.code
        except json.JSONDecodeError:
            return None, e.code
    except Exception as e:
        log(f"  请求异常：{type(e).__name__}: {e}")
        return None, 0


def request_with_retry(url, data=None, cookie=None, retry=3):
    for i in range(retry):
        result, status = http_json(url, data, cookie)
        if result is not None:
            return result
        if i < retry - 1:
            wait = 1.5 * (2 ** i) + random.uniform(0, 1)
            time.sleep(wait)
    return None


# ==================== 签名 ====================
def sign(data: dict) -> str:
    raw = "".join(f"{k}={data[k]}" for k in sorted(data)) + SIGN_KEY
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def signed(data: dict) -> dict:
    out = dict(data)
    out["sign"] = sign(out)
    return out


# ==================== 业务 ====================
class TiebaClient:
    def __init__(self, bduss: str):
        self.bduss = bduss
        self.cookie = f"BDUSS={bduss}"

    def get_tbs(self):
        result = request_with_retry(TBS_URL, cookie=self.cookie)
        if not result:
            return None
        return result.get("tbs", "")

    def get_favorites(self):
        forums, page_no = [], 1
        while True:
            data = signed({
                "BDUSS": self.bduss,
                "_client_type": "2",
                "_client_id": CLIENT_ID,
                "_client_version": CLIENT_VERSION,
                "_phone_imei": "000000000000000",
                "from": "1008621y",
                "page_no": str(page_no),
                "page_size": "200",
                "model": "MI+5",
                "net_type": "1",
                "timestamp": str(int(time.time())),
                "vcode_tag": "11",
            })
            result = request_with_retry(LIKE_URL, data)
            if not result:
                log("  获取贴吧列表失败，停止翻页")
                break
            forum_list = result.get("forum_list") or {}
            for key in ("non-gconforum", "gconforum"):
                items = forum_list.get(key, [])
                if isinstance(items, list):
                    forums.extend(items)
                elif isinstance(items, dict):
                    forums.append(items)
            if result.get("has_more") != "1":
                break
            page_no += 1
            time.sleep(random.uniform(1, 2))
        return forums

    def sign_forum(self, fid: str, name: str, tbs: str) -> dict:
        data = signed({
            "BDUSS": self.bduss,
            "_client_type": "2",
            "_client_version": CLIENT_VERSION,
            "_phone_imei": "000000000000000",
            "model": "MI+5",
            "net_type": "1",
            "fid": fid,
            "kw": name,
            "tbs": tbs,
            "timestamp": str(int(time.time())),
        })
        result = request_with_retry(SIGN_URL, data)
        if not result:
            return {"status": "error", "rank": None, "message": "网络请求失败"}
        code = str(result.get("error_code", ""))
        msg = result.get("error_msg", "")
        if code == "0":
            rank = (result.get("user_info") or {}).get("user_sign_rank")
            return {"status": "success", "rank": int(rank) if rank else None,
                    "message": "签到成功"}
        if code == "160002":
            return {"status": "exist", "rank": None, "message": msg or "今日已签到"}
        if code == "340006":
            return {"status": "shield", "rank": None, "message": "贴吧已被屏蔽"}
        return {"status": "error", "rank": None,
                "message": f"{msg or '未知错误'}(code={code})"}


# ==================== 状态文件（首次认证日期） ====================
def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def auth_date(bduss: str, path: str) -> str:
    fp = hashlib.sha256(bduss.encode("utf-8")).hexdigest()[:12]
    state = load_state(path)
    creds = state.get("credentials") or {}
    if creds.get(fp):
        return creds[fp]
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    creds[fp] = today
    state["credentials"] = creds
    save_state(path, state)
    return today


# ==================== 推送 ====================
def build_footer(bduss_list, state_path: str) -> str:
    lines = []
    if os.environ.get("GITHUB_ACTIONS") == "true":
        wf = os.environ.get("GITHUB_WORKFLOW") or ""
        lines.append(f"来源：GitHub Actions{' · ' + wf if wf else ''}")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if repo and run_id:
            base = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            lines.append(f"运行记录：{base}/{repo}/actions/runs/{run_id}")
    else:
        lines.append("来源：本地运行")
    dates = [auth_date(b, state_path) for b in bduss_list if b]
    if dates:
        lines.append(f"Token 认证日期：{', '.join(sorted(set(dates)))}")
    return "\n".join(lines)


def push_notify(title: str, content: str, bduss_list, state_path: str) -> bool:
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    body = f"{content}\n\n{'-' * 22}\n{build_footer(bduss_list, state_path)}"
    log(f"推送正文:\n{body}")
    if not token:
        log("[推送] 未配置 PUSHPLUS_TOKEN，跳过推送")
        return False
    result, status = http_json(
        "https://www.pushplus.plus/send",
        data={"token": token, "title": title, "content": body, "template": "txt"},
    )
    ok = status == 200 and isinstance(result, dict) and result.get("code") == 200
    log(f"[推送{'成功' if ok else '失败'}] {title}"
        + ("" if ok else f" resp={sanitize(result)}"))
    return ok


# ==================== 单账号签到 ====================
def sign_one_account(bduss: str, account_label: str, state_path: str):
    """跑一个账号的完整签到流程。返回 dict 汇总结果。"""
    log(f"--- {account_label} 开始（BDUSS={mask_cred(bduss)}）---")
    started = time.time()
    client = TiebaClient(bduss)

    t0 = time.time()
    tbs = client.get_tbs()
    if not tbs:
        log(f"{account_label} 获取 tbs 失败，BDUSS 可能已失效")
        return {
            "label": account_label, "ok": False, "reason": "BDUSS 失效",
            "total": 0, "success": 0, "exist": 0, "shield": 0, "error": 0,
            "failed_fp": [], "elapsed": time.time() - started,
        }
    log(f"{account_label} tbs 获取成功（{fmt_duration(time.time() - t0)}）")

    forums = client.get_favorites()
    if not forums:
        log(f"{account_label} 未获取到关注的贴吧")
        return {
            "label": account_label, "ok": False, "reason": "未取到贴吧列表",
            "total": 0, "success": 0, "exist": 0, "shield": 0, "error": 0,
            "failed_fp": [], "elapsed": time.time() - started,
        }
    log(f"{account_label} 共获取到 {len(forums)} 个关注的贴吧")

    total = len(forums)
    stats = {"success": 0, "exist": 0, "shield": 0, "error": 0}
    failed = []
    est = total * ((MIN_DELAY + MAX_DELAY) / 2 + 1.5) / 60
    log(f"{account_label} 开始第 1 轮签到，共 {total} 个（预计约 {est:.0f} 分钟）")

    for idx, forum in enumerate(forums):
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        if (idx + 1) % REST_EVERY == 0:
            rest = random.uniform(REST_MIN, REST_MAX)
            log(f"  {account_label} 已签 {idx + 1}/{total}，休息 {rest:.1f}s")
            time.sleep(rest)
        name = forum.get("name", "")
        res = client.sign_forum(forum.get("id", ""), name, tbs)
        stats[res["status"]] += 1
        mark = tag(name, idx + 1, total)
        if res["status"] == "success":
            rank_str = f"，第 {res['rank']} 个签到" if res["rank"] else ""
            log(f"  {account_label} {mark} 签到成功{rank_str}")
        elif res["status"] == "exist":
            log(f"  {account_label} {mark} 已签到")
        elif res["status"] == "shield":
            log(f"  {account_label} {mark} 被屏蔽")
        else:
            log(f"  {account_label} {mark} 失败：{res['message']}")
            failed.append(forum)

    # 第二轮重试
    final_failed = []
    if failed:
        log(f"{account_label} 第 1 轮结束，{len(failed)} 个失败，15s 后刷新 tbs 重试")
        time.sleep(15)
        new_tbs = client.get_tbs()
        if new_tbs:
            tbs = new_tbs
        for forum in failed:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            name = forum.get("name", "")
            res = client.sign_forum(forum.get("id", ""), name, tbs)
            mark = tag(name, 0, 0)
            if res["status"] == "success":
                stats["error"] -= 1
                stats["success"] += 1
                log(f"  {account_label} 重试 {mark} 成功")
            elif res["status"] == "exist":
                stats["error"] -= 1
                stats["exist"] += 1
                log(f"  {account_label} 重试 {mark} 已签到")
            elif res["status"] == "shield":
                stats["error"] -= 1
                stats["shield"] += 1
                log(f"  {account_label} 重试 {mark} 被屏蔽")
            else:
                final_failed.append(short_fp(name))
                log(f"  {account_label} 重试 {mark} 仍失败：{res['message']}")

    elapsed = time.time() - started
    per = elapsed / total if total else 0
    log(f"{account_label} 完成：成功 {stats['success']} / 已签 {stats['exist']} / "
        f"屏蔽 {stats['shield']} / 失败 {stats['error']}，耗时 {fmt_duration(elapsed)}")

    return {
        "label": account_label, "ok": True, "reason": "",
        "total": total, "success": stats["success"], "exist": stats["exist"],
        "shield": stats["shield"], "error": stats["error"],
        "failed_fp": final_failed, "elapsed": elapsed, "per": per,
    }


# ==================== 主流程 ====================
def main() -> int:
    log("=== 百度贴吧自动签到（多账号版） ===")
    raw = os.environ.get("TIEBA_BDUSS", "").strip()
    state_path = os.environ.get("TIEBA_STATE_FILE", ".tieba-state.json")

    if not raw:
        push_notify("❌ 贴吧签到失败", "未配置 TIEBA_BDUSS，请在仓库 Secrets 中添加。",
                    [], state_path)
        return 1

    # 支持用英文逗号分隔多个 BDUSS
    bduss_list = [b.strip() for b in raw.split(",") if b.strip()]
    log(f"共 {len(bduss_list)} 个账号待签到")

    all_results = []
    total_started = time.time()
    for i, bduss in enumerate(bduss_list, 1):
        label = f"账号{i}" if len(bduss_list) > 1 else "账号"
        r = sign_one_account(bduss, label, state_path)
        all_results.append((bduss, r))
        # 账号之间间隔一下，别连着打
        if i < len(bduss_list):
            gap = random.uniform(5, 10)
            log(f"账号切换，休息 {gap:.1f}s")
            time.sleep(gap)

    total_elapsed = time.time() - total_started

    # 汇总推送内容
    lines = []
    grand = {"total": 0, "success": 0, "exist": 0, "shield": 0, "error": 0}
    any_error = False
    any_invalid = False
    for bduss, r in all_results:
        if not r["ok"]:
            any_invalid = True
            lines.append(f"【{r['label']}】失败：{r['reason']}")
            continue
        grand["total"] += r["total"]
        grand["success"] += r["success"]
        grand["exist"] += r["exist"]
        grand["shield"] += r["shield"]
        grand["error"] += r["error"]
        head = f"【{r['label']}】共 {r['total']} 个：成功 {r['success']}，已签 {r['exist']}，屏蔽 {r['shield']}，失败 {r['error']}"
        lines.append(head)
        if r["error"] > 0:
            any_error = True
        if r["failed_fp"]:
            lines.append(f"  重试失败指纹：{', '.join(r['failed_fp'])}")

    lines.append("")
    lines.append(f"合计：成功 {grand['success']} / 已签 {grand['exist']} / "
                 f"屏蔽 {grand['shield']} / 失败 {grand['error']}")
    lines.append(f"总耗时：{fmt_duration(total_elapsed)}")
    lines.append(f"时间：{now_bj()}")

    summary = "\n".join(lines)
    log("========== 全部账号汇总 ==========\n" + summary + "\n================================")

    push_bduss = [b for b, _ in all_results]
    if any_invalid:
        push_notify("❌ 贴吧签到异常", summary, push_bduss, state_path)
        return 1
    if any_error:
        push_notify("⚠️ 贴吧签到有失败", summary, push_bduss, state_path)
        return 1
    push_notify("✅ 贴吧签到完成", summary, push_bduss, state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
