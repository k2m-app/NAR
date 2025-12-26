import time
import json
import re
import requests
import streamlit as st
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
from supabase import create_client, Client

# ==========================================
# 1. 設定・定数
# ==========================================

# 競馬ブックの場所コード(10-13)をNetKeibaのURL用コード(42-45)に変換
# 10:大井(44), 11:川崎(45), 12:船橋(43), 13:浦和(42)
KB_TO_NK_CODE = {
    "10": "44", # 大井
    "11": "45", # 川崎
    "12": "43", # 船橋
    "13": "42"  # 浦和
}

# 表示用・照合用の場所名マップ
KB_TO_PLACE_NAME = {
    "10": "大井",
    "11": "川崎",
    "12": "船橋",
    "13": "浦和"
}

# ユーザー入力（サイドバー）
st.sidebar.title("設定")
YEAR = st.sidebar.text_input("年 (YYYY)", "2025")
MONTH = st.sidebar.text_input("月 (MM)", "12")
DAY = st.sidebar.text_input("日 (DD)", "26")
PLACE_CODE = st.sidebar.selectbox("開催場所", ["10", "11", "12", "13"], format_func=lambda x: KB_TO_PLACE_NAME.get(x, x))

# ==========================================
# 2. NetKeiba専用関数
# ==========================================

def login_netkeiba(driver):
    """
    NetKeibaにログインする関数
    secrets.toml に [netkeiba] email, password が必要
    """
    login_url = "https://regist.netkeiba.com/account/?pid=login"
    try:
        driver.get(login_url)
        time.sleep(1)

        if "logout" in driver.page_source:
            st.info("✅ NetKeiba: 既にログイン済みです")
            return True

        if "netkeiba" in st.secrets and "email" in st.secrets["netkeiba"]:
            email = st.secrets["netkeiba"]["email"]
            password = st.secrets["netkeiba"]["password"]
            
            driver.find_element(By.NAME, "login_id").send_keys(email)
            driver.find_element(By.NAME, "pswd").send_keys(password)
            
            # ログインボタンをクリック（クラス名などは変更される可能性あり）
            submit_btn = driver.find_element(By.CLASS_NAME, "SubmitBtn")
            submit_btn.click()
            time.sleep(2)
            st.success("✅ NetKeiba: ログイン成功")
            return True
        else:
            st.warning("⚠️ SecretsにNetKeibaのログイン情報がありません。指数が見られない可能性があります。")
            return False
    except Exception as e:
        st.warning(f"⚠️ NetKeibaログイン失敗（非ログインで継続）: {e}")
        return False

def get_netkeiba_speed_url(year, month, day, kb_place_code, race_num):
    """
    NetKeibaタイム指数ページのURLを生成
    URL例: https://nar.netkeiba.com/race/speed.html?race_id=202544122601&type=shutuba&mode=past
    """
    nk_place = KB_TO_NK_CODE.get(kb_place_code)
    if not nk_place:
        return None
    
    date_str = f"{month.zfill(2)}{day.zfill(2)}"
    race_str = str(race_num).zfill(2)
    # ID構成: YYYY + 場所コード(2桁) + MMDD + RR
    race_id = f"{year}{nk_place}{date_str}{race_str}"
    
    return f"https://nar.netkeiba.com/race/speed.html?race_id={race_id}&type=shutuba&mode=past"

