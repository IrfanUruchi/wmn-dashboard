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

st.markdown(
    """
<style>
:root {
  --bg: #0b0f17;
  --panel: #101827;
  --panel2: #0f172a;
  --border: rgba(255,255,255,0.08);
  --text: rgba(255,255,255,0.92);
  --muted: rgba(255,255,255,0.60);
  --muted2: rgba(255,255,255,0.42);
  --good: #34d399;
  --warn: #fbbf24;
  --bad:  #fb7185;
}

.block-container { padding-top: 2.2rem; padding-bottom: 2.2rem; max-width: 1180px; }
body { color: var(--text); }
[data-testid="stAppViewContainer"] { background: radial-gradient(1200px 600px at 20% 0%, rgba(59,130,246,0.15), transparent 60%),
                                     radial-gradient(900px 500px at 100% 20%, rgba(34,197,94,0.10), transparent 55%),
                                     var(--bg); }
[data-testid="stHeader"] { background: transparent; }

.hdr {
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom: 1.1rem;
}
.title {
  font-size: 22px; font-weight: 650; letter-spacing: -0.02em;
}
.sub {
  color: var(--muted);
  font-size: 13px;
  margin-top: 2px;
}
.chips { display:flex; gap:10px; align-items:center; }
.chip {
  display:inline-flex; align-items:center; gap:8px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: rgba(255,255,255,0.03);
  font-size: 12.5px;
  color: var(--muted);
}
.dot { width: 8px; height:8px; border-radius: 999px; background: var(--muted2); }
.dot.live { background: var(--good); }
.dot.warn { background: var(--warn); }
.dot.bad  { background: var(--bad); }

.panel {
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.03);
  border-radius: 18px;
  padding: 14px 14px;
}
.kpis { display:grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.kpi {
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  border-radius: 16px;
  padding: 14px 14px;
  min-height: 84px;
}
.kpiLabel { color: var(--muted); font-size: 12px; }
.kpiValue { font-size: 26px; font-weight: 650; letter-spacing:-0.02em; margin-top: 6px; }
.kpiHint  { color: var(--muted2); font-size: 12px; margin-top: 6px; }

.sectionTitle {
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
  margin: 4px 0 10px 0;
  color: rgba(255,255,255,0.86);
}

.small { color: var(--muted2); font-size: 12px; }
hr { border: none; border-top: 1px solid var(--border); margin: 18px 0; }
</style>
""",
    unsafe_allow_html=True,
)

@st.cache_resource
def init_mqtt():
    lock = threading.Lock()
    data_store = {
        "metrics": {},
        "analysis": {},
        "explain": {},
        "latency_hist": {},
        "connected": False,
        "last_msg_ts": 0.0,
    }

    if not MQTT_BROKER:
        return data_store

    def on_connect(client, userdata, flags, reason_code, properties):
        with lock:
            data_store["connected"] = True
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
                        dq = data_store["latency_hist"].setdefault(device_id, deque(maxlen=180))
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


def fmt_num(x, suffix=""):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.1f}{suffix}"
    return f"{x}{suffix}"


def health_dot(latency_ms):
    if latency_ms is None:
        return "dot"
    if latency_ms <= 80:
        return "dot live"
    if latency_ms <= 150:
        return "dot warn"
    return "dot bad"


data_store = init_mqtt()

if not MQTT_BROKER:
    st.error("MQTT_BROKER is not set.")
    st.stop()

with st.sidebar:
    st.markdown("### Controls")
    live_refresh = st.toggle("Live refresh", value=True)
    refresh_sec = st.slider("Refresh interval", 2, 15, 3)
    show_debug = st.toggle("Debug", value=False)
    st.markdown("---")
    st.markdown("**Broker**")
    st.caption(f"{MQTT_BROKER}:{MQTT_PORT}")

devices = sorted(list(data_store["metrics"].keys()))
if "device" not in st.session_state:
    st.session_state.device = devices[0] if devices else None

last_seen = data_store.get("last_msg_ts", 0.0)
seconds_ago = None if not last_seen else max(0, int(time.time() - last_seen))

