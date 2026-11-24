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


def calc_stats_like_app(records: list[dict], now_tw: datetime.datetime) -> dict:
    """對齊記帳本統計規則：支出為負、收入為正；分類統計加總金額"""
    today = 0.0
    week = 0.0
    month = 0.0
    category_map: dict[str, float] = {}

    for r in records:
        dt_tw = parse_to_taipei(r.get("date"))
        if not dt_tw:
            continue

        try:
            amt = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            continue

        value = -amt if r.get("type") == "expense" else amt

        # 今日（台灣時區同一天）
        if dt_tw.date() == now_tw.date():
            today += value

        # 本週（與記帳本一致：7 天內）
        if (now_tw - dt_tw).total_seconds() < 7 * 86400:
            week += value

        # 本月（台灣時區同年同月）
        if dt_tw.year == now_tw.year and dt_tw.month == now_tw.month:
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


def format_report(stats: dict, now_tw: datetime.datetime) -> str:
    """排版對齊記帳本畫面的統計與分類統計"""
    date_label = now_tw.strftime("%Y-%m-%d")
    month_label = now_tw.strftime("%Y-%m")
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
    stats = calc_stats_like_app(records, now_tw)
    msg = format_report(stats, now_tw)
    send_line_push(msg)


if __name__ == "__main__":
    main()
