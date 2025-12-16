import time
import json
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

# デフォルト設定（サイドバー等で set_race_params が呼ばれると書き換わる）
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


def save_history(
    year: str,
    place_code: str,
    place_name: str,
    month: str,
    day: str,
    race_num_str: str,
    race_id: str,
    ai_answer: str,
) -> None:
    """history テーブルに 1 レース分の予想を保存する。"""
    supabase = get_supabase_client()
    if supabase is None:
        return

    # 地方競馬に合わせてカラム内容は適宜読み替えて保存
    # (既存テーブルの構造を変えないため、kaiやdayに便宜的に値を入れる)
    data = {
        "year": str(year),
        "kai": "",          # 地方では不使用のため空文字
        "place_code": str(place_code),
        "place_name": place_name,
        "day": str(day),    # 日付(日)を入れる
        "month": str(month), # ※DBにカラムがあれば入れる、なければ省略
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
    """レース情報のヘッダーを取得"""
    soup = BeautifulSoup(html, "html.parser")
    racetitle = soup.find("div", class_="racetitle")
    if not racetitle:
        return {"date_meet": "", "race_name": "", "cond1": "", "course_line": ""}

    racemei = racetitle.find("div", class_="racemei")
    date_meet = ""
    race_name = ""
    if racemei:
        ps = racemei.find_all("p")
        if len(ps) >= 1:
            date_meet = ps[0].get_text(strip=True)
        if len(ps) >= 2:
            race_name = ps[1].get_text(strip=True)

    racetitle_sub = racetitle.find("div", class_="racetitle_sub")
    cond1 = ""
    course_line = ""
    if racetitle_sub:
        sub_ps = racetitle_sub.find_all("p")
        if len(sub_ps) >= 1:
            cond1 = sub_ps[0].get_text(strip=True)
        if len(sub_ps) >= 2:
            course_line = sub_ps[1].get_text(" ", strip=True)

    return {
        "date_meet": date_meet,
        "race_name": race_name,
        "cond1": cond1,
        "course_line": course_line,
    }

def parse_danwa_comments(html: str):
    """厩舎の話（談話）をパース"""
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
    """調教データをパース"""
    soup = BeautifulSoup(html, "html.parser")
    cyokyo_dict = {}
    section = None
    h2 = soup.find("h2", string=lambda s: s and "調教" in s)
    if h2:
        midasi_div = h2.find_parent("div", class_="midasi")
        if midasi_div:
            section = midasi_div.find_next_sibling("div", class_="section")
    if section is None:
        section = soup
    tables = section.find_all("table", class_="cyokyo")
    for tbl in tables:
        tbody = tbl.find("tbody")
        if not tbody:
            continue
        rows = tbody.find_all("tr", recursive=False)
        if not rows:
            continue
        header = rows[0]
        uma_td = header.find("td", class_="umaban")
        name_td = header.find("td", class_="kbamei")
        if not uma_td or not name_td:
            continue
        umaban = uma_td.get_text(strip=True)
        bamei = name_td.get_text(" ", strip=True)
        tanpyo_td = header.find("td", class_="tanpyo")
        tanpyo = tanpyo_td.get_text(strip=True) if tanpyo_td else ""
        detail_row = rows[1] if len(rows) >= 2 else None
        detail_text = ""
        if detail_row:
            detail_text = detail_row.get_text(" ", strip=True)
        final_text = f"【馬名】{bamei}（馬番{umaban}） 【短評】{tanpyo} 【調教詳細】{detail_text}"
        cyokyo_dict[umaban] = final_text
    return cyokyo_dict


BASE_URL = "https://s.keibabook.co.jp"
def fetch_cyokyo_dict(driver, race_id: str):
    # 地方競馬URL: /chihou/cyokyo/1/{ID}
    url = f"{BASE_URL}/chihou/cyokyo/1/{race_id}"
    driver.get(url)
    try:
        # 要素が見つからなくてもエラーにせず、空なら空を返す
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.cyokyo"))
        )
    except Exception:
        return {}
    html = driver.page_source
    return parse_cyokyo(html)


# ==================================================
# ★Dify ワークフロー用ストリーミング関数
# ==================================================
def stream_dify_workflow(full_text: str):
    if not DIFY_API_KEY:
        yield "⚠️ エラー: DIFY_API_KEY が設定されていません。"
        return

    payload = {
        "inputs": {"text": full_text},
        "response_mode": "streaming",
        "user": "keiba-bot-user",
    }

    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        res = requests.post(
            "https://api.dify.ai/v1/workflows/run",
            headers=headers,
            json=payload,
            stream=True,
            timeout=300, 
        )

        if res.status_code != 200:
            yield f"⚠️ エラー: Dify API Error {res.status_code}\n{res.text}"
            return

        for line in res.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith("data:"):
                    json_str = decoded_line.replace("data: ", "")
                    try:
                        data = json.loads(json_str)
                        event = data.get("event")
                        if event in ["workflow_started", "node_started", "node_finished"]:
                            yield ""
                            continue
                        chunk = data.get("answer", "")
                        if chunk:
                            yield chunk
                        if event == "workflow_finished":
                            outputs = data.get("data", {}).get("outputs", {})
                            if outputs:
                                found_text = ""
                                for key, value in outputs.items():
                                    if isinstance(value, str):
                                        found_text += value + "\n"
                                if found_text:
                                    yield found_text
                                else:
                                    yield f"⚠️ テキストが見つかりませんでした。Raw: {outputs}"
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        yield f"⚠️ Parse Error: {str(e)}"

    except Exception as e:
        yield f"⚠️ Request Error: {str(e)}"


