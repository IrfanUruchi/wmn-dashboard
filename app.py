import pandas as pd
import altair as alt

st.set_page_config(layout="wide")
st.title("🌐 WMN Distributed Network Dashboard")

# ---- DEVICE SELECTOR ----
all_devices = list(data_store["metrics"].keys())

if not all_devices:
    st.warning("Waiting for devices...")
    st.stop()

device = st.selectbox("Select Device", all_devices)

metrics = data_store["metrics"].get(device, {})
analysis = data_store["analysis"].get(device, {})
explain = data_store["explain"].get(device, {})

# ---- KPI CARDS ----
col1, col2, col3, col4 = st.columns(4)

rssi = metrics.get("rssi", 0)
latency = metrics.get("latency_ms", 0)
jitter = metrics.get("jitter_ms", 0)
score = analysis.get("experience_score", 0)

col1.metric("📶 RSSI (dBm)", rssi)
col2.metric("⏱ Latency (ms)", latency)
col3.metric("📡 Jitter (ms)", jitter)
col4.metric("⭐ Experience Score", score)

st.divider()

# ---- EXPERIENCE SCORE BAR ----
st.subheader("Experience Score")

score_color = "green" if score > 75 else "orange" if score > 50 else "red"

st.progress(score / 100)

st.divider()

# ---- LATENCY CHART ----
st.subheader("Latency Trend")

if "history" in metrics:
    df = pd.DataFrame(metrics["history"])
    chart = alt.Chart(df).mark_line().encode(
        x="timestamp:T",
        y="latency_ms:Q"
    ).properties(height=300)

    st.altair_chart(chart, use_container_width=True)
else:
    st.info("No historical data available.")

st.divider()

# ---- LLM EXPLANATION ----
st.subheader("🧠 AI Explanation")

if explain:
    st.success(explain.get("text", "No explanation text."))
else:
    st.info("No explanation yet.")
