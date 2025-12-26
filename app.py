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
# 1. セキュリティ & Secrets 設定
# ==================================================
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if st.session_state["password_correct"]: return True
    st.title("🔒 ログイン")
    ADMIN_PASS = st.secrets.get("ADMIN_PASSWORD", "admin123")
    user_input = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン"):
        if user_input == ADMIN_PASS:
            st.session_state["password_correct"] = True
            st.rerun()
        else: st.error("パスワードが違います")
    return False

if not check_password(): st.stop()

# 認証情報
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")

# 変換マップ (競馬ブックID -> 南関URL用)
KB_TO_NANKAN_PLACE = {"10": "20", "11": "21", "12": "19", "13": "18"}
PLACE_NAMES = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}

# ==================================================
# 2. スクレイピングコア (対策強化版)
# ==================================================

def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # ボット検知回避用
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    return webdriver.Chrome(options=options)

def get_nankan_base_id(driver, date_str, kb_place_code):
    """南関公式からベースID(14桁)を特定する"""
    nankan_place = KB_TO_NANKAN_PLACE.get(kb_place_code)
    try:
        # クッキーを焼くためにトップへ
        driver.get("https://www.nankankeiba.com/")
        time.sleep(1)
        # プログラムページへ
        url = f"https://www.nankankeiba.com/program/{date_str}{nankan_place}.do"
        driver.get(url)
        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # プランA: リンクから抽出
        links = soup.find_all("a", href=re.compile(r"race_info/\d+"))
        for l in links:
            m = re.search(r'(\d{14})', l['href'])
            if m: return m.group(1)

        # プランB: ページ内の「第xx回」「第yy日」テキストから生成
        txt = soup.get_text()
        k_match = re.search(r'第\s*(\d+)\s*回', txt)
        n_match = re.search(r'第\s*(\d+)\s*日', txt)
        if k_match and n_match:
            k = k_match.group(1).zfill(2)
            n = n_match.group(1).zfill(2)
            return f"{date_str}{nankan_place}{k}{n}"
    except: pass
    return None

def fetch_book_race_ids(driver, date_str, kb_place_code):
    """競馬ブックから対象場所の全レースID(16桁)を取得"""
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10"
    driver.get(url)
    time.sleep(2)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    ids = []
    for a in soup.find_all("a", href=re.compile(r'/chihou/syutuba/\d{16}')):
        rid = re.search(r'(\d{16})', a['href']).group(1)
        if rid[6:8] == kb_place_code and rid not in ids:
            ids.append(rid)
    return sorted(ids)

def fetch_nankan_compatibility(driver, base_id, r_num, h_num):
    """南関の相性表から『厩舎所属馬』成績をピンポイント抽出"""
    url = f"https://www.nankankeiba.com/aisyou_cho/{base_id}{str(r_num).zfill(2)}01{str(h_num).zfill(2)}.do"
    try:
        driver.get(url)
        time.sleep(0.5)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        # nk23新デザイン・旧デザイン両対応
        table = soup.find("table", class_=re.compile(r"nk23_c-table01|maintable"))
        rows = table.find_all("tr") if table else soup.find_all("tr")
        for row in rows:
            if "厩舎所属馬" in row.get_text():
                tds = row.find_all("td")
                return f"勝率{tds[4].text.strip()} / 連対率{tds[5].text.strip()}"
    except: pass
    return "データなし"

def get_race_details(driver, rid):
    """競馬ブックの出馬表・談話・調教を統合取得"""
    data = {"jockeys": {}, "danwas": {}, "trainings": {}}
    # 1. 出馬表 (騎手・乗り替わり)
    driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{rid}")
    soup = BeautifulSoup(driver.page_source, "html.parser")
    tbl = soup.find("table", class_="syutuba_sp")
    if tbl:
        for r in tbl.find_all("tr"):
            tds = r.find_all("td")
            if tds and tds[0].text.strip().isdigit():
                u = tds[0].text.strip()
                kp = r.find("p", class_="kisyu")
                if kp and kp.find("a"):
                    a = kp.find("a")
                    data["jockeys"][u] = {"name": a.text.strip(), "change": bool(a.find("strong"))}
    # 2. 談話
    driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{rid}")
    soup = BeautifulSoup(driver.page_source, "html.parser")
    tbl = soup.find("table", class_="danwa")
    if tbl:
        cur = None
        for r in tbl.find_all("tr"):
            u_td = r.find("td", class_="umaban")
            if u_td: cur = u_td.text.strip()
            txt = r.find("td", class_="danwa")
            if txt and cur: data["danwas"][cur] = txt.text.strip()
    # 3. 調教
    driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{rid}")
    soup = BeautifulSoup(driver.page_source, "html.parser")
    for t in soup.find_all("table", class_="cyokyo"):
        u = t.find("td", class_="umaban")
        tp = t.find("td", class_="tanpyo")
        if u and tp: data["trainings"][u.text.strip()] = tp.text.strip()
    return data

