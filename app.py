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

# 認証情報 (secrets.toml)
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
# ★追加: Netkeiba情報
NETKEIBA_EMAIL = st.secrets.get("NETKEIBA_EMAIL", "") 
NETKEIBA_PASS = st.secrets.get("NETKEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")

# 変換マップ 
# 競馬ブック場所コード -> Netkeiba場所コード
KB_TO_NK_CODE = {
    "10": "44", # 大井
    "11": "45", # 川崎
    "12": "43", # 船橋
    "13": "42"  # 浦和
}
PLACE_NAMES = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}

# ==================================================
# 2. スクレイピングコア
# ==================================================

def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1080")
    # ボット検知回避用
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    return webdriver.Chrome(options=options)

# --- 競馬ブック関連 ---

def fetch_book_race_ids(driver, date_str, kb_place_code):
    """競馬ブックから対象場所の全レースID(16桁)を取得"""
    # 競馬ブックの日程ページ (例: /chihou/nittei/2025122610)
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}{kb_place_code}"
    driver.get(url)
    time.sleep(1)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    ids = []
    # リンクからID抽出
    for a in soup.find_all("a", href=re.compile(r'/chihou/syutuba/\d{16}')):
        rid = re.search(r'(\d{16})', a['href']).group(1)
        # IDの場所コード(7-8文字目)が一致するか確認
        if rid[6:8] == kb_place_code and rid not in ids:
            ids.append(rid)
    return sorted(ids)

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
            # 馬番がある行のみ処理
            if tds and tds[0].text.strip().isdigit():
                u = tds[0].text.strip() # 馬番
                kp = r.find("p", class_="kisyu")
                if kp and kp.find("a"):
                    a = kp.find("a")
                    # strongタグがあれば乗り替わり
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

# --- Netkeiba関連 (新ロジック) ---

def login_netkeiba(driver):
    """Netkeibaへログイン"""
    if not NETKEIBA_EMAIL or not NETKEIBA_PASS:
        return False
    try:
        driver.get("https://regist.netkeiba.com/account/?pid=login")
        time.sleep(1)
        if "logout" in driver.page_source: return True
        
        driver.find_element(By.NAME, "login_id").send_keys(NETKEIBA_EMAIL)
        driver.find_element(By.NAME, "pswd").send_keys(NETKEIBA_PASS)
        driver.find_element(By.CLASS_NAME, "SubmitBtn").click()
        time.sleep(1)
        return True
    except:
        return False

def get_netkeiba_speed_url(year, month, day, kb_place_code, race_num):
    """Netkeibaタイム指数ページのURL生成"""
    nk_place = KB_TO_NK_CODE.get(kb_place_code)
    if not nk_place: return None
    date_str = f"{month.zfill(2)}{day.zfill(2)}"
    race_str = str(race_num).zfill(2)
    # ID構成: YYYY + 場所(2) + MMDD + R(2)
    race_id = f"{year}{nk_place}{date_str}{race_str}"
    return f"https://nar.netkeiba.com/race/speed.html?race_id={race_id}&type=shutuba&mode=past"

def scrape_netkeiba_speed_index(driver, url, current_place_name):
    """タイム指数ページからデータを取得"""
    data = {}
    try:
        driver.get(url)
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # 現在のレース条件取得 (例: "大井ダ1400")
        current_condition = ""
        race_data_div = soup.find("div", class_="RaceData01")
        if race_data_div:
            text = race_data_div.get_text()
            dist_match = re.search(r'(\d{3,4})m', text)
            if dist_match:
                track_type = "芝" if "芝" in text else "ダ"
                current_condition = f"{current_place_name}{track_type}{dist_match.group(1)}"

        # テーブル解析
        table = soup.find("table", class_="SpeedIndex_Table")
        if not table: return {}

        rows = table.find_all("tr", class_="HorseList")
        for row in rows:
            try:
                # 馬番取得
                umaban_td = row.find("td", class_=re.compile("umaban", re.I))
                if not umaban_td: continue
                umaban = umaban_td.get_text(strip=True)

                # 指数データの開始位置特定
                cols = row.find_all("td")
                start_idx = -1
                for i, col in enumerate(cols):
                    if "Horse_Name" in " ".join(col.get("class", [])):
                        start_idx = i + 1
                        break
                if start_idx == -1: continue

                # 近5走データの抽出
                target_cols = cols[start_idx+1 : start_idx+6]
                past_list = []
                speed_match_list = []

                for td in target_cols:
                    course_span = td.find("span")
                    if not course_span: continue
                    course_str = course_span.get_text(strip=True)
                    
                    idx_a = td.find("a")
                    idx_val = idx_a.get_text(strip=True) if idx_a else "-"
                    
                    if idx_val.isdigit():
                        past_list.append(f"{course_str}({idx_val})")
                        # 同条件判定
                        if current_condition and current_condition in course_str:
                            speed_match_list.append(idx_val)
                
                data[umaban] = {
                    "past": " / ".join(past_list) if past_list else "なし",
                    "speed_index": ", ".join(speed_match_list) if speed_match_list else "該当なし",
                    "condition": current_condition
                }
            except: continue
        return data
    except: return {}

