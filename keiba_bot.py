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

# ... (Secrets読み込みや設定、Supabase関連は既存のまま) ...

# ==================================================
# 南関東競馬スクレイピング用 定数・関数
# ==================================================

# 競馬ブックの場所コード(10-13)を南関東の場所コード(18-21)に変換
KB_TO_NANKAN_PLACE = {
    "10": "20",  # 大井
    "11": "21",  # 川崎
    "12": "19",  # 船橋
    "13": "18"   # 浦和
}

def get_nankan_base_id(driver, year, month, day, kb_place_code):
    """
    その日の開催回・日数を特定するため、プログラムページからベースIDを取得する
    戻り値例: "20251226201403" (YYYYMMDD + 場所 + 回 + 日)
    """
    nankan_place = KB_TO_NANKAN_PLACE.get(kb_place_code)
    if not nankan_place:
        return None

    date_str = f"{year}{month}{day}"
    # 南関の日程ページ (例: https://www.nankankeiba.com/program/2025122620.do)
    url = f"https://www.nankankeiba.com/program/{date_str}{nankan_place}.do"
    
    try:
        driver.get(url)
        time.sleep(1) # 負荷対策
        
        # ページ内の任意のレースリンクからID構造を抽出
        # href="/race_info/2025122620140301.do" のようなリンクを探す
        soup = BeautifulSoup(driver.page_source, "html.parser")
        link = soup.find("a", href=re.compile(r"/race_info/\d+\.do"))
        
        if link:
            href = link['href']
            match = re.search(r'(\d{14})\d{2}\.do', href) # 先頭14桁を取得
            if match:
                base_id = match.group(1)
                st.success(f"✅ 南関東IDベース取得: {base_id} (開催回・日数を特定)")
                return base_id
                
        st.warning("⚠️ 南関東の開催情報が見つかりませんでした。休催日かURL構造変更の可能性があります。")
        return None
    except Exception as e:
        st.error(f"南関ID取得エラー: {e}")
        return None

def fetch_jockey_trainer_compatibility(driver, base_id, race_num, horse_num):
    """
    馬番ごとの相性ページにアクセスし、特定のHTML箇所から成績を抽出する
    URL構造: base_id(14桁) + Race(2桁) + 固定(01) + 馬番(2桁) .do
    """
    if not base_id:
        return None

    # URL生成
    # 例: 20251226201403 + 09 + 01 + 03 .do
    race_str = str(race_num).zfill(2)
    horse_str = str(horse_num).zfill(2)
    target_url = f"https://www.nankankeiba.com/aisyou_cho/{base_id}{race_str}01{horse_str}.do"

    try:
        driver.get(target_url)
        # 連続アクセスになるため、少し待機時間を設けることを推奨
        time.sleep(0.5) 
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # 1. 馬名チェック（念のためデータの整合性を確認）
        # <h2 class="nk23_c-title01">チャンチャン</h2>
        horse_name_tag = soup.find("h2", class_="nk23_c-title01")
        horse_name = horse_name_tag.get_text(strip=True) if horse_name_tag else "不明"

        # 2. テーブルデータの抽出
        # クラス指定でテーブルを特定
        table = soup.find("table", class_="nk23_c-table01__table")
        if not table:
            return None

        # 「厩舎所属馬」の行を探す
        target_stats = None
        rows = table.find_all("tr")
        
        for row in rows:
            th = row.find("th")
            if not th:
                continue
            
            header_text = th.get_text(strip=True)
            # 「厩舎所属馬」が含まれる行をヒットさせる
            if "厩舎所属馬" in header_text:
                cols = row.find_all("td")
                # カラム構成: [0]1着 [1]2着 [2]3着 [3]4着以下 [4]勝率 [5]連対率
                if len(cols) >= 6:
                    win_rate = cols[4].get_text(strip=True)
                    ren_rate = cols[5].get_text(strip=True)
                    target_stats = f"勝率{win_rate} 連対{ren_rate}"
                break
        
        if target_stats:
            return {"horse_name": horse_name, "stats": target_stats}
        else:
            return None

    except Exception as e:
        # 個別ページの取得エラーはログに出す程度にして止まらないようにする
        print(f"相性取得スキップ(R{race_num} H{horse_num}): {e}")
        return None

