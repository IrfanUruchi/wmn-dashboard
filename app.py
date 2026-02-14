import streamlit as st
import os
import json
import threading
import requests
import paho.mqtt.client as mqtt

# Config

MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_TLS = True

EXPLAINER_HTTP_BASE = os.getenv("EXPLAINER_HTTP_BASE")

TOPICS = [
    "wmn/metrics/#",
    "wmn/analysis/#",
    "wmn/explain/#"
]

# Global message store 
if "data_store" not in st.session_state:
    st.session_state.data_store = {
        "metrics": {},
        "analysis": {},
        "explain": {}
    }

data_store = st.session_state.data_store

# MQTT handling

def on_connect(client, userdata, flags, rc):
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


def start_mqtt():
    client = mqtt.Client()
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    if MQTT_TLS:
        client.tls_set()

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()


# Start MQTT only once
if "mqtt_started" not in st.session_state:
    threading.Thread(target=start_mqtt, daemon=True).start()
    st.session_state.mqtt_started = True


# UI

st.set_page_config(layout="wide")
st.title("WMN Distributed Network Dashboard")

st.subheader("Live Metrics")
for device, data in data_store["metrics"].items():
    st.json(data)

st.subheader("Analyzer Results")
for device, data in data_store["analysis"].items():
    st.json(data)

st.subheader("LLM Explanations")
for device, data in data_store["explain"].items():
    st.json(data)


st.write("Broker:", MQTT_BROKER)
st.write("Username:", MQTT_USERNAME)

# Q/A Section

st.subheader("Ask the explainer")

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
