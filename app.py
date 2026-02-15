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
EXPLAINER_HTTP_BASE = (os.getenv("EXPLAINER_HTTP_BASE") or "").rstrip("/")
NGROK_SKIP_WARNING = os.getenv("NGROK_SKIP_WARNING", "true").lower() in ("1", "true", "yes", "y")

TOPICS = ["wmn/metrics/#", "wmn/analysis/#", "wmn/explain/#"]

st.set_page_config(layout="wide", page_title="WMN Command Center")

st.markdown(
    """
<style>
.block-container { padding-top: 1.8rem; max-width: 1500px; }
h1 { letter-spacing: -0.03em; }
.section { font-size: 14px; font-weight: 700; opacity: 0.82; margin: 18px 0 10px; }
.badge { display:inline-flex; align-items:center; gap:8px; padding:6px 12px; border-radius:999px; border:1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.03); font-size:12px; }
.dot { width:8px; height:8px; border-radius:99px; background:#666; }
.dot.ok { background:#34d399; }
.dot.warn { background:#fbbf24; }
.dot.bad { background:#fb7185; }
.card { border:1px solid rgba(255,255,255,0.10); background: rgba(255,255,255,0.02); border-radius:16px; padding:14px; }
.kpi { border:1px solid rgba(255,255,255,0.10); background: rgba(255,255,255,0.02); border-radius:16px; padding:14px; min-height: 92px; }
.kpi .l { font-size:12px; opacity:0.55; }
.kpi .v { font-size:24px; font-weight:800; margin-top:6px; letter-spacing:-0.02em; }
.kpi .h { font-size:12px; opacity:0.45; margin-top:6px; }
.small { font-size:12px; opacity:0.55; }
hr { border:none; border-top:1px solid rgba(255,255,255,0.08); margin: 16px 0; }
</style>
""",
    unsafe_allow_html=True,
)

def now_s() -> float:
    return time.time()

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def safe_num(x):
    return float(x) if isinstance(x, (int, float)) else None

def compute_score(rssi, latency, jitter, loss):
    if rssi is None and latency is None and jitter is None and loss is None:
        return None
    s = 100.0
    if rssi is not None:
        s -= 0 if rssi >= -60 else 8 if rssi >= -70 else 20 if rssi >= -80 else 35
    if latency is not None:
        s -= 0 if latency <= 60 else 10 if latency <= 120 else 25 if latency <= 200 else 40
    if jitter is not None:
        s -= 0 if jitter <= 15 else 10 if jitter <= 35 else 20 if jitter <= 60 else 30
    if loss is not None:
        s -= 0 if loss <= 1 else 15 if loss <= 3 else 30 if loss <= 6 else 45
    return int(clamp(s, 0, 100))

def sev_from_score(score):
    if score is None:
        return "unknown"
    if score >= 85:
        return "ok"
    if score >= 70:
        return "warn"
    return "bad"

def dot_class(sev: str) -> str:
    return "dot ok" if sev == "ok" else "dot warn" if sev == "warn" else "dot bad" if sev == "bad" else "dot"

