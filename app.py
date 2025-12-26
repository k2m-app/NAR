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

# ==================================================
# Streamlit UI (サイドバー)
# ==================================================
st.sidebar.title("🏇 南関×ブック 分析Bot")

# 1. 日付・場所設定
y = st.sidebar.text_input("年", value=today["year"])
m = st.sidebar.text_input("月", value=today["month"])
d = st.sidebar.text_input("日", value=today["day"])

places = {"10": "大井", "11": "川崎", "12": "船橋", "13": "浦和"}
p_choice = st.sidebar.selectbox(
    "場所", 
    options=list(places.keys()), 
    format_func=lambda x: f"{x}:{places[x]}",
    index=1
)

st.sidebar.write("---")

# 2. レース選択ロジック
st.sidebar.write("### 🏁 分析対象レース")
all_races_cb = st.sidebar.checkbox("全レース（1〜12R）を予想する", value=True)

selected_races = []

if all_races_cb:
    # 全選択の場合は1〜12をリストに入れる
    selected_races = list(range(1, 13))
    st.sidebar.info("全レースが対象です")
else:
    # 個別選択（3列のグリッドで表示してスペースを節約）
    st.sidebar.write("個別に選択してください:")
    cols = st.sidebar.columns(3)
    for i in range(1, 13):
        col_idx = (i - 1) % 3  # 0, 1, 2 を繰り返す
        with cols[col_idx]:
            if st.checkbox(f"{i}R", key=f"race_{i}"):
                selected_races.append(i)

st.sidebar.write("---")

# 3. 実行ボタン
if st.sidebar.button("分析を開始する"):
    if not selected_races:
        st.sidebar.error("⚠️ レースを1つ以上選択してください。")
    else:
        # グローバル変数に日付と場所をセット
        set_race_params(y, p_choice, m, d)
        
        # 実行メッセージ
        st.info(f"📅 実行: {YEAR}/{MONTH}/{DAY} ({places[PLACE_CODE]})")
        st.info(f"対象レース: {sorted(selected_races)}R")
        
        # メイン処理を呼び出し（引数に選択されたレースリストを渡す）
        run_all_races(target_races=selected_races)
