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
from supabase import create_client, Client

# ==================================================
# 1. 設定・定数・Secrets読み込み
# ==================================================

# パスワード認証（簡易）
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

# 認証情報の読み込み
KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
NETKEIBA_EMAIL = st.secrets.get("NETKEIBA_EMAIL", "")
NETKEIBA_PASS = st.secrets.get("NETKEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

# 場所コード変換マップ (競馬ブック -> Netkeiba)
KB_TO_NK_CODE = {
    "10": "44", # 大井
    "11": "45", # 川崎
    "12": "43", # 船橋
    "13": "42"  # 浦和
}
PLACE_NAMES = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}

# ==================================================
# 2. ヘルパー関数 (Supabase, Driver)
# ==================================================

@st.cache_resource
def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY: return None
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def save_history(year, place_code, place_name, month, day, race_num_str, race_id, ai_answer):
    """Supabaseに履歴を保存"""
    supabase = get_supabase_client()
    if not supabase: return
    data = {
        "year": str(year),
        "place_code": str(place_code),
        "place_name": place_name,
        "day": str(day),
        "month": str(month),
        "race_num": race_num_str,
        "race_id": race_id,
        "output_text": ai_answer,
    }
    try:
        supabase.table("history").insert(data).execute()
    except Exception as e:
        st.error(f"Supabase save error: {e}")

def get_driver():
    """Seleniumドライバーの起動設定"""
    options = Options()
    options.add_argument("--headless") # ヘッドレスモード
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1080")
    # Bot検知回避のためのUser-Agent
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    return webdriver.Chrome(options=options)

# ==================================================
# 3. 競馬ブック スクレイピング関数
# ==================================================

def login_keibabook(driver):
    if not KEIBA_ID or not KEIBA_PASS:
        st.warning("⚠️ 競馬ブックのID/PASSが設定されていません。")
        return False
    try:
        driver.get("https://s.keibabook.co.jp/login/login")
        # 要素が見つかるまで待機
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(KEIBA_ID)
        driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys(KEIBA_PASS)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        time.sleep(1)
        return True
    except Exception as e:
        st.error(f"競馬ブック ログインエラー: {e}")
        return False

def fetch_race_ids_from_schedule(driver, year, month, day, target_place_code):
    """日程ページから対象競馬場の全レースIDを取得"""
    date_str = f"{year}{month}{day}"
    url = f"https://s.keibabook.co.jp/chihou/nittei/{date_str}10" # 末尾10は地方トップ固定
    
    st.info(f"📅 日程取得中: {url}")
    driver.get(url)
    time.sleep(1)
    
    soup = BeautifulSoup(driver.page_source, "html.parser")
    race_ids = []
    seen = set()
    
    # リンクからID抽出
    for a in soup.find_all("a", href=True):
        href = a['href']
        match = re.search(r'(\d{16})', href)
        if match:
            rid = match.group(1)
            # IDの6-7文字目(場所コード)が一致するか
            if rid[6:8] == target_place_code:
                if rid not in seen:
                    race_ids.append(rid)
                    seen.add(rid)
    race_ids.sort()
    return race_ids

def parse_race_info(html: str):
    """レース名・条件などを取得"""
    soup = BeautifulSoup(html, "html.parser")
    racetitle = soup.find("div", class_="racetitle")
    if not racetitle: return {}
    
    racemei = racetitle.find("div", class_="racemei")
    race_name = racemei.find_all("p")[1].get_text(strip=True) if racemei and len(racemei.find_all("p")) >= 2 else ""
    
    sub = racetitle.find("div", class_="racetitle_sub")
    cond = sub.find_all("p")[1].get_text(" ", strip=True) if sub and len(sub.find_all("p")) >= 2 else ""
    return {"race_name": race_name, "cond": cond}

def parse_danwa_comments(html: str):
    """談話を取得"""
    soup = BeautifulSoup(html, "html.parser")
    danwa_dict = {}
    table = soup.find("table", class_="danwa")
    if table and table.tbody:
        current_uma = None
        for row in table.tbody.find_all("tr"):
            uma_td = row.find("td", class_="umaban")
            if uma_td:
                current_uma = uma_td.get_text(strip=True)
                continue
            txt_td = row.find("td", class_="danwa")
            if txt_td and current_uma:
                danwa_dict[current_uma] = txt_td.get_text(strip=True)
                current_uma = None
    return danwa_dict