def http_post_json(url: str, payload: dict, timeout_s: int = 30):
    headers = {"Accept": "application/json"}
    if NGROK_SKIP_WARNING:
        headers["ngrok-skip-browser-warning"] = "true"
    r = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
    ctype = (r.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        return r.status_code, r.json(), None
    text = (r.text or "").strip()
    return r.status_code, None, {"error": "Non-JSON response", "status_code": r.status_code, "content_type": ctype, "text_preview": text[:800]}

@st.cache_resource
def init_mqtt():
    lock = threading.Lock()
    store = {
        "metrics": {},
        "analysis": {},
        "explain": {},
        "lat_hist": {},
        "score_hist": {},
        "last_seen_by_device": {},
        "connected": False,
        "last_msg_ts": 0.0,
        "events": deque(maxlen=3000),
        "lock": lock,
    }

    if not MQTT_BROKER:
        return store

    def on_connect(client, userdata, flags, reason_code, properties):
        with lock:
            store["connected"] = True
        for topic in TOPICS:
            client.subscribe(topic)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            device_id = payload.get("device_id", "unknown")
            ts = now_s()

            with lock:
                store["last_msg_ts"] = ts
                store["last_seen_by_device"][device_id] = ts
                store["events"].appendleft({"ts": pd.Timestamp.utcnow(), "topic": msg.topic, "device": device_id})

                if msg.topic.startswith("wmn/metrics"):
                    store["metrics"][device_id] = payload
                    mb = payload.get("metrics", {}) if isinstance(payload, dict) else {}
                    lat = safe_num(mb.get("latency_ms_avg"))
                    if lat is not None:
                        dq = store["lat_hist"].setdefault(device_id, deque(maxlen=1200))
                        dq.append({"t": pd.Timestamp.utcnow(), "lat": lat})

                elif msg.topic.startswith("wmn/analysis"):
                    store["analysis"][device_id] = payload
                    ab = payload.get("analysis", {}) if isinstance(payload, dict) else {}
                    sc = safe_num(ab.get("wireless_score_0_100"))
                    if sc is not None:
                        dq = store["score_hist"].setdefault(device_id, deque(maxlen=1200))
                        dq.append({"t": pd.Timestamp.utcnow(), "score": float(sc)})

                elif msg.topic.startswith("wmn/explain"):
                    store["explain"][device_id] = payload

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

    return store

data = init_mqtt()

if not MQTT_BROKER:
    st.error("MQTT_BROKER is not set.")
    st.stop()

st.title("WMN Wireless Command Center")

with st.sidebar:
    st.markdown("### Ops Controls")
    refresh_sec = st.slider("Refresh interval (s)", 3, 30, 7)
    pause_refresh = st.toggle("Pause refresh", value=False)
    debug_mode = st.toggle("Debug payloads", value=False)

    st.markdown("### Thresholds")
    online_grace_s = st.slider("Online grace (s)", 5, 120, 20)
    rssi_bad = st.slider("RSSI weak threshold (dBm)", -95, -65, -80)
    lat_warn = st.slider("Latency warn (ms)", 80, 300, 150)
    jit_warn = st.slider("Jitter warn (ms)", 10, 150, 50)
    loss_warn = st.slider("Loss warn (%)", 1, 20, 3)

    st.markdown("### Anomaly Detection")
    z_window = st.slider("Rolling window (samples)", 10, 200, 50)
    z_thresh = st.slider("Z-threshold", 1.5, 6.0, 3.0, 0.1)
    min_samples = st.slider("Min samples", 10, 200, 30)

def snapshot_devices():
    lock = data.get("lock")
    with lock:
        devices = sorted(list(set(list(data["metrics"].keys()) + list(data["analysis"].keys()) + list(data["explain"].keys()))))
        now_ts = now_s()
        rows = []
        for dev in devices:
            mp = data["metrics"].get(dev, {})
            mb = mp.get("metrics", {}) if isinstance(mp, dict) else {}
            ap = data["analysis"].get(dev, {})
            ab = ap.get("analysis", {}) if isinstance(ap, dict) else {}

            rssi = safe_num(mb.get("rssi_dbm"))
            lat = safe_num(mb.get("latency_ms_avg"))
            jit = safe_num(mb.get("jitter_ms"))
            loss = safe_num(mb.get("packet_loss_pct"))

            analyzer_score = safe_num(ab.get("wireless_score_0_100"))
            computed = compute_score(rssi, lat, jit, loss)
            health = analyzer_score if analyzer_score is not None else computed

            last_seen = data["last_seen_by_device"].get(dev)
            age_s = None if not last_seen else int(now_ts - last_seen)
            online = (age_s is not None and age_s <= online_grace_s)

            iface = mb.get("interface")
            chan = mb.get("channel")
            handover = bool(ab.get("handover_detected"))
            congestion = bool(ab.get("congestion_detected"))

            rows.append(
                {
                    "device": dev,
                    "online": online,
                    "last_seen_s": age_s,
                    "health": health,
                    "rssi_dbm": rssi,
                    "latency_ms": lat,
                    "jitter_ms": jit,
                    "loss_pct": loss,
                    "handover": handover,
                    "congestion": congestion,
                    "interface": iface,
                    "channel": chan,
                }
            )

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by=["online", "health", "device"], ascending=[False, False, True])
        return df

