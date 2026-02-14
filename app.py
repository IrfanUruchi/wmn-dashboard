import os
import json
import time
import threading
from collections import deque

import streamlit as st
import requests
import pandas as pd
import altair as alt
import paho.mqtt.client as mqtt

MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
EXPLAINER_HTTP_BASE = os.getenv("EXPLAINER_HTTP_BASE")

TOPICS = ["wmn/metrics/#", "wmn/analysis/#", "wmn/explain/#"]

st.set_page_config(layout="wide")

st.markdown("""
<style>
.block-container { padding-top: 2.5rem; max-width: 1200px; }

.section-title {
    font-size: 16px;
    font-weight: 600;
    margin-top: 20px;
    margin-bottom: 10px;
}

.metric-card {
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 14px;
    background: rgba(255,255,255,0.02);
}

.metric-label {
    font-size: 12px;
    color: rgba(255,255,255,0.55);
}

.metric-value {
    font-size: 22px;
    font-weight: 600;
}

.small-muted {
    font-size: 12px;
    color: rgba(255,255,255,0.45);
}
</style>
""", unsafe_allow_html=True)

@st.cache_resource
def init_mqtt():
    lock = threading.Lock()
    data_store = {
        "metrics": {},
        "analysis": {},
        "explain": {},
        "latency_hist": {},
        "last_msg_ts": 0
    }

    if not MQTT_BROKER:
        return data_store

    def on_connect(client, userdata, flags, reason_code, properties):
        for topic in TOPICS:
            client.subscribe(topic)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            device_id = payload.get("device_id", "unknown")

            with lock:
                data_store["last_msg_ts"] = time.time()

                if msg.topic.startswith("wmn/metrics"):
                    data_store["metrics"][device_id] = payload
                    metrics_block = payload.get("metrics", {})
                    latency = metrics_block.get("latency_ms_avg")

                    if isinstance(latency, (int, float)):
                        dq = data_store["latency_hist"].setdefault(device_id, deque(maxlen=240))
                        dq.append({
                            "timestamp": pd.Timestamp.utcnow(),
                            "latency_ms": float(latency)
                        })

                elif msg.topic.startswith("wmn/analysis"):
                    data_store["analysis"][device_id] = payload

                elif msg.topic.startswith("wmn/explain"):
                    data_store["explain"][device_id] = payload
        except:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except:
        pass

    return data_store


data_store = init_mqtt()

st.title("Wireless Telemetry Monitor")

with st.sidebar:
    refresh_sec = st.slider("Refresh interval (seconds)", 4, 15, 6)

devices = sorted(list(data_store["metrics"].keys()))

if not devices:
    st.info("Awaiting telemetry data.")
    time.sleep(refresh_sec)
    st.rerun()
    st.stop()

device = st.selectbox("Device", devices)

metrics_payload = data_store["metrics"].get(device, {})
analysis_payload = data_store["analysis"].get(device, {})
explain_payload = data_store["explain"].get(device, {})

metrics_block = metrics_payload.get("metrics", {})
analysis_block = analysis_payload.get("analysis", {})

rssi = metrics_block.get("rssi_dbm")
latency = metrics_block.get("latency_ms_avg")
jitter = metrics_block.get("jitter_ms")
loss = metrics_block.get("packet_loss_pct")
channel = metrics_block.get("channel")
iface = metrics_block.get("interface")

score = analysis_block.get("wireless_score_0_100")
handover = analysis_block.get("handover_detected")
congestion = analysis_block.get("congestion_detected")

def classify_signal(rssi):
    if rssi is None: return "—"
    if rssi >= -60: return "Excellent"
    if rssi >= -70: return "Good"
    if rssi >= -80: return "Fair"
    return "Weak"

def classify_latency(lat):
    if lat is None: return "—"
    if lat < 60: return "Low"
    if lat < 120: return "Moderate"
    return "High"

st.markdown('<div class="section-title">Telemetry</div>', unsafe_allow_html=True)

col1, col2, col3, col4 = st.columns(4)

col1.metric("RSSI", f"{rssi} dBm" if rssi else "—")
col2.metric("Latency Avg", f"{latency:.1f} ms" if latency else "—")
col3.metric("Jitter", f"{jitter:.1f} ms" if jitter else "—")
col4.metric("Packet Loss", f"{loss} %" if loss else "—")

st.caption(f"Interface: {iface or '—'} | Channel: {channel or '—'}")

st.markdown('<div class="section-title">Derived Health</div>', unsafe_allow_html=True)

h1, h2, h3 = st.columns(3)
h1.metric("Signal Quality", classify_signal(rssi))
h2.metric("Latency Class", classify_latency(latency))
h3.metric("Experience Score", score if score else "—")

st.markdown('<div class="section-title">Analysis Flags</div>', unsafe_allow_html=True)

a1, a2 = st.columns(2)
a1.metric("Handover Detected", "Yes" if handover else "No")
a2.metric("Congestion Detected", "Yes" if congestion else "No")

st.markdown('<div class="section-title">Latency Diagnostics</div>', unsafe_allow_html=True)

hist = list(data_store["latency_hist"].get(device, []))

if hist:
    df = pd.DataFrame(hist)
    recent = df.tail(15)

    delta = 0
    if len(recent) > 1:
        delta = recent["latency_ms"].iloc[-1] - recent["latency_ms"].iloc[0]

    variance = df["latency_ms"].var()

    d1, d2 = st.columns(2)
    d1.metric("Recent Delta (15 samples)", f"{delta:.2f} ms")
    d2.metric("Latency Variance", f"{variance:.2f}")

    chart = alt.Chart(df).mark_line().encode(
        x="timestamp:T",
        y="latency_ms:Q"
    ).properties(height=300)

    st.altair_chart(chart, use_container_width=True)

else:
    st.info("No latency history available.")

st.markdown('<div class="section-title">Analysis Output</div>', unsafe_allow_html=True)

if explain_payload:
    st.write(explain_payload.get("text") or explain_payload)
else:
    st.write("No analysis output available.")

st.markdown('<div class="section-title">Query Analysis</div>', unsafe_allow_html=True)

if "qa_last" not in st.session_state:
    st.session_state.qa_last = None

question = st.text_input("Enter query")

if st.button("Submit"):
    if EXPLAINER_HTTP_BASE and question.strip():
        try:
            resp = requests.post(
                f"{EXPLAINER_HTTP_BASE}/explain",
                json={"analysis": {"question": question, "device_id": device}},
                timeout=30
            )
            st.session_state.qa_last = resp.json()
        except Exception as e:
            st.session_state.qa_last = {"error": str(e)}

if st.session_state.qa_last:
    st.json(st.session_state.qa_last)

last_seen = data_store.get("last_msg_ts")
if last_seen:
    st.caption(f"Last update: {int(time.time() - last_seen)} seconds ago")

time.sleep(refresh_sec)
st.rerun()