# ... (既存の parse_syutuba_jockey などはそのまま) ...

# ==================================================
# メイン実行ロジック (run_all_races) の修正版
# ==================================================
def run_all_races(target_races=None):
    # ... (ドライバ初期化・ログイン処理など既存通り) ...
    
    # ------------------------------------------------
    # 1. 南関東の「開催回・日数」ベースIDを取得（1日1回でOK）
    # ------------------------------------------------
    nankan_base_id = get_nankan_base_id(driver, YEAR, MONTH, DAY, PLACE_CODE)
    
    # 2. 競馬ブックの日程からレースID取得
    race_ids = fetch_race_ids_from_schedule(driver, YEAR, MONTH, DAY, PLACE_CODE)
    
    if not race_ids:
        return

    # 3. 各レースループ
    for i, race_id in enumerate(race_ids):
        race_num = i + 1
        if target_races is not None and race_num not in target_races:
            continue
            
        st.markdown(f"### {race_num}R 分析開始")
        status_area = st.empty()
        result_area = st.empty()
        
        try:
            # ... (競馬ブックからのデータ取得: 談話、出馬表、調教 は既存通り) ...
            driver.get(f"https://s.keibabook.co.jp/chihou/syutuba/{race_id}")
            jockey_dict = parse_syutuba_jockey(driver.page_source)
            # ... (danwa_dict, cyokyo_dict 取得も同様) ...

            # ==========================================
            # 4. 【追加】全馬の相性データをループ取得
            # ==========================================
            # 注意: 馬の数だけページ遷移するため時間がかかります
            compatibility_data = {}
            
            if nankan_base_id:
                status_area.info("🏇 南関東データ(騎手×厩舎相性)を取得中... 時間がかかります")
                
                # 馬番リストを作成
                # jockey_dictのキーは馬番(str)
                horse_numbers = sorted([int(k) for k in jockey_dict.keys()])
                
                # プログレスバー（Streamlit用）
                progress_bar = st.progress(0)
                
                for idx, h_num in enumerate(horse_numbers):
                    # スクレイピング実行
                    comp_res = fetch_jockey_trainer_compatibility(driver, nankan_base_id, race_num, h_num)
                    
                    if comp_res:
                        # 競馬ブックの馬名と、南関の馬名が一致するか確認（念のため）
                        # ※ここでは単純に馬番をキーとして保存
                        compatibility_data[str(h_num)] = comp_res["stats"]
                    
                    # 進捗更新
                    progress_bar.progress((idx + 1) / len(horse_numbers))
                
                progress_bar.empty()

            # ==========================================
            # 5. データ結合 & プロンプト作成
            # ==========================================
            merged_text = []
            all_uma = sorted(list(jockey_dict.keys()), key=lambda x: int(x))

            for uma in all_uma:
                j = jockey_dict.get(uma, {"name": "不明", "is_change": False})
                
                # 相性データ取得
                comp_stats = compatibility_data.get(uma, "データなし")
                
                # テキスト生成
                # ここで「騎手×厩舎相性」をAIに渡すテキストに追加
                line = (
                    f"▼[馬番{uma}] {j['name']} "
                    f"{'【⚠️乗り替わり】' if j['is_change'] else ''}\n"
                    f" 騎手×厩舎相性: {comp_stats}\n"  # ←ここに追加
                    # ... 他の談話や調教データ ...
                )
                merged_text.append(line)

            # ... (以下、AIへの送信処理は既存通り) ...

        except Exception as e:
            st.error(f"Error: {e}")

    driver.quit()