def compute_latency_anomaly(dev: str):
    hist = list(data["lat_hist"].get(dev, []))
    if len(hist) < max(min_samples, z_window):
        return None
    df = pd.DataFrame(hist).sort_values("t")
    df["mu"] = df["lat"].rolling(z_window, min_periods=z_window).mean()
    df["sd"] = df["lat"].rolling(z_window, min_periods=z_window).std()
    last = df.iloc[-1]
    if pd.isna(last["mu"]) or pd.isna(last["sd"]) or float(last["sd"]) == 0.0:
        return None
    z = (float(last["lat"]) - float(last["mu"])) / float(last["sd"])
    return float(z)

def build_incidents(df_devices: pd.DataFrame):
    incidents = []
    ts = pd.Timestamp.utcnow()

    for _, r in df_devices.iterrows():
        dev = r["device"]
        if r["online"] is False:
            incidents.append({"ts": ts, "device": dev, "sev": "bad", "type": "offline", "detail": f"Last seen {r['last_seen_s']}s ago"})
            continue

        rssi = r["rssi_dbm"]
        lat = r["latency_ms"]
        jit = r["jitter_ms"]
        loss = r["loss_pct"]

        if rssi is not None and rssi < rssi_bad:
            incidents.append({"ts": ts, "device": dev, "sev": "warn", "type": "weak_signal", "detail": f"RSSI {rssi:.0f} dBm"})
        if lat is not None and lat > lat_warn:
            sev = "bad" if lat > (lat_warn * 1.6) else "warn"
            incidents.append({"ts": ts, "device": dev, "sev": sev, "type": "high_latency", "detail": f"Latency {lat:.1f} ms"})
        if jit is not None and jit > jit_warn:
            sev = "bad" if jit > (jit_warn * 1.6) else "warn"
            incidents.append({"ts": ts, "device": dev, "sev": sev, "type": "high_jitter", "detail": f"Jitter {jit:.1f} ms"})
        if loss is not None and loss > loss_warn:
            sev = "bad" if loss > (loss_warn * 2.0) else "warn"
            incidents.append({"ts": ts, "device": dev, "sev": sev, "type": "packet_loss", "detail": f"Loss {loss:.2f}%"})

        if bool(r["handover"]):
            incidents.append({"ts": ts, "device": dev, "sev": "warn", "type": "handover", "detail": "Roam/handover detected"})
        if bool(r["congestion"]):
            incidents.append({"ts": ts, "device": dev, "sev": "warn", "type": "congestion", "detail": "Congestion flagged"})

        z = compute_latency_anomaly(dev)
        if z is not None and abs(z) >= z_thresh:
            sev = "bad" if abs(z) >= (z_thresh * 1.35) else "warn"
            incidents.append({"ts": ts, "device": dev, "sev": sev, "type": "latency_anomaly", "detail": f"Latency anomaly z={z:.2f}"})

    if not incidents:
        return pd.DataFrame(columns=["ts", "device", "sev", "type", "detail"])

    df_i = pd.DataFrame(incidents)
    df_i = df_i.sort_values(by=["sev", "ts"], ascending=[True, False])
    return df_i

df_devices = snapshot_devices()

age = None
if data.get("last_msg_ts", 0):
    age = int(now_s() - data["last_msg_ts"])

b1, b2, b3 = st.columns([1, 1, 2])

with b1:
    st.markdown(
        f'<span class="badge"><span class="dot {"ok" if data.get("connected") else "warn"}"></span>'
        f'Broker: {"Connected" if data.get("connected") else "Connecting"}'
        f"</span>",
        unsafe_allow_html=True,
    )

