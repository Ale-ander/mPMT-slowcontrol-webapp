#!/usr/bin/env python3
# coding: utf-8

import argparse
import socket
import time
from contextlib import closing
from flask import Flask, jsonify, render_template, request
from libs.poller import Poller, FakePoller
import threading

data_lock = threading.Lock()
def make_app(channels, poller, host):
    app = Flask(__name__)

    @app.route("/")
    def index():
        refresh_ms = int(float(request.args.get("refresh", 3000)))
        return render_template(
            "index.html",
            refresh_ms=refresh_ms,
            channels=channels,
            ip=host
        )

    @app.route("/api/mainboard")
    def api_mainboard():
        return jsonify(poller.mainboard_status)

    @app.route("/api/sensors/latest")
    def api_sensors_latest():
        with data_lock:
            return jsonify(dict(poller.latest_sensor_data))

    @app.route("/api/daq/latest")
    def api_daq_latest():
        return jsonify(poller.daq_status)

    @app.route("/api/rc/latest")
    def api_rc_latest():
        return jsonify(poller.runcontrol_status)

    @app.route("/api/latest")
    def api_latest():
        html = request.args.get("html") == "1"

        with data_lock:
            rows = [dict(poller.latest_readings[ch]) for ch in channels if ch in poller.latest_readings]

        out = []

        for r in rows:
            status = r.get("status")
            alarm = r.get("alarm")

            st_lower = (status or "").lower()
            if "trip" == st_lower:
                tag = '<span class="tag trip">TRIP</span>' if html else "TRIP"
            elif "up" == st_lower:
                tag = '<span class="tag up">UP</span>' if html else "UP"
            elif "down" == st_lower:
                tag = '<span class="tag down">DOWN</span>' if html else "DOWN"
            else:
                tag = status or "?"

            out.append({
                "timestamp": r.get("ts"),
                "channel": r.get("channel"),
                "rate_hz": r.get("rate_hz"),
                "rate_th": r.get("rate_th"),
                "ttp": r.get("ttp"),
                "voltage": r.get("voltage"),
                "voltage_set": r.get("voltage_set"),
                "current": r.get("current"),
                "temperature": r.get("temperature"),
                "status": status,
                "status_tag": tag,
                "alarm": alarm,
                "alarm_str": poller.alarm_string(alarm, html=html),
                "threshold": r.get("threshold"),
                "turn_on": bool(r.get("turn_on")),
                "acq_enabled": bool(r.get("acq_enabled")),
                "trig_enabled": bool(r.get("trig_enabled")),
                "puls_enabled": bool(r.get("puls_enabled")),
                "rst_enabled": bool(r.get("rst_enabled")),
                "hv_on": bool(r.get("hv_on")),
            })

        return jsonify({"rows": out, "count": len(out)} | poller.single_read)

    @app.route("/api/last_update")
    def api_last_update():
        with data_lock:
            updated_at = poller.latest_update

        return jsonify({
            "updated_at": updated_at,
            "rc": {
                "status": "connected" if poller.rc_status["connected"] else "disconnected",
                "last_ok": poller.rc_status["last_ok"],
                "error": poller.rc_status["error"],
            },
        })

    @app.route("/api/firmware")
    def api_firmware():
        with poller._lock:
            data = poller.get_Firmwarever()
        return data

    @app.route("/api/channels")
    def api_channels():
        return jsonify({"channels": list(channels)})

    # -------- HV endpoints --------
    @app.post("/api/hv/param")
    def api_hv_param():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel", "all")
        param = data.get("param")
        value = data.get("value")
        if param is None or value is None:
            return jsonify({"error": "Missing param or value"}), 400
        try:
            v = float(value)
        except Exception:
            return jsonify({"error": "Invalid value"}), 400
        try:
            poller.set_param_hv("all" if channel == "all" else int(channel), str(param), v)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "message": f"{param}={int(v)} applied to {channel}"}), 200

    @app.post("/api/hv/power")
    def api_hv_power():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel", "all")
        state = (data.get("state") or "").lower()
        if state not in ("on", "off"):
            return jsonify({"error": "state must be 'on' or 'off'"}), 400
        poller.power("all" if channel == "all" else int(channel), on=(state == "on"))
        return jsonify({"ok": True, "message": f"HV {state.upper()} sent to {channel}"}), 200

    @app.post("/api/hv/all")
    def api_hv_all():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").lower()
        try:
            with poller._lock:
                for ch in poller.channels:
                    if action == "on":
                        poller.hv.powerOn(slave=ch)
                    else:
                        poller.hv.powerOff(slave=ch)
            print(f"[HV] ALL channels -> {action.upper()}")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # -------- RC endpoints --------
    @app.post("/api/rc/param")
    def api_rc_param():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel", "all")
        param = data.get("param")
        value = data.get("value")
        if param is None or value is None:
            return jsonify({"error": "Missing param or value"}), 400
        try:
            v = int(value)
        except Exception:
            return jsonify({"error": "Invalid value"}), 400
        try:
            poller.set_param_rc("all" if channel == "all" else int(channel), str(param), v)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "message": f"{param}={int(v)} applied to {channel}"}), 200

    @app.post("/api/rc/power")
    def api_rc_power():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        state = (data.get("state") or "").lower()
        if state not in ("on", "off"):
            return jsonify({"error": "state must be 'on' or 'off'"}), 400
        try:
            with poller._lock:
                  acq_reg = poller.rc.read(0)
                  acq_enabled = bool(acq_reg & (1 << (channel - 1)))
                  if state == "on":
                      if acq_enabled:
                          return jsonify({
                              "error": f"Cannot turn ON channel {channel}: acquisition must be DISABLED first."
                          }), 400
                      poller.rc.turn_on([channel])
                      msg = f"TURN ON applied to channel {channel}"
                  else:
                      poller.rc.turn_off([channel])
                      msg = f"TURN OFF applied to channel {channel}"
                  return jsonify({"ok": True, "message": msg})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/acq")
    def api_rc_acq():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        action = (data.get("action") or "").lower()
        if action not in ("enable", "disable"):
            return jsonify({"error": "action must be 'enable' or 'disable'"}), 400
        try:
            with poller._lock:
                if action == "enable":
                    poller.rc.enable_channel([int(channel)])
                else:
                    poller.rc.disable_channel([int(channel)])
            return jsonify({"ok": True, "message": f"ACQ {action.upper()} for channel {channel}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/trigger")
    def api_rc_trigger():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        action = (data.get("action") or "").lower()
        if action not in ("enable", "disable"):
            return jsonify({"error": "action must be 'enable' or 'disable'"}), 400
        try:
            with poller._lock:
                if action == "enable":
                    poller.rc.enable_trigger([int(channel)])
                else:
                    poller.rc.disable_trigger([int(channel)])
            return jsonify({"ok": True, "message": f"TRIGGER {action.upper()} for channel {channel}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/pulser")
    def api_rc_pulser():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        action = (data.get("action") or "").lower()
        if action not in ("enable", "disable"):
            return jsonify({"error": "action must be 'enable' or 'disable'"}), 400
        try:
            with poller._lock:
                if action == "enable":
                    poller.rc.enable_pulser([int(channel)])
                else:
                    poller.rc.disable_pulser([int(channel)])
            return jsonify({"ok": True, "message": f"TRIGGER {action.upper()} for channel {channel}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/rst")
    def api_rc_rst():
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        action = (data.get("action") or "").lower()
        if action not in ("lock", "free"):
            return jsonify({"error": "action must be 'lock' or 'free'"}), 400
        try:
            with poller._lock:
                if action == "lock":
                    poller.rc.lock_channel([int(channel)])
                else:
                    poller.rc.free_channel([int(channel)])
            return jsonify({"ok": True, "message": f"TRIGGER {action.upper()} for channel {channel}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/rstfifo")
    def api_rc_rstfifo():
        try:
            with poller._lock:
                fifo_status = poller.rc.reset_fifo(verbose=False)
            return jsonify({"ok": True, "message": "FIFO resetted" if fifo_status.lower() == "reset" else "FIFO free"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/turn_all")
    def api_rc_turn_all():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").lower()
        try:
            with poller._lock:
                if action == "on":
                    poller.rc.turn_on(all_channels=True)
                else:
                    poller.rc.turn_off(all_channels=True)
            print(f"[TURN] ALL channels -> {action.upper()}")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/acq_all")
    def api_rc_acq_all():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").lower()
        try:
            with poller._lock:
                if action == "enable":
                    poller.rc.enable_channel(all_channels=True)
                else:
                    poller.rc.disable_channel(all_channels=True)
            print(f"[ACQ] ALL channels -> {action.upper()}")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/trigger_all")
    def api_rc_trigger_all():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").lower()
        try:
            with poller._lock:
                reg_val = 0x7FFFF if action == "enable" else 0x0
                poller.rc.write(58, reg_val)
            print(f"[TRIGGER] ALL channels -> {action.upper()}")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/pulser_all")
    def api_rc_pulser_all():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").lower()
        try:
            with poller._lock:
                reg_val = 0x7FFFF if action == "enable" else 0x0
                poller.rc.write(59, reg_val)
            print(f"[PULSER] ALL channels -> {action.upper()}")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/rc/rst_all")
    def api_rc_rst_all():
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").lower()
        try:
            with poller._lock:
                reg_val = 0x7FFFF if action == "lock" else 0x0
                poller.rc.write(5, reg_val)
            print(f"[BLOCK] ALL channels -> {action.upper()}")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/acq/start")
    def api_acq_start():
        data = request.get_json(silent=True) or {}
        ip = (data.get("ip_addr") or "")
        try:
            with poller._lock:
                poller.rc.process_evbuilder_start(host=ip)
            return jsonify({"ok": True, "message": f"ACQ started on {ip}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/api/acq/stop")
    def api_acq_stop():
        try:
            with poller._lock:
                poller.rc.process_evbuilder_stop()
            return jsonify({"ok": True, "message": f"ACQ stopped"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app

def get_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

def parse_channels(s: str):
    if s == "1-19":
        return list(range(1, 20))
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x.strip()) for x in s.split(",") if x.strip()]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="FD mPMT live control monitor")
    p.add_argument("--host", default='192.168.1.1', help="RunControl/HV host")
    p.add_argument("--rc-port", type=int, default=9000, help="RunControl TCP port")
    p.add_argument("--channels", default="1-19", help="Channels, e.g. 1-19 or 1,2,3")
    p.add_argument("--interval", type=float, default=1.0, help="Polling interval [s]")
    p.add_argument("--server-port", type=int, default=5678, help="HTTP port")
    p.add_argument("--fake", action="store_true", help="Run with fake local data, no sockets and no hardware")
    args = p.parse_args()

    monitoring_channels = parse_channels(args.channels)
    port = args.server_port if args.server_port != 0 else get_free_port()

    if args.fake:
        pollerclass = FakePoller("dummy", args.rc_port, monitoring_channels, args.interval)
    else:
        pollerclass = Poller(args.host, args.rc_port, monitoring_channels, args.interval)

    pollerclass.start()

    # Flask app
    application = make_app(monitoring_channels, pollerclass, args.host or "dummy data")

    print(f"\nWeb UI: http://localhost:{port}/")
    mode = "FAKE" if args.fake else "REAL"
    print(f"Mode: {mode} | Poll: every {args.interval:.2f} s | channels: {monitoring_channels}\n")

    try:
        application.run(host="0.0.0.0", port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping poller...")
        pollerclass.stop()
        time.sleep(0.5)
        print("Bye.")
