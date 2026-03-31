"""
エネルギーダッシュボード データ自動更新スクリプト
ソース:
  - 資源エネルギー庁 (enecho.meti.go.jp) - LNG週次在庫
  - 石油化学工業協会 (jpca.or.jp)        - エチレン稼働率
  - 石油連盟 (paj.gr.jp)                 - 石油製品在庫
"""

import json
import re
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4")
    sys.exit(1)

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "dashboard.json"
TODAY = date.today().isoformat()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; EnergyDashboard/1.0; "
        "+https://github.com/YOUR_USERNAME/energy-dashboard)"
    )
}


def load_current() -> dict:
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved to {DATA_FILE}")


# ── Fetcher: LNG weekly inventory (資源エネルギー庁) ──────────────────────
def fetch_lng_inventory(data: dict) -> dict:
    """
    資源エネルギー庁のLNG在庫モニタリングページをスクレイプ。
    ページURL: https://www.enecho.meti.go.jp/category/electricity_and_gas/electricity_measures/lng/
    """
    url = "https://www.enecho.meti.go.jp/category/electricity_and_gas/electricity_measures/lng/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # 最新在庫数値を探す (単位: 万トン または 百万トン)
        # ページ構造が変わる可能性があるため、正規表現で数値抽出
        text = soup.get_text(separator=" ")

        # パターン例: "XXX万トン" or "X.X百万トン"
        mt_pattern = re.findall(r"(\d{2,4})\s*万トン", text)
        if mt_pattern:
            # 最初に見つかった値を電力+都市ガス合計として使用
            total_mt = int(mt_pattern[0])
            data["lng"]["totalStock_mt"] = total_mt
            print(f"[LNG] totalStock_mt = {total_mt}万トン")
        else:
            print("[LNG] 在庫数値のパース失敗 — 前回値を維持")

    except Exception:
        print("[LNG] fetch失敗 — 前回値を維持")
        traceback.print_exc()

    return data


# ── Fetcher: エチレン稼働率 (石油化学工業協会) ──────────────────────────
def fetch_ethylene_stats(data: dict) -> dict:
    """
    石油化学工業協会の月次実績ページをスクレイプ。
    URL: https://www.jpca.or.jp/statistics/monthly/memo.html
    """
    url = "https://www.jpca.or.jp/statistics/monthly/memo.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator=" ")

        # 稼働率パターン: "稼働率 XX.X%" or "XX.X％"
        rate_pattern = re.findall(r"稼働率[^\d]*(\d{2,3}\.?\d*)\s*[%%]", text)
        if rate_pattern:
            rate = float(rate_pattern[0])
            data["naphtha"]["ethylene"]["crackerOperatingRate"] = rate
            print(f"[ETHYLENE] crackerOperatingRate = {rate}%")
        else:
            print("[ETHYLENE] 稼働率パース失敗 — 前回値を維持")

        # 連続低迷月数パターン
        month_pattern = re.findall(r"(\d+)\s*か月連続", text)
        if month_pattern:
            months = int(month_pattern[0])
            data["naphtha"]["ethylene"]["consecutiveMonthsBelowBenchmark"] = f"{months}ヶ月連続"
            print(f"[ETHYLENE] consecutiveMonths = {months}")

    except Exception:
        print("[ETHYLENE] fetch失敗 — 前回値を維持")
        traceback.print_exc()

    return data


# ── Fetcher: 石油製品在庫 (石油連盟) ────────────────────────────────────
def fetch_petroleum_inventory(data: dict) -> dict:
    """
    石油連盟の統計ページをスクレイプ。
    公表停止中の場合は note を更新するのみ。
    URL: https://www.paj.gr.jp/statis/statis
    """
    url = "https://www.paj.gr.jp/statis/statis"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator=" ")

        # 公表停止メッセージが含まれているか確認
        if "見合わせ" in text or "停止" in text or "公表" in text:
            data["meta"]["note"] = "石油連盟は2026年3月24日より石油製品在庫データ公表を停止中（精度維持困難を理由として）"
            print("[PAJ] 公表停止メッセージ確認 — note更新")
        else:
            # ナフサ在庫 (kl or 日数) の抽出を試みる
            naphtha_pattern = re.findall(r"ナフサ[^\d]*(\d+)[^\d]*(日|日分|kl|kL)", text)
            if naphtha_pattern:
                val, unit = naphtha_pattern[0]
                if unit.startswith("日"):
                    data["naphtha"]["daysEquivalent"] = int(val)
                    print(f"[PAJ] naphtha daysEquivalent = {val}日")

    except Exception:
        print("[PAJ] fetch失敗 — 前回値を維持")
        traceback.print_exc()

    return data


# ── Fetcher: 経済産業省 備蓄放出情報 ────────────────────────────────────
def fetch_meti_reserve_release(data: dict) -> dict:
    """
    経済産業省のプレスリリースページから最新の備蓄放出情報を確認。
    """
    url = "https://www.meti.go.jp/press/index.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # 備蓄放出に関するリンクを探す
        links = soup.find_all("a", href=True)
        release_links = [
            a for a in links
            if "備蓄" in (a.get_text() or "") or "放出" in (a.get_text() or "")
        ]
        if release_links:
            print(f"[METI] 備蓄関連リンク {len(release_links)}件確認")
            data["crude"]["releaseActive"] = True
        else:
            print("[METI] 備蓄放出リンクなし — 現在の状態を維持")

    except Exception:
        print("[METI] fetch失敗 — 前回値を維持")
        traceback.print_exc()

    return data


# ── Update timestamp ─────────────────────────────────────────────────────
def update_timestamp(data: dict) -> dict:
    data["meta"]["lastUpdated"] = TODAY
    print(f"[META] lastUpdated = {TODAY}")
    return data


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    print(f"=== Energy Dashboard Data Fetch === {datetime.now().isoformat()}")

    data = load_current()
    print(f"[LOAD] Current data loaded (lastUpdated: {data['meta']['lastUpdated']})")

    data = fetch_lng_inventory(data)
    data = fetch_ethylene_stats(data)
    data = fetch_petroleum_inventory(data)
    data = fetch_meti_reserve_release(data)
    data = update_timestamp(data)

    save(data)
    print("=== Done ===")


if __name__ == "__main__":
    main()