with b2:
    sev_ingest = "ok" if (age is not None and age <= online_grace_s) else "warn" if (age is not None and age <= online_grace_s * 3) else "bad"
    st.markdown(
        f'<span class="badge"><span class="dot {sev_ingest}"></span>'
        f'Ingest: {("—" if age is None else str(age) + "s ago")}'
        f"</span>",
        unsafe_allow_html=True,
    )

with b3:
    total = int(len(df_devices)) if not df_devices.empty else 0
    online = int(df_devices["online"].sum()) if total else 0
    fleet_dot = "ok" if online == total else ("warn" if online >= max(1, int(total * 0.7)) else "bad")
    st.markdown(
        f'<span class="badge"><span class="dot {fleet_dot}"></span>'
        f"Fleet: {online}/{total} online"
        f"</span>",
        unsafe_allow_html=True,
    )

tab_fleet, tab_device, tab_incidents, tab_ops = st.tabs(["Fleet", "Device", "Incidents", "Ops"])

with tab_fleet:
    st.markdown('<div class="section">Fleet KPIs</div>', unsafe_allow_html=True)

    if df_devices.empty:
        st.info("Awaiting telemetry on wmn/metrics/#")
    else:
        avg_health = float(df_devices["health"].dropna().mean()) if df_devices["health"].notna().any() else None
        worst = df_devices.dropna(subset=["health"]).tail(1)
        worst_dev = worst["device"].iloc[0] if not worst.empty else "—"
        worst_score = worst["health"].iloc[0] if not worst.empty else None

        df_inc = build_incidents(df_devices)
        active_cnt = int(len(df_inc))

        k1, k2, k3, k4 = st.columns(4)
        k1.markdown(f'<div class="kpi"><div class="l">Online devices</div><div class="v">{int(df_devices["online"].sum())}/{len(df_devices)}</div><div class="h">Heartbeat ≤ {online_grace_s}s</div></div>', unsafe_allow_html=True)
        k2.markdown(f'<div class="kpi"><div class="l">Avg health</div><div class="v">{("—" if avg_health is None else f"{avg_health:.0f}")}</div><div class="h">Analyzer or fallback score</div></div>', unsafe_allow_html=True)
        k3.markdown(f'<div class="kpi"><div class="l">Worst device</div><div class="v">{worst_dev}</div><div class="h">Score {("—" if worst_score is None else int(worst_score))}</div></div>', unsafe_allow_html=True)
        k4.markdown(f'<div class="kpi"><div class="l">Active incidents</div><div class="v">{active_cnt}</div><div class="h">Rules + anomaly detection</div></div>', unsafe_allow_html=True)

        st.markdown('<div class="section">Fleet Table</div>', unsafe_allow_html=True)

        df_show = df_devices.copy()
        df_show["sev"] = df_show["health"].apply(sev_from_score)
        df_show["status"] = df_show["sev"].apply(lambda x: "OK" if x == "ok" else "WARN" if x == "warn" else "BAD" if x == "bad" else "—")

        st.dataframe(
            df_show[["device", "online", "last_seen_s", "health", "status", "rssi_dbm", "latency_ms", "jitter_ms", "loss_pct", "handover", "congestion", "interface", "channel"]],
            width="stretch",
            hide_index=True,
            column_config={
                "device": st.column_config.TextColumn("Device"),
                "online": st.column_config.CheckboxColumn("Online"),
                "last_seen_s": st.column_config.NumberColumn("Last seen (s)", format="%.0f"),
                "health": st.column_config.NumberColumn("Health (0-100)", format="%.0f"),
                "status": st.column_config.TextColumn("Status"),
                "rssi_dbm": st.column_config.NumberColumn("RSSI (dBm)", format="%.0f"),
                "latency_ms": st.column_config.NumberColumn("Latency (ms)", format="%.1f"),
                "jitter_ms": st.column_config.NumberColumn("Jitter (ms)", format="%.1f"),
                "loss_pct": st.column_config.NumberColumn("Loss (%)", format="%.2f"),
                "handover": st.column_config.CheckboxColumn("Handover"),
                "congestion": st.column_config.CheckboxColumn("Congestion"),
            },
        )

        st.markdown('<div class="section">Fleet Analytics</div>', unsafe_allow_html=True)

        c1, c2 = st.columns([1, 1])

        with c1:
            dist = df_devices.dropna(subset=["health"])[["device", "health"]]
            if not dist.empty:
                chart = alt.Chart(dist).mark_bar(opacity=0.85).encode(
                    x=alt.X("health:Q", bin=alt.Bin(maxbins=20), title="Health (0-100)"),
                    y=alt.Y("count():Q", title="Devices"),
                    tooltip=[alt.Tooltip("count():Q", title="Devices")],
                ).properties(height=250)
                st.altair_chart(chart, use_container_width=True)
            else:
                st.info("No health scores yet.")

        with c2:
            heat_rows = []
            for dev in df_devices["device"].tolist():
                hist = list(data["lat_hist"].get(dev, []))
                if len(hist) < 5:
                    continue
                dfh = pd.DataFrame(hist).sort_values("t").tail(60)
                if dfh.empty:
                    continue
                dfh["minute"] = dfh["t"].dt.floor("min")
                agg = dfh.groupby("minute", as_index=False)["lat"].mean()
                for _, rr in agg.iterrows():
                    heat_rows.append({"device": dev, "minute": rr["minute"], "lat": float(rr["lat"])})
            if heat_rows:
                df_heat = pd.DataFrame(heat_rows)
                heat = alt.Chart(df_heat).mark_rect().encode(
                    x=alt.X("minute:T", title="Time"),
                    y=alt.Y("device:N", title="Device"),
                    color=alt.Color("lat:Q", title="Latency (ms)"),
                    tooltip=[alt.Tooltip("device:N"), alt.Tooltip("minute:T"), alt.Tooltip("lat:Q", format=".1f")],
                ).properties(height=250)
                st.altair_chart(heat, use_container_width=True)
            else:
                st.info("Not enough latency samples for heatmap.")

