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
KAI = "04"
PLACE = "02"
DAY = "02"


def set_race_params(year, kai, place, day):
    """main.py から開催情報を差し替えるための関数"""
    global YEAR, KAI, PLACE, DAY
    YEAR = str(year)
    KAI = str(kai).zfill(2)
    PLACE = str(place).zfill(2)
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
    kai: str,
    place_code: str,
    place_name: str,
    day: str,
    race_num_str: str,
    race_id: str,
    ai_answer: str,
) -> None:
    """history テーブルに 1 レース分の予想を保存する。"""
    supabase = get_supabase_client()
    if supabase is None:
        return

    data = {
        "year": str(year),
        "kai": str(kai),
        "place_code": str(place_code),
        "place_name": place_name,
        "day": str(day),
        "race_num": race_num_str,
        "race_id": race_id,
        "output_text": ai_answer,
    }

    try:
        supabase.table("history").insert(data).execute()
    except Exception as e:
        print("Supabase insert error:", e)


# ==================================================
# HTML パース関数群 (変更なし)
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


def parse_zenkoso_interview(html: str):
    soup = BeautifulSoup(html, "html.parser")
    h2 = soup.find("h2", string=lambda s: s and "前走のインタビュー" in s)
    if not h2:
        return []

    midasi = h2.find_parent("div", class_="midasi")
    table = midasi.find_next("table", class_="syoin")
    if not table or not table.tbody:
        return []

    rows = table.tbody.find_all("tr")
    result = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if "spacer" in (row.get("class") or []):
            i += 1
            continue

        waku_td = row.find("td", class_="waku")
        uma_td = row.find("td", class_="umaban")
        bamei_td = row.find("td", class_="bamei")
        if not (waku_td and uma_td and bamei_td):
            i += 1
            continue

        waku = waku_td.get_text(strip=True)
        umaban = uma_td.get_text(strip=True)
        name = bamei_td.get_text(strip=True)

        prev_date = ""
        prev_class = ""
        prev_finish = ""
        prev_comment = ""

        detail = rows[i + 1] if i + 1 < len(rows) else None
        if detail:
            syoin_td = detail.find("td", class_="syoin")
            if syoin_td:
                sdata = syoin_td.find("div", class_="syoindata")
                if sdata:
                    ps = sdata.find_all("p")
                    if ps:
                        prev_date = ps[0].get_text(strip=True)
                    if len(ps) >= 2:
                        spans = ps[1].find_all("span")
                        if len(spans) >= 1:
                            prev_class = spans[0].get_text(strip=True)
                        if len(spans) >= 2:
                            prev_finish = spans[1].get_text(strip=True)

                direct = syoin_td.find_all("p", recursive=False)
                if direct:
                    txt = direct[0].get_text(strip=True)
                    if txt != "－":
                        prev_comment = txt

        result.append({
            "waku": waku,
            "umaban": umaban,
            "name": name,
            "prev_date_course": prev_date,
            "prev_class": prev_class,
            "prev_finish": prev_finish,
            "prev_comment": prev_comment,
        })
        i += 2
    return result


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
    url = f"{BASE_URL}/cyuou/cyokyo/0/{race_id}"
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.cyokyo"))
        )
    except Exception:
        return {}
    html = driver.page_source
    return parse_cyokyo(html)


