#!/usr/bin/env python3
# coding: utf-8

import argparse
from datetime import datetime
import os
import socket
import threading
import time
from types import SimpleNamespace
from contextlib import closing

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from libs.hvmodbus import HVModbus
from libs.rc_client import RunControlClient
from libs.helper_functions import decode_status
import math


single_read = {}
latest_readings = {}
latest_sensor_data = {}
latest_update = None
data_lock = threading.Lock()
daq_status = {}
runcontrol_status = {}

rc_status = {
    "connected": False,
    "last_ok": None,
    "error": None,
}

mainboard_status = {
    "temperature": None,
    "humidity": None,
    "power_ok": None,
    "voltage_ok": None,
}


def read_rc_status(rc):
    """
    Read RC-related registers from RunControl.
    Returns dict with deadtime, fifo, tr32, clock info.
    """
    try:
        ovc_reg = 'Ok' if rc.read(2) == 0 else 'Overcurrent detected!'
        spi_speed_b = rc.read(4) >> 19
        try:
            pulser_reg = 1000000 / rc.read(7)
        except ZeroDivisionError:
            pulser_reg = 0

        match spi_speed_b:
            case 0: spi_speed = 10.42
            case 1: spi_speed = 12.5
            case 2: spi_speed = 15.625
            case _: spi_speed = 20.83

        return {
            "overcurrent": ovc_reg,
            "spi_speed": spi_speed,
            "pulser_freq": pulser_reg,
        }

    except Exception as e:
        return {"error": str(e)}

def read_daq_status(rc):
    """
    Read DAQ-related registers from RunControl.
    Returns dict with deadtime, fifo, tr32, clock info.
    """
    try:
        reg3 = rc.read(3)
        reg4 = rc.read(4)

        deadtime = round((65535 - rc.read(27)) / 65535 * 100, 2)
        fifodata = rc.read(43)
        fifo_full = (reg3 & 0x1) > 0

        # --- TR32 ---
        tr32_received = not (reg3 & 0x800)
        tr32_aligned = not (reg3 & 0x400)
        tr32_sync = not (reg3 & 0x1000)
        tr32_count = rc.read(45)

        tagt_received = not (reg3 & 0x2000)
        tagt_parity_ok = not (reg3 & 0x4000)

        # --- CLOCK ---
        pll_locked = (reg3 & 0x2) > 0
        pll_stable = not (reg3 & 0x8000)

        clock_source = "Quartz" if (reg3 & 0x200) > 0 else "Cable"
        clock_source_set = "Quartz" if (reg4 & 0x400) > 0 else "Cable"

        clock_cable = 2 if (reg3 & 0x100) > 0 else 1
        clock_cable_set = 2 if (reg4 & 0x800) > 0 else 1

        return {
            "deadtime": deadtime,
            "fifo_words": fifodata,
            "fifo_full": fifo_full,

            "tr32_received": tr32_received,
            "tr32_aligned": tr32_aligned,
            "tr32_sync": tr32_sync,
            "tr32_count": tr32_count,

            "tagt_received": tagt_received,
            "tagt_parity_ok": tagt_parity_ok,

            "pll_locked": pll_locked,
            "pll_stable": pll_stable,

            "clock_source": clock_source,
            "clock_source_set": clock_source_set,
            "clock_cable": clock_cable,
            "clock_cable_set": clock_cable_set,
        }

    except Exception as e:
        return {"error": str(e)}

def alarm_string(alarm_code: int, html: bool = True) -> str:
    if not isinstance(alarm_code, int):
        return "none"
    if alarm_code == 0:
        return "none"
    tags = []
    if alarm_code & 1: tags.append("OV")
    if alarm_code & 2: tags.append("UV")
    if alarm_code & 4: tags.append("OC")
    if alarm_code & 8: tags.append("OT")
    text = " ".join(tags)
    if html:
        return f'<span style="background:#ffb3b3;color:#000;padding:2px 6px;border-radius:6px;font-weight:600">{text}</span>'
    return text

def read_mainboard_hk(rc):
    """
    Read main board house-keeping registers.
    Returns dict with temperature, humidity, power_ok, voltage_ok.
    """
    try:
        reg56 = rc.read(56)
        reg61 = rc.read(61)

        temperature = (reg56 >> 12) / 100.0
        humidity = (reg56 & 0xFFF) / 100.0
        power_ok = not bool(reg61 & 0x2)
        voltage_ok = not bool(reg61 & 0x1)

        return {
            "temperature": temperature,
            "humidity": humidity,
            "power_ok": power_ok,
            "voltage_ok": voltage_ok
        }

    except Exception as e:
        return {
            "temperature": None,
            "humidity": None,
            "power_ok": None,
            "voltage_ok": None,
            "error": str(e)
        }