with tab_device:
    if df_devices.empty:
        st.info("Awaiting telemetry...")
    else:
        if "selected_device" not in st.session_state:
            st.session_state.selected_device = df_devices["device"].iloc[0]

        devices = df_devices["device"].tolist()
        st.session_state.selected_device = st.selectbox(
            "Inspect device",
            devices,
            index=devices.index(st.session_state.selected_device)
            if st.session_state.selected_device in devices
            else 0,
        )
        dev = st.session_state.selected_device

        row = df_devices[df_devices["device"] == dev].head(1)
        mp = data["metrics"].get(dev, {})
        ap = data["analysis"].get(dev, {})
        ep = data["explain"].get(dev, {})

        m = mp.get("metrics", {}) if isinstance(mp, dict) else {}
        a = ap.get("analysis", {}) if isinstance(ap, dict) else {}

        rssi = safe_num(m.get("rssi_dbm"))
        lat = safe_num(m.get("latency_ms_avg"))
        jit = safe_num(m.get("jitter_ms"))
        loss = safe_num(m.get("packet_loss_pct"))
        analyzer_score = safe_num(a.get("wireless_score_0_100"))
        health = analyzer_score if analyzer_score is not None else compute_score(rssi, lat, jit, loss)

        online = bool(row["online"].iloc[0]) if not row.empty else False
        last_seen = row["last_seen_s"].iloc[0] if not row.empty else None

        # ---------------- Device Summary ----------------

        st.markdown('<div class="section">Device Summary</div>', unsafe_allow_html=True)

        kk1, kk2, kk3, kk4 = st.columns(4)
        kk1.markdown(f'<div class="kpi"><div class="l">Status</div><div class="v">{("ONLINE" if online else "OFFLINE")}</div><div class="h">Last seen {("—" if last_seen is None else str(int(last_seen))+"s")}</div></div>', unsafe_allow_html=True)
        kk2.markdown(f'<div class="kpi"><div class="l">Health</div><div class="v">{("—" if health is None else int(health))}</div><div class="h">Analyzer or fallback</div></div>', unsafe_allow_html=True)
        kk3.markdown(f'<div class="kpi"><div class="l">RSSI</div><div class="v">{("—" if rssi is None else f"{rssi:.0f} dBm")}</div><div class="h">Threshold bad < {rssi_bad} dBm</div></div>', unsafe_allow_html=True)
        kk4.markdown(f'<div class="kpi"><div class="l">Latency</div><div class="v">{("—" if lat is None else f"{lat:.1f} ms")}</div><div class="h">Warn > {lat_warn} ms</div></div>', unsafe_allow_html=True)

        # ---------------- Alerts ----------------

        st.markdown('<div class="section">Alerts</div>', unsafe_allow_html=True)

        alerts = []
        if not online:
            alerts.append(("bad", "Device offline"))
        if rssi is not None and rssi < rssi_bad:
            alerts.append(("warn", f"Weak signal: RSSI {rssi:.0f} dBm"))
        if lat is not None and lat > lat_warn:
            alerts.append(("bad" if lat > lat_warn * 1.6 else "warn", f"High latency: {lat:.1f} ms"))
        if jit is not None and jit > jit_warn:
            alerts.append(("bad" if jit > jit_warn * 1.6 else "warn", f"High jitter: {jit:.1f} ms"))
        if loss is not None and loss > loss_warn:
            alerts.append(("bad" if loss > loss_warn * 2 else "warn", f"Packet loss: {loss:.2f}%"))
        if bool(a.get("handover_detected")):
            alerts.append(("warn", "Handover detected"))
        if bool(a.get("congestion_detected")):
            alerts.append(("warn", "Congestion flagged"))

        if alerts:
            level = "bad" if any(s == "bad" for s, _ in alerts) else "warn"
            text = "\n".join([f"• {t}" for _, t in alerts])
            (st.error if level == "bad" else st.warning)(text)
        else:
            st.success("No active alerts")

        # ---------------- LLM Interpretation (MQTT auto) ----------------

        st.markdown('<div class="section">LLM Interpretation</div>', unsafe_allow_html=True)

        if ep:
            txt = ep.get("text") or ep.get("explanation") or json.dumps(ep, indent=2)
            st.markdown(f'<div class="card">{txt}</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="card"><span class="small">No explanation received on wmn/explain/#.</span></div>', unsafe_allow_html=True)

        # ---------------- Ask the Explainer (HTTP manual trigger) ----------------

        st.markdown('<div class="section">Ask the Explainer</div>', unsafe_allow_html=True)

        if "qa_last" not in st.session_state:
            st.session_state.qa_last = None
        if "qa_lock_until" not in st.session_state:
            st.session_state.qa_lock_until = 0.0

        # ---- Quick Diagnostics ----

        col1, col2, col3 = st.columns(3)
        quick = None

        if col1.button("Why is health low?"):
            quick = "Explain why the wireless score is low."

        if col2.button("Is congestion likely?"):
            quick = "Assess congestion likelihood from telemetry."

        if col3.button("Signal strength analysis"):
            quick = "Assess signal strength stability."

        # ---- Optional Custom Question ----

        custom_q = st.text_input(
            "Optional custom question",
            placeholder="Ask something specific about this device...",
        )

        submit = st.button(
            "Run Diagnostic",
            type="primary",
            disabled=now_s() < st.session_state.qa_lock_until
        )

        if submit or quick:

            st.session_state.qa_lock_until = now_s() + 6
            question_to_use = quick if quick else custom_q.strip()

            try:
                with st.spinner("Running wireless diagnostic..."):

                    status, data_json, err = http_post_json(
                        f"{EXPLAINER_HTTP_BASE}/explain",
                        {
                            "analysis": {
                                "device_id": dev,
                                "raw": m,
                                "analysis": a,
                            }
                        },
                        timeout_s=30,
                    )

                if data_json is not None:
                    st.session_state.qa_last = data_json
                else:
                    st.session_state.qa_last = err or {
                        "error": "Unknown response",
                        "status_code": status,
                    }

            except Exception as e:
                st.session_state.qa_last = {"error": str(e)}

        if st.session_state.qa_last:
            answer = (
                st.session_state.qa_last.get("text")
                or st.session_state.qa_last.get("explanation")
            )

            if answer:
                st.markdown(f'<div class="card">{answer}</div>', unsafe_allow_html=True)
            else:
                st.json(st.session_state.qa_last)

        if debug_mode:
            with st.expander("Debug payloads"):
                st.json(mp)
                st.json(ap)
                st.json(ep)


