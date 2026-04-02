"""
石油化学・代替調達ニュース自動スクレイピング

対象キーワード:
  ナフサ代替調達 / エチレン供給 / プロピレン石油化学 /
  BTX芳香族 / ホルムズ封鎖 / AdBlue物流 など

ソース:
  - Google News RSS (パブリック)
  - 経済産業省プレスリリース (meti.go.jp/press)
  - 資源エネルギー庁 トピックス (enecho.meti.go.jp)

出力: data/news.json
"""

import json
import sys
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4")
    sys.exit(1)

ROOT = Path(__file__).parent.parent
NEWS_FILE = ROOT / "data" / "news.json"
MAX_ITEMS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; EnergyDashboard/1.0; "
        "+https://github.com/YOUR_USERNAME/energy-dashboard)"
    )
}

# (検索クエリ, カテゴリ) のリスト
# カテゴリ: naphtha / ethylene / propylene / btx / logistics
GOOGLE_NEWS_QUERIES = [
    ("ナフサ 代替調達 石油化学", "naphtha"),
    ("ナフサ 供給 中東 代替", "naphtha"),
    ("ナフサ クラッカー 稼働", "ethylene"),
    ("エチレン 石油化学 供給制約", "ethylene"),
    ("プロピレン 石油化学 供給", "propylene"),
    ("BTX 芳香族 供給", "btx"),
    ("石油化学 ホルムズ 代替調達", "naphtha"),
    ("AdBlue アドブルー 尿素 物流", "logistics"),
    ("ディーゼル 供給 物流 制約", "logistics"),
]


# ── Google News RSS ──────────────────────────────────────────────────────────

def fetch_google_news(query: str, category: str) -> list[dict]:
    """Google News RSS から記事を取得する。"""
    url = (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item"):
            title    = (item.findtext("title") or "").strip()
            link     = (item.findtext("link")  or "").strip()
            pub_raw  = (item.findtext("pubDate") or "").strip()
            src_el   = item.find("source")
            source   = src_el.text.strip() if src_el is not None and src_el.text else ""

            if not title or not link:
                continue

            try:
                pub_iso = parsedate_to_datetime(pub_raw).strftime("%Y-%m-%d")
            except Exception:
                pub_iso = ""

            items.append({
                "title":    title,
                "url":      link,
                "source":   source,
                "pubDate":  pub_iso,
                "category": category,
            })
        print(f"[RSS] '{query}' → {len(items)} items")
        return items
    except Exception:
        print(f"[RSS] '{query}' fetch失敗")
        traceback.print_exc()
        return []


# ── METI プレスリリース ───────────────────────────────────────────────────────

METI_KEYWORDS = [
    "ナフサ", "エチレン", "プロピレン", "石油化学",
    "BTX", "芳香族", "代替調達", "備蓄放出",
]

def fetch_meti_press() -> list[dict]:
    """経済産業省プレスリリース一覧から石油化学関連を抽出する。"""
    url = "https://www.meti.go.jp/press/index.html"
    items = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # プレスリリース行を走査
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text:
                continue
            if not any(kw in text for kw in METI_KEYWORDS):
                continue

            href = a["href"]
            if href.startswith("/"):
                href = "https://www.meti.go.jp" + href

            # 日付は親要素のテキストから推測（td やリスト項目）
            parent_text = ""
            for parent in a.parents:
                parent_text = parent.get_text(" ", strip=True)
                if len(parent_text) > len(text) + 5:
                    break
            date_match = __import__("re").search(r"(\d{4}[年\-/]\d{1,2}[月\-/]\d{1,2})", parent_text)
            pub_iso = ""
            if date_match:
                raw = date_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
                try:
                    pub_iso = datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
                except Exception:
                    pass

            category = _classify_meti(text)
            items.append({
                "title":    text[:120],
                "url":      href,
                "source":   "経済産業省",
                "pubDate":  pub_iso,
                "category": category,
            })

        print(f"[METI] {len(items)} 石油化学関連プレス")
    except Exception:
        print("[METI] fetch失敗")
        traceback.print_exc()
    return items


def _classify_meti(text: str) -> str:
    if any(k in text for k in ["ナフサ", "クラッカー", "代替調達", "備蓄"]):
        return "naphtha"
    if any(k in text for k in ["エチレン"]):
        return "ethylene"
    if any(k in text for k in ["プロピレン"]):
        return "propylene"
    if any(k in text for k in ["BTX", "芳香族"]):
        return "btx"
    return "naphtha"


# ── 資源エネルギー庁 トピックス ──────────────────────────────────────────────

ENECHO_KEYWORDS = ["ナフサ", "エチレン", "プロピレン", "石油化学", "BTX", "代替", "備蓄"]

def fetch_enecho_topics() -> list[dict]:
    """資源エネルギー庁のトピックスページから関連記事を抽出する。"""
    url = "https://www.enecho.meti.go.jp/notice/topics/"
    items = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        import re

        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text or not any(kw in text for kw in ENECHO_KEYWORDS):
                continue

            href = a["href"]
            if href.startswith("/"):
                href = "https://www.enecho.meti.go.jp" + href

            # 近傍の日付テキスト
            parent_text = ""
            for parent in a.parents:
                parent_text = parent.get_text(" ", strip=True)
                if len(parent_text) > len(text) + 5:
                    break
            date_match = re.search(r"(\d{4}[年\-]\d{1,2}[月\-]\d{1,2})", parent_text)
            pub_iso = ""
            if date_match:
                raw = date_match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
                try:
                    pub_iso = datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
                except Exception:
                    pass

            items.append({
                "title":    text[:120],
                "url":      href,
                "source":   "資源エネルギー庁",
                "pubDate":  pub_iso,
                "category": "naphtha",
            })

        print(f"[ENECHO] {len(items)} 関連トピックス")
    except Exception:
        print("[ENECHO] fetch失敗")
        traceback.print_exc()
    return items


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== News Fetch === {datetime.now().isoformat()}")

    seen_urls: set[str] = set()
    all_items: list[dict] = []

    def add_items(items: list[dict]) -> None:
        for item in items:
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_items.append(item)

    # Google News RSS (複数クエリ)
    for query, category in GOOGLE_NEWS_QUERIES:
        add_items(fetch_google_news(query, category))

    # 公式ソース
    add_items(fetch_meti_press())
    add_items(fetch_enecho_topics())

    # 日付降順 → 上位 MAX_ITEMS 件
    all_items.sort(key=lambda x: x.get("pubDate", ""), reverse=True)
    all_items = all_items[:MAX_ITEMS]

    print(f"[NEWS] 合計 {len(all_items)} 件（重複除去済み）")

    result = {
        "updated": datetime.now().strftime("%Y-%m-%d"),
        "items":   all_items,
    }

    with open(NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved to {NEWS_FILE}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