def parse_syutuba_jockey(html: str):
    """出馬表から騎手・乗り替わり情報を取得"""
    soup = BeautifulSoup(html, "html.parser")
    jockey_info = {}
    table = soup.find("table", class_="syutuba_sp")
    if not table or not table.find("tbody"): return {}

    for row in table.find("tbody").find_all("tr"):
        tds = row.find_all("td")
        if not tds: continue
        
        # 1列目が馬番
        umaban_text = tds[0].get_text(strip=True)
        if not umaban_text.isdigit(): continue
        umaban = umaban_text
        
        # 騎手情報
        kisyu_p = row.find("p", class_="kisyu")
        if kisyu_p and kisyu_p.find("a"):
            anchor = kisyu_p.find("a")
            name = anchor.get_text(strip=True)
            is_change = bool(anchor.find("strong"))
            jockey_info[umaban] = {"name": name, "is_change": is_change}
            
    return jockey_info

def parse_cyokyo(html: str):
    """調教データを取得"""
    soup = BeautifulSoup(html, "html.parser")
    cyokyo_dict = {}
    tables = soup.find_all("table", class_="cyokyo")
    for tbl in tables:
        tbody = tbl.find("tbody")
        if not tbody: continue
        rows = tbody.find_all("tr", recursive=False)
        if not rows: continue
        
        h_row = rows[0]
        uma_td = h_row.find("td", class_="umaban")
        name_td = h_row.find("td", class_="kbamei")
        if not uma_td or not name_td: continue
        
        umaban = uma_td.get_text(strip=True)
        bamei = name_td.get_text(" ", strip=True)
        tanpyo = h_row.find("td", class_="tanpyo").get_text(strip=True) if h_row.find("td", class_="tanpyo") else ""
        detail = rows[1].get_text(" ", strip=True) if len(rows) > 1 else ""
        
        cyokyo_dict[umaban] = f"【馬名】{bamei} 【短評】{tanpyo} 【詳細】{detail}"
    return cyokyo_dict

# ==================================================
# 4. Netkeiba スクレイピング関数 (修正版)
# ==================================================

def login_netkeiba(driver):
    """Netkeibaにログイン（修正版）"""
    if not NETKEIBA_EMAIL or not NETKEIBA_PASS:
        st.warning("⚠️ Netkeibaのログイン情報がありません。")
        return False
    try:
        login_url = "https://regist.netkeiba.com/account/?pid=login"
        driver.get(login_url)
        
        # ページ読み込み待機 (最大10秒)
        wait = WebDriverWait(driver, 10)
        
        # ログインフォームが表示されるか確認
        if "logout" in driver.page_source:
            st.info("✅ Netkeiba: 既にログイン済み")
            return True
            
        # ID入力待機
        login_id_input = wait.until(EC.visibility_of_element_located((By.NAME, "login_id")))
        login_id_input.clear()
        login_id_input.send_keys(NETKEIBA_EMAIL)
        
        # パスワード入力
        password_input = driver.find_element(By.NAME, "pswd")
        password_input.clear()
        password_input.send_keys(NETKEIBA_PASS)
        
        # ★修正ポイント: ボタンを探さず、フォームをsubmitする
        password_input.submit()
        
        time.sleep(2) # 遷移待ち
        return True
        
    except Exception as e:
        st.error(f"Netkeiba ログインエラー: {e}")
        return False

def get_netkeiba_speed_url(year, month, day, kb_place_code, race_num):
    """Netkeibaのタイム指数URL生成"""
    nk_place = KB_TO_NK_CODE.get(kb_place_code)
    if not nk_place: return None
    date_str = f"{month.zfill(2)}{day.zfill(2)}"
    race_str = str(race_num).zfill(2)
    # ID構成: YYYY + NK場所コード + MMDD + RR
    race_id = f"{year}{nk_place}{date_str}{race_str}"
    return f"https://nar.netkeiba.com/race/speed.html?race_id={race_id}&type=shutuba&mode=past"