with tab_incidents:
    st.markdown('<div class="section">Active Incidents</div>', unsafe_allow_html=True)

    if df_devices.empty:
        st.info("Awaiting telemetry...")
    else:
        df_inc = build_incidents(df_devices)

        if df_inc.empty:
            st.success("No active incidents.")
        else:
            sev_filter = st.multiselect("Severity", ["bad", "warn"], default=["bad", "warn"])
            type_filter = st.multiselect("Type", sorted(df_inc["type"].unique().tolist()), default=sorted(df_inc["type"].unique().tolist()))
            view = df_inc[df_inc["sev"].isin(sev_filter) & df_inc["type"].isin(type_filter)].copy()

            st.dataframe(view, width="stretch", hide_index=True)

            csv = view.to_csv(index=False).encode("utf-8")
            st.download_button("Export CSV", data=csv, file_name="wmn_incidents.csv", mime="text/csv")

            st.markdown('<div class="section">Incident Breakdown</div>', unsafe_allow_html=True)
            b1, b2 = st.columns([1, 1])
            with b1:
                by_type = view.groupby("type", as_index=False).size()
                chart = alt.Chart(by_type).mark_bar(opacity=0.85).encode(
                    x=alt.X("size:Q", title="Count"),
                    y=alt.Y("type:N", sort="-x", title=""),
                    tooltip=[alt.Tooltip("type:N"), alt.Tooltip("size:Q")],
                ).properties(height=260)
                st.altair_chart(chart, use_container_width=True)
            with b2:
                by_dev = view.groupby("device", as_index=False).size().sort_values("size", ascending=False).head(12)
                chart = alt.Chart(by_dev).mark_bar(opacity=0.85).encode(
                    x=alt.X("size:Q", title="Count"),
                    y=alt.Y("device:N", sort="-x", title=""),
                    tooltip=[alt.Tooltip("device:N"), alt.Tooltip("size:Q")],
                ).properties(height=260)
                st.altair_chart(chart, use_container_width=True)

with tab_ops:
    st.markdown('<div class="section">Ingest & Event Stream</div>', unsafe_allow_html=True)

    lock = data.get("lock")
    with lock:
        events = list(data["events"])[:200]

    if events:
        df_e = pd.DataFrame(events)
        st.dataframe(df_e, width="stretch", hide_index=True)
    else:
        st.info("No events yet.")

    st.markdown('<div class="section">Data Retention</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="card"><div class="small">Latency samples per device</div>'
        f'<div style="font-size:22px;font-weight:800">{1200}</div>'
        f'<div class="small">Score samples per device</div>'
        f'<div style="font-size:22px;font-weight:800">{1200}</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="section">Health Model</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="card">'
        '<div class="small">Primary</div><div style="font-size:16px;font-weight:800">Analyzer score (wireless_score_0_100)</div>'
        "<hr/>"
        '<div class="small">Fallback</div><div style="font-size:16px;font-weight:800">Local composite score from RSSI + latency + jitter + loss</div>'
        "</div>",
        unsafe_allow_html=True,
    )

should_refresh = (not pause_refresh) and (now_s() >= st.session_state.get("qa_lock_until", 0.0))
if should_refresh:
    time.sleep(refresh_sec)
    st.rerun()
