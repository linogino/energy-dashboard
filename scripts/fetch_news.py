"""
石油化学・包材ニュース自動スクレイピング

対象キーワード: ナフサ / エチレン / プロピレン / BTX / 尿素 / アルミ / 包材

ソース (優先順):
  1. 化学工業日報   (kagakukogyonippo.com) ─ 見出し無料公開
  2. 日刊工業新聞   (nikkan.co.jp)         ─ RSS
  3. 包装タイムス   (hosotime.com)         ─ 包材業界紙
  4. 日本アルミニウム協会 (aluminum.or.jp) ─ アルミ専門
  5. 石油化学工業協会    (jpca.or.jp)      ─ 業界団体
  6. 経済産業省         (meti.go.jp)       ─ プレスリリース
  7. Google News RSS ─ 化学工業日報・日経指定クエリ

出力: data/news.json
"""

import json
import re
import sys
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4")
    sys.exit(1)

ROOT      = Path(__file__).parent.parent
NEWS_FILE = ROOT / "data" / "news.json"
MAX_ITEMS = 40
TODAY     = datetime.now().strftime("%Y-%m-%d")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── 信頼ソース ホワイトリスト ─────────────────────────────────────────────────
# Google News RSS の source フィールドと突合。部分一致で判定。
TRUSTED_SOURCE_KEYWORDS: tuple[str, ...] = (
    # 経済紙・業界紙
    "日本経済新聞", "日経",
    "化学工業日報",
    "日刊工業新聞",
    "包装タイムス",
    # 政府・省庁（直接スクレイプ分も統一ラベルとして使用）
    "経済産業省", "資源エネルギー庁", "農林水産省",
    # 業界団体（直接スクレイプ分）
    "石油化学工業協会", "日本アルミニウム協会", "石油連盟",
    "日本化学工業協会", "日本包装技術協会",
)

# Google News <source url="..."> 属性のドメイン照合（テキストが合わなくてもURLで判定）
TRUSTED_DOMAINS: tuple[str, ...] = (
    "nikkei.com",
    "kagakukogyonippo.com",
    "nikkan.co.jp",
    "hosotime.com",
    "meti.go.jp",
    "enecho.meti.go.jp",
    "aluminum.or.jp",
    "jpca.or.jp",
    "maff.go.jp",
    "jogmec.go.jp",
    "cas.go.jp",
)

# Google News の title 末尾に "- SourceName" が入る場合の検出パターン
# 「電子版」「オンライン」等のサフィックスも許容
_TITLE_SOURCE_RE = re.compile(
    r'\s*[ー\-]\s*(日本経済新聞|化学工業日報|日刊工業新聞|包装タイムス|経済産業省|資源エネルギー庁)'
    r'[\w・]*\s*$'
)


def is_trusted(source: str, title: str = "", url: str = "") -> bool:
    """
    source テキスト・タイトル末尾ソース表記・source URL ドメインのいずれかで
    信頼ソースか判定。Yahoo・TV・ポータルをすべて除外する。
    """
    if any(kw in source for kw in TRUSTED_SOURCE_KEYWORDS):
        return True
    # タイトルが "記事タイトル - 日本経済新聞電子版" のような形式
    if _TITLE_SOURCE_RE.search(title):
        return True
    # Google News RSS <source url="https://www.nikkei.com"> のドメインで判定
    if url and any(d in url for d in TRUSTED_DOMAINS):
        return True
    return False


def clean_title(title: str) -> str:
    """タイトル末尾の ' - SourceName[電子版]' サフィックスを除去する。"""
    return _TITLE_SOURCE_RE.sub('', title).strip()


def extract_source_from_title(title: str) -> str | None:
    """タイトル末尾にソース名が含まれていればベース名（グループ1）を返す。"""
    m = _TITLE_SOURCE_RE.search(title)
    return m.group(1) if m else None


# ── タイトルベース重複排除 ────────────────────────────────────────────────────