def clean_hv_info_field(value: str) -> str:
    """
    Clean a string returned by HVModbus.getInfo() from non-printable or null bytes.
    Converts b'\x00' padding and other weird characters into a readable, safe string.
    """
    if not isinstance(value, str):
        value = str(value)
    value = value.replace('\x00', '').replace('\u0000', '')
    value = ''.join(ch for ch in value if 32 <= ord(ch) <= 126)

    return value.strip()

# -----------------------------------------------------------------------------
# Poller thread — reads HV and RC, stores to DB + thread-safe control ops
# -----------------------------------------------------------------------------
class Poller(threading.Thread):
    def __init__(self, host: str, rc_port: int, monitoring_channels, interval: float):
        super().__init__(daemon=True)
        self.host = host
        self.rc_port = rc_port
        self.channels = list(monitoring_channels)
        self.interval = max(0.2, float(interval))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sensor_counter = 0 # counter for sensors readings

        # Interfaces
        self.rc = RunControlClient(self.host, self.rc_port)
        self.hv = HVModbus(SimpleNamespace(port="/dev/ttyPS1", host=self.host, mode="tcp"))

    # ---------- Helpers ----------
    def _iter_targets(self, channel):
        if channel == "all":
            return self.channels
        return [int(channel)]

    # ---------- Control operations (thread-safe) ----------
    def set_param_hv(self, channel, param: str, value):
        p = (param or '').strip().lower()
        with self._lock:
            for ch in self._iter_targets(channel):
                try:
                    if not self.hv.open(ch):
                        continue
                    if p == "vset":
                        v = int(max(0, min(1450, int(value))))
                        self.hv.setVoltageSet(v)
                    elif p == "thr":
                        v = int(max(0, min(4095, int(value))))
                        self.hv.setThreshold(v)
                except Exception:       # swallow per-channel errors to continue others
                    pass

    def set_param_rc(self, channel, param: str, value):
        p = (param or '').strip().lower()
        with self._lock:
            for ch in self._iter_targets(channel):
                try:
                    if p == "rate_threshold":
                        self.rc.set_rate_threshold(value, [ch], verbose=False)
                    elif p == "time_to_peak":
                        self.rc.set_time_to_peak(round(value/3.7), [ch], verbose=True)
                    elif p == "pulser_frequency":
                        self.rc.pulser_set_frequency(value, verbose=False)
                    elif p == "spi_speed":
                        self.rc.set_spi_speed(value, verbose=False)
                except Exception:
                    pass
        return

    def power(self, channel, on: bool):
        with self._lock:
            for ch in self._iter_targets(channel):
                try:
                    if self.hv.open(ch):
                        if on:
                            self.hv.powerOn(ch)
                        else:
                            self.hv.powerOff(ch)
                except Exception:
                    pass

    # ---------- Polling loop ----------
    def run(self):
        global latest_update, single_read

        while not self._stop.is_set():
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # ----------------------------
            # Global RC connectivity check
            # ----------------------------
            try:
                rc_status["connected"] = True
                rc_status["last_ok"] = datetime.now().isoformat()
                rc_status["error"] = None
            except Exception as e:
                print("❌ Lost connection to RunControl")
                rc_status["connected"] = False
                rc_status["error"] = str(e)
                if not self._reconnect_rc():
                    print(f"🚨 NO CONNECTION to main board (RunControl) @ {self.host}:{self.rc_port}")
                    self._stop.wait(self.interval)
                    break

                if not self._reconnect_hv():
                    print(f"🚨 NO CONNECTION to main board (HV) @ {self.host}")
                    self._stop.wait(self.interval)
                    break

            try:
                # --- Read RC state registers once ---
                hk = read_mainboard_hk(self.rc)
                mainboard_status.update(hk)
                global daq_status, runcontrol_status
                daq_status = read_daq_status(self.rc)
                runcontrol_status = read_rc_status(self.rc)

                acq_reg = self.rc.read(0)
                turn_reg = self.rc.read(1)
                rst_reg = self.rc.read(5)
                trig_reg = self.rc.read(58)
                puls_reg = self.rc.read(59)

                current_rows = {}
                for ch in self.channels:
                    mask = 1 << (ch - 1)
                    turn_on = bool(turn_reg & mask)
                    acq_enabled = bool(acq_reg & mask)
                    trig_enabled = bool(trig_reg & mask)
                    puls_enabled = bool(puls_reg & mask)
                    rst_enabled = bool(rst_reg & mask)

                    rate = self.rc.read(ch + 7)

                    chaddress = (ch - 1) // 2 + 28
                    cleanreg = self.rc.read(chaddress)
                    if ch % 2 == 0:
                        ttp = float(cleanreg & 0xFFF)
                    else:
                        ttp = float((cleanreg & 0xFFF000) >> 12)

                    chaddress = (ch - 1) // 2 + 46  # 46..55
                    cleanreg = self.rc.read(chaddress)
                    if ch % 2 == 0:
                        rate_th = (cleanreg & 0xFFFF0000) >> 16
                    else:
                        rate_th = cleanreg & 0xFFFF

                    # --- Read HV only if channel is turned ON ---
                    voltage = voltage_set = current = temperature =  status_txt = alarm = threshold = None
                    hv_on = '0'
                    if turn_on:
                        try:
                            mon = self.hv.readMonRegisters(slave=ch)
                            if mon:
                                voltage = float(mon.get("V"))
                                voltage_set = mon.get("Vset")
                                current = float(mon.get("I"))
                                temperature = float(mon.get("T"))
                                status_txt = decode_status(mon.get("status"))
                                alarm = mon.get("alarm")
                                threshold = mon.get("threshold")
                                hv_on = mon.get("status") in (0, 2)
                        except Exception as e:
                            print(f"⚠️ HV communication error on channel {ch}: {e}")
                            status_txt = "⚠️ HV reading ERROR "

                    row = {
                        "ts": timestamp,
                        "channel": ch,
                        "rate_hz": rate,
                        "rate_th": rate_th,
                        "ttp": ttp * 3.7,
                        "voltage": voltage,
                        "voltage_set": voltage_set,
                        "current": current,
                        "temperature": temperature,
                        "status": status_txt,
                        "alarm": alarm,
                        "threshold": threshold,
                        "turn_on": int(turn_on),
                        "acq_enabled": int(acq_enabled),
                        "trig_enabled": int(trig_enabled),
                        "puls_enabled": int(puls_enabled),
                        "rst_enabled": int(rst_enabled),
                        "hv_on": int(hv_on)
                    }

                    current_rows[ch] = row
                with data_lock:
                    latest_readings.clear()
                    latest_readings.update(current_rows)
                    latest_update = timestamp

            except Exception as e:
                print(f"⚠️ Polling failed: {e}")


            # ----------------------------
            # SENSOR READ (every N cycles)
            # ----------------------------
            self._sensor_counter += 1

            if self._sensor_counter == 5:   # every ~5 sec (adjust)
                self._sensor_counter = 0
                try:
                    sens = self.rc.read_sensors()

                    sensor_row = {
                        "V_5V": sens.get("V_5V"),
                        "V_3V3": sens.get("V_3V3"),

                        "I_poeA": sens.get("I_poeA"),
                        "I_poeB": sens.get("I_poeB"),

                        "P_poeA": sens.get("P_poeA"),
                        "P_poeB": sens.get("P_poeB"),

                        "T": sens.get("T_C"),
                        "P": sens.get("P_hPa"),
                        "H": sens.get("H_pct"),

                        "Mx": sens.get("Mag_x"),
                        "My": sens.get("Mag_y"),
                        "Mz": sens.get("Mag_z"),
                    }

                    with data_lock:
                        latest_sensor_data.clear()
                        latest_sensor_data.update(sensor_row)

                except Exception as e:
                    print(f"⚠️ Sensor read failed: {e}")

            self._stop.wait(self.interval)
                
                
    def _fatal_connection_error(self, what: str):
        """
        Stop poller permanently after unrecoverable connection failure.
        """
        msg = f"🚨 FATAL: {what} connection failed to {self.host}"
        print(msg)
        self._stop.set()
    
    def stop(self):
        self._stop.set()
        try:
            self.rc.close()
        except Exception:
            pass

    # ---------- Connection helpers ----------
    def _reconnect_rc(self, retries=5, delay=2):
        for i in range(1, retries + 1):
            try:
                print(f"🔄 Reconnecting RunControl ({i}/{retries})...")
                try:
                    self.rc.close()
                except Exception:
                    pass
                self.rc = RunControlClient(self.host, self.rc_port)
                self.rc.read(0)  # sanity check
                print("✅ RunControl connected")
                return True
            except Exception as e:
                print(f"⚠️ RC reconnect failed: {e}")
                time.sleep(delay)

        self._fatal_connection_error("RunControl")
        return False

    def _reconnect_hv(self, retries=5, delay=2):
        for i in range(1, retries + 1):
            try:
                print(f"🔄 Reconnecting HV Modbus ({i}/{retries})...")
                self.hv = HVModbus(
                    SimpleNamespace(port="/dev/ttyPS1", host=self.host, mode="tcp")
                )
                self.hv.open(1)
                print("✅ HV Modbus connected")
                return True
            except Exception as e:
                print(f"⚠️ HV reconnect failed: {e}")
                time.sleep(delay)

        self._fatal_connection_error("HV Modbus")
        return False

    def get_Firmwarever(self):
        date = str(hex(self.rc.read(61))[2:])
        ver = str(hex(self.rc.read(104)))
        try:
            return {'version': f'v{ver[2]}.{int(ver[3:5])}.{int(ver[5:], 16)}',
                'date': f'{date[6:]}-{date[4:6]}-{date[:4]}'}
        except ValueError:
            return {'version': 'v0.0.0', 'date': '01-01-1970'}

