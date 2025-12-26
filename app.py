import streamlit as st
from datetime import datetime
import pytz # 日本時間を正確に取るために必要（pip install pytz）

# ==================================================
# 1. 日付の自動取得（日本時間）
# ==================================================
def get_today_jst():
    """現在の日付を日本時間で取得する"""
    jst = pytz.timezone('Asia/Tokyo')
    now = datetime.now(jst)
    return {
        "year": str(now.year),
        "month": str(now.month).zfill(2),
        "day": str(now.day).zfill(2)
    }

today = get_today_jst()

# ==================================================
# 2. 変数の初期化（ページを開いた日の日付をセット）
# ==================================================
YEAR = today["year"]
MONTH = today["month"]
DAY = today["day"]
PLACE_CODE = "11" # 初期値

def set_race_params(year, place_code, month, day):
    global YEAR, PLACE_CODE, MONTH, DAY
    YEAR = str(year)
    PLACE_CODE = str(place_code).zfill(2)
    MONTH = str(month).zfill(2)
    DAY = str(day).zfill(2)

# ... (その他のスクレイピング関数は既存のまま) ...

# ==================================================
# 3. Streamlit UI (サイドバー)
# ==================================================
st.sidebar.title("🏇 南関×ブック 分析Bot")

# st.text_input の value に自動取得した日付を指定
y = st.sidebar.text_input("年", value=today["year"])
m = st.sidebar.text_input("月", value=today["month"])
d = st.sidebar.text_input("日", value=today["day"])

# 開催場所の選択（コードと名前を分離して管理）
places = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
p_choice = st.sidebar.selectbox(
    "場所", 
    options=list(places.keys()), 
    format_func=lambda x: f"{x}:{places[x]}",
    index=1 # デフォルトで「11:川崎」を選択
)

# 実行ボタン
if st.sidebar.button("分析を開始する"):
    # ここでUIの値をグローバル変数に反映
    set_race_params(y, p_choice, m, d)
    
    st.info(f"📅 実行条件: {YEAR}年{MONTH}月{DAY}日 / 場所コード:{PLACE_CODE}")
    
    # 実際の処理を呼び出す
    # run_all_races()
