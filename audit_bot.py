import datetime
import json
import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore
import pytz
import requests

TZ = pytz.timezone("Asia/Taipei")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.environ.get("LINE_USER_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "serviceAccountKey.json").strip()
COLLECTION_NAME = os.environ.get("FIRESTORE_COLLECTION", "expenses").strip()


def parse_to_taipei(date_val) -> datetime.datetime | None:
    """將 Firestore 的 date（ISO 字串或 datetime）轉為台灣時間 (Asia/Taipei)"""
    if not date_val:
        return None

    if isinstance(date_val, datetime.datetime):
        dt = date_val
    else:
        s = str(date_val).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)

    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)

    return dt.astimezone(TZ)


def resolve_audit_cutoff(now_tw: datetime.datetime) -> datetime.datetime:
    """
    決定結算截止時間（台灣時區 23:00）。
    若排程延遲至隔日凌晨（00:00 ~ 05:59）才執行，自動回推前一天的 23:00。
    """
    if now_tw.hour < 6:
        report_date = (now_tw - datetime.timedelta(days=1)).date()
    else:
        report_date = now_tw.date()

    naive_cutoff = datetime.datetime.combine(report_date, datetime.time(23, 0, 0))
    return TZ.localize(naive_cutoff)


def calc_stats_like_app(records: list[dict], cutoff_dt: datetime.datetime) -> dict:
    """對齊記帳本統計規則（截至 cutoff_dt 為止）：支出為負、收入為正；分類統計加總金額"""
    today = 0.0
    week = 0.0
    month = 0.0
    category_map: dict[str, float] = {}

    for r in records:
        dt_tw = parse_to_taipei(r.get("date"))
        if not dt_tw:
            continue

        # 僅統計截至結算時間（例如 23:00）以前的紀錄
        if dt_tw > cutoff_dt:
            continue

        try:
            amt = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            continue

        value = -amt if r.get("type") == "expense" else amt

        # 今日（結算基準日同一天）
        if dt_tw.date() == cutoff_dt.date():
            today += value

        # 本週（與記帳本一致：截至 cutoff_dt 前 7 天內）
        diff_seconds = (cutoff_dt - dt_tw).total_seconds()
        if 0 <= diff_seconds < 7 * 86400:
            week += value

        # 本月（結算基準日同年同月）
        if dt_tw.year == cutoff_dt.year and dt_tw.month == cutoff_dt.month:
            month += value

        # 分類統計
        cat = str(r.get("category") or "其他").strip() or "其他"
        category_map[cat] = category_map.get(cat, 0.0) + amt

    return {
        "today": today,
        "week": week,
        "month": month,
        "category_map": category_map,
    }


def format_report(stats: dict, cutoff_dt: datetime.datetime, now_tw: datetime.datetime) -> str:
    """排版對齊記帳本畫面的統計與分類統計"""
    date_label = cutoff_dt.strftime("%Y-%m-%d")
    month_label = cutoff_dt.strftime("%Y-%m")
    lines = [
        f"統計 截至 {date_label} 23:00 止",
        f"今日：NT$ {stats['today']:g}",
        f"本週：NT$ {stats['week']:g}",
        f"{month_label} 結餘：NT$ {stats['month']:g}",
        "",
        "分類統計",
    ]

    if stats["category_map"]:
        for cat, val in stats["category_map"].items():
            lines.append(f"{cat}：NT$ {val:g}")
    else:
        lines.append("尚無資料")

    # 若實際執行時間晚於結算時間超過 15 分鐘，附註實際產生時間方便辨識
    delay_minutes = (now_tw - cutoff_dt).total_seconds() / 60
    if delay_minutes > 15:
        lines.append("")
        lines.append(f"（註：此報表於 {now_tw.strftime('%Y-%m-%d %H:%M')} 延遲產出）")

    return "\n".join(lines)


def send_line_push(text: str) -> None:
    """發送 LINE Messaging API 推播；若尚未設定 Token/User ID 則僅在終端機印出"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("⚠️ 尚未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID，僅在終端機預覽訊息：")
        print("-" * 40)
        print(text)
        print("-" * 40)
        return

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code >= 400:
        print(f"❌ LINE 推播失敗（HTTP {resp.status_code}）：{resp.text}", file=sys.stderr)
        resp.raise_for_status()

    print("✅ LINE 推播成功！")


def init_firestore(service_account_path: str, service_account_json: str = ""):
    if not firebase_admin._apps:
        if service_account_json:
            cert_dict = json.loads(service_account_json)
            cred = credentials.Certificate(cert_dict)
        else:
            if not os.path.exists(service_account_path):
                raise FileNotFoundError(
                    f"找不到 Firebase 金鑰檔案：{service_account_path}\n"
                    "請設定 FIREBASE_SERVICE_ACCOUNT_JSON 環境變數，或放置 serviceAccountKey.json。"
                )
            cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def main():
    db = init_firestore(SERVICE_ACCOUNT_PATH, SERVICE_ACCOUNT_JSON)
    docs = db.collection(COLLECTION_NAME).stream()
    records = [doc.to_dict() for doc in docs if doc.exists]

    now_tw = datetime.datetime.now(TZ)
    cutoff_dt = resolve_audit_cutoff(now_tw)
    stats = calc_stats_like_app(records, cutoff_dt)
    msg = format_report(stats, cutoff_dt, now_tw)
    send_line_push(msg)


if __name__ == "__main__":
    main()
