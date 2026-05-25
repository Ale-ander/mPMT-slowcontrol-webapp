from datetime import datetime
import threading
import time
from types import SimpleNamespace
from .hvmodbus import HVModbus
from .rc_client import RunControlClient


class Poller(threading.Thread):
    def __init__(self, host: str, rc_port: int, monitoring_channels, interval: float):
        super().__init__(daemon=True)
        self.host = host
        self.rc_port = rc_port
        self.channels = list(monitoring_channels)
        self.interval = max(0.2, float(interval))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sensor_counter = 0  # counter for sensors readings

        self.single_read = {}
        self.latest_readings = {}
        self.latest_sensor_data = {}
        self.latest_update = None
        self.data_lock = threading.Lock()
        self.daq_status = {}
        self.runcontrol_status = {}

        self.rc_status = {
            "connected": False,
            "last_ok": '1970-01-01',
            "error": None,
        }

        self.mainboard_status = {
            "temperature": None,
            "humidity": None,
            "power_ok": None,
            "voltage_ok": None,
        }

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
                        v = max(0, min(1450, int(value)))
                        self.hv.setVoltageSet(v)
                    elif p == "thr":
                        v = float(max(0, min(4095, value)))
                        self.hv.setThreshold(v)
                except Exception:  # swallow per-channel errors to continue others
                    pass

    def set_param_rc(self, channel, param: str, value):
        p = (param or '').strip().lower()
        with self._lock:
            for ch in self._iter_targets(channel):
                try:
                    if p == "rate_threshold":
                        self.rc.set_rate_threshold(value, [ch], verbose=False)
                    elif p == "time_to_peak":
                        self.rc.set_time_to_peak(round(value / 3.7), [ch], verbose=True)
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

        while not self._stop.is_set():
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # ----------------------------
            # Global RC connectivity check
            # ----------------------------
            try:
                self.rc_status["connected"] = True
                self.rc_status["last_ok"] = datetime.now().isoformat()
                self.rc_status["error"] = None
            except Exception as e:
                print("❌ Lost connection to RunControl")
                self.rc_status["connected"] = False
                self.rc_status["error"] = str(e)
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
                hk = self.read_mainboard_hk(self.rc)
                self.mainboard_status.update(hk)
                self.daq_status = self.read_daq_status(self.rc)
                self.runcontrol_status = self.read_rc_status(self.rc)

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
                    voltage = voltage_set = current = temperature = status_txt = alarm = threshold = None
                    hv_on = '0'
                    if turn_on and self.hv.open(ch):
                        try:
                            mon = self.hv.readMonRegisters()
                            if mon:
                                voltage = float(mon.get("V"))
                                voltage_set = mon.get("Vset")
                                current = float(mon.get("I"))
                                temperature = float(mon.get("T"))
                                status_txt = self.decode_status(mon.get("status"))
                                alarm = mon.get("alarm")
                                threshold = mon.get('thresholdm')+mon.get('thresholdq')/10
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
                with self.data_lock:
                    self.latest_readings.clear()
                    self.latest_readings.update(current_rows)
                    self.latest_update = timestamp

            except Exception as e:
                print(f"⚠️ Polling failed: {e}")

            # ----------------------------
            # SENSOR READ (every N cycles)
            # ----------------------------
            self._sensor_counter += 1

            if self._sensor_counter == 5:  # every ~5 sec (adjust)
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

                    with self.data_lock:
                        self.latest_sensor_data.clear()
                        self.latest_sensor_data.update(sensor_row)

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

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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

    @staticmethod
    def read_mainboard_hk(rc) -> dict:
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

    @staticmethod
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

    @staticmethod
    def decode_status(status_code: int) -> str:
        """Convert numeric HV status code to human-readable string."""
        mapping = {
            0: 'UP', 1: 'DOWN', 2: 'RUP', 3: 'RDN',
            4: 'TUP', 5: 'TDN', 6: 'TRIP', -1: 'ERR'
        }
        return mapping.get(status_code, 'undef')


