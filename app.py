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
.metric-card {
    padding: 20px;
    border-radius: 12px;
    background-color: #111827;
}
.status-green { color: #22c55e; font-weight: bold; }
.status-orange { color: #f59e0b; font-weight: bold; }
.status-red { color: #ef4444; font-weight: bold; }
.live-dot {
    height:10px;
    width:10px;
    background-color:#22c55e;
    border-radius:50%;
    display:inline-block;
    margin-right:6px;
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
        "connected": False,
        "last_msg": 0,
    }

    if not MQTT_BROKER:
        return data_store

    def on_connect(client, userdata, flags, reason_code, properties):
        data_store["connected"] = True
        for topic in TOPICS:
            client.subscribe(topic)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            device_id = payload.get("device_id", "unknown")

            with lock:
                data_store["last_msg"] = time.time()

                if msg.topic.startswith("wmn/metrics"):
                    data_store["metrics"][device_id] = payload
                    metrics_block = payload.get("metrics", {})
                    latency = metrics_block.get("latency_ms_avg")
                    if isinstance(latency, (int, float)):
                        dq = data_store["latency_hist"].setdefault(device_id, deque(maxlen=120))
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

st.title("Wireless Network Operations Dashboard")

colA, colB = st.columns([6, 1])

with colA:
    st.markdown("### System Overview")

with colB:
    if data_store["connected"]:
        st.markdown('<span class="live-dot"></span><span class="status-green">LIVE</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="status-red">OFFLINE</span>', unsafe_allow_html=True)

if not data_store["metrics"]:
    st.info("Waiting for telemetry...")
    time.sleep(2)
    st.rerun()

device = st.selectbox("Device", sorted(data_store["metrics"].keys()))

metrics_payload = data_store["metrics"].get(device, {})
analysis_payload = data_store["analysis"].get(device, {})
explain_payload = data_store["explain"].get(device, {})

metrics_block = metrics_payload.get("metrics", {})

rssi = metrics_block.get("rssi_dbm")
latency = metrics_block.get("latency_ms_avg")
jitter = metrics_block.get("jitter_ms")
score = analysis_payload.get("analysis", {}).get("wireless_score_0_100")

def metric_color(value, thresholds):
    if value is None:
        return ""
    if value <= thresholds[0]:
        return "status-green"
    elif value <= thresholds[1]:
        return "status-orange"
    return "status-red"

k1, k2, k3, k4 = st.columns(4)

with k1:
    st.metric("RSSI (dBm)", rssi)

with k2:
    st.metric("Latency (ms)", latency)
    if latency:
        st.markdown(f'<span class="{metric_color(latency,[80,150])}">Health</span>', unsafe_allow_html=True)

with k3:
    st.metric("Jitter (ms)", jitter)

with k4:
    st.metric("Experience Score", score)
    if score:
        st.progress(score/100)

st.divider()

st.subheader("Latency Trend")

hist = list(data_store["latency_hist"].get(device, []))

if hist:
    df = pd.DataFrame(hist)
    chart = alt.Chart(df).mark_line(point=False).encode(
        x="timestamp:T",
        y="latency_ms:Q"
    ).properties(height=300)
    st.altair_chart(chart, use_container_width=True)
else:
    st.info("No trend data yet")

st.divider()

st.subheader("AI Insight")

if explain_payload:
    st.success(explain_payload.get("text") or json.dumps(explain_payload, indent=2))
else:
    st.info("Awaiting AI explanation...")

st.divider()

st.subheader("Ask AI")

question = st.text_input("Network question")

if st.button("Submit"):
    if EXPLAINER_HTTP_BASE and question.strip():
        try:
            resp = requests.post(
                f"{EXPLAINER_HTTP_BASE}/explain",
                json={"analysis": {"question": question}},
                timeout=30
            )
            st.json(resp.json())
        except Exception as e:
            st.error(str(e))

time.sleep(2)
st.rerun()
