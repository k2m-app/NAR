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
# 1. セキュリティ & 設定
# ==================================================
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if st.session_state["password_correct"]: return True
    st.title("🔒 ログイン")
    ADMIN_PASS = st.secrets.get("ADMIN_PASSWORD", "admin123")
    user_input = st.text_input("パスワード", type="password")
    if st.button("ログイン"):
        if user_input == ADMIN_PASS:
            st.session_state["password_correct"] = True
            st.rerun()
        else: st.error("パスワードが違います")
    return False

if not check_password(): st.stop()

# Secrets
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")

# 変換マップ
KB_TO_NANKAN_PLACE = {"10": "20", "11": "21", "12": "19", "13": "18"}
PLACE_NAMES = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}

# ==================================================
# 2. スクレイピング関数
# ==================================================

def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)

def get_nankan_base_id(driver, date_str, kb_place_code):
    """南関公式からベースID(14桁)を取得"""
    nankan_place = KB_TO_NANKAN_PLACE.get(kb_place_code)
    url = f"https://www.nankankeiba.com/program/{date_str}{nankan_place}.do"
    try:
        driver.get(url)
        time.sleep(2)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        # race_infoのリンクから14桁を抽出
        link = soup.find("a", href=re.compile(r"/race_info/\d+\.do"))
        if link:
            match = re.search(r'(\d{14})', link['href'])
            return match.group(1)
    except: return None
    return None

def fetch_book_race_ids(driver, date_str, kb_place_code):
    """競馬ブックからその日の全レースID(16桁)を取得"""
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10"
    try:
        driver.get(url)
        time.sleep(2)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        ids = []
        for a in soup.find_all("a", href=True):
            match = re.search(r'(\d{16})', a['href'])
            if match:
                rid = match.group(1)
                # IDの6-8桁目が場所コードと一致するか
                if rid[6:8] == kb_place_code and rid not in ids:
                    ids.append(rid)
        return sorted(ids)
    except: return []

def fetch_jockey_trainer_stats(driver, base_id, r_num, h_num):
    """南関公式から相性データを取得"""
    url = f"https://www.nankankeiba.com/aisyou_cho/{base_id}{str(r_num).zfill(2)}01{str(h_num).zfill(2)}.do"
    try:
        driver.get(url)
        time.sleep(0.5)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        rows = soup.find_all("tr")
        for row in rows:
            if "厩舎所属馬" in row.get_text():
                tds = row.find_all("td")
                return f"勝率{tds[4].text} 連対{tds[5].text}"
    except: pass
    return "データなし"

# 競馬ブックのパース系 (既存コードを圧縮)
def parse_book_data(driver, race_id):
    # 出馬表
    driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
    soup = BeautifulSoup(driver.page_source, "html.parser")
    jockeys = {}
    table = soup.find("table", class_="syutuba_sp")
    if table:
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if tds and tds[0].text.isdigit():
                u = tds[0].text.strip()
                kp = row.find("p", class_="kisyu")
                if kp and kp.find("a"):
                    a = kp.find("a")
                    jockeys[u] = {"name": a.text.strip(), "is_change": bool(a.find("strong"))}
    # 談話
    driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
    soup = BeautifulSoup(driver.page_source, "html.parser")
    danwas = {}
    tbl = soup.find("table", class_="danwa")
    if tbl:
        cur = None
        for r in tbl.find_all("tr"):
            u_td = r.find("td", class_="umaban")
            if u_td: cur = u_td.text.strip()
            txt = r.find("td", class_="danwa")
            if txt and cur: danwas[cur] = txt.text.strip()
    return jockeys, danwas

# Dify連携
def run_dify(text):
    if not DIFY_API_KEY: return "Dify API Key未設定"
    payload = {"inputs": {"text": text}, "response_mode": "blocking", "user": "bot"}
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    try:
        res = requests.post("https://api.dify.ai/v1/workflows/run", headers=headers, json=payload)
        return res.json().get("data", {}).get("outputs", {}).get("text", "分析失敗")
    except: return "Dify通信エラー"

# ==================================================
# 3. UI & 実行
# ==================================================
st.title("🏇 南関×ブック 相性分析Bot")

jst = pytz.timezone('Asia/Tokyo')
today_jst = datetime.now(jst)

col1, col2 = st.columns(2)
with col1: target_date = st.date_input("分析日", today_jst)
with col2: place_code = st.selectbox("競馬場", options=list(PLACE_NAMES.keys()), format_func=lambda x: f"{x}:{PLACE_NAMES[x]}", index=1)

all_races = st.checkbox("全12レース一括分析", value=True)
selected = []
if not all_races:
    cols = st.columns(6)
    for i in range(1, 13):
        with cols[(i-1)//2]:
            if st.checkbox(f"{i}R", key=f"r{i}"): selected.append(i)
else: selected = list(range(1, 13))

if st.button("🚀 分析を開始する", type="primary", use_container_width=True):
    date_str = target_date.strftime("%Y%m%d")
    driver = get_driver()
    
    try:
        # 1. ログイン
        st.write("🔑 競馬ブックにログイン中...")
        driver.get("https://s.keibabook.co.jp/login/login")
        driver.find_element(By.NAME, "login_id").send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        
        # 2. ID取得
        st.write("📡 開催情報を取得中...")
        nankan_base_id = get_nankan_base_id(driver, date_str, place_code)
        book_ids = fetch_book_race_ids(driver, date_str, place_code)
        
        if not book_ids:
            st.error("指定日のレース情報が見つかりませんでした。")
        elif not nankan_base_id:
            st.error("南関東競馬のベースIDが取得できませんでした。休催日ではないか確認してください。")
        else:
            # 3. 各レース実行
            for rid in book_ids:
                r_num = int(rid[-2:])
                if r_num not in selected: continue
                
                with st.expander(f"📊 {r_num}R 分析中...", expanded=True):
                    status = st.empty()
                    status.info("データ収集中...")
                    
                    # データ取得
                    jockeys, danwas = parse_book_data(driver, rid)
                    
                    merged = []
                    for uma, info in jockeys.items():
                        # 相性取得
                        compat = fetch_jockey_trainer_stats(driver, nankan_base_id, r_num, uma)
                        dan = danwas.get(uma, "（なし）")
                        alert = "【⚠️乗り替わり】" if info["is_change"] else ""
                        merged.append(f"▼[馬番{uma}] {info['name']} {alert}\n 相性: {compat}\n 談話: {dan}")
                    
                    prompt = f"{PLACE_NAMES[place_code]} {r_num}R 分析データ\n\n" + "\n".join(merged)
                    
                    status.info("🤖 AI分析中...")
                    ans = run_dify(prompt)
                    st.markdown(ans)
                    status.success("完了")
                    
    finally:
        driver.quit()
