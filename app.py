import time
import json
import re
import requests
import streamlit as st
from datetime import datetime
import pytz
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup

# ==================================================
# 1. セキュリティ設定 (パスワードガード)
# ==================================================
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if st.session_state["password_correct"]:
        return True

    st.title("🔒 競馬分析システム ログイン")
    ADMIN_PASS = st.secrets.get("ADMIN_PASSWORD", "admin123")
    user_input = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン"):
        if user_input == ADMIN_PASS:
            st.session_state["password_correct"] = True
            st.rerun()
        else:
            st.error("パスワードが正しくありません。")
    return False

if not check_password():
    st.stop()

# ==================================================
# 2. 基本設定・定数
# ==================================================
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")

# 競馬場コード変換 (ブック -> 南関)
KB_TO_NANKAN_PLACE = {"10": "20", "11": "21", "12": "19", "13": "18"}
PLACE_NAMES = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}

# 日本時間の日付取得
jst = pytz.timezone('Asia/Tokyo')
today = datetime.now(jst)

# ==================================================
# 3. スクレイピング・ロジック
# ==================================================

def get_nankan_base_id(driver, date_str, kb_place_code):
    """南関の開催回・日数を含むベースID(14桁)を取得"""
    nankan_place = KB_TO_NANKAN_PLACE.get(kb_place_code)
    url = f"https://www.nankankeiba.com/program/{date_str}{nankan_place}.do"
    try:
        driver.get(url)
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        link = soup.find("a", href=re.compile(r"/race_info/\d+\.do"))
        if link:
            match = re.search(r'(\d{14})\d{2}\.do', link['href'])
            return match.group(1) if match else None
    except Exception as e:
        st.error(f"南関ベースID取得失敗: {e}")
    return None

def fetch_nankan_compatibility(driver, base_id, race_num, horse_num):
    """馬番ごとの相性ページから『厩舎所属馬』の成績を抽出"""
    r_str = str(race_num).zfill(2)
    h_str = str(horse_num).zfill(2)
    url = f"https://www.nankankeiba.com/aisyou_cho/{base_id}{r_str}01{h_str}.do"
    try:
        driver.get(url)
        time.sleep(0.5)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        table = soup.find("table", class_="nk23_c-table01__table")
        if not table: return "データなし"
        
        for row in table.find_all("tr"):
            if "厩舎所属馬" in row.get_text():
                cols = row.find_all("td")
                if len(cols) >= 6:
                    return f"勝率{cols[4].get_text(strip=True)} 連対率{cols[5].get_text(strip=True)}"
    except: return "取得エラー"
    return "データなし"

# --- 競馬ブック系のパース関数 (既存ロジックを統合) ---
def parse_syutuba_jockey(html):
    soup = BeautifulSoup(html, "html.parser")
    jockey_info = {}
    table = soup.find("table", class_="syutuba_sp")
    if not table: return {}
    for row in table.find_all("tr"):
        tds = row.find_all("td")
        if not tds or not tds[0].text.isdigit(): continue
        umaban = tds[0].text.strip()
        kisyu_p = row.find("p", class_="kisyu")
        if kisyu_p and kisyu_p.find("a"):
            anchor = kisyu_p.find("a")
            jockey_info[umaban] = {
                "name": anchor.get_text(strip=True),
                "is_change": bool(anchor.find("strong"))
            }
    return jockey_info

# ==================================================
# 4. メインUIレイアウト
# ==================================================
st.title("🏇 南関競馬 騎手×調教師 相性分析Bot")
st.markdown("競馬ブックの談話・調教情報と、南関公式サイトの相性データを自動突合します。")

# --- 設定エリア ---
with st.container():
    col1, col2, col3 = st.columns(3)
    with col1:
        target_date = st.date_input("分析対象日", today)
    with col2:
        place_code = st.selectbox("競馬場", options=list(PLACE_NAMES.keys()), format_func=lambda x: f"{x}:{PLACE_NAMES[x]}", index=1)
    with col3:
        st.write(" ") # 余白

    st.write("### 🏁 レース選択")
    all_races = st.checkbox("全レース（1〜12R）を選択", value=True)
    
    selected_races = []
    if not all_races:
        race_cols = st.columns(6)
        for i in range(1, 13):
            with race_cols[(i-1)//2]:
                if st.checkbox(f"{i}R", key=f"r{i}"):
                    selected_races.append(i)
    else:
        selected_races = list(range(1, 13))

# --- 実行ボタン ---
if st.button("🚀 分析を開始する", type="primary", use_container_width=True):
    if not selected_races:
        st.warning("分析対象のレースを選択してください。")
    else:
        # 日付フォーマット
        date_str = target_date.strftime("%Y%m%d")
        y, m, d = date_str[:4], date_str[4:6], date_str[6:8]
        
        # Selenium 起動
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        driver = webdriver.Chrome(options=options)

        try:
            # 1. ログイン処理
            driver.get("https://s.keibabook.co.jp/login/login")
            driver.find_element(By.NAME, "login_id").send_keys(KEIBA_ID)
            driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
            driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
            
            # 2. 南関ベースIDの取得
            nankan_base_id = get_nankan_base_id(driver, date_str, place_code)
            
            # 3. レースごとのループ
            for r_num in selected_races:
                st.subheader(f"📍 {PLACE_NAMES[place_code]} {r_num}R")
                
                # A. ブック出馬表から騎手情報取得 (本来はここでIDを逆算するが簡易化)
                # 注: 実際には日程ページからブックの16桁IDを取得する工程が必要
                # ここでは前述の `fetch_race_ids_from_schedule` を使う想定
                
                # --- [データ収集・突合イメージ] ---
                # comp_stats = fetch_nankan_compatibility(driver, nankan_base_id, r_num, horse_num)
                # ... 結合処理 ...
                # st.write(f"馬番X: {comp_stats}")
                
                st.info(f"{r_num}R のデータを収集中... (南関ベースID: {nankan_base_id})")
                
                # AI分析呼び出し... (省略)
                
        finally:
            driver.quit()