def scrape_netkeiba_speed_index(driver, url, current_place_name):
    """
    タイム指数ページ(speed.html)からデータを取得
    戻り値: { "馬番": { "past_summary": "...", "speed_index": "..." } }
    """
    data = {}
    try:
        driver.get(url)
        time.sleep(1) # 読み込み待ち
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # --- A. 現在のレース距離を取得 ---
        current_dist = ""
        race_data_div = soup.find("div", class_="RaceData01")
        if race_data_div:
            text = race_data_div.get_text()
            match = re.search(r'(\d{3,4})m', text)
            if match:
                current_dist = match.group(1)
        
        # 現在の条件文字列を作成 (例: "大井ダ1400")
        # 地方競馬は基本的にダート前提だが、念のため
        track_type = "芝" if "芝" in (race_data_div.get_text() if race_data_div else "") else "ダ"
        current_condition = f"{current_place_name}{track_type}{current_dist}"
        
        st.info(f"📏 現在の条件設定: {current_condition} (これと一致する過去指数を抽出します)")

        # --- B. テーブル解析 ---
        table = soup.find("table", class_="SpeedIndex_Table")
        if not table:
            # ログインしていない、または有料会員でない場合など
            st.warning("⚠️ タイム指数テーブルが見つかりません。")
            return {}

        rows = table.find_all("tr", class_="HorseList")
        for row in rows:
            try:
                # 1. 馬番取得
                umaban_td = row.find("td", class_=re.compile("umaban", re.I))
                if not umaban_td:
                    continue
                umaban = umaban_td.get_text(strip=True)

                # 2. 指数カラムの特定
                cols = row.find_all("td")
                # "Horse_Name"クラスを持つ列を探し、その次の列からが指数データ
                start_idx = -1
                for i, col in enumerate(cols):
                    if "Horse_Name" in " ".join(col.get("class", [])):
                        start_idx = i + 1
                        break
                
                if start_idx == -1:
                    continue

                # HTML構造: [馬名] [最高値] [5走前] [4走前] [3走前] [2走前] [前走]
                # 近5走を取得したいので、start_idx+1 (5走前) から start_idx+6 (前走) まで
                # ※start_idxは「最高値」の列
                
                # target_cols = cols[start_idx : start_idx+6] # 最高値も含める場合
                target_cols = cols[start_idx+1 : start_idx+6] # 近5走のみ
                
                past_list = []
                speed_match_list = [] # 同条件の指数リスト

                for td in target_cols:
                    # <span>大井ダ1200</span> H <a>53</a>
                    course_span = td.find("span")
                    if not course_span:
                        continue
                    
                    course_str = course_span.get_text(strip=True) # 例: "大井ダ1200"
                    
                    idx_a = td.find("a")
                    idx_val = idx_a.get_text(strip=True) if idx_a else "-"
                    
                    # データがない場合 "-" や空文字
                    if not idx_val.isdigit():
                        continue
                    
                    entry_str = f"{course_str}({idx_val})"
                    past_list.append(entry_str)
                    
                    # ★同条件判定★
                    if current_condition in course_str:
                         speed_match_list.append(idx_val)
                
                data[umaban] = {
                    "past_summary": " / ".join(past_list) if past_list else "なし",
                    "speed_index": ", ".join(speed_match_list) if speed_match_list else "該当なし"
                }
                
            except Exception as e:
                continue
                
        return data

    except Exception as e:
        st.error(f"NetKeiba指数取得エラー: {e}")
        return {}


# ==========================================
# 3. 競馬ブック & 共通ヘルパー関数 (既存ロジック想定)
# ==========================================

def fetch_race_ids_from_schedule(driver, year, month, day, place_code):
    """
    競馬ブックの日程ページからレースIDを取得する (既存のものを想定)
    """
    # ★ここに既存の fetch_race_ids_from_schedule のコードを入れてください
    # なければ簡易的なものを記述します（URL生成のみ）
    # 競馬ブックのID形式が不明なため、既存コードを優先してください
    # 仮実装:
    date_str = f"{year}{month.zfill(2)}{day.zfill(2)}"
    # 本来はスクレイピングしてIDリストを返すべきですが、
    # ユーザーの環境に合わせてここを修正してください。
    # 例として「1Rだけ」返すダミーリスト
    # return [f"{date_str}{place_code}01"] 
    
    # ↓ ユーザー様の既存コードを使用する場合はここを置き換えてください ↓
    st.warning("⚠️ `fetch_race_ids_from_schedule` 関数は既存のコードを使用してください。")
    return [f"20251226{place_code}01"] # ダミーID

def parse_syutuba_jockey(html_source):
    """
    競馬ブックの出馬表から騎手・馬名などを取得 (既存のものを想定)
    """
    soup = BeautifulSoup(html_source, "html.parser")
    data = {}
    # ★ここに既存のパース処理を入れてください
    # 以下はダミー実装
    try:
        # テーブルを探して馬番と馬名を取得する一般的な処理
        rows = soup.find_all("tr")
        for row in rows:
            cols = row.find_all("td")
            if len(cols) > 5:
                # 簡易的な判定（実際はもっと厳密に）
                umaban = cols[0].get_text(strip=True)
                horse_name = cols[3].get_text(strip=True)
                if umaban.isdigit():
                    data[umaban] = {"name": horse_name, "is_change": False}
    except:
        pass
    return data

# ==========================================
# 4. メイン実行ロジック
# ==========================================

