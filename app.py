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
# 2. スクレイピング関数 (強化版)
# ==================================================

def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # ボット検知回避用のUser-Agent
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    return webdriver.Chrome(options=options)

def get_nankan_base_id(driver, date_str, kb_place_code):
    """南関公式からベースIDを取得（より柔軟な検索に変更）"""
    nankan_place = KB_TO_NANKAN_PLACE.get(kb_place_code)
    url = f"https://www.nankankeiba.com/program/{date_str}{nankan_place}.do"
    try:
        driver.get(url)
        time.sleep(3) # 読み込み待ちを長めに
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # race_infoを含む全てのリンクをスキャン
        all_links = soup.find_all("a", href=True)
        for link in all_links:
            href = link['href']
            # YYYYMMDD + PlaceCode を含む14〜16桁の数字を探す
            match = re.search(rf'({date_str}{nankan_place}\d{{4}})', href)
            if match:
                base_id = match.group(1)
                return base_id
        
        # 予備：テキストから開催回を取得（例：第14回）
        page_text = soup.get_text()
        kaisuu_match = re.search(r'第(\d+)回', page_text)
        nichiji_match = re.search(r'第(\d+)日', page_text)
        if kaisuu_match and nichiji_match:
            k = kaisuu_match.group(1).zfill(2)
            n = nichiji_match.group(1).zfill(2)
            return f"{date_str}{nankan_place}{k}{n}"
            
    except Exception as e:
        st.error(f"南関ベースID取得エラー: {e}")
    return None

def fetch_book_race_ids(driver, date_str, kb_place_code):
    """競馬ブックから全レースID取得"""
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
                if rid[6:8] == kb_place_code and rid not in ids:
                    ids.append(rid)
        return sorted(ids)
    except: return []

def fetch_jockey_trainer_stats(driver, base_id, r_num, h_num):
    """南関公式から相性データ(nk23対応)を取得"""
    # base_id(14桁) + レース(2桁) + 固定(01) + 馬番(2桁)
    url = f"https://www.nankankeiba.com/aisyou_cho/{base_id}{str(r_num).zfill(2)}01{str(h_num).zfill(2)}.do"
    try:
        driver.get(url)
        time.sleep(0.5)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        # 新サイト構造(nk23)のテーブルを探す
        table = soup.find("table", class_=re.compile("nk23_c-table01"))
        if not table:
            # 旧サイト構造のフォールバック
            rows = soup.find_all("tr")
        else:
            rows = table.find_all("tr")
            
        for row in rows:
            if "厩舎所属馬" in row.get_text():
                tds = row.find_all("td")
                if len(tds) >= 6:
                    return f"勝率{tds[4].text.strip()} 連対率{tds[5].text.strip()}"
    except: pass
    return "データなし"

def parse_book_data(driver, race_id):
    """競馬ブックの出馬表・談話・調教を取得"""
    # 出馬表
    driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
    time.sleep(1)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    jockeys = {}
    table = soup.find("table", class_="syutuba_sp")
    if table:
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if tds and tds[0].text.strip().isdigit():
                u = tds[0].text.strip()
                kp = row.find("p", class_="kisyu")
                if kp and kp.find("a"):
                    a = kp.find("a")
                    jockeys[u] = {"name": a.text.strip(), "is_change": bool(a.find("strong"))}
    
    # 談話
    driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
    time.sleep(1)
    soup_d = BeautifulSoup(driver.page_source, "html.parser")
    danwas = {}
    tbl_d = soup_d.find("table", class_="danwa")
    if tbl_d:
        cur = None
        for r in tbl_d.find_all("tr"):
            u_td = r.find("td", class_="umaban")
            if u_td: cur = u_td.text.strip()
            txt = r.find("td", class_="danwa")
            if txt and cur: danwas[cur] = txt.text.strip()
            
    # 調教
    driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
    time.sleep(1)
    soup_c = BeautifulSoup(driver.page_source, "html.parser")
    cyokyos = {}
    tbl_c = soup_c.find_all("table", class_="cyokyo")
    for t in tbl_c:
        u_td = t.find("td", class_="umaban")
        tanpyo = t.find("td", class_="tanpyo")
        if u_td and tanpyo:
            cyokyos[u_td.text.strip()] = tanpyo.text.strip()

    return jockeys, danwas, cyokyos

def run_dify(text):
    if not DIFY_API_KEY: return "Dify API Key未設定"
    payload = {"inputs": {"text": text}, "response_mode": "blocking", "user": "bot"}
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    try:
        res = requests.post("https://api.dify.ai/v1/workflows/run", headers=headers, json=payload, timeout=60)
        return res.json().get("data", {}).get("outputs", {}).get("text", "分析完了（出力テキスト取得失敗）")
    except Exception as e: return f"Difyエラー: {e}"

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
        st.write("🔑 ログイン中...")
        driver.get("https://s.keibabook.co.jp/login/login")
        driver.find_element(By.NAME, "login_id").send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        
        st.write("📡 開催情報を取得中...")
        nankan_base_id = get_nankan_base_id(driver, date_str, place_code)
        book_ids = fetch_book_race_ids(driver, date_str, place_code)
        
        if not book_ids:
            st.error(f"競馬ブックで {date_str} {PLACE_NAMES[place_code]} のレースIDが見つかりませんでした。")
        elif not nankan_base_id:
            st.error("南関公式のベースIDを取得できませんでした。サイト構成が変更されたか、ボット検知された可能性があります。")
        else:
            st.success(f"南関ベースID特定: {nankan_base_id}")
            for rid in book_ids:
                # 競馬ブックIDからレース番号を抽出 (重要: rid[10:12]がR番号)
                r_num = int(rid[10:12])
                if r_num not in selected: continue
                
                with st.expander(f"📊 {PLACE_NAMES[place_code]} {r_num}R (ID:{rid})", expanded=True):
                    status = st.empty()
                    status.info(f"{r_num}R の詳細データを収集中...")
                    
                    # データ取得
                    jockeys, danwas, cyokyos = parse_book_data(driver, rid)
                    
                    merged = []
                    # 出馬表の馬番順に処理
                    for uma in sorted(jockeys.keys(), key=int):
                        info = jockeys[uma]
                        compat = fetch_jockey_trainer_stats(driver, nankan_base_id, r_num, uma)
                        dan = danwas.get(uma, "（なし）")
                        cyo = cyokyos.get(uma, "（短評なし）")
                        alert = "【⚠️乗り替わり】" if info["is_change"] else ""
                        merged.append(f"▼[馬番{uma}] {info['name']} {alert}\n 相性: {compat}\n 談話: {dan}\n 調教: {cyo}")
                    
                    prompt = f"{PLACE_NAMES[place_code]} {r_num}R 分析用データ\n\n" + "\n".join(merged)
                    
                    status.info("🤖 AI分析を実行中...")
                    ans = run_dify(prompt)
                    st.markdown(ans)
                    status.success("完了")
                    
    finally:
        driver.quit()
