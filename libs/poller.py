from datetime import datetime
import threading
import time
from . import mssclient


class Poller(threading.Thread):
    def __init__(self, host: str, interval: float):
        super().__init__(daemon=True)
        self.host = host
        self.channels = list(range(1, 20))
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
            "temperatureC": None,
            "relativeHumidityPercent": None,
            "powerOk": None,
            "voltageOk": None,
        }

        # Client
        self.client = mssclient.MSSClient(url=host)

    # ---------- Control operations (thread-safe) ----------
    def set_param_hv(self, channel: int | str, param: str, value: int):
        p = (param or '').strip().lower()
        with self._lock:
            try:
                onlinechannels = self.client.febmgr.getOnlineChannels(channel_type=mssclient.DeviceType.PMT)
                if channel != "all" and channel not in onlinechannels:
                    pass
                if p == "vset":
                    if channel == "all":
                        self.client.febmgr.setPMTVoltageSetAll(value)
                    else:
                        self.client.febmgr.setPMTVoltageSet(channel, value)
                elif p == "thr":
                    if channel == "all":
                        self.client.febmgr.setPMTThresholdAll(value)
                    else:
                        self.client.febmgr.setPMTThreshold(channel, value)
            except Exception:  # swallow per-channel errors to continue others
                pass

    def set_param_rc(self, channel: int | str, param: str, value: int):
        p = (param or '').strip().lower()
        with self._lock:
            try:
                if p == "rate_threshold":
                    if channel == "all":
                        print(f"set_param_rc: {channel} {p} {value}")
                        self.client.febmgr.setAllRateThreshold(value)
                    else:
                        self.client.febmgr.setRateThresholdChannel(channel, value)
                elif p == "time_to_peak":
                    if channel == "all":
                        self.client.febmgr.setAllTimeToPeak(value)
                    else:
                        self.client.febmgr.setTimeToPeakChannel(channel, value)
                elif p == "pulser_frequency":
                    self.client.fpga.setPulserFrequency(value)
                elif p == "spi_speed":
                    self.client.fpga.setSpiClock(value)
            except Exception:
                pass
        return

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
                self.rc_status["connected"] = False
                self.rc_status["error"] = str(e)
                if not self._reconnect_client():
                    print(f"NO CONNECTION to MainBoard @ {self.host}")
                    self._stop.wait(self.interval)
                    break

            try:
                # --- Read RC state registers once ---
                onlinechannels = self.client.febmgr.getOnlineChannels(channel_type=mssclient.DeviceType.PMT)
                hk = self.client.fpga.getHousekeeping()
                self.mainboard_status.update(hk)
                self.runcontrol_status = self.read_rc_status()
                self.daq_status = self.read_daq_status()
                status = self.client.febmgr.getStatus()
                current_rows = {}

                for ch in self.channels:
                    if ch in onlinechannels:
                        row = {
                            "ts": timestamp,
                            "channel": ch,
                            "rate_hz": status[str(ch)]['Rate'],
                            "rate_th": status[str(ch)]['Rate threshold'],
                            "ttp": status[str(ch)]['Time to peak'],
                            "voltage": status[str(ch)]['V'],
                            "voltage_set": status[str(ch)]['Vset'],
                            "current": status[str(ch)]['I'],
                            "temperature": status[str(ch)]['T'],
                            "status": status[str(ch)]['Status']['string'],
                            "alarm": status[str(ch)]['Alarm']['string'],
                            "threshold": status[str(ch)]['Threshold'],
                            "turn_on": True,
                            "acq_enabled": status[str(ch)]['Acquisition'] == 'enabled',
                            "trig_enabled": status[str(ch)]['Trigger'] == 'enabled',
                            "puls_enabled": status[str(ch)]['Pulser'] == 'enabled',
                            "rst_enabled": status[str(ch)]['Block'] == 'blocked',
                            "hv_on": status[str(ch)]['Status']['value'] in (0,2),
                        }
                    else:
                        row = {
                            "ts": timestamp,
                            "channel": ch,
                            "rate_hz": 0,
                            "rate_th": self.client.febmgr.getRateThreshold()[str(ch)],
                            "ttp": self.client.febmgr.getTimeToPeak()[str(ch)]*3.7,
                            "voltage": '',
                            "voltage_set": '',
                            "current": '',
                            "temperature": '',
                            "status": '',
                            "alarm": '',
                            "threshold": '',
                            "turn_on": 0,
                            "acq_enabled": 0,
                            "trig_enabled": 0,
                            "puls_enabled": 0,
                            "rst_enabled": 0,
                            "hv_on": 0
                        }

                    current_rows[ch] = row
                with self.data_lock:
                    self.latest_readings.clear()
                    self.latest_readings.update(current_rows)
                    self.latest_update = timestamp

            except Exception as e:
                print(f"Polling failed: {e}")

            # ----------------------------
            # SENSOR READ (every N cycles)
            # ----------------------------
            self._sensor_counter += 1

            if self._sensor_counter == 5:  # every ~5 sec (adjust)
                self._sensor_counter = 0
                try:
                    sens = self.client.sensors.read()

                    sensor_row = {
                        "V_5V": sens['tla2024'][0]['value'],
                        "V_3V3": sens['tla2024'][1]['value'],

                        "I_poeA": sens['tla2024'][2]['value'],
                        "I_poeB": sens['tla2024'][3]['value'],

                        "P_poeA": sens['tla2024'][4]['value'],
                        "P_poeB": sens['tla2024'][5]['value'],

                        "T": sens['bme280-in'][0]['value'],
                        "P": sens['bme280-in'][1]['value'],
                        "H": sens['bme280-in'][2]['value'],

                        "Mx": sens['bm1422'][0]['value'],
                        "My": sens['bm1422'][1]['value'],
                        "Mz": sens['bm1422'][2]['value']
                    }

                    with self.data_lock:
                        self.latest_sensor_data.clear()
                        self.latest_sensor_data.update(sensor_row)

                except Exception as e:
                    print(f"Sensor read failed: {e}")

            self._stop.wait(self.interval)

    def _fatal_connection_error(self, what: str):
        """Stop poller permanently after unrecoverable connection failure."""
        msg = f"FATAL: {what} connection failed to {self.host}"
        print(msg)
        self._stop.set()

    def stop(self):
        self._stop.set()
        try:
            self.rc.close()
        except Exception:
            pass

    # ---------- Connection helpers ----------
    def _reconnect_client(self, retries=5, delay=2):
        for i in range(1, retries + 1):
            try:
                print(f"Reconnecting Client ({i}/{retries})...")
                self.client = mssclient.BaseRpcClient(url=f'{self.host}:8000/rpc')
                print("Cleint connected")
                return True
            except Exception as e:
                print(f"Client reconnection failed: {e}")
                time.sleep(delay)

        self._fatal_connection_error("HV Modbus")
        return False

    def read_rc_status(self):
        """
        Read RC-related registers from RunControl.
        Returns dict with overcurrent, spi clock speed and pusler frequency
        """
        try:
            return {
                "overcurrent": self.client.fpga.readRegister(2),
                "spi_speed": self.client.fpga.getSpiClock(),
                "pulser_freq": self.client.fpga.getPulserFrequency()['frequencyHz'],
            }

        except Exception as e:
            return {"error": str(e)}

    def read_daq_status(self):
        """
        Read DAQ-related registers from RunControl.
        Returns dict with deadtime, fifo, tr32 and clock info.
        """
        try:
            clock_regs = self.client.fpga.getClockStatus()
            tr_regs = self.client.fpga.getTr32Status()
            fifo_regs = self.client.fpga.getFifoStatus()

            return {
                "deadtime": self.client.fpga.getDeadtime()['percent'],
                "fifo_words": fifo_regs['words'],
                "fifo_full": fifo_regs['full'],

                "tr32_received": tr_regs['received'],
                "tr32_aligned": tr_regs['aligned'],
                "tr32_sync": tr_regs['arrivedEarly'],
                "tr32_count": tr_regs['count'],

                "tagt_received": tr_regs['tagTReceived'],
                "tagt_parity_ok": tr_regs['tagTParityOk'],

                "pll_locked": clock_regs['pllLocked'],
                "pll_stable": clock_regs['clockUnstable'],

                "clock_source": clock_regs['activeSource'],
                "clock_source_set": clock_regs['configuredSource'],
                "clock_cable": clock_regs['activeCable'],
                "clock_cable_set": clock_regs['configuredCable'],
            }

        except Exception as e:
            return {"error": str(e)}

    def get_Firmwarever(self):
        infos = self.client.fpga.getFirmwareInfo()
        return {
            "version": infos['version'],
            "date": infos['bitstreamDate'],
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


######################################
## Fake Poller for testing purposes ##
######################################
class FakePoller(threading.Thread):
    """
    Fake local poller for UI development/testing without RunControl, rc_tcp.py, or HV hardware.
    It fills the same global structures used by the Flask API.
    """

    def __init__(self, host: str, rc_port: int, interval: float):
        super().__init__(daemon=True)
        self.host = host
        self.rc_port = rc_port
        self.channels = list(range(1, 20))
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
    def get_Firmwarever(self):
        return {
            "version": 'v1.1.1',
            "date": '01-01-1970',
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

