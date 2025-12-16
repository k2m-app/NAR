import streamlit as st
import keiba_bot  # keiba_bot.py を読み込む

# Supabase と日付用
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

# ★Supabase の設定（Secrets から取得）
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")

@st.cache_resource
def get_supabase_client() -> Client:
    """Supabase クライアントを1回だけ作って使い回す"""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def show_history():
    """直近1週間の履歴を Supabase から取り出して表示する"""
    supabase = get_supabase_client()
    if supabase is None:
        st.error("Supabase の設定がされていないため、履歴を表示できません。")
        st.info("streamlit の Secrets に SUPABASE_URL と SUPABASE_ANON_KEY を追加してください。")
        return

    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    seven_days_ago_iso = seven_days_ago.isoformat()

    try:
        res = (
            supabase
            .table("history")
            .select("*")
            .gte("created_at", seven_days_ago_iso)
            .order("created_at", desc=True)
            .execute()
        )
        rows = res.data
    except Exception as e:
        st.error(f"履歴の取得に失敗しました: {e}")
        return

    st.subheader("直近1週間の履歴")

    if not rows:
        st.info("直近1週間の履歴はまだありません。")
        return

    for row in rows:
        # 地方競馬用に表示を少し調整
        title = f"{row.get('created_at', '')} / {row.get('place_name', '')} {row.get('race_num', '')}R"
        with st.expander(title):
            st.write(f"**日付**: {row.get('created_at', '')}")
            st.write(
                f"**開催**: {row.get('year', '')}年 "
                f"{row.get('place_name', '')} "
                f"{row.get('month', '')}月{row.get('day', '')}日"
            )
            st.write(f"**レース**: {row.get('race_num', '')}R（ID: {row.get('race_id', '')}）")
            st.write("---")
            st.write("**AI予想結果**")
            st.write(row.get("output_text", ""))


st.title("🐎 地方競馬AI分析アプリ")
mode = st.sidebar.radio("メニュー", ["予想する", "直近1週間の履歴を見る"])

if mode == "予想する":
    st.sidebar.header("開催設定")

    year = st.sidebar.text_input("年 (YEAR)", "2025")

    # 月 (MONTH)
    month_options = [f"{i:02}" for i in range(1, 13)]
    month = st.sidebar.selectbox("月 (MONTH)", month_options, index=11) # デフォルト12月

    # 日 (DAY)
    day_options = [f"{i:02}" for i in range(1, 32)]
    day = st.sidebar.selectbox("日 (DAY)", day_options, index=15) # デフォルト16日

    # 地方競馬場コードマップ
    places = {
        "10": "大井", "11": "川崎", "12": "船橋", "13": "浦和",
        "30": "園田", "42": "門別", "19": "笠松", "34": "名古屋",
        "20": "金沢", "29": "水沢", "33": "盛岡", "58": "帯広",
        "26": "高知", "23": "佐賀"
    }
    place_name = st.sidebar.selectbox("競馬場 (PLACE)", list(places.values()), index=1) # デフォルト川崎
    place_code = [k for k, v in places.items() if v == place_name][0]

    st.sidebar.header("分析するレースを選択")

    # ✅ checkbox の key そのものを初期化（初回だけ）
    for i in range(1, 13):
        k = f"race_{i}"
        if k not in st.session_state:
            st.session_state[k] = (i == 1)  # 初期は1RだけON

    # ✅ ボタン：checkboxキーを直接書き換える
    def select_all_races():
        for i in range(1, 13):
            st.session_state[f"race_{i}"] = True

    def clear_all_races():
        for i in range(1, 13):
            st.session_state[f"race_{i}"] = False

    col1, col2 = st.sidebar.columns(2)
    with col1:
        st.button("全レース選択", on_click=select_all_races)
    with col2:
        st.button("全解除", on_click=clear_all_races)

    # checkbox表示
    selected_races = []
    for i in range(1, 13):
        if st.sidebar.checkbox(f"{i}R", key=f"race_{i}"):
            selected_races.append(i)

    st.write(f"### 設定: {year}年 {month}月{day}日 {place_name}")
    st.write("サイドバーでレースを選んでから、ボタンを押すと分析を開始します。")

    if st.button("分析スタート 🚀"):
        if not selected_races:
            st.warning("少なくとも1つのレースを選んでください。")
        else:
            with st.spinner("分析中...これには数分かかります..."):
                try:
                    # 地方競馬用にパラメータセット
                    keiba_bot.set_race_params(year, place_code, month, day)
                    keiba_bot.run_all_races(target_races=selected_races)
                    st.success(f"{', '.join(f'{r}R' for r in selected_races)} の分析が完了しました！")
                except Exception as e:
                    st.error(f"エラーが発生しました: {e}")

elif mode == "直近1週間の履歴を見る":
    show_history()
