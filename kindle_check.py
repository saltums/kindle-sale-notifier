"""
Kindle Sale Notifier - Amazon.co.jp の登録著者/シリーズのセールを検知してLINE通知
Playwright を使用してブラウザ経由でアクセス（ボット検知回避）
"""
import json
import os
import re
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
CACHE_FILE = BASE_DIR / "prices_cache.json"
LOG_FILE = BASE_DIR / "check_log.txt"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {CONFIG_FILE}")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def parse_price(text: str) -> int | None:
    cleaned = text.replace("￥", "").replace(",", "").replace("円", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def search_kindle_playwright(page, query: str) -> list[dict]:
    url = f"https://www.amazon.co.jp/s?k={requests.utils.quote(query)}&i=digital-text&language=ja_JP"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(random.randint(1500, 3000))

        # CAPTCHA チェック
        if "api-services-support@amazon.com" in page.content() or page.locator("input#captchacharacters").count() > 0:
            log(f"  ⚠ CAPTCHA 検出 - スキップ")
            return []

        items = page.locator('[data-component-type="s-search-result"]').all()
        log(f"  → {len(items)} 件取得")

        books = []
        for item in items:
            try:
                asin = item.get_attribute("data-asin") or ""
                if not asin:
                    continue

                # タイトル取得（複数セレクターを試す）
                title = ""
                for sel in ["h2 .a-link-normal span", "h2 a span", "h2 span"]:
                    el = item.locator(sel).first
                    if el.count() > 0:
                        t = el.inner_text().strip()
                        if t and len(t) > 1:
                            title = t
                            break
                if not title:
                    title = "不明"

                price_el = item.locator(".a-price .a-offscreen").first
                if price_el.count() == 0:
                    continue
                price = parse_price(price_el.inner_text())
                if price is None:
                    continue

                orig_price = None
                for orig_el in item.locator(".a-text-price .a-offscreen").all():
                    orig = parse_price(orig_el.inner_text())
                    if orig and orig > price:
                        orig_price = orig
                        break

                discount_pct = None
                badge_el = item.locator(".savingsPercentage").first
                if badge_el.count() > 0:
                    badge_text = badge_el.inner_text().strip().replace("%", "").replace("-", "")
                    try:
                        discount_pct = int(badge_text)
                    except ValueError:
                        pass

                if discount_pct is None and orig_price and orig_price > 0:
                    discount_pct = int((1 - price / orig_price) * 100)

                # ポイント還元率を取得（例: "500pt (50%還元)"）
                point_pct = None
                point_text = item.inner_text()
                m = re.search(r'(\d+)\s*%\s*還元', point_text)
                if m:
                    point_pct = int(m.group(1))

                books.append({
                    "asin": asin,
                    "title": title,
                    "price": price,
                    "orig_price": orig_price,
                    "discount_pct": discount_pct,
                    "point_pct": point_pct,
                    "url": f"https://www.amazon.co.jp/dp/{asin}",
                })
            except Exception:
                continue

        return books

    except PWTimeout:
        log(f"  タイムアウト: {query}")
        return []
    except Exception as e:
        log(f"  エラー: {e}")
        return []


def send_line_message(token: str, user_id: str, text: str) -> bool:
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}],
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            log(f"LINE API エラー: {resp.status_code} {resp.text}")
            return False
        return True
    except Exception as e:
        log(f"LINE 送信エラー: {e}")
        return False


def build_message(sale_books: list[dict]) -> str:
    now = datetime.now().strftime("%m/%d %H:%M")
    lines = [f"📚 Kindle セール通知 ({now})\n"]

    for book in sale_books[:15]:
        disc = book.get("discount_pct")
        point = book.get("point_pct")
        price = book["price"]
        orig = book.get("orig_price")
        orig_str = f" (元 ¥{orig:,})" if orig else ""

        if disc and disc >= 50:
            label = f"▼{disc}% オフ"
        elif point and point >= 50:
            label = f"🪙 {point}% ポイント還元"
        else:
            label = f"▼{disc or point}%"

        lines.append(
            f"{label}\n"
            f"{book['title']}\n"
            f"¥{price:,}{orig_str}\n"
            f"{book['url']}\n"
        )

    if len(sale_books) > 15:
        lines.append(f"…他 {len(sale_books) - 15} 件")

    return "\n".join(lines)


def check_sales():
    config = load_config()

    line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or config.get("line_channel_access_token", "")
    line_user_id = os.environ.get("LINE_USER_ID") or config.get("line_user_id", "")
    min_discount = config.get("min_discount_pct", 50)

    authors: list[str] = config.get("authors", [])
    series_list: list[str] = config.get("series", [])
    queries = [(a, "著者") for a in authors] + [(s, "シリーズ") for s in series_list]

    if not queries:
        log("著者・シリーズが config.json に登録されていません")
        return

    cache = load_cache()
    today = datetime.now().strftime("%Y-%m-%d")
    sale_books = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Amazon トップを一度開いてセッションを確立
        try:
            page.goto("https://www.amazon.co.jp/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(random.randint(1000, 2000))
        except Exception:
            pass

        for query, query_type in queries:
            log(f"検索中 [{query_type}]: {query}")
            books = search_kindle_playwright(page, query)

            for book in books:
                asin = book["asin"]
                entry = cache.setdefault(asin, {
                    "title": book["title"],
                    "url": book["url"],
                    "query": query,
                    "history": [],
                    "last_notified_price": None,
                })
                entry["title"] = book["title"]
                entry["url"] = book["url"]
                entry["query"] = query

                history: list = entry.setdefault("history", [])
                if not history or history[-1]["date"] != today:
                    history.append({
                        "date": today,
                        "price": book["price"],
                        "orig_price": book.get("orig_price"),
                        "discount_pct": book.get("discount_pct"),
                        "point_pct": book.get("point_pct"),
                    })
                    entry["history"] = history[-90:]

                disc = book.get("discount_pct") or 0
                point = book.get("point_pct") or 0
                # 割引率 OR ポイント還元率が min_discount 以上ならセール対象
                is_sale = disc >= min_discount or point >= min_discount
                if is_sale:
                    if entry.get("last_notified_price") != book["price"]:
                        sale_books.append({"query": query, **book})
                        entry["last_notified_price"] = book["price"]
                else:
                    entry["last_notified_price"] = None

            time.sleep(random.uniform(3, 6))

        browser.close()

    save_cache(cache)

    if not sale_books:
        log(f"セール中の本は見つかりませんでした（対象: {min_discount}%以上割引）")
        return

    log(f"セール検出: {len(sale_books)} 件")

    if not line_token or not line_user_id:
        log("LINE 未設定のため通知スキップ")
        return

    message = build_message(sale_books)
    if send_line_message(line_token, line_user_id, message):
        log(f"LINE 通知送信完了 ({len(sale_books)} 件)")
    else:
        log("LINE 通知の送信に失敗しました")


if __name__ == "__main__":
    try:
        check_sales()
    except FileNotFoundError as e:
        print(f"エラー: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"予期しないエラー: {e}")
        raise
