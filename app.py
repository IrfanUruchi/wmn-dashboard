import streamlit as st
import os
import json
import requests
import paho.mqtt.client as mqtt
import time

# Config
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
EXPLAINER_HTTP_BASE = os.getenv("EXPLAINER_HTTP_BASE")

TOPICS = [
    "wmn/metrics/#",
    "wmn/analysis/#",
    "wmn/explain/#"
]
# MQTT Singleton

@st.cache_resource
def init_mqtt():

    data_store = {
        "metrics": {},
        "analysis": {},
        "explain": {}
    }

    def on_connect(client, userdata, flags, reason_code, properties):
        for topic in TOPICS:
            client.subscribe(topic)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            device_id = payload.get("device_id", "unknown")

            if msg.topic.startswith("wmn/metrics"):
                data_store["metrics"][device_id] = payload

            elif msg.topic.startswith("wmn/analysis"):
                data_store["analysis"][device_id] = payload

            elif msg.topic.startswith("wmn/explain"):
                data_store["explain"][device_id] = payload

        except Exception as e:
            print("MQTT error:", e)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.tls_set()

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    return data_store


data_store = init_mqtt()
# UI
st.set_page_config(layout="wide")
st.title("WMN Distributed Network Dashboard")

st.write("MQTT Broker:", MQTT_BROKER)

st.subheader("Live Metrics")
if data_store["metrics"]:
    for device, data in data_store["metrics"].items():
        st.json(data)
else:
    st.info("No metrics received yet.")

st.subheader("Analyzer Results")
if data_store["analysis"]:
    for device, data in data_store["analysis"].items():
        st.json(data)
else:
    st.info("No analysis received yet.")

st.subheader("LLM Explanations")
if data_store["explain"]:
    for device, data in data_store["explain"].items():
        st.json(data)
else:
    st.info("No explanations received yet.")

# Q/A Section

st.subheader(" Ask the Explainer")

question = st.text_input("Ask about current network conditions")

if st.button("Send Question"):
    if not EXPLAINER_HTTP_BASE:
        st.error("Explainer HTTP URL not configured.")
    else:
        try:
            response = requests.post(
                f"{EXPLAINER_HTTP_BASE}/explain",
                json={"analysis": {"question": question}},
                timeout=30
            )
            st.json(response.json())
        except Exception as e:
            st.error(f"Request failed: {e}")

if st.button("Refresh Data"):
    time.sleep(1)
    st.experimental_rerun()
