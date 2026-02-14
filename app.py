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
.block-container { padding-top: 2.2rem; max-width: 1300px; }
h1, h2, h3 { letter-spacing: -0.02em; }

.section-title {
  font-size: 15px;
  font-weight: 650;
  margin-top: 18px;
  margin-bottom: 10px;
  color: rgba(255,255,255,0.86);
}

.subtle { color: rgba(255,255,255,0.55); font-size: 12px; }

.card {
  border: 1px solid rgba(255,255,255,0.10);
  background: rgba(255,255,255,0.02);
  border-radius: 16px;
  padding: 14px;
}

.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.kpi {
  border: 1px solid rgba(255,255,255,0.10);
  background: rgba(255,255,255,0.02);
  border-radius: 16px;
  padding: 14px;
  min-height: 92px;
}
.kpi .label { color: rgba(255,255,255,0.55); font-size: 12px; }
.kpi .value { font-size: 24px; font-weight: 700; margin-top: 6px; letter-spacing: -0.02em; }
.kpi .hint  { color: rgba(255,255,255,0.40); font-size: 12px; margin-top: 6px; }

.badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.10);
  background: rgba(255,255,255,0.03);
  font-size: 12px;
  color: rgba(255,255,255,0.70);
}
.dot { width: 8px; height: 8px; border-radius: 99px; background: rgba(255,255,255,0.35); }
.dot.ok { background: #34d399; }
.dot.warn { background: #fbbf24; }
.dot.bad { background: #fb7185; }

hr { border: none; border-top: 1px solid rgba(255,255,255,0.08); margin: 16px 0; }

.small-note { font-size: 12px; color: rgba(255,255,255,0.40); }
</style>
""",
    unsafe_allow_html=True,
)

def now_s():
    return time.time()

def fmt(x, suffix=""):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.1f}{suffix}"
    return f"{x}{suffix}"

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

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

def dot_class_from_latency(lat):
    if lat is None:
        return "dot"
    if lat <= 80:
        return "dot ok"
    if lat <= 150:
        return "dot warn"
    return "dot bad"

def dot_class_from_loss(loss):
    if loss is None:
        return "dot"
    if loss <= 1:
        return "dot ok"
    if loss <= 3:
        return "dot warn"
    return "dot bad"

def compute_health_score(rssi, latency, jitter, loss):
    if rssi is None and latency is None and jitter is None and loss is None:
        return None

    score = 100.0

    if rssi is not None:
        if rssi >= -60:
            score -= 0
        elif rssi >= -70:
            score -= 8
        elif rssi >= -80:
            score -= 18
        else:
            score -= 30

    if latency is not None:
        if latency <= 60:
            score -= 0
        elif latency <= 120:
            score -= 10
        elif latency <= 200:
            score -= 22
        else:
            score -= 35

    if jitter is not None:
        if jitter <= 15:
            score -= 0
        elif jitter <= 35:
            score -= 8
        elif jitter <= 60:
            score -= 18
        else:
            score -= 28

    if loss is not None:
        if loss <= 1:
            score -= 0
        elif loss <= 3:
            score -= 12
        elif loss <= 6:
            score -= 24
        else:
            score -= 40

    return int(clamp(score, 0, 100))

@st.cache_resource
def init_mqtt():
    lock = threading.Lock()
    data_store = {
        "metrics": {},
        "analysis": {},
        "explain": {},
        "latency_hist": {},
        "last_seen_by_device": {},
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
                ts = now_s()
                data_store["last_msg_ts"] = ts
                data_store["last_seen_by_device"][device_id] = ts

                if msg.topic.startswith("wmn/metrics"):
                    data_store["metrics"][device_id] = payload
                    mb = payload.get("metrics", {})
                    lat = mb.get("latency_ms_avg")
                    if isinstance(lat, (int, float)):
                        dq = data_store["latency_hist"].setdefault(device_id, deque(maxlen=600))
                        dq.append({"timestamp": pd.Timestamp.utcnow(), "latency_ms": float(lat)})

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

data_store = init_mqtt()

st.title("Wireless Telemetry Monitor")

if not MQTT_BROKER:
    st.error("MQTT_BROKER is not set.")
    st.stop()

with st.sidebar:
    st.markdown("### Controls")
    refresh_sec = st.slider("Refresh interval (seconds)", 5, 30, 10)
    pause_refresh = st.toggle("Pause refresh", value=False)
    show_debug = st.toggle("Show debug payloads", value=False)

devices = sorted(list(data_store["metrics"].keys()))

age = None
if data_store.get("last_msg_ts", 0):
    age = int(now_s() - data_store["last_msg_ts"])

badge_left, badge_right = st.columns([1, 1])
with badge_left:
    st.markdown(
        f'<span class="badge"><span class="dot {"ok" if data_store.get("connected") else ""}"></span>'
        f'Broker: {"Connected" if data_store.get("connected") else "Connecting"}'
        f'</span>',
        unsafe_allow_html=True,
    )
with badge_right:
    st.markdown(
        f'<span class="badge"><span class="dot {"ok" if (age is not None and age <= 10) else ("warn" if (age is not None and age <= 30) else "bad")}"></span>'
        f'Last message: {("—" if age is None else str(age) + "s ago")}'
        f'</span>',
        unsafe_allow_html=True,
    )

if not devices:
    st.info("Awaiting telemetry on wmn/metrics/#")
    if not pause_refresh:
        time.sleep(refresh_sec)
        st.rerun()
    st.stop()

st.markdown('<div class="section-title">Devices</div>', unsafe_allow_html=True)

rows = []
for dev in devices:
    mp = data_store["metrics"].get(dev, {})
    mb = mp.get("metrics", {}) if isinstance(mp, dict) else {}
    ap = data_store["analysis"].get(dev, {})
    ab = ap.get("analysis", {}) if isinstance(ap, dict) else {}

    rssi = mb.get("rssi_dbm")
    lat = mb.get("latency_ms_avg")
    jit = mb.get("jitter_ms")
    loss = mb.get("packet_loss_pct")

    score = ab.get("wireless_score_0_100")
    computed = compute_health_score(rssi, lat, jit, loss)
    final_score = score if isinstance(score, (int, float)) else computed

    last_seen = data_store["last_seen_by_device"].get(dev)
    last_seen_age = None if not last_seen else int(now_s() - last_seen)

    rows.append({
        "device": dev,
        "last_seen_s": last_seen_age if last_seen_age is not None else None,
        "rssi_dbm": rssi,
        "latency_ms": lat,
        "loss_pct": loss,
        "score_0_100": final_score
    })

df_devices = pd.DataFrame(rows).sort_values(by=["score_0_100", "device"], ascending=[False, True])

st.dataframe(
    df_devices,
    use_container_width=True,
    hide_index=True,
    column_config={
        "device": st.column_config.TextColumn("Device"),
        "last_seen_s": st.column_config.NumberColumn("Last seen (s)", format="%.0f"),
        "rssi_dbm": st.column_config.NumberColumn("RSSI (dBm)", format="%.0f"),
        "latency_ms": st.column_config.NumberColumn("Latency (ms)", format="%.1f"),
        "loss_pct": st.column_config.NumberColumn("Loss (%)", format="%.2f"),
        "score_0_100": st.column_config.NumberColumn("Health (0-100)", format="%.0f"),
    },
)

if "selected_device" not in st.session_state:
    st.session_state.selected_device = devices[0]

st.session_state.selected_device = st.selectbox("Inspect device", devices, index=devices.index(st.session_state.selected_device) if st.session_state.selected_device in devices else 0)
device = st.session_state.selected_device

metrics_payload = data_store["metrics"].get(device, {})
analysis_payload = data_store["analysis"].get(device, {})
explain_payload = data_store["explain"].get(device, {})

metrics_block = metrics_payload.get("metrics", {}) if isinstance(metrics_payload, dict) else {}
analysis_block = analysis_payload.get("analysis", {}) if isinstance(analysis_payload, dict) else {}

rssi = metrics_block.get("rssi_dbm")
latency = metrics_block.get("latency_ms_avg")
jitter = metrics_block.get("jitter_ms")
loss = metrics_block.get("packet_loss_pct")
channel = metrics_block.get("channel")
iface = metrics_block.get("interface")
tx_kbps = metrics_block.get("tx_kbps") or metrics_block.get("uplink_kbps") or metrics_block.get("throughput_kbps")
rx_kbps = metrics_block.get("rx_kbps") or metrics_block.get("downlink_kbps")

score = analysis_block.get("wireless_score_0_100")
handover = analysis_block.get("handover_detected")
congestion = analysis_block.get("congestion_detected")

computed_score = compute_health_score(rssi, latency, jitter, loss)
health = score if isinstance(score, (int, float)) else computed_score

st.markdown('<div class="section-title">Telemetry</div>', unsafe_allow_html=True)

st.markdown('<div class="kpi-grid">', unsafe_allow_html=True)

st.markdown(
    f"""
<div class="kpi">
  <div class="label">RSSI</div>
  <div class="value">{fmt(rssi, " dBm")}</div>
  <div class="hint">Quality: {classify_signal(rssi)}</div>
</div>
<div class="kpi">
  <div class="label">Latency (avg)</div>
  <div class="value">{fmt(latency, " ms")}</div>
  <div class="hint"><span class="{dot_class_from_latency(latency)}"></span> Class: {classify_latency(latency)}</div>
</div>
<div class="kpi">
  <div class="label">Jitter</div>
  <div class="value">{fmt(jitter, " ms")}</div>
  <div class="hint">Stability metric</div>
</div>
<div class="kpi">
  <div class="label">Loss</div>
  <div class="value">{fmt(loss, "%")}</div>
  <div class="hint"><span class="{dot_class_from_loss(loss)}"></span> Packet integrity</div>
</div>
""",
    unsafe_allow_html=True,
)

st.markdown("</div>", unsafe_allow_html=True)

meta_left, meta_right = st.columns([1, 1])
with meta_left:
    st.markdown(f'<div class="small-note">Interface: {iface or "—"} · Channel: {channel or "—"}</div>', unsafe_allow_html=True)
with meta_right:
    st.markdown(f'<div class="small-note">RX: {fmt(rx_kbps, " kbps")} · TX: {fmt(tx_kbps, " kbps")}</div>', unsafe_allow_html=True)

st.markdown('<div class="section-title">Health</div>', unsafe_allow_html=True)
h1, h2, h3 = st.columns([1, 1, 2])
h1.metric("Health score (0-100)", health if health is not None else "—")
h2.metric("Analyzer score (0-100)", score if score is not None else "—")
with h3:
    if isinstance(health, (int, float)):
        st.progress(clamp(float(health) / 100.0, 0.0, 1.0))
    else:
        st.caption("Score not available yet.")

st.markdown('<div class="section-title">Flags</div>', unsafe_allow_html=True)
f1, f2, f3 = st.columns(3)
f1.metric("Handover detected", "Yes" if bool(handover) else "No")
f2.metric("Congestion detected", "Yes" if bool(congestion) else "No")
f3.metric("Signal class", classify_signal(rssi))

st.markdown('<div class="section-title">Latency diagnostics</div>', unsafe_allow_html=True)

hist = list(data_store["latency_hist"].get(device, []))
if hist:
    df = pd.DataFrame(hist).sort_values("timestamp")
    df["latency_ma"] = df["latency_ms"].rolling(window=10, min_periods=1).mean()

    recent = df.tail(20)
    delta = 0.0
    if len(recent) > 1:
        delta = float(recent["latency_ms"].iloc[-1] - recent["latency_ms"].iloc[0])

    p50 = float(df["latency_ms"].quantile(0.50))
    p95 = float(df["latency_ms"].quantile(0.95))
    variance = float(df["latency_ms"].var())

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Δ (last 20)", f"{delta:.1f} ms")
    d2.metric("p50", f"{p50:.1f} ms")
    d3.metric("p95", f"{p95:.1f} ms")
    d4.metric("Variance", f"{variance:.1f}")

    base = alt.Chart(df).encode(x=alt.X("timestamp:T", title=""))

    line_latency = base.mark_line(opacity=0.85).encode(
        y=alt.Y("latency_ms:Q", title="Latency (ms)"),
        tooltip=[alt.Tooltip("timestamp:T"), alt.Tooltip("latency_ms:Q", format=".1f"), alt.Tooltip("latency_ma:Q", format=".1f")]
    )

    line_ma = base.mark_line(strokeDash=[6, 4], opacity=0.9).encode(
        y=alt.Y("latency_ma:Q", title="")
    )

    rule_warn = alt.Chart(pd.DataFrame({"y": [150]})).mark_rule(opacity=0.35).encode(y="y:Q")
    rule_bad = alt.Chart(pd.DataFrame({"y": [250]})).mark_rule(opacity=0.25).encode(y="y:Q")

    st.altair_chart((line_latency + line_ma + rule_warn + rule_bad).properties(height=320), use_container_width=True)

    st.markdown('<div class="section-title">Latency distribution</div>', unsafe_allow_html=True)
    hist_chart = alt.Chart(df).mark_bar(opacity=0.85).encode(
        x=alt.X("latency_ms:Q", bin=alt.Bin(maxbins=30), title="Latency (ms)"),
        y=alt.Y("count():Q", title="Samples")
    ).properties(height=220)
    st.altair_chart(hist_chart, use_container_width=True)
else:
    st.info("No latency history available yet.")

st.markdown('<div class="section-title">Alerts</div>', unsafe_allow_html=True)
alerts = []
if rssi is not None and rssi < -80:
    alerts.append("RSSI is weak (< -80 dBm). Check placement / interference.")
if latency is not None and latency > 150:
    alerts.append("Latency is elevated (> 150 ms). Check congestion / routing path.")
if jitter is not None and jitter > 50:
    alerts.append("Jitter is high (> 50 ms). Check link stability.")
if loss is not None and loss > 3:
    alerts.append("Packet loss > 3%. Check RF environment / retransmissions.")
if bool(handover):
    alerts.append("Handover detected. Expect short instability during roam.")
if bool(congestion):
    alerts.append("Congestion flagged by analyzer.")

if alerts:
    st.warning("\n".join([f"• {a}" for a in alerts]))
else:
    st.markdown('<div class="card"><span class="subtle">No active alerts for the selected device.</span></div>', unsafe_allow_html=True)

st.markdown('<div class="section-title">Analysis output</div>', unsafe_allow_html=True)
if explain_payload:
    text = explain_payload.get("text") or explain_payload.get("explanation") or json.dumps(explain_payload, indent=2)
    st.markdown(f'<div class="card">{text}</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="card"><span class="subtle">No explanation received on wmn/explain/#.</span></div>', unsafe_allow_html=True)

st.markdown('<div class="section-title">Query</div>', unsafe_allow_html=True)

if "qa_last" not in st.session_state:
    st.session_state.qa_last = None
if "qa_lock_until" not in st.session_state:
    st.session_state.qa_lock_until = 0.0

q = st.text_input("Enter query", placeholder="e.g., explain current latency + jitter behavior", key="qa_text")

submit = st.button("Submit", type="primary")

if submit:
    st.session_state.qa_lock_until = now_s() + 8
    if not EXPLAINER_HTTP_BASE:
        st.session_state.qa_last = {"error": "EXPLAINER_HTTP_BASE not configured."}
    elif not q.strip():
        st.session_state.qa_last = {"error": "Empty query."}
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
    st.json(st.session_state.qa_last)

if show_debug:
    with st.expander("Debug payloads"):
        st.json(metrics_payload)
        st.json(analysis_payload)
        st.json(explain_payload)

last_seen = data_store.get("last_msg_ts")
if last_seen:
    st.caption(f"Last update: {int(now_s() - last_seen)} seconds ago")

should_refresh = (not pause_refresh) and (now_s() >= st.session_state.get("qa_lock_until", 0.0))
if should_refresh:
    time.sleep(refresh_sec)
    st.rerun()