def scrape_netkeiba_speed_index(driver, url, current_place_name):
    """タイム指数ページからデータを取得"""
    data = {}
    try:
        driver.get(url)
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # 現在のレース条件 (例: "大井ダ1400")
        current_condition = ""
        race_data_div = soup.find("div", class_="RaceData01")
        if race_data_div:
            text = race_data_div.get_text()
            dist_match = re.search(r'(\d{3,4})m', text)
            if dist_match:
                track_type = "芝" if "芝" in text else "ダ"
                current_condition = f"{current_place_name}{track_type}{dist_match.group(1)}"
        
        table = soup.find("table", class_="SpeedIndex_Table")
        if not table: return {}
        
        rows = table.find_all("tr", class_="HorseList")
        for row in rows:
            try:
                # 馬番
                umaban_td = row.find("td", class_=re.compile("umaban", re.I))
                if not umaban_td: continue
                umaban = umaban_td.get_text(strip=True)
                
                # 指数データの開始列特定
                cols = row.find_all("td")
                start_idx = -1
                for i, col in enumerate(cols):
                    if "Horse_Name" in " ".join(col.get("class", [])):
                        start_idx = i + 1
                        break
                if start_idx == -1: continue
                
                # 近5走データ取得 (start_idx+1 から 5つ分)
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
                        # 同条件判定 (部分一致)
                        if current_condition and current_condition in course_str:
                            speed_match_list.append(idx_val)
                            
                data[umaban] = {
                    "past": " / ".join(past_list) if past_list else "なし",
                    "speed_index": ", ".join(speed_match_list) if speed_match_list else "該当なし",
                    "condition": current_condition
                }
            except: continue
            
        return data
    except Exception as e:
        return {} # エラー時は空データを返す

# ==================================================
# 5. Dify API連携 (ストリーミング)
# ==================================================

def stream_dify_workflow(full_text: str):
    if not DIFY_API_KEY:
        yield "⚠️ DIFY_API_KEY未設定"
        return
    
    payload = {"inputs": {"text": full_text}, "response_mode": "streaming", "user": "keiba-bot"}
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    
    try:
        res = requests.post("https://api.dify.ai/v1/workflows/run", headers=headers, json=payload, stream=True, timeout=300)
        for line in res.iter_lines():
            if line:
                decoded = line.decode('utf-8')
                if decoded.startswith("data:"):
                    try:
                        data = json.loads(decoded.replace("data: ", ""))
                        if "answer" in data:
                            yield data.get("answer", "")
                    except: pass
    except Exception as e:
        yield f"⚠️ API Error: {str(e)}"

# ==================================================
# 6. メイン画面・実行ロジック
# ==================================================

st.title("🏇 南関×ブック×NK 統合分析Bot")
jst = pytz.timezone('Asia/Tokyo')
now = datetime.now(jst)

