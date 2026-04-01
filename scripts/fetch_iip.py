#!/usr/bin/env python3
"""
fetch_iip.py ─ 鉱工業指数（生産・在庫）取得 → 業種別在庫バッファー HTML 可視化

取得戦略 (順に試行):
  1. METI ダウンロードページ → Excel リンク抽出 → ダウンロード → 解析
  2. e-Stat ファイル一覧ページ → Excel リンク抽出 → ダウンロード → 解析
  3. フォールバック: デモデータで出力（ソース注記付き）

使い方:
  python scripts/fetch_iip.py               # 自動取得
  python scripts/fetch_iip.py --file path/to/iip.xlsx  # ローカルExcelを直接指定
  python scripts/fetch_iip.py --demo         # デモデータで強制出力
"""

import sys
import re
import json
import io
import os
import argparse
import textwrap
from datetime import datetime, date
from pathlib import Path

import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# ── 設定 ─────────────────────────────────────────────────────────────────────

METI_DOWNLOAD_URL = "https://www.meti.go.jp/statistics/tyo/iip/b2020_result-2.html"
METI_BASE_URL     = "https://www.meti.go.jp"
ESTAT_FILES_URL   = "https://www.e-stat.go.jp/stat-search/files?toukei=00550300&tstat=000001049060&cycle=1"
OUTPUT_FILE       = Path("index.html")

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ja,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 30

# 在庫バッファー判定閾値（月）
BUFFER_GREEN  = 3.0
BUFFER_YELLOW = 1.0

# IIP 主要業種名（解析時のマッチングに使用）
INDUSTRY_NAMES_JA = [
    "製造工業",
    "食料品・たばこ工業",
    "繊維工業",
    "木材・木製品工業",
    "パルプ・紙・紙加工品工業",
    "化学工業",
    "石油・石炭製品工業",
    "プラスチック製品工業",
    "窯業・土石製品工業",
    "鉄鋼業",
    "非鉄金属工業",
    "金属製品工業",
    "はん用機械工業",
    "生産用機械工業",
    "業務用機械工業",
    "電子部品・デバイス工業",
    "電気機械工業",
    "情報通信機械工業",
    "輸送機械工業",
    "その他工業",
]

# ── データ取得 ────────────────────────────────────────────────────────────────

def _get(url, stream=False):
    """共通 GET ラッパー（リトライなし）"""
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=stream)
    resp.raise_for_status()
    return resp