# ==================================================
# メイン処理: 全レース実行
# ==================================================
def run_all_races(target_races=None):
    """
    地方競馬IDルールに基づきスクレイピング -> Difyへ送信 -> ストリーミング表示 -> Supabase保存
    """
    
    race_numbers = (
        list(range(1, 13))
        if target_races is None
        else sorted({int(r) for r in target_races})
    )

    place_names = {
        "10": "大井", "11": "川崎", "12": "船橋", "13": "浦和",
        "30": "園田", "42": "門別", "19": "笠松", "34": "名古屋",
        "20": "金沢", "29": "水沢", "33": "盛岡", "58": "帯広",
        "26": "高知", "23": "佐賀"
    }
    place_name = place_names.get(PLACE_CODE, "地方")

    # Selenium 設定
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)

    try:
        # --- 1. ログイン処理 ---
        st.info("🔑 競馬ブックへログイン中...")
        driver.get("https://s.keibabook.co.jp/login/login")
        
        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.NAME, "login_id"))
        ).send_keys(KEIBA_ID)
        
        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        ).send_keys(KEIBA_PASS)
        
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='submit'], .btn-login"))
        ).click()
        
        time.sleep(2)

        st.success("ログイン成功。レース分析を開始します。")

        # --- 2. 各レース処理 ---
        for r in race_numbers:
            race_num_str = f"{r:02}"
            
            # ★URL生成ルール変更（ユーザー指定: 年 + 11(固定) + 競馬場 + 01(固定) + レース + 月日）
            # 例: 2025 + 11 + 11(川崎) + 01 + 01(1R) + 1216(日付)
            date_str = f"{MONTH}{DAY}"
            race_id = f"{YEAR}11{PLACE_CODE}01{race_num_str}{date_str}"

            st.markdown(f"### {place_name} {r}R")
            
            status_area = st.empty()
            result_area = st.empty()
            full_answer = ""

            try:
                # ==========================
                # Phase A: データ収集中
                # ==========================
                status_area.info(f"📡 {place_name}{r}R のデータを収集中... (ID: {race_id})")
                
                # A-1. 厩舎コメント・基本情報 (地方URL: /chihou/danwa/1/...)
                url_danwa = f"https://s.keibabook.co.jp/chihou/danwa/1/{race_id}"
                driver.get(url_danwa)
                time.sleep(1)
                html_danwa = driver.page_source
                
                # 取得可否チェック（タイトルが取れるかで判断）
                race_info = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)

                # A-2. 前走インタビューは削除 (ユーザー指示により割愛)

                # A-3. 調教 (地方URL: /chihou/cyokyo/1/...)
                cyokyo_dict = fetch_cyokyo_dict(driver, race_id)

                # A-4. データ結合
                # 地方競馬はすべての馬のデータが揃わないことがあるため、
                # danwa_dict のキー(馬番)をベースにするか、1~16番までループするか。
                # ここでは danwa_dict または cyokyo_dict に存在する馬番を網羅的にリストアップする。
                all_uma = sorted(list(set(list(danwa_dict.keys()) + list(cyokyo_dict.keys()))), key=lambda x: int(x) if x.isdigit() else 99)

                merged = []
                for uma in all_uma:
                    d_txt = danwa_dict.get(uma, '（情報なし）')
                    c_txt = cyokyo_dict.get(uma, '（情報なし）')
                    
                    text = (
                        f"▼[馬番{uma}]\n"
                        f"  【厩舎の話】 {d_txt}\n"
                        f"  【調教】 {c_txt}\n"
                    )
                    merged.append(text)

                if not merged:
                    status_area.warning(f"⚠️ {place_name} {r}R: データが取得できませんでした(ID: {race_id})。スキップします。")
                    continue

                # プロンプト作成
                race_header_lines = []
                if race_info["date_meet"]: race_header_lines.append(race_info["date_meet"])
                if race_info["race_name"]: race_header_lines.append(race_info["race_name"])
                if race_info["cond1"]: race_header_lines.append(race_info["cond1"])
                if race_info["course_line"]: race_header_lines.append(race_info["course_line"])
                race_header = "\n".join(race_header_lines)

                merged_text = "\n".join(merged)
                full_text = (
                    "■レース情報\n"
                    f"{race_header}\n\n"
                    f"以下は{place_name}{r}Rのデータである。\n"
                    "各馬について【厩舎の話】および【調教】を基に分析せよ。\n\n"
                    "■出走馬詳細データ\n"
                    + merged_text
                )

                # ==========================
                # Phase B: AI思考中
                # ==========================
                status_area.info("🤖 AIが分析・執筆中です...")
                
                for chunk in stream_dify_workflow(full_text):
                    if chunk:
                        full_answer += chunk
                        result_area.markdown(full_answer + "▌")
                
                # ==========================
                # Phase C: 完了
                # ==========================
                result_area.markdown(full_answer)
                
                if full_answer:
                    status_area.success("✅ 分析完了")
                    # Supabase 保存
                    save_history(
                        YEAR, PLACE_CODE, place_name, MONTH, DAY,
                        race_num_str, race_id, full_answer
                    )
                else:
                    status_area.error("⚠️ AIからの回答が空でした。")

            except Exception as e:
                err_msg = f"❌ エラー発生 ({place_name} {r}R): {str(e)}"
                print(err_msg)
                status_area.error(err_msg)
            
            st.write("---")

    finally:
        driver.quit()