# 設定UI
with st.container():
    c1, c2 = st.columns(2)
    with c1: target_date = st.date_input("分析日", now)
    with c2: 
        # 場所コードの選択肢
        PLACE_CODE = st.selectbox("開催場所", ["10", "11", "12", "13"], 
                                  format_func=lambda x: f"{x}: {PLACE_NAMES.get(x)}")
    
    st.write("### 🏁 レース選択")
    all_races = st.checkbox("全レースを一括分析する", value=True)
    target_races = []
    if not all_races:
        cols = st.columns(6)
        for i in range(1, 13):
            with cols[(i-1)//2]:
                if st.checkbox(f"{i}R", key=f"r{i}"): target_races.append(i)
    else:
        target_races = list(range(1, 13))

# 実行ボタン
if st.button("🚀 分析開始", type="primary"):
    # 日付文字列の準備
    date_str = target_date.strftime("%Y%m%d")
    year_str = target_date.strftime("%Y")
    month_str = target_date.strftime("%m")
    day_str = target_date.strftime("%d")
    place_name = PLACE_NAMES.get(PLACE_CODE, "不明")

    driver = get_driver()
    
    try:
        st.info("🔑 各サイトへログイン中...")
        login_keibabook(driver)
        
        # Netkeibaログイン (タイム指数用)
        if login_netkeiba(driver):
            st.success("✅ Netkeibaログイン成功")
        else:
            st.warning("⚠️ Netkeibaログイン失敗 (タイム指数は取得できない可能性があります)")

        st.info("📡 レースIDを取得中...")
        race_ids = fetch_race_ids_from_schedule(driver, year_str, month_str, day_str, PLACE_CODE)
        
        if not race_ids:
            st.error("レース情報が見つかりませんでした。")
        else:
            # 取得したIDごとにループ
            for race_id in race_ids:
                race_num = int(race_id[10:12]) # IDの11,12桁目がレース番号
                if target_races and race_num not in target_races:
                    continue
                
                st.markdown(f"### {place_name} {race_num}R")
                status_area = st.empty()
                result_area = st.empty()
                
                try:
                    status_area.info("📚 データを収集中...")
                    
                    # A. 競馬ブック情報 (談話・騎手・調教)
                    driver.get(f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}")
                    html_danwa = driver.page_source
                    race_meta = parse_race_info(html_danwa)
                    danwa_dict = parse_danwa_comments(html_danwa)
                    
                    driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
                    jockey_dict = parse_syutuba_jockey(driver.page_source)
                    
                    driver.get(f"https://s.keibabook.co.jp/chihou/cyokyo/1/{race_id}")
                    cyokyo_dict = parse_cyokyo(driver.page_source)
                    
                    # B. Netkeiba情報 (タイム指数)
                    nk_url = get_netkeiba_speed_url(year_str, month_str, day_str, PLACE_CODE, race_num)
                    nk_data = {}
                    if nk_url:
                        nk_data = scrape_netkeiba_speed_index(driver, nk_url, place_name)
                    
                    # C. データ結合
                    merged_text = []
                    # 全馬番のリスト作成
                    all_uma = sorted(list(set(list(jockey_dict.keys()) + list(nk_data.keys()))), 
                                     key=lambda x: int(x) if x.isdigit() else 999)
                    
                    for uma in all_uma:
                        j = jockey_dict.get(uma, {"name": "不明", "is_change": False})
                        d = danwa_dict.get(uma, "（なし）")
                        c = cyokyo_dict.get(uma, "（なし）")
                        nk = nk_data.get(uma, {})
                        
                        speed_idx = nk.get("speed_index", "なし")
                        condition = nk.get("condition", "不明")
                        
                        # スピード指数の強調表示
                        speed_txt = ""
                        if speed_idx != "なし" and speed_idx != "該当なし":
                            speed_txt = f"★【絶対スピード指数(同条件:{condition})】: {speed_idx}"
                        
                        alert = "【⚠️乗り替わり】" if j["is_change"] else ""
                        
                        line = (
                            f"▼[馬番{uma}] {j['name']} {alert}\n"
                            f" {speed_txt}\n"
                            f" 近5走指数: {nk.get('past', '-')}\n"
                            f" 談話: {d}\n"
                            f" 調教: {c}"
                        )
                        merged_text.append(line)

                    if not merged_text:
                        status_area.warning("データが取得できませんでした。スキップします。")
                        continue

                    # D. プロンプト作成
                    prompt = (
                        f"レース名: {race_meta.get('race_name','')}\n"
                        f"条件: {race_meta.get('cond','')}\n\n"
                        "以下の各馬のデータ（騎手、タイム指数、談話、調教）から、推奨馬を分析してください。\n"
                        "特に「絶対スピード指数」が高い馬、および「乗り替わり」の有無を重視すること。\n\n"
                        + "\n".join(merged_text)
                    )
                    
                    # E. AI分析 (ストリーミング表示)
                    status_area.info("🤖 AI分析を実行中...")
                    full_ans = ""
                    for chunk in stream_dify_workflow(prompt):
                        full_ans += chunk
                        result_area.markdown(full_ans + "▌")
                    
                    result_area.markdown(full_ans)
                    status_area.success("分析完了")
                    
                    # F. 保存
                    save_history(year_str, PLACE_CODE, place_name, month_str, day_str, f"{race_num:02}", race_id, full_ans)
                    
                except Exception as e:
                    status_area.error(f"エラー発生: {e}")
                
                st.divider()

    finally:
        driver.quit()