def call_dify(text):
    if not DIFY_API_KEY: return "AI分析キー未設定"
    payload = {"inputs": {"text": text}, "response_mode": "blocking", "user": "keiba-user"}
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    try:
        res = requests.post("https://api.dify.ai/v1/workflows/run", headers=headers, json=payload, timeout=120)
        return res.json().get("data", {}).get("outputs", {}).get("text", "分析完了")
    except: return "AI通信エラー"

# ==================================================
# 3. メインUI
# ==================================================
st.title("🏇 南関×ブック 統合分析Bot")

jst = pytz.timezone('Asia/Tokyo')
now = datetime.now(jst)

# 設定セクション
with st.container():
    c1, c2 = st.columns(2)
    with c1: target_date = st.date_input("分析日", now)
    with c2: place_code = st.selectbox("競馬場", options=list(PLACE_NAMES.keys()), format_func=lambda x: f"{x}:{PLACE_NAMES[x]}", index=1)

    st.write("### 🏁 レース選択")
    all_races = st.checkbox("全レースを一括分析する", value=True)
    selected_races = []
    if not all_races:
        cols = st.columns(6)
        for i in range(1, 13):
            with cols[(i-1)//2]:
                if st.checkbox(f"{i}R", key=f"r{i}"): selected_races.append(i)
    else: selected_races = list(range(1, 13))

# 実行ボタン
if st.button("🚀 分析を開始する", type="primary", use_container_width=True):
    date_str = target_date.strftime("%Y%m%d")
    driver = get_driver()
    
    try:
        st.write("🔑 競馬ブックへログイン...")
        driver.get("https://s.keibabook.co.jp/login/login")
        driver.find_element(By.NAME, "login_id").send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        
        st.write("📡 共通IDを生成中...")
        nankan_base_id = get_nankan_base_id(driver, date_str, place_code)
        book_ids = fetch_book_race_ids(driver, date_str, place_code)
        
        if not nankan_base_id or not book_ids:
            st.error("開催情報が取得できません。日付や場所、休催日を確認してください。")
        else:
            st.success(f"ID紐付け成功: 南関BaseID[{nankan_base_id}]")
            
            for rid in book_ids:
                r_num = int(rid[10:12]) # 競馬ブックIDの11-12桁目
                if r_num not in selected_races: continue
                
                with st.expander(f"📊 {PLACE_NAMES[place_code]} {r_num}R 分析", expanded=True):
                    status = st.empty()
                    status.info(f"{r_num}R データ収集中...")
                    
                    # 競馬ブックデータ取得
                    details = get_race_details(driver, rid)
                    
                    # 南関相性と結合
                    merged = []
                    for uma in sorted(details["jockeys"].keys(), key=int):
                        j = details["jockeys"][uma]
                        compat = fetch_nankan_compatibility(driver, nankan_base_id, r_num, uma)
                        merged.append(
                            f"▼[馬番{uma}] {j['name']} {'【⚠️乗り替わり】' if j['change'] else ''}\n"
                            f" 相性: {compat}\n"
                            f" 談話: {details['danwas'].get(uma, 'なし')}\n"
                            f" 調教: {details['trainings'].get(uma, 'なし')}"
                        )
                    
                    full_prompt = f"{PLACE_NAMES[place_code]} {r_num}R\n" + "\n".join(merged)
                    
                    status.info("🤖 AI分析を実行中...")
                    ans = call_dify(full_prompt)
                    st.markdown(ans)
                    status.success("分析完了")
                    
    finally:
        driver.quit()