def run_all_races():
    # Chromeオプション設定
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    # エラー回避のための追加設定
    options.add_argument('--disable-gpu')
    options.add_argument("--window-size=1280,1080")
    
    driver = webdriver.Chrome(options=options)

    try:
        st.markdown("## 🏇 競馬予想データ生成開始")

        # 1. NetKeibaログイン
        login_netkeiba(driver)

        # 2. レースID取得 (競馬ブック)
        # ※本来はスクレイピングで全レースIDを取得
        # ここでは1R〜12Rを想定してループ、あるいは既存関数を使用
        race_ids = fetch_race_ids_from_schedule(driver, YEAR, MONTH, DAY, PLACE_CODE)
        
        # 開催地名を取得
        current_place_name = KB_TO_PLACE_NAME.get(PLACE_CODE, "不明")

        # レースごとのループ
        # ※race_idsが正しく取得できている前提
        # もしIDリスト取得が難しいなら、単純に1~12のループでURL生成しても良い
        
        target_races = [1] # テスト用に1Rのみ。全レースやるなら range(1, 13)
        
        for race_num in target_races:
            st.markdown(f"### {race_num}R 分析中...")
            
            # --- A. 競馬ブック (調教・談話) ---
            # URL生成ロジックは既存コードに合わせてください
            # 例: https://s.keibabook.co.jp/chihou/syutuba/202512261001
            kb_race_id = f"{YEAR}{MONTH.zfill(2)}{DAY.zfill(2)}{PLACE_CODE}{str(race_num).zfill(2)}"
            kb_url = f"https://s.keibabook.co.jp/chihou/syutuba/{kb_race_id}"
            
            driver.get(kb_url)
            time.sleep(1)
            kb_source = driver.page_source
            
            # 既存関数でパース
            jockey_dict = parse_syutuba_jockey(kb_source)
            # danwa_dict = parse_danwa(kb_source) # 既存があれば
            # cyokyo_dict = parse_cyokyo(kb_source) # 既存があれば
            
            # --- B. NetKeiba (タイム指数) ---
            nk_url = get_netkeiba_speed_url(YEAR, MONTH, DAY, PLACE_CODE, race_num)
            st.write(f"🔗 NetKeiba参照: {nk_url}")
            
            netkeiba_data = {}
            if nk_url:
                netkeiba_data = scrape_netkeiba_speed_index(driver, nk_url, current_place_name)
            
            # --- C. データ結合 ---
            merged_text = []
            
            # 馬番順にソート
            all_uma = sorted(list(jockey_dict.keys()), key=lambda x: int(x))
            
            if not all_uma and netkeiba_data:
                 # 競馬ブックから馬番が取れなかった場合、NetKeibaのキーを使うバックアップ
                 all_uma = sorted(list(netkeiba_data.keys()), key=lambda x: int(x))

            for uma in all_uma:
                # 競馬ブック情報
                j_info = jockey_dict.get(uma, {"name": "名称取得失敗"})
                
                # NetKeiba情報
                nk_info = netkeiba_data.get(uma, {})
                past_log = nk_info.get("past_summary", "-")
                speed_idx = nk_info.get("speed_index", "なし")
                
                # 絶対スピード指数の強調テキスト
                if speed_idx != "なし" and speed_idx != "該当なし":
                    speed_text = f"★【絶対スピード指数(同条件)】: {speed_idx} (今回の舞台で出した指数)"
                else:
                    speed_text = "  (同条件での指数記録なし)"

                # プロンプト用テキスト作成
                line = (
                    f"▼[馬番{uma}] {j_info['name']}\n"
                    # f"  【談話】{danwa_dict.get(uma, 'なし')}\n"
                    # f"  【調教】{cyokyo_dict.get(uma, 'なし')}\n"
                    f"  【近5走指数履歴】{past_log}\n"
                    f"  {speed_text}\n"
                )
                merged_text.append(line)
            
            final_prompt = "\n".join(merged_text)
            
            # 結果表示
            with st.expander(f"{race_num}R AI入力データ確認", expanded=True):
                st.text(final_prompt)
            
            # --- D. AIへの送信・保存処理 ---
            # ここに既存の Supabase / Gemini / Dify 連携コードを記述
            # save_to_supabase(...) 
            # call_ai_api(...)

    finally:
        driver.quit()
        st.success("🎉 全工程完了")

# ==========================================
# 5. アプリ起動
# ==========================================

if st.button("予想データ作成開始"):
    run_all_races()