# --- AI連携 ---

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
st.title("🏇 南関×ブック×Netkeiba 統合分析Bot")

jst = pytz.timezone('Asia/Tokyo')
now = datetime.now(jst)

# 設定セクション
with st.container():
    c1, c2 = st.columns(2)
    with c1: target_date = st.date_input("分析日", now)
    with c2: place_code = st.selectbox("競馬場", options=list(PLACE_NAMES.keys()), format_func=lambda x: f"{x}:{PLACE_NAMES[x]}", index=0)

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
    year_str = target_date.strftime("%Y")
    month_str = target_date.strftime("%m")
    day_str = target_date.strftime("%d")
    
    driver = get_driver()
    
    try:
        # 1. ログイン処理
        st.write("🔑 サイトへログイン中...")
        # 競馬ブック
        driver.get("https://s.keibabook.co.jp/login/login")
        driver.find_element(By.NAME, "login_id").send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        
        # Netkeiba
        if login_netkeiba(driver):
            st.success("✅ Netkeibaログイン成功")
        else:
            st.warning("⚠️ Netkeibaログイン失敗（タイム指数が取得できない可能性があります）")
        
        st.write("📡 レース情報を収集中...")
        book_ids = fetch_book_race_ids(driver, date_str, place_code)
        
        if not book_ids:
            st.error("開催情報が取得できません。日付や場所、休催日を確認してください。")
        else:
            current_place_name = PLACE_NAMES[place_code]
            
            for rid in book_ids:
                r_num = int(rid[10:12]) # IDからレース番号抽出
                if r_num not in selected_races: continue
                
                with st.expander(f"📊 {current_place_name} {r_num}R 分析", expanded=True):
                    status = st.empty()
                    status.info(f"{r_num}R データ収集中...")
                    
                    # A. 競馬ブックデータ取得
                    details = get_race_details(driver, rid)
                    
                    # B. Netkeibaタイム指数取得
                    nk_url = get_netkeiba_speed_url(year_str, month_str, day_str, place_code, r_num)
                    nk_data = {}
                    if nk_url:
                        nk_data = scrape_netkeiba_speed_index(driver, nk_url, current_place_name)
                    
                    # C. データ結合
                    merged = []
                    # 馬番順にソート (ブックの馬番を正とする)
                    all_uma = sorted(details["jockeys"].keys(), key=int)
                    
                    for uma in all_uma:
                        # ブック情報
                        j = details["jockeys"][uma]
                        danwa = details["danwas"].get(uma, "なし")
                        training = details["trainings"].get(uma, "なし")
                        
                        # Netkeiba情報
                        nk_info = nk_data.get(uma, {})
                        past_log = nk_info.get("past", "データなし")
                        speed_idx = nk_info.get("speed_index", "なし")
                        condition = nk_info.get("condition", "不明")
                        
                        # 絶対スピード指数の強調
                        speed_text = ""
                        if speed_idx != "なし" and speed_idx != "該当なし":
                            speed_text = f"★【絶対スピード指数(同条件:{condition})】: {speed_idx}"
                        else:
                            speed_text = " (同条件での指数記録なし)"

                        merged.append(
                            f"▼[馬番{uma}] {j['name']} {'【⚠️乗り替わり】' if j['change'] else ''}\n"
                            f" {speed_text}\n"
                            f" 近5走指数: {past_log}\n"
                            f" 談話: {danwa}\n"
                            f" 調教: {training}"
                        )
                    
                    full_prompt = f"{current_place_name} {r_num}R\n" + "\n".join(merged)
                    
                    # D. AI分析
                    status.info("🤖 AI分析を実行中...")
                    # デバッグ用にプロンプトを確認したい場合はコメントアウトを外す
                    # st.text_area("prompt", full_prompt) 
                    ans = call_dify(full_prompt)
                    st.markdown(ans)
                    status.success("分析完了")
                    
    finally:
        driver.quit()