# ==================================================
# ★Dify ワークフロー用ストリーミング関数 (強化版)
# ==================================================
def stream_dify_workflow(full_text: str):
    """
    Dify Workflow をストリーミングモードで呼び出し、
    ブロッキング回避(脈打ち)をしつつ、最終的な答えを確実に抽出してyieldする。
    """
    if not DIFY_API_KEY:
        yield "⚠️ エラー: DIFY_API_KEY が設定されていません。"
        return

    payload = {
        "inputs": {"text": full_text},
        "response_mode": "streaming",  # ストリーミング必須
        "user": "keiba-bot-user",
    }

    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        # タイムアウトを長めに設定(5分)
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

        # 1行ずつ受信
        for line in res.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith("data:"):
                    json_str = decoded_line.replace("data: ", "")
                    
                    try:
                        data = json.loads(json_str)
                        event = data.get("event")
                        
                        # ★通信維持のための脈打ち（重要）
                        # 途中経過イベントの時は空文字を返してループを止めない
                        if event in ["workflow_started", "node_started", "node_finished"]:
                            yield ""
                            continue

                        # パターン1: Chatアプリライクなストリーミング
                        chunk = data.get("answer", "")
                        if chunk:
                            yield chunk

                        # パターン2: Workflow完了時の一括出力
                        if event == "workflow_finished":
                            outputs = data.get("data", {}).get("outputs", {})
                            if outputs:
                                # 変数名が何であっても、値があるものを結合して返す
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
    全頭データをスクレイピング -> Difyへ送信 -> ストリーミング表示 -> Supabase保存
    """
    
    # レース番号のリスト作成
    race_numbers = (
        list(range(1, 13))
        if target_races is None
        else sorted({int(r) for r in target_races})
    )

    base_id = f"{YEAR}{KAI}{PLACE}{DAY}"
    place_names = {
        "00": "京都", "01": "阪神", "02": "中京", "03": "小倉", "04": "東京",
        "05": "中山", "06": "福島", "07": "新潟", "08": "札幌", "09": "函館",
    }
    place_name = place_names.get(PLACE, "不明")

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
        
        time.sleep(2) # 遷移待ち

        st.success("ログイン成功。レース分析を開始します。")

        # --- 2. 各レース処理 ---
        for r in race_numbers:
            race_num = f"{r:02}"
            race_id = base_id + race_num

            # UI: レースごとのヘッダー
            st.markdown(f"### {place_name} {r}R")
            
            # ステータス表示用のコンテナ（ここに状況を逐次出す）
            status_area = st.empty()
            result_area = st.empty()
            full_answer = ""

            try:
                # ==========================
                # Phase A: データ収集中
                # ==========================
                status_area.info(f"📡 {place_name}{r}R のデータを収集中... (数秒かかります)")
                
                # A-1. 厩舎コメント・基本情報
                url_danwa = f"https://s.keibabook.co.jp/cyuou/danwa/0/{race_id}"
                driver.get(url_danwa)
                time.sleep(1)
                html_danwa = driver.page_source
                race_info = parse_race_info(html_danwa)
                danwa_dict = parse_danwa_comments(html_danwa)

                # A-2. 前走インタビュー
                url_inter = f"https://s.keibabook.co.jp/cyuou/syoin/{race_id}"
                driver.get(url_inter)
                time.sleep(1)
                zenkoso = parse_zenkoso_interview(driver.page_source)

                # A-3. 調教
                cyokyo_dict = fetch_cyokyo_dict(driver, race_id)

                # A-4. データ結合
                merged = []
                for h in zenkoso:
                    uma = h["umaban"]
                    text = (
                        f"▼[枠{h['waku']} 馬番{uma}] {h['name']}\n"
                        f"  【厩舎の話】 {danwa_dict.get(uma, '（無し）')}\n"
                        f"  【前走情報】 {h['prev_date_course']} ({h['prev_class']}) {h['prev_finish']}\n"
                        f"  【前走談話】 {h['prev_comment'] or '（無し）'}\n"
                        f"  【調教】 {cyokyo_dict.get(uma, '（無し）')}\n"
                    )
                    merged.append(text)

                if not merged:
                    status_area.warning(f"⚠️ {place_name} {r}R: データが取得できませんでした。スキップします。")
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
                    f"以下は{place_name}{r}Rの全頭データである。\n"
                    "各馬について【厩舎の話】【前走情報・前走談話】【調教】を基に分析せよ。\n\n"
                    "■出走馬詳細データ\n"
                    + merged_text
                )

                # ==========================
                # Phase B: AI思考中
                # ==========================
                status_area.info("🤖 AIが分析・執筆中です... (数十秒〜1分ほどお待ちください)")
                
                # Difyストリーミング呼び出し
                for chunk in stream_dify_workflow(full_text):
                    if chunk:
                        full_answer += chunk
                        # 思考中でもここが更新される（カーソル表示）
                        result_area.markdown(full_answer + "▌")
                
                # ==========================
                # Phase C: 完了
                # ==========================
                result_area.markdown(full_answer) # 最終結果表示（カーソル消す）
                
                if full_answer:
                    status_area.success("✅ 分析完了")
                    # Supabase 保存
                    save_history(
                        YEAR, KAI, PLACE, place_name, DAY,
                        race_num, race_id, full_answer
                    )
                else:
                    status_area.error("⚠️ AIからの回答が空でした。")

            except Exception as e:
                err_msg = f"❌ エラー発生 ({place_name} {r}R): {str(e)}"
                print(err_msg)
                status_area.error(err_msg)
            
            # レース間の区切り線
            st.write("---")

    finally:
        driver.quit()
