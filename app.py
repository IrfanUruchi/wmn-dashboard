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

@st.cache_resource
def init_mqtt():
    lock = threading.Lock()

    data_store = {
        "metrics": {},
        "analysis": {},
        "explain": {},
        "latency_hist": {},
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
                if msg.topic.startswith("wmn/metrics"):
                    data_store["metrics"][device_id] = payload
                    latency = payload.get("latency_ms")
                    if isinstance(latency, (int, float)):
                        dq = data_store["latency_hist"].setdefault(device_id, deque(maxlen=120))
                        dq.append({"timestamp": pd.Timestamp.utcnow(), "latency_ms": float(latency)})

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
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception:
        pass

    return data_store


st.set_page_config(layout="wide")
st.title("🌐 WMN Distributed Network Dashboard")

data_store = init_mqtt()

if not MQTT_BROKER:
    st.error("MQTT_BROKER is not set in environment variables.")
    st.stop()

with st.sidebar:
    st.header("Controls")
    auto_refresh = st.toggle("Auto refresh", value=True)
    refresh_sec = st.slider("Refresh interval (sec)", 1, 15, 3)

if auto_refresh:
    now = time.time()
    last = st.session_state.get("_last_refresh", 0)
    if now - last >= refresh_sec:
        st.session_state["_last_refresh"] = now
        st.rerun()

all_devices = sorted(list(data_store["metrics"].keys()))

if not all_devices:
    st.info("Waiting for devices...")
    st.stop()

device = st.selectbox("Select Device", all_devices)

metrics = data_store["metrics"].get(device, {})
analysis = data_store["analysis"].get(device, {})
explain = data_store["explain"].get(device, {})

c1, c2, c3, c4 = st.columns(4)

rssi = metrics.get("rssi")
latency = metrics.get("latency_ms")
jitter = metrics.get("jitter_ms")
score = analysis.get("experience_score")

c1.metric("📶 RSSI (dBm)", "—" if rssi is None else rssi)
c2.metric("⏱ Latency (ms)", "—" if latency is None else latency)
c3.metric("📡 Jitter (ms)", "—" if jitter is None else jitter)
c4.metric("⭐ Experience Score", "—" if score is None else score)

st.divider()

st.subheader("⭐ Experience Score")
if isinstance(score, (int, float)):
    st.progress(max(0.0, min(1.0, float(score) / 100.0)))
else:
    st.info("No experience score yet.")

st.subheader("📈 Latency Trend")
hist = list(data_store["latency_hist"].get(device, []))

if hist:
    df = pd.DataFrame(hist)
    chart = alt.Chart(df).mark_line().encode(
        x=alt.X("timestamp:T"),
        y=alt.Y("latency_ms:Q"),
        tooltip=["timestamp:T", "latency_ms:Q"]
    ).properties(height=280)
    st.altair_chart(chart, use_container_width=True)
else:
    st.info("No latency history yet.")

st.divider()

st.subheader("🧠 LLM Explanation")
if explain:
    text = explain.get("text") or explain.get("explanation") or json.dumps(explain, indent=2)
    st.success(text)
else:
    st.info("No explanation yet.")

with st.expander("Raw payloads"):
    colA, colB, colC = st.columns(3)
    colA.json(metrics)
    colB.json(analysis)
    colC.json(explain)

st.divider()

st.subheader("❓ Ask the Explainer")
question = st.text_input("Ask about current network conditions")

if st.button("Send Question"):
    if not EXPLAINER_HTTP_BASE:
        st.error("EXPLAINER_HTTP_BASE not configured.")
    elif not question.strip():
        st.warning("Type a question.")
    else:
        try:
            resp = requests.post(
                f"{EXPLAINER_HTTP_BASE}/explain",
                json={"analysis": {"question": question}},
                timeout=30
            )
            st.json(resp.json())
        except Exception as e:
            st.error(f"Request failed: {e}")