st.markdown(
    f"""
<div class="hdr">
  <div>
    <div class="title">WMN Network Monitor</div>
    <div class="sub">Real-time telemetry → analysis → AI insight</div>
  </div>
  <div class="chips">
    <div class="chip"><span class="dot {'live' if data_store.get('connected') else ''}"></span>
      {'Connected' if data_store.get('connected') else 'Connecting'}
    </div>
    <div class="chip">Last seen: {('—' if seconds_ago is None else str(seconds_ago) + 's ago')}</div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

if not devices:
    st.markdown('<div class="panel"><div class="sectionTitle">No devices yet</div><div class="small">Waiting for wmn/metrics/# messages…</div></div>', unsafe_allow_html=True)
    if live_refresh:
        time.sleep(refresh_sec)
        st.rerun()
    st.stop()

st.session_state.device = st.selectbox("Device", devices, index=devices.index(st.session_state.device) if st.session_state.device in devices else 0)

device = st.session_state.device
metrics_payload = data_store["metrics"].get(device, {})
analysis_payload = data_store["analysis"].get(device, {})
explain_payload = data_store["explain"].get(device, {})

metrics_block = metrics_payload.get("metrics", {})
rssi = metrics_block.get("rssi_dbm")
latency = metrics_block.get("latency_ms_avg")
jitter = metrics_block.get("jitter_ms")
loss = metrics_block.get("packet_loss_pct")
score = analysis_payload.get("analysis", {}).get("wireless_score_0_100")

st.markdown('<div class="sectionTitle">Key metrics</div>', unsafe_allow_html=True)

st.markdown('<div class="kpis">', unsafe_allow_html=True)

st.markdown(
    f"""
<div class="kpi">
  <div class="kpiLabel">RSSI</div>
  <div class="kpiValue">{fmt_num(rssi, " dBm")}</div>
  <div class="kpiHint">Signal strength</div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown(
    f"""
<div class="kpi">
  <div class="kpiLabel">Latency</div>
  <div class="kpiValue">{fmt_num(latency, " ms")}</div>
  <div class="kpiHint"><span class="{health_dot(latency)}"></span> Health</div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown(
    f"""
<div class="kpi">
  <div class="kpiLabel">Jitter</div>
  <div class="kpiValue">{fmt_num(jitter, " ms")}</div>
  <div class="kpiHint">Stability</div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown(
    f"""
<div class="kpi">
  <div class="kpiLabel">Loss</div>
  <div class="kpiValue">{fmt_num(loss, "%")}</div>
  <div class="kpiHint">Packet loss</div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown("</div>", unsafe_allow_html=True)

st.markdown("<hr/>", unsafe_allow_html=True)

left, right = st.columns([1.35, 1])

with left:
    st.markdown('<div class="sectionTitle">Latency trend</div>', unsafe_allow_html=True)
    hist = list(data_store["latency_hist"].get(device, []))
    if hist:
        df = pd.DataFrame(hist)
        chart = alt.Chart(df).mark_line().encode(
            x=alt.X("timestamp:T", title=""),
            y=alt.Y("latency_ms:Q", title="")
        ).properties(height=260)
        st.altair_chart(chart, use_container_width=True)
    else:
        st.markdown('<div class="panel"><div class="small">No trend yet. Waiting for latency_ms_avg…</div></div>', unsafe_allow_html=True)

with right:
    st.markdown('<div class="sectionTitle">AI insight</div>', unsafe_allow_html=True)
    insight = None
    if explain_payload:
        insight = explain_payload.get("text") or explain_payload.get("explanation")
    if not insight:
        insight = "Awaiting explanation…"
    st.markdown(f'<div class="panel">{insight}</div>', unsafe_allow_html=True)

st.markdown("<hr/>", unsafe_allow_html=True)

st.markdown('<div class="sectionTitle">Ask the explainer</div>', unsafe_allow_html=True)

if "qa_last" not in st.session_state:
    st.session_state.qa_last = None

q = st.text_input("Question", placeholder="e.g., Why did latency spike in the last minute?")

col1, col2 = st.columns([1, 2])
with col1:
    ask = st.button("Ask", use_container_width=True)
with col2:
    st.caption("This response is preserved across refreshes.")

if ask:
    if not EXPLAINER_HTTP_BASE:
        st.error("EXPLAINER_HTTP_BASE not configured.")
    elif not q.strip():
        st.warning("Type a question.")
    else:
        try:
            resp = requests.post(
                f"{EXPLAINER_HTTP_BASE}/explain",
                json={"analysis": {"question": q, "device_id": device}},
                timeout=30
            )
            st.session_state.qa_last = resp.json()
        except Exception as e:
            st.session_state.qa_last = {"error": str(e)}

if st.session_state.qa_last:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.json(st.session_state.qa_last)
    st.markdown("</div>", unsafe_allow_html=True)

if show_debug:
    with st.expander("Debug payloads"):
        st.json(metrics_payload)
        st.json(analysis_payload)
        st.json(explain_payload)

if live_refresh:
    time.sleep(refresh_sec)
    st.rerun()
