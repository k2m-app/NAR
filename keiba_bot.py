import time
import json
import re
import requests
import streamlit as st
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ==================================================
# 【設定エリア】secretsから読み込み
# ==================================================

KEIBA_ID = st.secrets.get("KEIBA_ID", "")
KEIBA_PASS = st.secrets.get("KEIBA_PASS", "")
DIFY_API_KEY = st.secrets.get("DIFY_API_KEY", "")

SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

# デフォルト変数（app.pyから上書きされる）
YEAR = "2025"
PLACE_CODE = "11"
MONTH = "12"
DAY = "16"

def set_race_params(year, place_code, month, day):
    """app.py から開催情報を差し替えるための関数"""
    global YEAR, PLACE_CODE, MONTH, DAY
    YEAR = str(year)
    PLACE_CODE = str(place_code).zfill(2)
    MONTH = str(month).zfill(2)
    DAY = str(day).zfill(2)

# ==================================================
# Supabase クライアント
# ==================================================
@st.cache_resource
def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def save_history(year, place_code, place_name, month, day, race_num_str, race_id, ai_answer):
    supabase = get_supabase_client()
    if supabase is None:
        return
    data = {
        "year": str(year),
        "kai": "", 
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
        print("Supabase insert error:", e)

# ==================================================
# HTML パース関数群
# ==================================================

def parse_race_info(html: str):
    soup = BeautifulSoup(html, "html.parser")
    racetitle = soup.find("div", class_="racetitle")
    if not racetitle:
        return {"date_meet": "", "race_name": "", "cond1": "", "course_line": ""}
    racemei = racetitle.find("div", class_="racemei")
    date_meet = ""
    race_name = ""
    if racemei:
        ps = racemei.find_all("p")
        if len(ps) >= 1: date_meet = ps[0].get_text(strip=True)
        if len(ps) >= 2: race_name = ps[1].get_text(strip=True)
    racetitle_sub = racetitle.find("div", class_="racetitle_sub")
    cond1 = ""
    course_line = ""
    if racetitle_sub:
        sub_ps = racetitle_sub.find_all("p")
        if len(sub_ps) >= 1: cond1 = sub_ps[0].get_text(strip=True)
        if len(sub_ps) >= 2: course_line = sub_ps[1].get_text(" ", strip=True)
    return {"date_meet": date_meet, "race_name": race_name, "cond1": cond1, "course_line": course_line}

def parse_danwa_comments(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="danwa")
    if not table or not table.tbody:
        return {}
    danwa_dict = {}
    current = None
    for row in table.tbody.find_all("tr"):
        uma_td = row.find("td", class_="umaban")
        if uma_td:
            current = uma_td.get_text(strip=True)
            continue
        danwa_td = row.find("td", class_="danwa")
        if danwa_td and current:
            danwa_dict[current] = danwa_td.get_text(strip=True)
            current = None
    return danwa_dict

def parse_cyokyo(html: str):
    soup = BeautifulSoup(html, "html.parser")
    cyokyo_dict = {}
    section = None
    h2 = soup.find("h2", string=lambda s: s and "調教" in s)
    if h2:
        midasi_div = h2.find_parent("div", class_="midasi")
        if midasi_div:
            section = midasi_div.find_next_sibling("div", class_="section")
    if section is None: section = soup
    tables = section.find_all("table", class_="cyokyo")
    for tbl in tables:
        tbody = tbl.find("tbody")
        if not tbody: continue
        rows = tbody.find_all("tr", recursive=False)
        if not rows: continue
        header = rows[0]
        uma_td = header.find("td", class_="umaban")
        name_td = header.find("td", class_="kbamei")
        if not uma_td or not name_td: continue
        umaban = uma_td.get_text(strip=True)
        bamei = name_td.get_text(" ", strip=True)
        tanpyo_td = header.find("td", class_="tanpyo")
        tanpyo = tanpyo_td.get_text(strip=True) if tanpyo_td else ""
        detail_row = rows[1] if len(rows) >= 2 else None
        detail_text = ""
        if detail_row: detail_text = detail_row.get_text(" ", strip=True)
        final_text = f"【馬名】{bamei}（馬番{umaban}） 【短評】{tanpyo} 【調教詳細】{detail_text}"
        cyokyo_dict[umaban] = final_text
    return cyokyo_dict

def parse_syutuba_jockey(html: str):
    soup = BeautifulSoup(html, "html.parser")
    jockey_info = {}
    sections = soup.find_all("div", class_="section")
    for sec in sections:
        umaban_div = sec.find("div", class_="umaban")
        if not umaban_div: continue
        umaban = umaban_div.get_text(strip=True)
        kisyu_p = sec.find("p", class_="kisyu")
        if kisyu_p:
            is_change = True if kisyu_p.find("strong") else False
            name = kisyu_p.get_text(strip=True)
            jockey_info[umaban] = {"name": name, "is_change": is_change}
    return jockey_info

# ==================================================
# URL / ID 制御ロジック (ここが重要)
# ==================================================
BASE_URL = "https://s.keibabook.co.jp"

def get_base_race_id(driver, year, month, day, place_name):
    """
    開催日カレンダーから、その日の「1RのID」を取得する。
    これにより「開催回数」「日数」の変動に自動対応する。
    """
    # 開催日ページへアクセス
    date_str = f"{year}{month}{day}"
    url = f"{BASE_URL}/chihou/kaisai_bi/{date_str}"
    
    st.info(f"🔍 開催情報からIDを特定中... ({url})")
    driver.get(url)
    time.sleep(1)
    
    try:
        # 1. 競馬場名のリンクを探してクリック (例: "川崎")
        # 部分一致検索で対応
        links = driver.find_elements(By.TAG_NAME, "a")
        target_link = None
        for link in links:
            if place_name in link.text:
                target_link = link
                break
        
        if not target_link:
             st.error(f"⚠️ 指定された日付に「{place_name}」の開催が見つかりませんでした。日付か競馬場を確認してください。")
             return None
        
        target_link.click()
        time.sleep(1)
        
        # 2. ページ遷移後のURLまたはリンクからIDを探す
        # 多くの場合、レース一覧ページか1Rへ飛ぶ
        
        current_url = driver.current_url
        
        # URL自体にID(16桁)が含まれているかチェック
        match = re.search(r'(\d{16})', current_url)
        if match:
            base_id = match.group(1)
            # レース番号部分(10-12文字目)を01に正規化して返す
            normalized_id = base_id[:10] + "01" + base_id[12:]
            st.info(f"✅ ID特定成功: {normalized_id} (1R基準)")
            return normalized_id

        # URLにない場合、画面内の「1R」などのリンクから探す
        links = driver.find_elements(By.TAG_NAME, "a")
        for link in links:
            href = link.get_attribute("href")
            if href:
                match = re.search(r'(\d{16})', href)
                if match:
                    base_id = match.group(1)
                    normalized_id = base_id[:10] + "01" + base_id[12:]
                    st.info(f"✅ ID特定成功: {normalized_id} (1R基準)")
                    return normalized_id
        
        st.error("⚠️ ページ内からレースIDパターンが見つかりませんでした。")
        return None
            
    except Exception as e:
        st.error(f"⚠️ ID取得処理中にエラーが発生しました: {e}")
        return None

def fetch_cyokyo_dict(driver, race_id: str):
    # 調教URL: /chihou/cyokyo/1/{ID}
    url = f"{BASE_URL}/chihou/cyokyo/1/{race_id}"
    driver.get(url)
    try:
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.cyokyo")))
    except: return {}
    return parse_cyokyo(driver.page_source)

def fetch_syutuba_dict(driver, race_id: str):
    # 出馬表URL: /chihou/syutuba/{ID} (※ここには /1/ が入らない)
    url = f"{BASE_URL}/chihou/syutuba/{race_id}"
    driver.get(url)
    try:
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CLASS_NAME, "umaban")))
    except: return {}
    return parse_syutuba_jockey(driver.page_source)