# -----------------------------------------------------------------------------
# Flask app (HTML + JSON + control endpoints)
# -----------------------------------------------------------------------------
def make_app(channels, poller: Poller, host):
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
        return jsonify(mainboard_status)

    @app.route("/api/sensors/latest")
    def api_sensors_latest():
        with data_lock:
            return jsonify(dict(latest_sensor_data))

    @app.route("/api/daq/latest")
    def api_daq_latest():
        return jsonify(daq_status)

    @app.route("/api/rc/latest")
    def api_rc_latest():
        return jsonify(runcontrol_status)

    @app.route("/api/latest")
    def api_latest():
        html = request.args.get("html") == "1"

        with data_lock:
            rows = [dict(latest_readings[ch]) for ch in channels if ch in latest_readings]

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
                "alarm_str": alarm_string(alarm, html=html),
                "threshold": r.get("threshold"),
                "turn_on": bool(r.get("turn_on")),
                "acq_enabled": bool(r.get("acq_enabled")),
                "trig_enabled": bool(r.get("trig_enabled")),
                "puls_enabled": bool(r.get("puls_enabled")),
                "rst_enabled": bool(r.get("rst_enabled")),
                "hv_on": bool(r.get("hv_on")),
            })

        return jsonify({"rows": out, "count": len(out)} | single_read)

    @app.route("/api/last_update")
    def api_last_update():
        with data_lock:
            updated_at = latest_update

        return jsonify({
            "updated_at": updated_at,
            "rc": {
                "status": "connected" if rc_status["connected"] else "disconnected",
                "last_ok": rc_status["last_ok"],
                "error": rc_status["error"],
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

# -----------------------------------------------------------------------------
# Utility to get a random free port
# -----------------------------------------------------------------------------
def get_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def parse_channels(s: str):
    if s == "1-19":
        return list(range(1, 20))
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x.strip()) for x in s.split(",") if x.strip()]

if __name__ == "__main__":
    load_dotenv(override=True)

    default_ip = os.getenv("TESTER_0_IP")

    p = argparse.ArgumentParser(description="FD mPMT live control monitor")
    p.add_argument("--host", default=default_ip, help="RunControl/HV host")
    p.add_argument("--rc-port", type=int, default=9000, help="RunControl TCP port")
    p.add_argument("--channels", default="1-19", help="Channels, e.g. 1-19 or 1,2,3")
    p.add_argument("--interval", type=float, default=1.0, help="Polling interval [s]")
    p.add_argument("--server-port", type=int, default=5678, help="HTTP port")
    args = p.parse_args()

    channels = parse_channels(args.channels)
    port = args.server_port if args.server_port != 0 else get_free_port()

    # Start poller thread
    poller = Poller(args.host, args.rc_port, channels, args.interval)
    poller.start()

    # Flask app
    app = make_app(channels, poller, args.host)

    print(f"\nWeb UI: http://localhost:{args.server_port}/")
    print(f"Poll: every {args.interval:.2f} s | channels: {channels}\n")

    try:
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping poller...")
        poller.stop()
        time.sleep(0.5)
        print("Bye.")