######################################
## Fake Poller for testing purposes ##
######################################
class FakePoller(threading.Thread):
    """
    Fake local poller for UI development/testing without RunControl, rc_tcp.py, or HV hardware.
    It fills the same global structures used by the Flask API.
    """

    def __init__(self, host: str, rc_port: int, monitoring_channels, interval: float):
        super().__init__(daemon=True)
        self.host = host
        self.rc_port = rc_port
        self.channels = list(monitoring_channels)
        self.interval = float(interval)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()

        self._turn_mask = 0

        self.single_read = {}
        self.latest_readings = {}
        self.latest_sensor_data = {}
        self.latest_update = None
        self.data_lock = threading.Lock()
        self.daq_status = {}
        self.runcontrol_status = {}

        self.rc_status = {
            "connected": False,
            "last_ok": '1970-01-01',
            "error": None,
        }

        self.mainboard_status = {
            "temperature": 0,
            "humidity": 0,
            "power_ok": False,
            "voltage_ok": False,
        }

    def run(self):

        self.rc_status["connected"] = True
        self.rc_status["last_ok"] = datetime.now().isoformat()
        self.rc_status["error"] = None

        while not self._stop.is_set():
            now = time.time()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self.mainboard_status = {
                "temperature": 28.0,
                "humidity": 42.0,
                "power_ok": True,
                "voltage_ok": True,
            }

            self.daq_status = {
                "deadtime": 5.0,
                "fifo_words": 1000,
                "fifo_full": False,
                "tr32_received": True,
                "tr32_aligned": True,
                "tr32_sync": True,
                "tr32_count": int(now - self._t0),
                "tagt_received": True,
                "tagt_parity_ok": True,
                "pll_locked": True,
                "pll_stable": True,
                "clock_source": "Quartz",
                "clock_source_set": "Quartz",
                "clock_cable": 1,
                "clock_cable_set": 1,
            }

            self.runcontrol_status = {
                "overcurrent": "Ok",
                "spi_speed": 12.5,
                "pulser_freq": 1000,
            }

            current_rows = {}

            for ch in self.channels:

                turn_on = True
                acq_enabled = True
                trig_enabled = True
                puls_enabled = True
                rst_enabled = True

                base_rate = 500 + 100 * ch
                rate = base_rate

                if turn_on and (ch % 2 == 0):
                    voltage = 950
                    voltage_set = 950
                    current = 0.2
                    temperature = 30
                    status_txt = "UP"
                    alarm = 0
                    threshold = 120
                    hv_on = 1
                else:
                    voltage = 0
                    voltage_set = 950
                    current = 0
                    temperature = 30
                    status_txt = "DOWN"
                    alarm = 0
                    threshold = 120
                    hv_on = 0

                current_rows[ch] = {
                    "ts": timestamp,
                    "channel": ch,
                    "rate_hz": rate,
                    "rate_th": 1000,
                    "ttp": 120.0,
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
                    "hv_on": int(hv_on),
                }

            sensor_row = {
                "V_5V": 5,
                "V_3V3": 3,
                "I_poeA": 1,
                "I_poeB": 2,
                "P_poeA": 1,
                "P_poeB": 2,
                "T": 30,
                "P": 1.000,
                "H": 20,
                "Mx": 10,
                "My": 20,
                "Mz": 30,
            }

            with self.data_lock:
                self.latest_readings.clear()
                self.latest_readings.update(current_rows)
                self.latest_sensor_data.clear()
                self.latest_sensor_data.update(sensor_row)
                self.latest_update = timestamp

            self.rc_status["connected"] = True
            self.rc_status["last_ok"] = datetime.now().isoformat()
            self.rc_status["error"] = None

            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    @staticmethod
    def get_Firmwarever():
        return {
            "version": "v0.0.1",
            "date": "01-01-1970",
        }

    @staticmethod
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

    def set_param_hv(self, channel, param: str, value):
        return

    def set_param_rc(self, channel, param: str, value):
        return

    def power(self, channel, on: bool):
        with self._lock:
            targets = self.channels if channel == "all" else [int(channel)]
            for ch in targets:
                mask = 1 << (ch - 1)
                if on:
                    self._turn_mask |= mask
                else:
                    self._turn_mask &= ~mask