# ==================================================
# Dify ストリーミング
# ==================================================
def stream_dify_workflow(full_text: str):
    if not DIFY_API_KEY:
        yield "⚠️ エラー: DIFY_API_KEY未設定"
        return
    payload = {"inputs": {"text": full_text}, "response_mode": "streaming", "user": "keiba-bot-user"}
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    try:
        res = requests.post("https://api.dify.ai/v1/workflows/run", headers=headers, json=payload, stream=True, timeout=300)
        if res.status_code != 200:
            yield f"⚠️ API Error {res.status_code}"
            return
        for line in res.iter_lines():
            if line:
                decoded = line.decode('utf-8')
                if decoded.startswith("data:"):
                    try:
                        data = json.loads(decoded.replace("data: ", ""))
                        if data.get("event") == "workflow_finished":
                            out = data.get("data", {}).get("outputs", {})
                            yield "".join([v for v in out.values() if isinstance(v, str)])
                        elif "answer" in data:
                            yield data.get("answer", "")
                    except: pass
    except Exception as e:
        yield f"⚠️ Req Error: {str(e)}"

# ==================================================
# メイン処理
# ==================================================
def run_all_races(target_races=None):
    race_numbers = list(range(1, 13)) if target_races is None else sorted({int(r) for r in target_races})
    place_names = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
    place_name = place_names.get(PLACE_CODE, "地方")

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)

    try:
        st.info("🔑 ログイン中...")
        driver.get("https://s.keibabook.co.jp/login/login")
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.NAME, "login_id"))).send_keys(KEIBA_ID)
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password']"))).send_keys(KEIBA_PASS)
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='submit'], .btn-login"))).click()
        time.sleep(2)
        st.success("ログイン成功")

        # 【ここが修正点】カレンダーから当日の正しいID構成を取得
        base_id_1r = get_base_race_id(driver, YEAR, MONTH, DAY, place_name)
        
        if not base_id_1r:
            st.error("🛑 レースIDが特定できなかったため中断します。")
            return

        for r in race_numbers:
            race_num_str = f"{r:02}"
            
            # ID生成: 取得した基準ID(1R)の「レース番号部分(10-12文字目)」だけ差し替える
            # 例: 2025131102 01 1216 -> 2025131102 {r} 1216
            race_id = base_id_1r[:10] + race_num_str + base_id_1r[12:]

            st.markdown(f"### {place_name} {r}R (ID: {race_id})")
            status_area = st.empty()
            result_area = st.empty()
            full_answer = ""

            try:
                status_area.info("📡 データ収集中...")
                
                # 談話 ( /danwa/1/ID )
                url_danwa = f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}"
                driver.get(url_danwa)
                time.sleep(1)
                html_danwa = driver.page_source
                race_info = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)

                # 出馬表 ( /syutuba/ID ) - /1/無し
                syutuba_dict = fetch_syutuba_dict(driver, race_id)

                # 調教 ( /cyokyo/1/ID )
                cyokyo_dict = fetch_cyokyo_dict(driver, race_id)

                all_uma = sorted(list(set(list(danwa_dict.keys()) + list(cyokyo_dict.keys()) + list(syutuba_dict.keys()))), key=lambda x: int(x) if x.isdigit() else 99)
                merged = []
                for uma in all_uma:
                    d = danwa_dict.get(uma, '（なし）')
                    c = cyokyo_dict.get(uma, '（なし）')
                    j = syutuba_dict.get(uma, {"name": "不明", "is_change": False})
                    alert = "【⚠️乗り替わり】" if j["is_change"] else "【継続騎乗】"
                    merged.append(f"▼[馬番{uma}]\n  【騎手】 {j['name']} {alert}\n  【談話】 {d}\n  【調教】 {c}\n")

                if not merged:
                    status_area.warning("データなしのためスキップ")
                    continue

                prompt = (
                    "■役割\n南関東競馬のプロ予想家\n\n"
                    "■レース情報\n" + "\n".join([v for v in race_info.values() if v]) + "\n\n"
                    "■指示\n以下のデータから推奨馬を分析せよ。\n"
                    "1. 乗り替わりの影響を考察すること。\n"
                    "2. 騎手の相性も考慮すること。(参考: https://www.nankankeiba.com/leading_kis/180000000003011.do)\n\n"
                    "■データ\n" + "\n".join(merged)
                )

                status_area.info("🤖 AI分析中...")
                for chunk in stream_dify_workflow(prompt):
                    if chunk:
                        full_answer += chunk
                        result_area.markdown(full_answer + "▌")
                
                result_area.markdown(full_answer)
                if full_answer:
                    status_area.success("完了")
                    save_history(YEAR, PLACE_CODE, place_name, MONTH, DAY, race_num_str, race_id, full_answer)

            except Exception as e:
                status_area.error(f"エラー: {e}")
            st.write("---")
    finally:
        driver.quit()