def find_excel_links_meti(html_text, page_url=METI_DOWNLOAD_URL):
    """METI ダウンロードページから .xlsx/.xls リンクを抽出して返す"""
    soup = BeautifulSoup(html_text, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(xlsx|xls)$", href, re.IGNORECASE):
            # 業種別原指数ファイルを優先（iipj を含むもの）
            full = urljoin(page_url, href)
            links.append((a.get_text(strip=True), full))
    # 優先順: si1j (業種別月次) > iipj > 6桁月次コード > その他
    def priority(item):
        t, u = item
        if "si1j" in u:  return 0   # 業種別月次原指数（最優先）
        if "iipj" in u:  return 1
        if re.search(r"\d{6}", u): return 2
        return 3
    links.sort(key=priority)
    return links


def find_excel_links_estat(html_text):
    """e-Stat ファイル一覧ページから .xlsx リンクを抽出"""
    soup = BeautifulSoup(html_text, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "download" in href and re.search(r"\.(xlsx|xls)", href, re.IGNORECASE):
            full = href if href.startswith("http") else "https://www.e-stat.go.jp" + href
            links.append((a.get_text(strip=True), full))
    return links


def download_excel_bytes(url):
    """Excel ファイルを bytes として取得"""
    resp = _get(url, stream=True)
    buf = io.BytesIO()
    for chunk in resp.iter_content(chunk_size=65536):
        buf.write(chunk)
    buf.seek(0)
    return buf


# ── Excel 解析 ────────────────────────────────────────────────────────────────

# 生産指数・在庫指数のシート名候補（優先順）
PROD_SHEET_HINTS = ["業種別生産", "生産指数", "生産", "prod", "production"]
INV_SHEET_HINTS  = ["業種別在庫", "在庫指数", "在庫", "inv", "inventory"]

# 主要業種コード → 業種名マッピング（METI si1j Excel の品目番号）
MAJOR_INDUSTRY_CODES = {
    "1000000000": "製造工業",
    "1100000000": "食料品・たばこ工業",
    "1200000000": "繊維工業",
    "1300000000": "木材・木製品工業",
    "1400000000": "パルプ・紙・紙加工品工業",
    "1500000000": "化学工業",
    "1600000000": "石油・石炭製品工業",
    "1700000000": "プラスチック製品工業",
    "1800000000": "窯業・土石製品工業",
    "1900000000": "鉄鋼業",
    "2000000000": "非鉄金属工業",
    "2100000000": "金属製品工業",
    "2200000000": "はん用機械工業",
    "2300000000": "生産用機械工業",
    "2400000000": "業務用機械工業",
    "2500000000": "電子部品・デバイス工業",
    "2600000000": "電気機械工業",
    "2700000000": "情報通信機械工業",
    "2800000000": "輸送機械工業",
    "2900000000": "その他工業",
}


def _find_sheet(xl, hints):
    """ヒント語を含むシート名を探す（大文字小文字無視）"""
    for hint in hints:
        for name in xl.sheet_names:
            if hint.lower() in name.lower():
                return name
    return None


def _parse_date_col(val):
    """列ヘッダーから (year, month) を抽出。複数フォーマット対応。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    # pandas Timestamp
    try:
        if hasattr(val, 'year') and hasattr(val, 'month'):
            if 2000 <= val.year <= 2035:
                return (val.year, val.month)
    except Exception:
        pass
    s = str(val).strip()
    # YYYYMM 形式 (例: "201801", "202412")
    m = re.fullmatch(r"(20[12]\d)(\d{2})", s)
    if m:
        yr, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return (yr, mo)
    # YYYY年MM月 / YYYY.MM 形式
    m = re.search(r"(20\d\d)[年.](\d{1,2})月?", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # 数値として解釈 (例: 201801.0)
    try:
        fv = float(s)
        iv = int(fv)
        m2 = re.fullmatch(r"(20[12]\d)(\d{2})", str(iv))
        if m2:
            yr, mo = int(m2.group(1)), int(m2.group(2))
            if 1 <= mo <= 12:
                return (yr, mo)
    except (ValueError, TypeError):
        pass
    return None


def _detect_header_row(df):
    """日付パターンを多く含む行インデックスを返す"""
    best_row, best_count = None, 0
    for i in range(min(10, len(df))):  # 先頭10行のみ検索
        row = df.iloc[i]
        count = sum(1 for v in row if _parse_date_col(v) is not None)
        if count > best_count:
            best_count, best_row = count, i
    return best_row if best_count >= 3 else None


def _find_name_col(df, hdr_row):
    """
    業種名が入っている列インデックスを検索。
    日本語文字（ひらがな・カタカナ・漢字）を含む値が多い列を選ぶ。
    """
    jp_pat = re.compile(r'[ぁ-んァ-ン\u4e00-\u9fff]')
    best_col, best_count = 0, 0
    for col in range(min(4, len(df.columns))):
        count = 0
        for row_idx in range(hdr_row + 1, min(hdr_row + 30, len(df))):
            v = df.iloc[row_idx, col]
            if pd.notna(v) and jp_pat.search(str(v)):
                count += 1
        if count > best_count:
            best_count, best_col = count, col
    return best_col if best_count > 0 else None


def _is_major_industry(code_val, name_val):
    """主要業種かどうか判定（コードまたは名前で確認）"""
    code_s = str(code_val).strip() if pd.notna(code_val) else ""
    name_s = str(name_val).strip() if pd.notna(name_val) else ""

    # コードで照合
    if code_s in MAJOR_INDUSTRY_CODES:
        return MAJOR_INDUSTRY_CODES[code_s]
    # コードの末尾ゼロ数で大分類判定（末尾8桁がゼロ → 大分類レベル）
    m = re.fullmatch(r"(\d{2,4})0{6,8}", code_s)
    if m:
        return name_s  # 名前をそのまま使う

    # 名前で既知業種リストと照合
    for known in INDUSTRY_NAMES_JA:
        if known == name_s or known in name_s:
            return known
    return None  # 主要業種ではない


def _extract_series(sheet_df):
    """
    シート DataFrame から { 業種名: {(year,month): value} } を返す。

    METI si1j 形式:
      列0 = 品目番号（数値コード）
      列1 = 品目名（日本語）
      列2 = ウェイト
      列3+ = 月次指数値（列ヘッダーが YYYYMM）
    """
    hdr_row = _detect_header_row(sheet_df)
    if hdr_row is None:
        hdr_row = 0

    # 日付列マッピング
    hdr = sheet_df.iloc[hdr_row]
    date_cols = {}
    for col_idx, col_val in enumerate(hdr):
        parsed = _parse_date_col(col_val)
        if parsed and parsed[1] is not None:
            date_cols[col_idx] = parsed

    if len(date_cols) < 2:
        return {}

    # 業種名列を検索
    name_col = _find_name_col(sheet_df, hdr_row)
    if name_col is None:
        return {}

    # コード列は名前列の1つ前 (通常 col=0) と仮定
    code_col = max(0, name_col - 1) if name_col > 0 else None

    result = {}
    for row_idx in range(hdr_row + 1, len(sheet_df)):
        row = sheet_df.iloc[row_idx]
        name_val = row.iloc[name_col]
        if pd.isna(name_val):
            continue
        code_val = row.iloc[code_col] if code_col is not None else None

        # 主要業種かどうかを確認（細分類を除外）
        canonical = _is_major_industry(code_val, name_val)
        if canonical is None:
            continue

        series = {}
        for col_idx, ym in date_cols.items():
            try:
                v = row.iloc[col_idx]
                if pd.notna(v):
                    series[ym] = float(v)
            except (ValueError, IndexError):
                pass
        if series:
            result[canonical] = series  # canonical 名を使う（重複を避ける）

    return result


def parse_iip_excel(excel_src):
    """
    Excel ファイル (パス or BytesIO) から生産・在庫データを解析。

    戻り値:
      {
        "data_date": "2025年MM月",
        "industries": [
          {
            "name": str,
            "production_history": [(year,month,value), ...],  # 12ヶ月分
            "inventory_history":  [(year,month,value), ...],
            "production": float,   # 最新月
            "inventory": float,    # 最新月
            "prod_yoy": float,     # 前年同月比 %
            "inv_yoy": float,
          }, ...
        ]
      }
    """
    xl = pd.ExcelFile(excel_src, engine="openpyxl")
    print(f"  シート一覧: {xl.sheet_names}", file=sys.stderr)

    prod_sheet = _find_sheet(xl, PROD_SHEET_HINTS)
    inv_sheet  = _find_sheet(xl, INV_SHEET_HINTS)

    if prod_sheet is None and inv_sheet is None:
        # シートが分かれていない場合は最初のシートを両方に使う
        print("  生産/在庫シートが見つからず。最初のシートを使用します。", file=sys.stderr)
        prod_sheet = inv_sheet = xl.sheet_names[0]

    def load(sheet):
        return pd.read_excel(xl, sheet_name=sheet, header=None, dtype=str)

    print(f"  生産シート: {prod_sheet}", file=sys.stderr)
    print(f"  在庫シート: {inv_sheet}", file=sys.stderr)

    prod_data = _extract_series(load(prod_sheet)) if prod_sheet else {}
    inv_data  = _extract_series(load(inv_sheet))  if inv_sheet  else {}

    if not prod_data and not inv_data:
        raise ValueError("Excelから生産・在庫データを抽出できませんでした。シート構造を確認してください。")

    # 利用可能な全日付を収集
    all_dates = set()
    for s in list(prod_data.values()) + list(inv_data.values()):
        all_dates.update(s.keys())
    if not all_dates:
        raise ValueError("日付カラムが抽出できませんでした。")

    sorted_dates = sorted(all_dates)

    # 予測値プレースホルダー検出：最新月の値が前年同月と全て一致する場合は1ヶ月戻す
    # (METIの季節調整済み予測値は前年同月をコピーすることがある)
    def _is_forecast_month(ym, data_dict):
        """指定月のデータが全て前年同月と一致 → 予測値と判断"""
        prev_ym = (ym[0] - 1, ym[1])
        diffs = 0
        total = 0
        for series in data_dict.values():
            cur  = series.get(ym)
            prev = series.get(prev_ym)
            if cur is not None and prev is not None and prev != 0:
                total += 1
                if abs(cur - prev) > 0.01:
                    diffs += 1
        # 比較できた業種が3以上あり、全て差分0 → 予測値
        return total >= 3 and diffs == 0

    # 生産・在庫それぞれで最新実績月を独立して決定
    def _find_latest_real(ym_list, data_dict):
        for ym in reversed(ym_list):
            if not _is_forecast_month(ym, data_dict):
                return ym
        return ym_list[-1]  # 全て予測値でも最新を使う

    latest_prod_ym = _find_latest_real(sorted_dates, prod_data) if prod_data else sorted_dates[-1]
    latest_inv_ym  = _find_latest_real(sorted_dates, inv_data)  if inv_data  else sorted_dates[-1]
    # バッファー計算には生産・在庫で同一月を使う（古い方に合わせる）
    latest_ym      = min(latest_prod_ym, latest_inv_ym)

    print(f"  生産最新実績月: {latest_prod_ym[0]}年{latest_prod_ym[1]}月", file=sys.stderr)
    print(f"  在庫最新実績月: {latest_inv_ym[0]}年{latest_inv_ym[1]}月", file=sys.stderr)
    print(f"  バッファー計算基準月: {latest_ym[0]}年{latest_ym[1]}月", file=sys.stderr)
    data_date = f"{latest_ym[0]}年{latest_ym[1]}月"

    def yoy(cur, prev):
        if cur is not None and prev and prev != 0:
            return round((cur - prev) / prev * 100, 1)
        return None

    # 業種を突き合わせ
    all_industries = set(prod_data.keys()) | set(inv_data.keys())
    industries = []
    for name in all_industries:
        ps  = prod_data.get(name, {})
        is_ = inv_data.get(name, {})

        # 最新共通月の値（バッファー計算を同一月で行うため）
        prod_val = ps.get(latest_ym)
        inv_val  = is_.get(latest_ym)
        if prod_val is None and inv_val is None:
            continue

        # 前年同月（共通月基準）
        prev_ym   = (latest_ym[0] - 1, latest_ym[1])
        prod_prev = ps.get(prev_ym)
        inv_prev  = is_.get(prev_ym)

        # 過去12ヶ月の時系列（共通最新月まで）
        end_idx = sorted_dates.index(latest_ym) if latest_ym in sorted_dates else len(sorted_dates) - 1
        hist_dates = sorted_dates[max(0, end_idx - 11): end_idx + 1]
        prod_hist  = [(y, m, ps.get((y, m)))  for y, m in hist_dates]
        inv_hist   = [(y, m, is_.get((y, m))) for y, m in hist_dates]

        industries.append({
            "name":               name,
            "production":         prod_val,
            "inventory":          inv_val,
            "prod_yoy":           yoy(prod_val, prod_prev),
            "inv_yoy":            yoy(inv_val, inv_prev),
            "production_history": prod_hist,
            "inventory_history":  inv_hist,
        })

    # 優先順位でソート（既知業種リスト順）
    def sort_key(ind):
        for i, n in enumerate(INDUSTRY_NAMES_JA):
            if n in ind["name"] or ind["name"] in n:
                return i
        return len(INDUSTRY_NAMES_JA)

    industries.sort(key=sort_key)
    return {"data_date": data_date, "industries": industries}


# ── バッファー計算 ────────────────────────────────────────────────────────────

def enrich(parsed):
    """在庫バッファー月数・色分けを計算して各業種データに追加"""
    for ind in parsed["industries"]:
        p = ind["production"]
        iv = ind["inventory"]
        if p and p > 0 and iv is not None:
            buf = round(iv / p, 2)
        else:
            buf = None
        ind["buffer_months"] = buf
        ind["color"] = (
            "green"  if buf is not None and buf >= BUFFER_GREEN  else
            "yellow" if buf is not None and buf >= BUFFER_YELLOW else
            "red"    if buf is not None                          else
            "gray"
        )
    return parsed


# ── フォールバックデモデータ ─────────────────────────────────────────────────

def fallback_demo_data():
    """実データ取得失敗時のデモデータ（注記付き）"""
    print("[DEMO] フォールバックデモデータを使用します。", file=sys.stderr)
    today = date.today()
    # 架空の12ヶ月データを生成
    import math
    industries_raw = [
        ("製造工業",             100.2, 98.5),
        ("食料品・たばこ工業",   98.4,  305.0),
        ("繊維工業",             82.1,  88.2),
        ("木材・木製品工業",     91.3,  120.4),
        ("パルプ・紙・紙加工品工業", 94.7, 180.2),
        ("化学工業",             103.5, 195.8),
        ("石油・石炭製品工業",   107.2, 85.3),
        ("プラスチック製品工業", 96.8,  105.5),
        ("窯業・土石製品工業",   88.5,  148.7),
        ("鉄鋼業",               95.4,  110.2),
        ("非鉄金属工業",         98.7,  88.9),
        ("金属製品工業",         91.2,  124.5),
        ("はん用機械工業",       102.4, 198.7),
        ("生産用機械工業",       107.8, 220.3),
        ("業務用機械工業",       99.3,  215.6),
        ("電子部品・デバイス工業", 108.5, 108.4),
        ("電気機械工業",         103.2, 118.9),
        ("情報通信機械工業",     112.4, 95.2),
        ("輸送機械工業",         105.7, 185.3),
        ("その他工業",           90.5,  130.2),
    ]
    industries = []
    for name, prod, inv in industries_raw:
        hist = []
        for i in range(12):
            offset = i - 11
            yr = today.year + (today.month + offset - 1) // 12
            mo = (today.month + offset - 1) % 12 + 1
            v = prod * (1 + 0.02 * math.sin(i * 0.5))
            hist.append((yr, mo, round(v, 1)))
        inv_hist = []
        for i, (yr, mo, pv) in enumerate(hist):
            v = inv * (1 + 0.015 * math.cos(i * 0.4))
            inv_hist.append((yr, mo, round(v, 1)))

        industries.append({
            "name": name,
            "production": prod,
            "inventory":  inv,
            "prod_yoy":   round((prod - prod * 0.97) / (prod * 0.97) * 100, 1),
            "inv_yoy":    round((inv  - inv  * 0.95) / (inv  * 0.95) * 100, 1),
            "production_history": hist,
            "inventory_history":  inv_hist,
        })
    return {
        "data_date": f"{today.year}年{today.month}月（デモデータ）",
        "industries": industries,
        "is_demo": True,
    }


# ── HTML 生成 ─────────────────────────────────────────────────────────────────

def generate_html(parsed, source_url=""):
    """自己完結型 HTML を生成して文字列で返す"""
    enrich(parsed)
    data_date = parsed["data_date"]
    is_demo   = parsed.get("is_demo", False)
    industries = parsed["industries"]

    # Chart.js 用データを事前計算
    for ind in industries:
        inv_h  = ind["inventory_history"]
        prod_h = ind["production_history"]
        ind["chart_labels"]   = [f"{m[0]}年{m[1]}月" for m in inv_h if m[2] is not None]
        ind["chart_inv"]      = [m[2] for m in inv_h  if m[2] is not None]
        ind["chart_prod"]     = [m[2] for m in prod_h if m[2] is not None]

    # 統計サマリー
    counts = {"green": 0, "yellow": 0, "red": 0, "gray": 0}
    for ind in industries:
        counts[ind["color"]] += 1

    # インデックスデータを JSON 化
    industries_json = json.dumps(
        [{
            "name":          ind["name"],
            "production":    ind["production"],
            "inventory":     ind["inventory"],
            "buffer_months": ind["buffer_months"],
            "color":         ind["color"],
            "prod_yoy":      ind["prod_yoy"],
            "inv_yoy":       ind["inv_yoy"],
            "chart_labels":  ind["chart_labels"],
            "chart_inv":     ind["chart_inv"],
            "chart_prod":    ind["chart_prod"],
        } for ind in industries],
        ensure_ascii=False, indent=2
    )

    color_css = {
        "green":  ("#16a34a", "#dcfce7", "#86efac"),
        "yellow": ("#ca8a04", "#fef9c3", "#fde047"),
        "red":    ("#dc2626", "#fee2e2", "#fca5a5"),
        "gray":   ("#6b7280", "#f3f4f6", "#d1d5db"),
    }

    def badge_html(color, label, count):
        fg, bg, border = color_css[color]
        return (
            f'<span class="badge" style="background:{bg};color:{fg};border:1px solid {border}">'
            f'{label} {count}業種</span>'
        )

    badges = (
        badge_html("green",  "🟢 3ヶ月以上",    counts["green"])  +
        badge_html("yellow", "🟡 1〜3ヶ月",      counts["yellow"]) +
        badge_html("red",    "🔴 1ヶ月未満",      counts["red"])    +
        badge_html("gray",   "⚪ データなし",      counts["gray"])
    )

    demo_banner = (
        '<div class="demo-banner">'
        '⚠️ 実データの取得に失敗しました。デモデータを表示しています。'
        '<br>実データを取得するには: <code>python scripts/fetch_iip.py</code>'
        '</div>'
        if is_demo else ""
    )

    source_note = (
        f'<a href="{source_url}" target="_blank" rel="noopener">{source_url}</a>'
        if source_url else
        '<a href="https://www.meti.go.jp/statistics/tyo/iip/b2020_result-2.html" '
        'target="_blank" rel="noopener">経済産業省 鉱工業指数データダウンロード</a>'
    )

    generated_at = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>鉱工業指数 — 業種別在庫バッファー</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
:root {{
  --green:  #16a34a; --green-bg:  #dcfce7; --green-bd:  #86efac;
  --yellow: #ca8a04; --yellow-bg: #fef9c3; --yellow-bd: #fde047;
  --red:    #dc2626; --red-bg:    #fee2e2; --red-bd:    #fca5a5;
  --gray:   #6b7280; --gray-bg:   #f3f4f6; --gray-bd:   #d1d5db;
  --bg: #f8fafc; --card: #fff; --text: #1e293b; --muted: #64748b;
  --radius: 10px; --shadow: 0 1px 4px rgba(0,0,0,.08),0 2px 12px rgba(0,0,0,.05);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Hiragino Sans','Noto Sans JP',sans-serif; background: var(--bg); color: var(--text); }}
.demo-banner {{
  background: #fef3c7; border-bottom: 2px solid #f59e0b;
  padding: 10px 20px; font-size: .875rem; color: #92400e;
  text-align: center;
}}
header {{
  background: linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);
  color: #fff; padding: 28px 24px 20px;
}}
header h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 6px; }}
header .sub {{ font-size: .875rem; opacity: .75; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 20px 16px; }}
/* サマリーバー */
.summary-bar {{
  display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px;
}}
.badge {{
  padding: 5px 12px; border-radius: 20px; font-size: .8rem; font-weight: 600; white-space: nowrap;
}}
/* フィルター */
.filter-bar {{
  display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap;
}}
.filter-btn {{
  padding: 6px 16px; border: 1.5px solid #cbd5e1; border-radius: 20px;
  background: #fff; color: var(--muted); font-size: .85rem; cursor: pointer;
  transition: all .15s;
}}
.filter-btn:hover {{ border-color: #94a3b8; color: var(--text); }}
.filter-btn.active {{ background: #1e293b; border-color: #1e293b; color: #fff; }}
/* グリッド */
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}}
/* カード */
.card {{
  background: var(--card); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 16px;
  border-top: 4px solid transparent; transition: transform .15s;
}}
.card:hover {{ transform: translateY(-2px); }}
.card.green  {{ border-top-color: var(--green);  }}
.card.yellow {{ border-top-color: var(--yellow); }}
.card.red    {{ border-top-color: var(--red);    }}
.card.gray   {{ border-top-color: var(--gray);   }}
.card-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }}
.card-name {{ font-size: .95rem; font-weight: 700; line-height: 1.3; }}
.card-badge {{
  font-size: .7rem; padding: 2px 8px; border-radius: 10px; white-space: nowrap;
  font-weight: 600; margin-left: 6px;
}}
.card.green  .card-badge {{ background: var(--green-bg);  color: var(--green);  border:1px solid var(--green-bd);  }}
.card.yellow .card-badge {{ background: var(--yellow-bg); color: var(--yellow); border:1px solid var(--yellow-bd); }}
.card.red    .card-badge {{ background: var(--red-bg);    color: var(--red);    border:1px solid var(--red-bd);    }}
.card.gray   .card-badge {{ background: var(--gray-bg);   color: var(--gray);   border:1px solid var(--gray-bd);   }}
.buffer-row {{ display: flex; align-items: baseline; gap: 4px; margin-bottom: 10px; }}
.buffer-num {{ font-size: 2.4rem; font-weight: 800; line-height: 1; }}
.card.green  .buffer-num {{ color: var(--green);  }}
.card.yellow .buffer-num {{ color: var(--yellow); }}
.card.red    .buffer-num {{ color: var(--red);    }}
.card.gray   .buffer-num {{ color: var(--gray);   }}
.buffer-unit {{ font-size: .85rem; color: var(--muted); }}
.card-metrics {{ display: flex; gap: 16px; margin-bottom: 12px; }}
.metric {{ font-size: .8rem; }}
.metric .label {{ color: var(--muted); display: block; margin-bottom: 2px; }}
.metric .value {{ font-weight: 600; }}
.metric .up   {{ color: #16a34a; }}
.metric .down {{ color: #dc2626; }}
.chart-wrap {{ position: relative; height: 80px; }}
/* 概要チャートセクション */
.overview-section {{ margin-bottom: 32px; }}
.panel {{
  background: var(--card); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 20px;
}}
.panel-title {{ font-size: 1rem; font-weight: 700; margin-bottom: 16px; color: var(--text); }}
.overview-chart-wrap {{ position: relative; height: 320px; }}
footer {{
  text-align: center; padding: 24px 16px; color: var(--muted); font-size: .8rem;
  border-top: 1px solid #e2e8f0; margin-top: 32px;
}}
footer a {{ color: #3b82f6; text-decoration: none; }}
footer a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>

{demo_banner}

<header>
  <div class="container" style="padding-top:0;padding-bottom:0">
    <h1>🏭 鉱工業指数 — 業種別在庫バッファー</h1>
    <div class="sub">データ基準月: <strong>{data_date}</strong>　|　2020年基準　|　生成: {generated_at}</div>
  </div>
</header>

<div class="container">

  <div class="summary-bar" style="margin-top:16px">
    {badges}
  </div>

  <div class="filter-bar">
    <button class="filter-btn active" data-filter="all">すべて</button>
    <button class="filter-btn" data-filter="green">🟢 3ヶ月以上</button>
    <button class="filter-btn" data-filter="yellow">🟡 1〜3ヶ月</button>
    <button class="filter-btn" data-filter="red">🔴 1ヶ月未満</button>
  </div>

  <!-- 業種別カード -->
  <div class="grid" id="card-grid"></div>

  <!-- 在庫指数12ヶ月推移 概要チャート -->
  <div class="overview-section" style="margin-top:32px">
    <div class="panel">
      <div class="panel-title">📊 在庫指数 12ヶ月推移（主要業種）</div>
      <div>
        <label for="overview-select" style="font-size:.85rem;color:#64748b">業種を選択: </label>
        <select id="overview-select" style="font-size:.85rem;padding:4px 8px;border-radius:4px;border:1px solid #cbd5e1;margin-left:4px"></select>
      </div>
      <div class="overview-chart-wrap" style="margin-top:12px">
        <canvas id="overview-chart"></canvas>
      </div>
    </div>
  </div>

</div><!-- /container -->

<footer>
  <p>出典: {source_note}</p>
  <p style="margin-top:4px">在庫バッファー（月数）= 在庫指数 ÷ 生産指数　|　前年同月比 = (最新月 − 前年同月) / 前年同月 × 100</p>
</footer>

<script>
const INDUSTRIES = {industries_json};

// ── カード描画 ────────────────────────────────────────────────────────────
const sparkCharts = {{}};

function formatYoY(v) {{
  if (v === null || v === undefined) return '<span class="value">—</span>';
  const cls = v >= 0 ? 'up' : 'down';
  const sign = v >= 0 ? '+' : '';
  return `<span class="value ${{cls}}">${{sign}}${{v.toFixed(1)}}%</span>`;
}}

function buildCard(ind) {{
  const buf = ind.buffer_months;
  const bufTxt = buf !== null ? buf.toFixed(1) : '—';
  const label = {{green:'3ヶ月以上', yellow:'1〜3ヶ月', red:'1ヶ月未満', gray:'データなし'}}[ind.color];
  const prodTxt = ind.production !== null ? ind.production.toFixed(1) : '—';
  const invTxt  = ind.inventory  !== null ? ind.inventory.toFixed(1)  : '—';

  const div = document.createElement('div');
  div.className = `card ${{ind.color}}`;
  div.dataset.color = ind.color;
  div.innerHTML = `
    <div class="card-header">
      <span class="card-name">${{ind.name}}</span>
      <span class="card-badge">${{label}}</span>
    </div>
    <div class="buffer-row">
      <span class="buffer-num">${{bufTxt}}</span>
      <span class="buffer-unit">ヶ月分</span>
    </div>
    <div class="card-metrics">
      <div class="metric">
        <span class="label">生産指数</span>
        <span class="value">${{prodTxt}}</span>
        <span style="font-size:.75rem;color:#94a3b8">前年比 </span>${{formatYoY(ind.prod_yoy)}}
      </div>
      <div class="metric">
        <span class="label">在庫指数</span>
        <span class="value">${{invTxt}}</span>
        <span style="font-size:.75rem;color:#94a3b8">前年比 </span>${{formatYoY(ind.inv_yoy)}}
      </div>
    </div>
    <div class="chart-wrap"><canvas id="spark-${{ind.name}}"></canvas></div>
  `;
  return div;
}}

function renderSparkline(ind) {{
  const canvas = document.getElementById(`spark-${{ind.name}}`);
  if (!canvas || !ind.chart_labels.length) return;
  const color = {{green:'#16a34a',yellow:'#ca8a04',red:'#dc2626',gray:'#9ca3af'}}[ind.color];
  sparkCharts[ind.name] = new Chart(canvas, {{
    type: 'line',
    data: {{
      labels: ind.chart_labels,
      datasets: [
        {{
          label: '在庫指数', data: ind.chart_inv,
          borderColor: color, backgroundColor: color + '22',
          borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.3,
        }},
        {{
          label: '生産指数', data: ind.chart_prod,
          borderColor: '#94a3b8', borderWidth: 1, pointRadius: 0,
          borderDash: [3,2], fill: false, tension: 0.3,
        }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      animation: false,
      plugins: {{ legend: {{ display: false }}, tooltip: {{
        callbacks: {{
          title: ctx => ctx[0].label,
          label: ctx => `${{ctx.dataset.label}}: ${{ctx.parsed.y?.toFixed(1) ?? '—'}}`,
        }}
      }} }},
      scales: {{
        x: {{ display: false }},
        y: {{ display: false }},
      }},
    }},
  }});
}}

// ── フィルター ────────────────────────────────────────────────────────────
let currentFilter = 'all';
function applyFilter(filter) {{
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === filter));
  document.querySelectorAll('#card-grid .card').forEach(card => {{
    card.style.display = (filter === 'all' || card.dataset.color === filter) ? '' : 'none';
  }});
}}
document.querySelectorAll('.filter-btn').forEach(btn => {{
  btn.addEventListener('click', () => applyFilter(btn.dataset.filter));
}});

// ── 初期描画 ─────────────────────────────────────────────────────────────
const grid = document.getElementById('card-grid');
INDUSTRIES.forEach(ind => {{
  const card = buildCard(ind);
  grid.appendChild(card);
  requestAnimationFrame(() => renderSparkline(ind));
}});

// ── 概要チャート ──────────────────────────────────────────────────────────
const select = document.getElementById('overview-select');
let overviewChart = null;

// 主要業種を初期選択（チャートデータがあるもの上位10）
const withData = INDUSTRIES.filter(i => i.chart_labels.length >= 3);
withData.forEach(ind => {{
  const opt = document.createElement('option');
  opt.value = ind.name;
  opt.textContent = ind.name;
  select.appendChild(opt);
}});

function renderOverviewChart(name) {{
  const ind = INDUSTRIES.find(i => i.name === name);
  if (!ind) return;
  const color = {{green:'#16a34a',yellow:'#ca8a04',red:'#dc2626',gray:'#9ca3af'}}[ind.color];
  const ctx = document.getElementById('overview-chart').getContext('2d');
  if (overviewChart) overviewChart.destroy();
  overviewChart = new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: ind.chart_labels,
      datasets: [
        {{
          type: 'bar', label: '在庫指数',
          data: ind.chart_inv, backgroundColor: color + '99',
          borderColor: color, borderWidth: 1, yAxisID: 'y',
        }},
        {{
          type: 'line', label: '生産指数',
          data: ind.chart_prod, borderColor: '#475569',
          borderWidth: 2, pointRadius: 3, fill: false, tension: 0.2, yAxisID: 'y',
        }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'top', labels: {{ font: {{ size: 12 }} }} }},
        tooltip: {{ mode: 'index', intersect: false }},
      }},
      scales: {{
        x: {{ ticks: {{ maxRotation: 45, font: {{ size: 11 }} }} }},
        y: {{
          title: {{ display: true, text: '指数（2020年=100）', font: {{ size: 11 }} }},
          ticks: {{ font: {{ size: 11 }} }},
        }},
      }},
    }},
  }});
}}

if (select.options.length > 0) {{
  renderOverviewChart(select.options[0].value);
  select.addEventListener('change', () => renderOverviewChart(select.value));
}}
</script>
</body>
</html>"""
    return html


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="鉱工業指数 在庫バッファー HTML 生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            例:
              python scripts/fetch_iip.py                          # 自動取得
              python scripts/fetch_iip.py --file iip.xlsx          # ローカル Excel
              python scripts/fetch_iip.py --demo                   # デモデータ
              python scripts/fetch_iip.py --out dashboard/iip.html # 出力先変更
        """),
    )
    parser.add_argument("--file",  help="解析する Excel ファイルのパス（省略時は自動取得）")
    parser.add_argument("--demo",  action="store_true", help="デモデータで強制出力")
    parser.add_argument("--out",   default=str(OUTPUT_FILE), help="出力 HTML パス")
    args = parser.parse_args()

    out_path   = Path(args.out)
    source_url = ""
    parsed     = None

    # ── 1. ローカル Excel 指定 ────────────────────────────────────────────
    if args.file:
        print(f"[LOCAL] {args.file} を解析中…", file=sys.stderr)
        try:
            parsed = parse_iip_excel(args.file)
            source_url = args.file
        except Exception as e:
            print(f"[ERROR] Excel 解析失敗: {e}", file=sys.stderr)
            sys.exit(1)

    # ── 2. デモモード ─────────────────────────────────────────────────────
    elif args.demo:
        parsed = fallback_demo_data()

    else:
        # ── 3. METI ダウンロードページ → Excel ──────────────────────────
        try:
            print(f"[METI] {METI_DOWNLOAD_URL} を取得中…", file=sys.stderr)
            resp = _get(METI_DOWNLOAD_URL)
            resp.encoding = resp.apparent_encoding or "utf-8"
            links = find_excel_links_meti(resp.text)
            print(f"[METI] Excelリンク {len(links)} 件発見", file=sys.stderr)

            for label, url in links[:5]:  # 上位5件を順に試す
                print(f"[METI]   試行: {url}", file=sys.stderr)
                try:
                    buf = download_excel_bytes(url)
                    parsed = parse_iip_excel(buf)
                    source_url = url
                    print(f"[METI] ✓ 成功: {url}", file=sys.stderr)
                    break
                except Exception as ex:
                    print(f"[METI]   スキップ ({ex})", file=sys.stderr)
        except Exception as e:
            print(f"[METI] 取得失敗: {e}", file=sys.stderr)

        # ── 4. e-Stat フォールバック ──────────────────────────────────────
        if parsed is None:
            try:
                print(f"[e-Stat] {ESTAT_FILES_URL} を取得中…", file=sys.stderr)
                resp = _get(ESTAT_FILES_URL)
                resp.encoding = resp.apparent_encoding or "utf-8"
                links = find_excel_links_estat(resp.text)
                print(f"[e-Stat] Excelリンク {len(links)} 件発見", file=sys.stderr)

                for label, url in links[:5]:
                    print(f"[e-Stat]   試行: {url}", file=sys.stderr)
                    try:
                        buf = download_excel_bytes(url)
                        parsed = parse_iip_excel(buf)
                        source_url = url
                        print(f"[e-Stat] ✓ 成功: {url}", file=sys.stderr)
                        break
                    except Exception as ex:
                        print(f"[e-Stat]   スキップ ({ex})", file=sys.stderr)
            except Exception as e:
                print(f"[e-Stat] 取得失敗: {e}", file=sys.stderr)

        # ── 5. デモデータ最終フォールバック ──────────────────────────────
        if parsed is None:
            print("[WARN] すべての取得ソースが失敗。デモデータで出力します。", file=sys.stderr)
            parsed = fallback_demo_data()

    # ── HTML 生成・出力 ───────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = generate_html(parsed, source_url)
    out_path.write_text(html, encoding="utf-8")
    print(f"[OK] {out_path} に出力しました。({len(html):,} bytes)", file=sys.stderr)
    print(str(out_path))  # stdout に出力パスを返す


if __name__ == "__main__":
    main()
