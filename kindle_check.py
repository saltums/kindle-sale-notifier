"""
Kindle Sale Notifier - Amazon.co.jp の登録著者/シリーズのセールを検知してLINE通知
"""
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
CACHE_FILE = BASE_DIR / "prices_cache.json"
LOG_FILE = BASE_DIR / "check_log.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}


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


def search_kindle(query: str) -> str | None:
    url = "https://www.amazon.co.jp/s"
    params = {
        "k": query,
        "i": "digital-text",
        "s": "price-asc-rank",
        "language": "ja_JP",
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log(f"検索エラー '{query}': {e}")
        return None


def parse_price(text: str) -> int | None:
    """'￥1,234' や '1,234円' などをパースして整数を返す"""
    cleaned = text.replace("￥", "").replace(",", "").replace("円", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_search_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    books = []

    for item in soup.select('[data-component-type="s-search-result"]'):
        try:
            asin = item.get("data-asin", "").strip()
            if not asin:
                continue

            title_el = item.select_one("h2 a span")
            title = title_el.text.strip() if title_el else "不明"

            # 現在価格
            price_el = item.select_one(".a-price .a-offscreen")
            if not price_el:
                continue
            price = parse_price(price_el.text)
            if price is None:
                continue

            # 参考価格（割引前の価格）
            orig_price = None
            for orig_el in item.select(".a-text-price .a-offscreen"):
                orig = parse_price(orig_el.text)
                if orig and orig > price:
                    orig_price = orig
                    break

            # 割引率バッジ（例: "-50%"）
            discount_pct = None
            badge_el = item.select_one(".savingsPercentage")
            if badge_el:
                badge_text = badge_el.text.strip().replace("%", "").replace("-", "")
                try:
                    discount_pct = int(badge_text)
                except ValueError:
                    pass

            if discount_pct is None and orig_price and orig_price > 0:
                discount_pct = int((1 - price / orig_price) * 100)

            books.append(
                {
                    "asin": asin,
                    "title": title,
                    "price": price,
                    "orig_price": orig_price,
                    "discount_pct": discount_pct,
                    "url": f"https://www.amazon.co.jp/dp/{asin}",
                }
            )
        except Exception:
            continue

    return books


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
        disc = book.get("discount_pct", "?")
        price = book["price"]
        orig = book.get("orig_price")
        orig_str = f" (元 ¥{orig:,})" if orig else ""
        lines.append(
            f"▼{disc}% オフ\n"
            f"{book['title']}\n"
            f"¥{price:,}{orig_str}\n"
            f"{book['url']}\n"
        )

    if len(sale_books) > 15:
        lines.append(f"…他 {len(sale_books) - 15} 件")

    return "\n".join(lines)


def check_sales():
    config = load_config()

    # GitHub Secrets (環境変数) を優先、なければ config.json を使用
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
    sale_books = []

    for query, query_type in queries:
        log(f"検索中 [{query_type}]: {query}")
        html = search_kindle(query)
        if not html:
            continue

        books = parse_search_results(html)
        log(f"  → {len(books)} 件取得")

        for book in books:
            disc = book.get("discount_pct")
            if disc is None or disc < min_discount:
                continue

            asin = book["asin"]
            # 同じ価格で通知済みならスキップ
            cached = cache.get(asin, {})
            if cached.get("notified_price") == book["price"]:
                continue

            sale_books.append({"query": query, **book})
            cache[asin] = {
                "notified_price": book["price"],
                "notified_at": datetime.now().isoformat(),
                "title": book["title"],
            }

        # Amazon への連続アクセスを避けるため待機
        time.sleep(random.uniform(3, 7))

    save_cache(cache)

    if not sale_books:
        log(f"セール中の本は見つかりませんでした（対象: {min_discount}%以上割引）")
        return

    log(f"セール検出: {len(sale_books)} 件")

    if not line_token or not line_user_id:
        log("LINE 未設定のため通知スキップ。config.json を確認してください")
        for b in sale_books:
            log(f"  [{b.get('discount_pct')}%OFF] {b['title']} ¥{b['price']}")
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