def _title_key(title: str) -> str:
    """
    重複判定用のキー。先頭25文字（スペース・記号・ソースサフィックスを除去後）。
    同一イベントを複数媒体が報じても1件に絞る。
    """
    t = clean_title(title)
    t = re.sub(r'[　\s「」『』【】〔〕・…]+', '', t)  # 記号・スペース除去
    return t[:25]

# ── カテゴリ分類キーワード ────────────────────────────────────────────────────

CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("naphtha",   ["ナフサ", "石脳油", "クラッカー", "分解炉", "代替調達", "中東産", "ホルムズ"]),
    ("ethylene",  ["エチレン", "PE樹脂", "ポリエチレン", "EDC", "VCM", "PVC"]),
    ("propylene", ["プロピレン", "PP樹脂", "ポリプロピレン", "PO", "SAP", "アクリル酸"]),
    ("btx",       ["BTX", "ベンゼン", "トルエン", "キシレン", "芳香族", "ポリアミド", "PET樹脂"]),
    ("urea",      ["尿素", "アドブルー", "AdBlue", "アンモニア", "窒素肥料", "SCR"]),
    ("aluminum",  ["アルミ", "アルミニウム", "AL箔", "アルミ箔", "ラミネート"]),
    ("packaging", ["包材", "包装フィルム", "軟包装", "バリア包装", "食品包装", "無菌包装",
                   "ポリ袋", "食品容器", "PETボトル", "紙容器"]),
]

ALL_KEYWORDS = [kw for _, kws in CATEGORY_RULES for kw in kws]


def classify(text: str) -> str:
    for cat, kws in CATEGORY_RULES:
        if any(k in text for k in kws):
            return cat
    return "naphtha"  # デフォルト: 石油化学一般


def is_relevant(text: str) -> bool:
    return any(k in text for k in ALL_KEYWORDS)


