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
                if price is None or price == 0:  # KU 無料本を除外
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


DASHBOARD_URL = "https://saltums.github.io/kindle-sale-notifier/"


def generate_dashboard(cache: dict):
    """価格データを埋め込んだ静的 HTML を docs/index.html に出力する"""
    docs_dir = BASE_DIR / "docs"
    docs_dir.mkdir(exist_ok=True)

    data_json = json.dumps(cache, ensure_ascii=False)
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kindle 価格トラッカー</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+JP:wght@400;600&family=Inter:wght@400;500;600&display=swap">
<style>
:root{{
  --bg:#111318;--surface:#1c1f2e;--surface-2:#252838;--border:#2f334a;
  --text:#dfe1ed;--muted:#686c92;--accent:#e85d26;--sale:#38c060;
  --font-serif:'Noto Serif JP',serif;--font-sans:'Inter',system-ui,sans-serif;
}}
@media(prefers-color-scheme:light){{
  :root{{--bg:#f0ede8;--surface:#fff;--surface-2:#f7f4ef;--border:#dddad3;--text:#1a1c28;--muted:#7a7d9a;}}
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--font-sans);font-size:13px;line-height:1.4;min-height:100vh}}
header{{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:10}}
.logo{{display:flex;align-items:center;gap:8px;font-family:var(--font-serif);font-weight:600;font-size:15px}}
.logo-dot{{width:7px;height:7px;border-radius:50%;background:var(--accent)}}
.last-updated{{font-size:11px;color:var(--muted)}}
.stats-bar{{display:flex;gap:1px;background:var(--border);border-bottom:1px solid var(--border)}}
.stat{{flex:1;background:var(--surface);padding:10px 16px;display:flex;flex-direction:column;gap:1px}}
.stat-value{{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}}
.stat-value.orange{{color:var(--accent)}}.stat-value.green{{color:var(--sale)}}
.stat-label{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
.toolbar{{display:flex;align-items:center;gap:6px;padding:10px 20px;flex-wrap:wrap;border-bottom:1px solid var(--border);background:var(--surface)}}
.filter-btn{{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:20px;padding:3px 12px;cursor:pointer;font-size:12px;font-family:var(--font-sans);transition:all .12s}}
.filter-btn.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
.filter-btn:hover:not(.active){{border-color:var(--text);color:var(--text)}}
/* ── List layout ── */
.list{{padding:0 20px 24px}}
.list-row{{
  display:grid;
  grid-template-columns:1fr auto;
  gap:8px 16px;
  align-items:center;
  padding:10px 0;
  border-bottom:1px solid var(--border);
  transition:background .1s;
}}
.list-row:hover{{background:var(--surface-2);margin:0 -20px;padding:10px 20px}}
.list-row.on-sale .row-title a{{color:var(--sale)}}
.list-row.on-point .row-title a{{color:#a78bfa}}
.row-left{{display:flex;flex-direction:column;gap:3px;min-width:0}}
.row-title{{font-family:var(--font-serif);font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.row-title a{{color:var(--text);text-decoration:none}}
.row-title a:hover{{text-decoration:underline}}
.row-meta{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.author-tag{{font-size:10px;color:var(--muted);background:var(--surface-2);border:1px solid var(--border);border-radius:3px;padding:1px 5px}}
.row-date{{font-size:10px;color:var(--muted)}}
.row-right{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.price-block{{text-align:right}}
.price-current{{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}}
.price-current.sale{{color:var(--sale)}}.price-current.point{{color:#a78bfa}}
.price-orig{{font-size:11px;color:var(--muted);text-decoration:line-through;font-variant-numeric:tabular-nums}}
.badge{{font-size:10px;font-weight:700;padding:2px 6px;border-radius:3px;white-space:nowrap}}
.badge.sale{{background:var(--sale);color:#fff}}
.badge.point{{background:#7c5cbf;color:#fff}}
.spark{{width:60px;height:22px;flex-shrink:0}}
.dir-up{{color:#f08080;font-size:11px}}.dir-down{{color:var(--sale);font-size:11px}}
.price-sub{{font-size:10px;color:var(--muted);text-align:right}}
.empty-state{{text-align:center;padding:48px 20px;color:var(--muted)}}
.empty-state h2{{font-size:16px;margin-bottom:6px;color:var(--text)}}
@media(max-width:500px){{
  .stats-bar{{flex-wrap:wrap}}.stat{{min-width:50%}}
  .spark{{display:none}}.list{{padding:0 12px 20px}}
  header{{padding:12px 14px}}.toolbar{{padding:8px 12px}}
  .list-row:hover{{margin:0 -12px;padding:10px 12px}}
}}
</style>
</head>
<body>
<header>
  <div class="logo"><div class="logo-dot"></div>Kindle 価格トラッカー</div>
  <span class="last-updated">更新: {updated}</span>
</header>
<div class="stats-bar">
  <div class="stat"><div class="stat-value" id="stat-total">—</div><div class="stat-label">追跡中の本</div></div>
  <div class="stat"><div class="stat-value orange" id="stat-sale">—</div><div class="stat-label">セール中</div></div>
  <div class="stat"><div class="stat-value" id="stat-authors">—</div><div class="stat-label">著者 / シリーズ</div></div>
  <div class="stat"><div class="stat-value green" id="stat-max-disc">—</div><div class="stat-label">最大お得率</div></div>
</div>
<div class="toolbar" id="toolbar"></div>
<div class="list" id="list"></div>
<script>
const DATA = {data_json};
let activeFilter = 'all';

function miniSpark(history, color) {{
  if (!history || history.length < 2) return '';
  const prices = history.slice(-10).map(h => h.price);
  const min = Math.min(...prices), max = Math.max(...prices), range = max - min || 1;
  const W = 60, H = 22, pad = 2;
  const pts = prices.map((p, i) => {{
    const x = pad + (i / (prices.length - 1)) * (W - pad * 2);
    const y = pad + ((max - p) / range) * (H - pad * 2);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }});
  const sc = color === 'sale' ? '#38c060' : color === 'point' ? '#a78bfa' : '#686c92';
  return `<svg class="spark" viewBox="0 0 ${{W}} ${{H}}" preserveAspectRatio="none">
    <polyline points="${{pts.join(' ')}}" fill="none" stroke="${{sc}}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".8"/>
    <circle cx="${{pts[pts.length-1].split(',')[0]}}" cy="${{pts[pts.length-1].split(',')[1]}}" r="2" fill="${{sc}}"/>
  </svg>`;
}}

function render() {{
  const entries = Object.entries(DATA);
  const queries = [...new Set(entries.map(([,v]) => v.query).filter(Boolean))];
  const saleEntries = entries.filter(([,v]) => {{
    const h = v.history?.at(-1);
    return (h?.discount_pct >= 50) || (h?.point_pct >= 50);
  }});
  const maxVal = Math.max(0, ...entries.map(([,v]) => {{
    const h = v.history?.at(-1);
    return Math.max(h?.discount_pct ?? 0, h?.point_pct ?? 0);
  }}));

  document.getElementById('stat-total').textContent = entries.length;
  document.getElementById('stat-sale').textContent = saleEntries.length;
  document.getElementById('stat-authors').textContent = queries.length;
  document.getElementById('stat-max-disc').textContent = maxVal > 0 ? maxVal + '%' : '—';

  const toolbar = document.getElementById('toolbar');
  const filters = [
    {{key:'all', label:'すべて'}},
    {{key:'sale', label:'セール中' + (saleEntries.length ? ' (' + saleEntries.length + ')' : '')}},
    ...queries.map(q => ({{key:'q:' + q, label:q}})),
  ];
  toolbar.innerHTML = filters.map(f =>
    `<button class="filter-btn ${{activeFilter===f.key?'active':''}}" data-filter="${{f.key}}">${{f.label}}</button>`
  ).join('');
  toolbar.querySelectorAll('.filter-btn').forEach(btn =>
    btn.addEventListener('click', () => {{ activeFilter = btn.dataset.filter; render(); }})
  );

  let visible = entries;
  if (activeFilter === 'sale') visible = saleEntries;
  else if (activeFilter.startsWith('q:')) visible = entries.filter(([,v]) => v.query === activeFilter.slice(2));

  if (!visible.length) {{
    document.getElementById('list').innerHTML = `<div class="empty-state"><h2>${{entries.length?'該当なし':'データなし'}}</h2><p>まだ価格データが収集されていません。</p></div>`;
    return;
  }}

  visible.sort(([,a],[,b]) => {{
    const av = Math.max(a.history?.at(-1)?.discount_pct??0, a.history?.at(-1)?.point_pct??0);
    const bv = Math.max(b.history?.at(-1)?.discount_pct??0, b.history?.at(-1)?.point_pct??0);
    return bv - av;
  }});

  document.getElementById('list').innerHTML = visible.map(([asin, book]) => {{
    const last = book.history?.at(-1);
    const prev = book.history?.at(-2);
    const price = last?.price;
    const orig = last?.orig_price;
    const disc = last?.discount_pct ?? 0;
    const point = last?.point_pct ?? 0;
    const isSale = disc >= 50, isPoint = !isSale && point >= 50;
    const priceDir = prev && price !== prev.price ? (price < prev.price ? '↓' : '↑') : '';
    const dirClass = priceDir === '↓' ? 'dir-down' : priceDir === '↑' ? 'dir-up' : '';
    const sparkKind = isSale ? 'sale' : isPoint ? 'point' : 'normal';
    const badges = [];
    if (disc >= 50) badges.push(`<span class="badge sale">▼${{disc}}%</span>`);
    if (point >= 50) badges.push(`<span class="badge point">🪙${{point}}%還元</span>`);
    const badge = badges.join(' ');
    return `<div class="list-row ${{isSale?'on-sale':isPoint?'on-point':''}}">
      <div class="row-left">
        <div class="row-title"><a href="${{book.url}}" target="_blank" rel="noopener">${{book.title||asin}}</a></div>
        <div class="row-meta">
          ${{book.query?`<span class="author-tag">${{book.query}}</span>`:''}}
          ${{last?.date?`<span class="row-date">${{last.date}}</span>`:''}}
        </div>
      </div>
      <div class="row-right">
        ${{miniSpark(book.history, sparkKind)}}
        <div class="price-block">
          <div class="price-current ${{isSale?'sale':isPoint?'point':''}}">
            ${{price!=null?'¥'+price.toLocaleString():'—'}}${{priceDir?`<span class="${{dirClass}}">${{priceDir}}</span>`:''}}
          </div>
          ${{orig&&orig!==price?`<div class="price-orig">¥${{orig.toLocaleString()}}</div>`:''}}
          ${{disc&&disc<50?`<div class="price-sub">▼${{disc}}%</div>`:''}}
          ${{point&&point<50?`<div class="price-sub">🪙${{point}}%還元</div>`:''}}
        </div>
        ${{badge}}
      </div>
    </div>`;
  }}).join('');
}}
render();
</script>
</body>
</html>"""

    out = docs_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    log(f"ダッシュボード生成: {out}")

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
        lines.append(f"…他 {len(sale_books) - 15} 件\n")

    lines.append(f"📊 一覧はこちら\n{DASHBOARD_URL}")

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
                today_entry = {
                    "date": today,
                    "price": book["price"],
                    "orig_price": book.get("orig_price"),
                    "discount_pct": book.get("discount_pct"),
                    "point_pct": book.get("point_pct"),
                }
                if history and history[-1]["date"] == today:
                    history[-1] = today_entry  # 同日なら上書き
                else:
                    history.append(today_entry)
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
    generate_dashboard(cache)

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
