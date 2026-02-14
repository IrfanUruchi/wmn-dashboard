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
.block-container { padding-top: 2rem; max-width: 1400px; }
.section { font-size: 15px; font-weight: 650; margin: 22px 0 12px 0; opacity: 0.85; }
.badge { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.03); font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 99px; background: #666; }
.dot.ok { background: #34d399; }
.dot.warn { background: #fbbf24; }
.dot.bad { background: #fb7185; }
.kpi { border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 16px; background: rgba(255,255,255,0.02); }
.kpi .v { font-size: 26px; font-weight: 700; margin-top: 4px; }
.small { font-size: 12px; opacity: 0.55; }
hr { border: none; border-top: 1px solid rgba(255,255,255,0.08); margin: 20px 0; }
</style>
""", unsafe_allow_html=True)


def now(): return time.time()
def clamp(v, lo, hi): return max(lo, min(hi, v))


def compute_score(rssi, latency, jitter, loss):
    if all(v is None for v in [rssi, latency, jitter, loss]): return None
    s = 100.0
    if rssi is not None: s -= 0 if rssi >= -60 else 8 if rssi >= -70 else 20 if rssi >= -80 else 35
    if latency is not None: s -= 0 if latency <= 60 else 10 if latency <= 120 else 25 if latency <= 200 else 40
    if jitter is not None: s -= 0 if jitter <= 15 else 10 if jitter <= 35 else 20 if jitter <= 60 else 30
    if loss is not None: s -= 0 if loss <= 1 else 15 if loss <= 3 else 30 if loss <= 6 else 45
    return int(clamp(s, 0, 100))


@st.cache_resource
def init():
    lock = threading.Lock()
    store = {
        "metrics": {},
        "analysis": {},
        "explain": {},
        "lat_hist": {},
        "last_seen": {},
        "connected": False,
        "last_global": 0.0
    }

    if not MQTT_BROKER:
        return store

    def on_connect(c, u, f, rc, p):
        with lock:
            store["connected"] = True
        for t in TOPICS:
            c.subscribe(t)

    def on_message(c, u, m):
        try:
            payload = json.loads(m.payload.decode())
            dev = payload.get("device_id", "unknown")
            with lock:
                ts = now()
                store["last_global"] = ts
                store["last_seen"][dev] = ts
                if m.topic.startswith("wmn/metrics"):
                    store["metrics"][dev] = payload
                    lat = payload.get("metrics", {}).get("latency_ms_avg")
                    if isinstance(lat, (int, float)):
                        dq = store["lat_hist"].setdefault(dev, deque(maxlen=800))
                        dq.append({"t": pd.Timestamp.utcnow(), "lat": float(lat)})
                elif m.topic.startswith("wmn/analysis"):
                    store["analysis"][dev] = payload
                elif m.topic.startswith("wmn/explain"):
                    store["explain"][dev] = payload
        except:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.tls_set()
    client.reconnect_delay_set(1, 30)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except:
        pass
    return store


data = init()

st.title("WMN Wireless Command Center")

if not MQTT_BROKER:
    st.error("MQTT_BROKER not configured.")
    st.stop()


with st.sidebar:
    refresh = st.slider("Refresh interval", 5, 30, 8)
    pause = st.toggle("Pause refresh")
    debug = st.toggle("Debug mode")


devices = sorted(data["metrics"].keys())

global_age = None if not data["last_global"] else int(now() - data["last_global"])

c1, c2 = st.columns(2)
with c1:
    st.markdown(f'<span class="badge"><span class="dot {"ok" if data["connected"] else "warn"}"></span>Broker {"Connected" if data["connected"] else "Connecting"}</span>', unsafe_allow_html=True)
with c2:
    state = "ok" if global_age is not None and global_age <= 10 else "warn" if global_age and global_age <= 30 else "bad"
    st.markdown(f'<span class="badge"><span class="dot {state}"></span>Last msg {global_age if global_age else "—"}s</span>', unsafe_allow_html=True)


if not devices:
    st.info("Awaiting telemetry...")
    if not pause:
        time.sleep(refresh)
        st.rerun()
    st.stop()


rows = []
for d in devices:
    mp = data["metrics"].get(d, {}).get("metrics", {})
    ap = data["analysis"].get(d, {}).get("analysis", {})
    rssi = mp.get("rssi_dbm")
    lat = mp.get("latency_ms_avg")
    jit = mp.get("jitter_ms")
    loss = mp.get("packet_loss_pct")
    score = ap.get("wireless_score_0_100")
    final = score if isinstance(score, (int, float)) else compute_score(rssi, lat, jit, loss)
    age = int(now() - data["last_seen"].get(d, now()))
    rows.append({"device": d, "score": final, "rssi": rssi, "latency": lat, "loss": loss, "last_seen": age})

df = pd.DataFrame(rows).sort_values("score", ascending=False)

st.markdown('<div class="section">Fleet Overview</div>', unsafe_allow_html=True)
st.dataframe(df, use_container_width=True, hide_index=True)


selected = st.selectbox("Inspect device", devices)
m = data["metrics"].get(selected, {}).get("metrics", {})
a = data["analysis"].get(selected, {}).get("analysis", {})

rssi = m.get("rssi_dbm")
lat = m.get("latency_ms_avg")
jit = m.get("jitter_ms")
loss = m.get("packet_loss_pct")
score = a.get("wireless_score_0_100")
health = score if isinstance(score, (int, float)) else compute_score(rssi, lat, jit, loss)

st.markdown('<div class="section">Live Telemetry</div>', unsafe_allow_html=True)
k1, k2, k3, k4 = st.columns(4)
k1.markdown(f'<div class="kpi"><div>RSSI</div><div class="v">{rssi if rssi else "—"} dBm</div></div>', unsafe_allow_html=True)
k2.markdown(f'<div class="kpi"><div>Latency</div><div class="v">{lat if lat else "—"} ms</div></div>', unsafe_allow_html=True)
k3.markdown(f'<div class="kpi"><div>Jitter</div><div class="v">{jit if jit else "—"} ms</div></div>', unsafe_allow_html=True)
k4.markdown(f'<div class="kpi"><div>Loss</div><div class="v">{loss if loss else "—"} %</div></div>', unsafe_allow_html=True)

st.markdown('<div class="section">Health Score</div>', unsafe_allow_html=True)
st.metric("Wireless Experience (0-100)", health if health else "—")
if health:
    st.progress(health / 100)


hist = list(data["lat_hist"].get(selected, []))
if hist:
    df_lat = pd.DataFrame(hist)
    df_lat["ma"] = df_lat["lat"].rolling(12, min_periods=1).mean()
    base = alt.Chart(df_lat).encode(x="t:T")
    l1 = base.mark_line().encode(y="lat:Q")
    l2 = base.mark_line(strokeDash=[4,4]).encode(y="ma:Q")
    st.altair_chart((l1 + l2).properties(height=300), use_container_width=True)


alerts = []
if rssi and rssi < -80: alerts.append("Weak signal")
if lat and lat > 150: alerts.append("High latency")
if jit and jit > 50: alerts.append("High jitter")
if loss and loss > 3: alerts.append("Packet loss elevated")
if a.get("handover_detected"): alerts.append("Handover detected")
if a.get("congestion_detected"): alerts.append("Congestion detected")

st.markdown('<div class="section">Alerts</div>', unsafe_allow_html=True)
if alerts:
    st.warning("\n".join(alerts))
else:
    st.success("No active alerts")


st.markdown('<div class="section">LLM Interpretation</div>', unsafe_allow_html=True)
exp = data["explain"].get(selected, {})
if exp:
    txt = exp.get("text") or exp.get("explanation") or json.dumps(exp)
    st.markdown(f'<div class="kpi">{txt}</div>', unsafe_allow_html=True)
else:
    st.info("No explanation received")


q = st.text_input("Ask about this device")
if st.button("Submit"):
    if EXPLAINER_HTTP_BASE and q.strip():
        try:
            r = requests.post(f"{EXPLAINER_HTTP_BASE}/explain",
                              json={"analysis": {"question": q, "device_id": selected}},
                              timeout=30)
            st.json(r.json())
        except Exception as e:
            st.error(str(e))


if debug:
    st.json(data["metrics"].get(selected))
    st.json(data["analysis"].get(selected))


if not pause:
    time.sleep(refresh)
    st.rerun()