def parse_date_jp(text: str) -> str:
    """YYYY年MM月DD日 / YYYY-MM-DD / YYYY/MM/DD → YYYY-MM-DD"""
    m = re.search(r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return ""


def get(url: str, timeout: int = 15) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r
    except Exception as e:
        print(f"  [WARN] GET {url} → {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Source 1: 化学工業日報 ── ヘッドライン
# ══════════════════════════════════════════════════════════════════════════════

def scrape_kagaku_nippo() -> list[dict]:
    """
    化学工業日報 (kagakukogyonippo.com) のトップ見出しをスクレイプ。
    記事本文は有料だが見出しリストは公開されている。
    """
    urls = [
        "https://www.kagakukogyonippo.com/headline/",
        "https://www.kagakukogyonippo.com/",
    ]
    items: list[dict] = []
    for base_url in urls:
        r = get(base_url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")

        # 記事リンクを探す（hrefが記事パスのもの）
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            if len(title) < 12:
                continue
            if not is_relevant(title):
                continue
            href = a["href"]
            full_url = urljoin(base_url, href)
            # 日付を近傍テキストから抽出
            ctx = ""
            for parent in a.parents:
                ctx = parent.get_text(" ", strip=True)
                if len(ctx) > len(title) + 4:
                    break
            pub = parse_date_jp(ctx)
            if not pub:
                continue  # 日付のないリンク（ナビ等）は除外
            items.append({
                "title":    title[:140],
                "url":      full_url,
                "source":   "化学工業日報",
                "pubDate":  pub,
                "category": classify(title),
            })
        if items:
            break  # どちらかで取れたら終了

    print(f"[化学工業日報] {len(items)} 件")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# Source 2: 日刊工業新聞 ── RSS
# ══════════════════════════════════════════════════════════════════════════════

NIKKAN_RSS_URLS = [
    # 素材 / 化学 / 環境カテゴリを試す
    "https://www.nikkan.co.jp/rss/1.0/category_7.xml",   # 素材・化学
    "https://www.nikkan.co.jp/rss/1.0/category_3.xml",   # 製造業
    "https://www.nikkan.co.jp/rss/1.0/",                 # 全記事
    "https://www.nikkan.co.jp/rss/",
]


def scrape_nikkan() -> list[dict]:
    """日刊工業新聞のRSSからキーワードにマッチする記事を抽出。"""
    items: list[dict] = []
    for rss_url in NIKKAN_RSS_URLS:
        r = get(rss_url)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for item in root.findall(".//item"):
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link")  or "").strip()
            pub_raw = (item.findtext("pubDate") or "")
            if not title or not link:
                continue
            if not is_relevant(title):
                continue
            try:
                pub_iso = parsedate_to_datetime(pub_raw).strftime("%Y-%m-%d")
            except Exception:
                pub_iso = parse_date_jp(pub_raw) or TODAY
            items.append({
                "title":    title[:140],
                "url":      link,
                "source":   "日刊工業新聞",
                "pubDate":  pub_iso,
                "category": classify(title),
            })
        if items:
            break

    print(f"[日刊工業新聞] {len(items)} 件")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# Source 3: 包装タイムス ── 包材業界紙
# ══════════════════════════════════════════════════════════════════════════════

def scrape_hosotime() -> list[dict]:
    """
    包装タイムス (hosotime.com) のニュース一覧をスクレイプ。
    包材・フィルム・容器関連記事。
    """
    urls = [
        "https://www.hosotime.com/category/news/",
        "https://www.hosotime.com/",
    ]
    items: list[dict] = []
    for base_url in urls:
        r = get(base_url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            if len(title) < 12:
                continue
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            # 包装タイムスは基本的に全記事が包材関連
            ctx = ""
            for parent in a.parents:
                ctx = parent.get_text(" ", strip=True)
                if len(ctx) > len(title) + 4:
                    break
            pub = parse_date_jp(ctx)
            if not pub:
                continue  # 日付のないリンク（ナビ等）は除外
            # 石油化学関連キーワードが含まれる記事のみ対象
            full_text = title + " " + ctx
            if is_relevant(full_text) or any(k in full_text for k in ["包材", "フィルム", "容器", "包装"]):
                items.append({
                    "title":    title[:140],
                    "url":      href,
                    "source":   "包装タイムス",
                    "pubDate":  pub,
                    "category": classify(full_text),
                })
        if items:
            break

    print(f"[包装タイムス] {len(items)} 件")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# Source 4: 日本アルミニウム協会 ── プレスリリース
# ══════════════════════════════════════════════════════════════════════════════

def scrape_aluminum_assoc() -> list[dict]:
    """日本アルミニウム協会のニュース・プレスリリースをスクレイプ。"""
    # ニュース・プレスリリース専用ページのみ対象
    news_urls = [
        "https://www.aluminum.or.jp/news/",
        "https://www.aluminum.or.jp/release/",
        "https://www.aluminum.or.jp/topics/",
    ]
    # 受け入れるリンクのURL パスパターン（ニュース系のみ）
    NEWS_PATH_RE = re.compile(r"/(news|release|topics|press|info)/", re.IGNORECASE)

    items: list[dict] = []
    for base_url in news_urls:
        r = get(base_url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            if len(title) < 15:
                continue
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            # ニュース系のURLのみ受け入れ（ナビリンクを除外）
            if not NEWS_PATH_RE.search(href):
                continue
            ctx = ""
            for parent in a.parents:
                ctx = parent.get_text(" ", strip=True)
                if len(ctx) > len(title) + 4:
                    break
            pub = parse_date_jp(ctx)
            if not pub:
                continue  # 日付が取れないものはナビリンクとして除外
            items.append({
                "title":    title[:140],
                "url":      href,
                "source":   "日本アルミニウム協会",
                "pubDate":  pub,
                "category": "aluminum",
            })
        if items:
            break

    print(f"[アルミニウム協会] {len(items)} 件")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# Source 5: 石油化学工業協会 ── ニュース
# ══════════════════════════════════════════════════════════════════════════════

def scrape_jpca() -> list[dict]:
    """石油化学工業協会 (jpca.or.jp) のニュース・お知らせをスクレイプ。"""
    urls = [
        "https://www.jpca.or.jp/06news/",
        "https://www.jpca.or.jp/news/",
        "https://www.jpca.or.jp/",
    ]
    items: list[dict] = []
    for base_url in urls:
        r = get(base_url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            if len(title) < 12:
                continue
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            ctx = ""
            for parent in a.parents:
                ctx = parent.get_text(" ", strip=True)
                if len(ctx) > len(title) + 4:
                    break
            pub = parse_date_jp(ctx)
            if not pub:
                continue  # 日付のないリンク（ナビ等）は除外
            if not is_relevant(title + " " + ctx):
                continue
            items.append({
                "title":    title[:140],
                "url":      href,
                "source":   "石油化学工業協会",
                "pubDate":  pub,
                "category": classify(title),
            })
        if items:
            break

    print(f"[石油化学工業協会] {len(items)} 件")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# Source 6: 経済産業省 ── プレスリリース
# ══════════════════════════════════════════════════════════════════════════════

METI_KEYS = ["ナフサ", "エチレン", "プロピレン", "石油化学", "BTX", "芳香族",
             "代替調達", "備蓄", "尿素", "アンモニア", "アルミ"]


def scrape_meti() -> list[dict]:
    """経済産業省プレスリリース一覧から石油化学・包材関連を抽出。"""
    r = get("https://www.meti.go.jp/press/index.html")
    if not r:
        return []
    soup  = BeautifulSoup(r.text, "html.parser")
    items: list[dict] = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if not title or not any(k in title for k in METI_KEYS):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.meti.go.jp" + href
        ctx = ""
        for parent in a.parents:
            ctx = parent.get_text(" ", strip=True)
            if len(ctx) > len(title) + 4:
                break
        pub = parse_date_jp(ctx) or TODAY
        items.append({
            "title":    title[:140],
            "url":      href,
            "source":   "経済産業省",
            "pubDate":  pub,
            "category": classify(title),
        })
    print(f"[METI] {len(items)} 件")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# Source 7: Google News RSS ── 信頼媒体指定クエリのみ
# ══════════════════════════════════════════════════════════════════════════════

# source 名をクエリに含めることで Google News がその媒体の記事を優先返却する。
# 取得後に is_trusted() で再フィルタし、Yahoo・TV・まとめサイトを排除。
GNEWS_QUERIES: list[tuple[str, str]] = [
    # 化学工業日報 指定クエリ
    ("化学工業日報 ナフサ",          "naphtha"),
    ("化学工業日報 エチレン",        "ethylene"),
    ("化学工業日報 プロピレン",      "propylene"),
    ("化学工業日報 BTX 芳香族",      "btx"),
    ("化学工業日報 尿素 アドブルー", "urea"),
    ("化学工業日報 アルミ 包材",     "aluminum"),
    ("化学工業日報 包装フィルム",    "packaging"),
    # 日本経済新聞 指定クエリ
    ("日本経済新聞 ナフサ 石油化学", "naphtha"),
    ("日本経済新聞 エチレン 供給",   "ethylene"),
    ("日本経済新聞 尿素 物流",       "urea"),
    ("日本経済新聞 アルミ 包材",     "aluminum"),
    # 日刊工業新聞 指定クエリ
    ("日刊工業新聞 ナフサ 石油化学", "naphtha"),
    ("日刊工業新聞 エチレン",        "ethylene"),
    ("日刊工業新聞 プロピレン",      "propylene"),
]


def fetch_google_news(query: str, category: str) -> list[dict]:
    """
    Google News RSS から記事を取得し、信頼ソースのみ返す。
    Yahoo・TV局・ポータルサイトは is_trusted() で除外。
    """
    url = (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
    )
    r = get(url)
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"  [WARN] RSS parse error for '{query}': {e}")
        return []

    items: list[dict] = []
    skipped = 0
    for item in root.findall(".//item"):
        raw_title = (item.findtext("title") or "").strip()
        link      = (item.findtext("link")  or "").strip()
        pub_raw   = (item.findtext("pubDate") or "")
        src_el     = item.find("source")
        source     = src_el.text.strip() if src_el is not None and src_el.text else ""
        source_url = (src_el.get("url") or "").strip() if src_el is not None else ""

        if not raw_title or not link:
            continue

        # ── 信頼ソースフィルタ（source テキスト + タイトルサフィックス + source URL ドメイン）──
        if not is_trusted(source, raw_title, source_url):
            skipped += 1
            continue

        # タイトルサフィックスからソース名を上書き（より正確な媒体名）
        title_src = extract_source_from_title(raw_title)
        if title_src:
            source = title_src
        title = clean_title(raw_title)

        try:
            pub_iso = parsedate_to_datetime(pub_raw).strftime("%Y-%m-%d")
        except Exception:
            pub_iso = parse_date_jp(pub_raw) or TODAY

        items.append({
            "title":    title[:140],
            "url":      link,
            "source":   source,
            "pubDate":  pub_iso,
            "category": category,
        })

    print(f"[GNews] '{query}' → {len(items)} 件採用 / {skipped} 件除外")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print(f"=== News Fetch === {datetime.now().isoformat()}")

    seen_urls:  set[str] = set()
    seen_keys:  set[str] = set()   # タイトルキーで類似記事を除外
    all_items: list[dict] = []

    # ソース優先順位（同一イベントで複数ソースがある場合、上位を優先）
    SOURCE_PRIORITY = ["化学工業日報", "日本経済新聞", "日刊工業新聞", "包装タイムス",
                       "経済産業省", "資源エネルギー庁", "石油化学工業協会",
                       "日本アルミニウム協会", "石油連盟"]

    def source_rank(src: str) -> int:
        for i, s in enumerate(SOURCE_PRIORITY):
            if s in src:
                return i
        return len(SOURCE_PRIORITY)

    def add(items: list[dict]) -> None:
        for item in items:
            u   = item.get("url", "").strip()
            key = _title_key(item.get("title", ""))

            if u and u in seen_urls:
                continue    # URL 完全一致
            if key and key in seen_keys:
                # 類似タイトルが既存 → より優先度の高いソースなら差し替え
                for i, existing in enumerate(all_items):
                    if _title_key(existing.get("title", "")) == key:
                        if source_rank(item.get("source","")) < source_rank(existing.get("source","")):
                            all_items[i] = item
                            if u:
                                seen_urls.add(u)
                        break
                continue
            if u:
                seen_urls.add(u)
            if key:
                seen_keys.add(key)
            all_items.append(item)

    # ── 業界誌・協会 (直接スクレイプ) ──
    add(scrape_kagaku_nippo())    # 化学工業日報
    add(scrape_nikkan())          # 日刊工業新聞
    add(scrape_hosotime())        # 包装タイムス
    add(scrape_aluminum_assoc())  # 日本アルミニウム協会
    add(scrape_jpca())            # 石油化学工業協会
    add(scrape_meti())            # 経済産業省

    # ── Google News RSS (補完・信頼ソース指定クエリのみ) ──
    for query, cat in GNEWS_QUERIES:
        add(fetch_google_news(query, cat))

    # 日付降順 → 上位 MAX_ITEMS 件
    all_items.sort(key=lambda x: x.get("pubDate", ""), reverse=True)
    all_items = all_items[:MAX_ITEMS]

    print(f"\n[NEWS] 合計 {len(all_items)} 件（URL・類似タイトル重複除去済み）")
    for cat in ["naphtha", "ethylene", "propylene", "btx", "urea", "aluminum", "packaging"]:
        n = sum(1 for i in all_items if i["category"] == cat)
        if n:
            print(f"  {cat}: {n} 件")

    result = {
        "updated": TODAY,
        "items":   all_items,
    }
    with open(NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] Saved to {NEWS_FILE}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
