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
.block-container { padding-top: 2.5rem; max-width: 1100px; }

.section-title {
    font-size: 15px;
    font-weight: 600;
    margin-bottom: 10px;
    color: rgba(255,255,255,0.85);
}

.metric-label {
    font-size: 12px;
    color: rgba(255,255,255,0.55);
}

.metric-value {
    font-size: 24px;
    font-weight: 600;
    margin-top: 4px;
}

.panel {
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 14px;
    background: rgba(255,255,255,0.02);
}

.small-muted {
    font-size: 12px;
    color: rgba(255,255,255,0.4);
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
                        dq = data_store["latency_hist"].setdefault(device_id, deque(maxlen=200))
                        dq.append({
                            "timestamp": pd.Timestamp.utcnow(),
                            "latency_ms": float(latency)
                        })

                elif msg.topic.startswith("wmn/analysis"):
                    data_store["analysis"][device_id] = payload

                elif msg.topic.startswith("wmn/explain"):
                    data_store["explain"][device_id] = payload

        except Exception:
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

rssi = metrics_block.get("rssi_dbm")
latency = metrics_block.get("latency_ms_avg")
jitter = metrics_block.get("jitter_ms")
loss = metrics_block.get("packet_loss_pct")
score = analysis_payload.get("analysis", {}).get("wireless_score_0_100")

st.markdown('<div class="section-title">Current Metrics</div>', unsafe_allow_html=True)

col1, col2, col3, col4 = st.columns(4)

def metric_block(label, value, suffix=""):
    with st.container():
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        st.markdown(f'<div class="metric-label">{label}</div>', unsafe_allow_html=True)
        if value is None:
            display = "—"
        elif isinstance(value, float):
            display = f"{value:.1f}{suffix}"
        else:
            display = f"{value}{suffix}"
        st.markdown(f'<div class="metric-value">{display}</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

with col1:
    metric_block("RSSI", rssi, " dBm")

with col2:
    metric_block("Latency", latency, " ms")

with col3:
    metric_block("Jitter", jitter, " ms")

with col4:
    metric_block("Packet Loss", loss, " %")

st.markdown("<br>", unsafe_allow_html=True)

st.markdown('<div class="section-title">Latency Trend</div>', unsafe_allow_html=True)

hist = list(data_store["latency_hist"].get(device, []))

if hist:
    df = pd.DataFrame(hist)
    chart = alt.Chart(df).mark_line().encode(
        x=alt.X("timestamp:T", title=None),
        y=alt.Y("latency_ms:Q", title=None)
    ).properties(height=280)
    st.altair_chart(chart, use_container_width=True)
else:
    st.markdown('<div class="panel small-muted">No trend data available.</div>', unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

st.markdown('<div class="section-title">Analysis Output</div>', unsafe_allow_html=True)

if explain_payload:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.write(explain_payload.get("text") or json.dumps(explain_payload, indent=2))
    st.markdown('</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="panel small-muted">No analysis available.</div>', unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

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
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.json(st.session_state.qa_last)
    st.markdown('</div>', unsafe_allow_html=True)

time.sleep(refresh_sec)
st.rerun()
