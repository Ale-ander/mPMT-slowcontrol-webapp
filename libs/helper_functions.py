#!/usr/bin/env python3
import json
import os
import csv
import time
import math
import pickle
import signal
import shutil
import tempfile
import subprocess
import datetime as dt
import warnings
import numpy as np
import pandas as pd
from tqdm import TqdmExperimentalWarning
import uproot
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from tqdm.rich import tqdm
from tabulate import tabulate
from scipy.optimize import curve_fit
from scipy.interpolate import UnivariateSpline

warnings.filterwarnings("ignore", category=TqdmExperimentalWarning)


# ==========================================
# Correction function
# ==========================================

def timewalk_correction_ns(amplitude, k=17.1, A0=8, c=0, alpha=0.1452):
    A = np.array(amplitude, dtype=float)
    dA = A - A0
    return k / (dA ** alpha) + c


def fmt(val, ndigits=2):
    return f"{val:.{ndigits}f}" if isinstance(val, (int, float)) else "—"


def json_default(o):
    if isinstance(o, (np.integer,)):     return int(o)
    if isinstance(o, (np.floating,)):    return float(o)
    if isinstance(o, (np.bool_,)):       return bool(o)
    if isinstance(o, (np.ndarray,)):     return o.tolist()
    if isinstance(o, (dt.datetime, dt.date)): return o.isoformat()
    # last resort:
    return str(o)


import numpy as np
import pandas as pd


def apply_timewalk_lut(t_ns, amplitude_adc, csv_path='timewalk_lut.csv'):
    """
    Full LUT-based time-walk correction (all-in-one).

    Parameters
    ----------
    t_ns : float or np.ndarray
        Time in ns
    amplitude_adc : float or np.ndarray
        Signal amplitude in ADC
    csv_path : str
        Path to LUT CSV file
    Returns
    -------
    t_corr_ns : np.ndarray
        Corrected time
    """

    # -----------------------------
    # LOAD + CLEAN LUT
    # -----------------------------
    df = pd.read_csv(csv_path)

    # Fix missing values (important for your table)
    df = df.interpolate().bfill().ffill()

    amplitude_lut = df["amplitude_adc"].values

    # Convert to numpy dict
    lut_global = df["timewalk_global_ns"].values

    # -----------------------------
    # PREP INPUTS
    # -----------------------------
    t = np.asarray(t_ns)
    amp = np.asarray(amplitude_adc)

    # -----------------------------
    # INTERPOLATION
    # -----------------------------
    correction = np.interp(
        amp,
        amplitude_lut,
        lut_global,
        left=lut_global[0],
        right=lut_global[-1]
    )

    # -----------------------------
    # APPLY CORRECTION
    # -----------------------------
    return t - correction


def remove_time_outliers(t_ns, threshold_ns=1e6):
    """
    Remove samples where the jump to neighbors exceeds threshold.

    Parameters
    ----------
    t_ns : np.ndarray
        Time array in ns (1D)
    threshold_ns : float
        Threshold difference (default = 1 ms = 1e6 ns)

    Returns
    -------
    np.ndarray
        Boolean mask of valid points
    """

    if len(t_ns) < 3:
        return np.ones_like(t_ns, dtype=bool)

    dt_prev = np.abs(t_ns[1:-1] - t_ns[:-2])
    dt_next = np.abs(t_ns[1:-1] - t_ns[2:])

    good_mid = (dt_prev < threshold_ns) & (dt_next < threshold_ns)

    mask = np.ones_like(t_ns, dtype=bool)
    mask[1:-1] = good_mid

    return mask


# ===============================================================
#  PROCESS CONTROL UTILITIES
# ===============================================================

def start_event_receiver(
        filename: str,
        script_path: str = "./even-receiver/event-receiver-uproot.py",
        data_folder: str = None,
        port: int = 5566
):
    """
    Start the event receiver process and log its output.

    Parameters
    ----------
    filename : str
        Output ROOT file name.
    script_path : str
        Path to event-receiver script (default: uproot version).
    data_folder : str
        Folder to save the file (default: $DATA_FOLDER or '.').

    Returns
    -------
    tuple
        (process, log_file, output_file)
    """
    if data_folder is None:
        data_folder = os.getenv("DATA_FOLDER", ".")
    os.makedirs(data_folder, exist_ok=True)
    output_file = os.path.join(data_folder, filename)

    cmd = ["python", script_path, "--filename", output_file, "-p", str(port)]
    print(f"\n▶️ Starting data taking...")
    print(f"Running command: {' '.join(cmd)}")

    tmpfile = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log")
    process = subprocess.Popen(cmd, stdout=tmpfile, stderr=subprocess.STDOUT)

    print(f"Started {os.path.basename(script_path)} → {output_file} "
          f"(PID={process.pid}) (log {tmpfile.name})")

    return process, tmpfile.name, output_file


def stop_event_receiver(process: subprocess.Popen, timeout: int = 30):
    """
    Stop the event receiver process cleanly (send Ctrl+C).
    If it doesn't terminate, force kill after timeout.

    Parameters
    ----------
    process : subprocess.Popen
        Running process handle.
    timeout : int
        Timeout before force kill (default: 10s).
    """
    print("🛑 Stopping event receiver...")
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
        print("Event-receiver terminated cleanly ✅")
    except subprocess.TimeoutExpired:
        process.kill()
        print("Event-receiver killed after timeout ❌")


# ===============================================================
#  HV MANAGEMENT FUNCTIONS
# ===============================================================

def decode_status(status_code: int) -> str:
    """Convert numeric HV status code to human-readable string."""
    mapping = {
        0: 'UP', 1: 'DOWN', 2: 'RUP', 3: 'RDN',
        4: 'TUP', 5: 'TDN', 6: 'TRIP', -1: 'ERR'
    }
    return mapping.get(status_code, 'undef')


def alarmString(alarmCode):
    """
    Decode HV alarm bitfield to human-readable string.
    """
    msg = ""
    if alarmCode == 0:
        return "none"
    if alarmCode & 1:
        msg += "OV "
    if alarmCode & 2:
        msg += "UV "
    if alarmCode & 4:
        msg += "OC "
    if alarmCode & 8:
        msg += "OT "
    return msg.strip()


def print_all_channels(dev, channels=range(1, 20)):
    """
    Print a full HV channel summary table with status, calibration, and limits.
    Combines readMonRegisters() (fast bulk read) with extra calibration reads.
    """
    table = []
    headers = [
        "Ch", "Status", "V [V]", "Vset [V]", "I [uA]", "T [°C]",
        "RateUP", "RateDN", "LimitV", "LimitI", "LimitT", "TRIP",
        "Threshold", "Alarm", "Vref [mV]", "Calib m", "Calib q", "Disc [mV]"
    ]

    print("\n📡 Reading full status data from HV channels (combined readMonRegisters + calibration)...")

    for ch in channels:
        try:
            if not dev.open(ch):
                table.append([ch, "❌", *["-"] * (len(headers) - 2)])
                continue

            # -----------------------------
            # 1. Bulk monitoring read
            # -----------------------------
            mon = dev.readMonRegisters(slave=ch)
            if mon is None:
                raise Exception("Modbus read error")

            voltage = mon.get("V", float("nan"))
            voltage_set = mon.get("Vset", float("nan"))
            current = mon.get("I", float("nan"))
            temperature = mon.get("T", float("nan"))
            rate_up = mon.get("rateUP", "-")
            rate_dn = mon.get("rateDN", "-")
            limit_v = mon.get("limitV", "-")
            limit_i = mon.get("limitI", "-")
            limit_t = mon.get("limitT", "-")
            trip = mon.get("limitTRIP", "-")
            threshold = mon.get("threshold", "-")
            alarm = mon.get("alarm", "-")
            status_code = mon.get("status", None)

            from helper_functions import decode_status
            status = decode_status(status_code) if status_code is not None else "?"

            # -----------------------------
            # 2. Calibration & Vref reads
            # -----------------------------
            try:
                vref = dev.getVref(slave=ch)
            except Exception:
                vref = float("nan")

            try:
                calib_m, calib_q, disc_mv = dev.readCalibRegisters(slave=ch)
            except Exception:
                calib_m, calib_q, disc_mv = float("nan"), float("nan"), float("nan")

            # -----------------------------
            # 3. Decode alarm string
            # -----------------------------
            alarm_str = alarmString(alarm)

            # -----------------------------
            # 3. Build row
            # -----------------------------
            table.append([
                ch,
                status,
                f"{voltage:.3f}" if np.isfinite(voltage) else "-",
                f"{voltage_set:.1f}" if np.isfinite(voltage_set) else "-",
                f"{current:.3f}" if np.isfinite(current) else "-",
                f"{temperature:.1f}" if np.isfinite(temperature) else "-",
                rate_up,
                rate_dn,
                limit_v,
                limit_i,
                limit_t,
                trip,
                threshold,
                alarm_str,
                f"{vref:.1f}" if np.isfinite(vref) else "-",
                f"{calib_m:.2f}" if np.isfinite(calib_m) else "-",
                f"{calib_q:.2f}" if np.isfinite(calib_q) else "-",
                f"{disc_mv:.1f}" if np.isfinite(disc_mv) else "-"
            ])

        except Exception as e:
            table.append([ch, f"⚠️ {e}", *["-"] * (len(headers) - 2)])

    print("\n=== Full HV Channel Summary ===")
    print(tabulate(table, headers=headers, tablefmt="pretty"))


def tabulate_keep_spaces(rows, headers):
    def _no_strip(val):
        # Disable trimming inside tabulate internals
        if isinstance(val, _text_type):
            return val
        return str(val)

    return tabulate(
        rows, headers=headers, tablefmt="plain",
        disable_numparse=True,
        stralign="center", numalign="center",
        _get_str_value=_no_strip
    )


def pad_with_underscores(s, width):
    """
    Pad or truncate string to fixed width, replacing internal spaces with underscores.
    - Non-empty strings are kept and padded with underscores.
    - Empty or whitespace-only strings become full underscores.
    """
    import re
    s = str(s).strip()
    s = s.replace("\x00", "")
    # Handle None or whitespace-only
    if s is None or s == "":
        return " " * width
    # Replace internal whitespace with underscores
    s = re.sub(r"\s", " ", str(s))
    # Pad or truncate
    if len(s) < width:
        return s + " " * (width - len(s))

    return s[:width]


def clean_string(sn):
    return str(sn).rstrip('\x00').strip()


def extend_string(sn: str, length: int = 12) -> str:
    if sn is None:
        sn = ""

    # Remove null bytes and surrounding spaces
    sn = clean_string(sn)

    # Pad or truncate to fixed length
    if len(sn) < length:
        sn = sn + " " * (length - len(sn))
    else:
        sn = sn[:length]

    return sn


def parse_bin_edges(spec: str):
    start, step, stop = map(float, spec.split(":"))
    return np.arange(start, stop + step, step)


def fetch_all_channel_info(dev, channels=range(1, 20)):
    """
    Fetch and display static board information and calibration data
    from all HV channels (no monitoring registers).

    Combines:
      • getInfo() — firmware version, PMT/HV/FEB serials, device ID
      • readCalibRegisters() — calibration slope (m), offset (q), and discriminator [mV]

    Parameters
    ----------
    dev : HVModbus
        Active HVModbus device instance.
    channels : iterable[int]
        List of channel addresses (default: 1–19).

    Returns
    -------
    list[dict]
        Per-channel dictionaries with info and calibration data.
    """

    headers = [
        "Ch", "FW ver", "PMT s/n", "HV s/n", "FEB s/n", "Device ID"
    ]
    table = []
    results = []

    # Fixed field widths for neat alignment
    WIDTH_FW = 6
    WIDTH_SN = 12
    WIDTH_FEB = 12
    WIDTH_DEVID = 10

    print("\n📡 Reading static info (getInfo + readCalibRegisters) from all HV channels...")

    for ch in channels:
        try:
            if not dev.open(ch):
                table.append([ch, "❌", *["-" * 10] * (len(headers) - 2)])
                continue

            # --- getInfo() ---
            try:
                fwver, pmtsn, hvsn, febsn, devid = dev.getInfo(ch)
            except Exception:
                fwver, pmtsn, hvsn, febsn, devid = "-", "-", "-", "-", "-"
            fwver = pad_with_underscores(fwver, WIDTH_FW)
            pmtsn = pad_with_underscores(pmtsn, WIDTH_SN)
            hvsn = pad_with_underscores(hvsn, WIDTH_SN)
            febsn = pad_with_underscores(febsn, WIDTH_FEB)
            devid = pad_with_underscores(devid, WIDTH_DEVID)

            # --- Format for table ---
            row = [f"{ch:02d}", f"{fwver:>6s}", f"{pmtsn:>6s}", f"{hvsn:>12s}", f"{febsn:>12s}", f"{devid:>10s}"]
            table.append(row)

            # --- Store in list for programmatic use ---
            results.append({
                "channel": ch,
                "fwver": fwver,
                "pmtsn": pmtsn,
                "hvsn": hvsn.strip(),
                "febsn": febsn.strip(),
                "device_id": devid.strip(),
            })

        except Exception as e:
            table.append([f"{ch:02d}", f"⚠️ {e}", *["-" * 10] * (len(headers) - 2)])

    # --- Print tabulated summary ---
    print("\n=== HV Channel Info + Calibration Summary ===")
    print(tabulate(
        table,
        headers=headers,
        tablefmt="pretty",
        numalign="center",
        stralign="center"
    ))

    return results


def print_all_channels_mon(dev, channels=range(1, 20)):
    """
    Print a simplified HV monitor summary for all given channels.
    Uses readMonRegisters() for fast single-read access.
    Returns a list of dicts with basic measured values.
    """
    table = []
    headers = ["Ch", "Status", "V [V]", "Vset [V]", "I [uA]", "T [°C]", "Alarm"]
    output_data = []

    print("\n📡 Reading status data from HV channels (via readMonRegisters)...")

    for ch in channels:
        try:
            # Try to open the Modbus channel
            if not dev.open(ch):
                table.append([ch, "❌", *["-"] * (len(headers) - 2)])
                continue

            mon = dev.readMonRegisters(slave=ch)
            if mon is None:
                raise Exception("Modbus read error")

            voltage = mon.get("V", float("nan"))
            voltage_set = mon.get("Vset", float("nan"))
            current = mon.get("I", float("nan"))
            temperature = mon.get("T", float("nan"))
            alarm = mon.get("alarm", 0)
            status_code = mon.get("status", None)

            from helper_functions import decode_status
            status = decode_status(status_code) if status_code is not None else "?"

            # Append to dict output
            output_data.append({
                "channel": ch,
                "status": status,
                "voltage": voltage,
                "voltage_set": voltage_set,
                "current": current,
                "temperature": temperature,
                "alarm": alarmString(alarm)
            })

            # Append to printable table
            table.append([
                ch,
                status,
                f"{voltage:.3f}" if np.isfinite(voltage) else "-",
                f"{voltage_set:.1f}" if np.isfinite(voltage_set) else "-",
                f"{current:.3f}" if np.isfinite(current) else "-",
                f"{temperature:.1f}" if np.isfinite(temperature) else "-",
                alarm
            ])

        except Exception as e:
            table.append([ch, f"⚠️ {e}", *["-"] * (len(headers) - 2)])

    print("\n=== Channel Monitor Summary ===")
    print(tabulate(table, headers=headers, tablefmt="pretty"))
    return output_data


def safe_get_status(hv, ch, retries=3, delay=0.1):
    for i in range(retries):
        try:
            return hv.getStatus(ch)
        except Exception as e:
            if i == retries - 1:
                return -1
            time.sleep(delay)


def wait_for_hv_ramp_up(hv, channels=range(1, 20), check_interval=2, max_wait_s=90):
    """
    Wait until all PMT channels have finished ramping up (status == 0 -> UP).
    """
    print("⏳ Waiting for PMT channels to finish ramp-up...")
    elapsed = 0
    status_display = {ch: f"{ch:02d}" for ch in channels}

    while elapsed < max_wait_s:
        ramping_channels = []
        for ch in channels:
            try:
                status_code = safe_get_status(hv, ch, retries=1)
                status_text = decode_status(status_code)
                if status_code == 2:
                    ramping_channels.append(ch)
                    status_display[ch] = status_text
                else:
                    status_display[ch] = f"{ch:>4}"
                    if status_code == -1:
                        status_display[ch] = f"{ch:>2}ER"
                    elif status_code == 1:
                        status_display[ch] = f"{ch:>2}UP"
            except Exception as e:
                status_display[ch] = "??"
                print(f"⚠️  Channel {ch}: error reading status ({e})")

        status_line = " ".join(f"{status_display[ch]:>4}" for ch in channels)
        print(f"   +{elapsed:3d}s: {status_line}")
        time.sleep(check_interval)
        elapsed += check_interval

        if not ramping_channels:
            print(f"\n✅ All channels finished ramp-up after {elapsed}s.")
            return True
        print("\033[1F", end="")
    time.sleep(1)
    print(f"\n⚠️ Timeout reached ({max_wait_s}s). Channels still ramping: {ramping_channels}")
    return False


def wait_for_hv_ramp_down(hv, channels=range(1, 20), check_interval=2, max_wait_s=60, voltage_threshold=100.0):
    """
    Wait until all PMT channels have ramped down (measured voltage below threshold).
    """
    print(f"⏳ Waiting for PMT channels to ramp down below {voltage_threshold:.1f} V...")
    elapsed = 0
    status_display = {ch: f"{ch:02d}" for ch in channels}

    while elapsed < max_wait_s:
        still_high = []
        for ch in channels:
            try:
                v_meas = np.nan
                status_code = safe_get_status(hv, ch, retries=1)
                if status_code != -1:
                    v_meas = hv.getVoltage(ch)
                status_text = decode_status(status_code)
                # if voltage > given thr and status is not down
                if v_meas > voltage_threshold and status_code != 1 and status_code != -1:
                    still_high.append(ch)
                    status_display[ch] = status_text
                else:
                    status_display[ch] = "__"
                    if status_code == -1:
                        status_display[ch] = status_text
            except Exception as e:
                status_display[ch] = "??"
                print(f"⚠️  Channel {ch}: error reading HV ({e})")

        status_line = " ".join(f"{status_display[ch]:>4}" for ch in channels)
        print(f"   +{elapsed:3d}s: {status_line}")
        time.sleep(check_interval)
        elapsed += check_interval

        if not still_high:
            print(f"\n✅ All channels are below {voltage_threshold:.1f} V after {elapsed}s.")
            return True
        print("\033[1F", end="")

    print(f"\n⚠️ Timeout reached ({max_wait_s}s). Channels still above {voltage_threshold:.1f} V: {still_high}")
    return False


# ===============================================================
#  THRESHOLD SCAN & ANALYSIS
# ===============================================================

def threshold_scan(
        rc,
        hv,
        filename: str,
        channels=range(1, 20),
        thresholds=range(50, 0, -1),
        n_measurements: int = 3,
        delay_between: float = 0.5,
        settle_time_s: float = 2.0,
        tqdm_title: str = "Threshold scan"
):
    """
    Perform a threshold scan:
      - loop over thresholds (mV)
      - take multiple synchronized rate readings per threshold
      - save averaged results to CSV
      - restore original thresholds afterwards

    Parameters
    ----------
    rc : RunControlClient
        RunControl interface object.
    hv : HVModbus
        High-voltage interface.
    filename : str
        Output CSV path.
    channels : iterable[int]
        List of channel numbers (1..19).
    thresholds : iterable[int]
        Threshold values in mV.
    n_measurements : int
        Number of rate readings to average per threshold (default 3).
    delay_between : float
        Delay (s) between repeated measurements for same threshold.

    Notes
    -----
    - Rates are read from register (ch + 7), as in existing system.
    """
    os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
    with open(filename, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["threshold_mv", "channel", "rate_hz"])

        # Save initial thresholds
        saved_thr_data = []
        for ch in channels:
            try:
                saved_thr_data.append(hv.getThreshold(ch))
            except Exception:
                saved_thr_data.append(None)
        time.sleep(0.5)

        # Main scan loop
        for th in tqdm(thresholds, desc=tqdm_title):
            # Apply threshold to all channels
            for ch in channels:
                try:
                    hv.setThreshold(th, ch)
                except Exception as e:
                    print(f"[WARNING] Failed to set threshold={th} for channel={ch}: {e}")
            time.sleep(settle_time_s)  # settle

            # Accumulate multiple readings in sync
            rate_accum = {ch: [] for ch in channels}
            for _ in range(max(1, n_measurements)):
                for ch in channels:
                    reg = ch + 7
                    try:
                        rate = rc.read(reg)
                    except Exception:
                        rate = float("nan")
                    rate_accum[ch].append(rate)
                if n_measurements > 1:
                    time.sleep(max(0.0, delay_between))

            # Write mean per channel
            for ch in channels:
                vals = np.array(rate_accum[ch], dtype=float)
                mean_rate = float(np.nanmean(vals))
                writer.writerow([th, ch, mean_rate])

        # Restore thresholds (best effort)
        for idx, ch in enumerate(channels):
            orig = saved_thr_data[idx]
            try:
                if orig is not None:
                    hv.setThreshold(orig, ch)
            except Exception:
                pass

    print(f"✅ Threshold scan finished. Data saved to {filename}")


# ===============================================================
#  PEDESTAL (.scf) UTILITIES
# ===============================================================

def load_pedestal_file(pedestal_path: str, default_ped=0) -> dict[int, dict[str, float]]:
    """
    Load pedestal calibration (.scf) file.
    Returns {channel: {'mean': μ, 'sigma': σ}}
    """
    pedestal: dict[int, dict[str, float]] = {}

    if not pedestal_path or not os.path.exists(pedestal_path):
        print(f"⚠️ No pedestal file provided or file not found. Assuming pedestals = {default_ped}.")
        return {ch: {"mean": default_ped, "sigma": 0.0} for ch in range(1, 20)}

    with open(pedestal_path, "r") as f:
        valid_lines = []
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith('"'):
                continue
            valid_lines.append(stripped)

        reader = csv.DictReader(valid_lines)
        for row in reader:
            try:
                ch = int(row["channel"])

                # Accept both naming conventions
                mean_val = row.get("pedestal_mean", row.get("mu", 0.0))
                sigma_val = row.get("pedestal_sigma", row.get("sigma", 0.0))

                ch = int(row["channel"])
                pedestal[ch] = {
                    "mean": float(mean_val),
                    "sigma": float(sigma_val),
                }
            except Exception as e:
                print(f"⚠️ Skipping malformed line: {row} ({e})")

    if not pedestal:
        print("⚠️ No valid pedestal entries found — assuming zeros.")
        pedestal = {ch: {"mean": 0.0, "sigma": 0.0} for ch in range(1, 20)}
    else:
        print(f"✅ Loaded {len(pedestal)} pedestal entries from {pedestal_path}")

    return pedestal


# ===============================================================
#  FITTING & FWHM
# ===============================================================

def gauss(x, A, mu, sigma):
    """Gaussian function with amplitude, mean, sigma."""
    return A * np.exp(-0.5 * ((x - mu) / sigma) ** 2)


def estimate_fwhm_from_data(x, y):
    """
    Estimate FWHM directly from data by where y drops below half-max
    on left/right of the peak. Returns width in same units as x or NaN.
    """
    if len(x) < 3:
        return np.nan

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if np.all(~np.isfinite(y)) or np.nanmax(y) == 0:
        return np.nan

    # Use finite values only
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if x.size < 3:
        return np.nan

    idx_max = np.nanargmax(y)
    ymax = y[idx_max]
    if ymax <= 0:
        return np.nan
    half = ymax / 2.0

    # Left side crossing
    left_idxs = np.where(y[:idx_max] < half)[0]
    if len(left_idxs) == 0:
        x_left = x[0]
    else:
        i2 = left_idxs[-1]
        i1 = i2 + 1
        if i1 <= idx_max - 1:
            # Linear interpolation between (i2, i1)
            x0, x1 = x[i2], x[i1]
            y0, y1 = y[i2], y[i1]
            if y1 != y0:
                x_left = x0 + (half - y0) * (x1 - x0) / (y1 - y0)
            else:
                x_left = x[i2]
        else:
            x_left = x[i2]

    # Right side crossing
    right_idxs = np.where(y[idx_max:] < half)[0]
    if len(right_idxs) == 0:
        x_right = x[-1]
    else:
        j1 = idx_max + right_idxs[0]
        j0 = j1 - 1
        if j0 >= idx_max:
            x0, x1 = x[j0], x[j1]
            y0, y1 = y[j0], y[j1]
            if y1 != y0:
                x_right = x0 + (half - y0) * (x1 - x0) / (y1 - y0)
            else:
                x_right = x[j1]
        else:
            x_right = x[j1]

    return abs(float(x_right) - float(x_left))


def calc_threshold_scan(csv_file, pickle_file=None):
    df = pd.read_csv(csv_file)
    channels = np.unique(df["channel"].to_numpy())
    results = []

    print("\n📊 Computing FWHM and σ from data:\n")

    for ch in channels:

        data = df[df["channel"] == ch].copy()
        thr_vect = data["threshold_mv"].to_numpy(dtype=float)
        rate_vect = data["rate_hz"].replace(33554431, 0).to_numpy(dtype=float)

        if data.empty or np.all(rate_vect == 0) or not np.any(np.isfinite(rate_vect)):
            results.append({
                "channel": int(ch),
                "mu": np.nan,
                "sigma": np.nan,
                "max_peak_pos": np.nan,
                "fwhm": np.nan,
                "rate_at_max_thr": np.nan
            })
            continue

        # -----------------------------
        # Peak position
        # -----------------------------
        peak_idx = np.nanargmax(rate_vect)
        peak_pos = float(thr_vect[peak_idx])
        peak_height = float(rate_vect[peak_idx])

        # -----------------------------
        # FWHM from raw data
        # -----------------------------
        fwhm = estimate_fwhm_from_data(thr_vect, rate_vect)

        sigma = fwhm / 2.35 if np.isfinite(fwhm) else np.nan

        print(
            f"Ch {int(ch):02d} → μ={peak_pos:7.2f}, "
            f"FWHM={fwhm:6.2f}, σ={sigma:6.2f}"
        )

        # -----------------------------
        # Gaussian fit
        # -----------------------------
        try:
            # initial guesses from your raw estimates
            A0 = max(peak_height - np.nanmin(rate_vect), 1.0)
            mu0 = peak_pos
            offset0 = max(np.nanmin(rate_vect), 0.0)

            p0 = [A0, mu0, sigma]

            # constraints:
            #   A > 0
            #   sigma > 0
            # mu stays inside scanned threshold range
            lower_bounds = [0.0, np.nanmin(thr_vect), 1e-6]
            upper_bounds = [np.inf, np.nanmax(thr_vect), np.inf]

            popt, pcov = curve_fit(
                gauss,
                thr_vect,
                rate_vect,
                p0=p0,
                bounds=(lower_bounds, upper_bounds),
                maxfev=20000
            )

            peak_height, peak_pos, sigma = popt
            fwhm = sigma * 2.35

        except Exception as e:
            pass
        results.append({
            "channel": int(ch),
            "peak_height": peak_height,
            "mu": peak_pos,
            "sigma": sigma,
            "fwhm": fwhm,
            "rate_at_max_thr": rate_vect[-1]
        })

    # -----------------------------
    # Optional pickle save
    # -----------------------------
    if pickle_file:
        with open(pickle_file, "wb") as f:
            pickle.dump(results, f)

        print(f"\n✅ Results saved to pickle file: {pickle_file}")

    return results


def smooth(y, window: int = 5):
    """Simple moving average smoother."""
    y = np.asarray(y, dtype=float)
    if window <= 1 or y.size < window:
        return y
    return np.convolve(y, np.ones(window, dtype=float) / float(window), mode="same")


# ===============================================================
#  THRESHOLD PLOTTING
# ===============================================================
def plot_threshold_scan(
        csv_file,
        save=None,
        log_y=False,
        fixed_axes=False,
        fit_gauss=False,
        separate=False,
):
    """
    Plot threshold scan results from CSV and overlay Gaussian fits.

    Uses results from `calc_threshold_scan(csv_file)` to annotate
    each channel with μ, σ, FWHM (or indicate fit failure).

    Produces either:
      • one combined 4x5 subplot figure (default)
      • or individual PNG files per channel if `separate=True`.

    Returns
    -------
    dict[int, str]
        Dictionary {channel: PNG file path}
    """
    import os
    import numpy as np
    import pandas as pd
    import matplotlib.pyplot as plt

    df = pd.read_csv(csv_file)
    if fit_gauss:
        fit_res = calc_threshold_scan(csv_file)
        fit_dict = {r["channel"]: r for r in fit_res if "channel" in r}
    else:
        fit_dict = {}

    saved_files: dict[int, str] = {}

    # -------------------------------
    # SEPARATE MODE (individual PNGs)
    # -------------------------------
    if separate:
        base_name = os.path.splitext(os.path.basename(csv_file))[0]
        base_dir = os.path.dirname(save) if save else os.path.dirname(csv_file)
        os.makedirs(base_dir, exist_ok=True)

        for ch in range(1, 20):
            data = df[df["channel"] == ch].copy()
            if data.empty:
                continue

            # Clean and sort
            x = np.array(data["threshold_mv"])
            y = np.array(data["rate_hz"].replace(33554431, 0)) / 1000.0
            sort_idx = np.argsort(x)
            x, y = x[sort_idx], y[sort_idx]
            dx = np.median(np.diff(x)) if len(x) > 1 else 1.0
            bar_width = abs(dx) * 0.9

            fit = fit_dict.get(ch, {})
            fit_ok = bool(fit) and np.isfinite(fit.get("mu", np.nan))
            mu = fit.get("mu", np.nan)
            sigma = fit.get("sigma", np.nan)
            fwhm = fit.get("fwhm", np.nan)
            peak_height = fit.get("peak_height", np.nan)

            # Single figure
            fig, ax = plt.subplots(figsize=(6, 4.5))

            # --- Threshold Scan ---
            ax.bar(
                x, y,
                width=bar_width,
                color="salmon",
                alpha=0.5,
                edgecolor="black",
                linewidth=0.4,
                label="Data"
            )

            if fit_ok:
                x_fit = np.linspace(min(x), max(x), 300)
                ax.plot(
                    x_fit,
                    gauss(x_fit, peak_height / 1000.0, mu, abs(sigma)),
                    "r-",
                    lw=1.5,
                    label="Gaussian fit"
                )

            ax.grid(True, linestyle="--", alpha=0.5)
            ax.set_xlabel("Threshold [mV]")
            ax.set_ylabel("Rate [kHz]")

            if log_y:
                ax.set_yscale("log")

            ax.set_title(
                f"Ch {ch:02d} — μ={mu:.1f}, σ={sigma:.1f}, FWHM={fwhm:.1f}"
            )

            ax.legend()

            plt.tight_layout()
            if save:
                out_path = os.path.join(base_dir, f"{base_name}_ch{ch:02d}.png")
                fig.savefig(out_path, dpi=150)
                plt.close(fig)
                print(f"✅ Saved → {out_path}")
                saved_files[ch] = out_path
            plt.close("all")
        if save:
            print(f"\n📁 All channel plots saved to {base_dir}")
        return saved_files

    # -------------------------------
    # GRID MODE (combined subplots)
    # -------------------------------
    fig1, axes1 = plt.subplots(4, 5, figsize=(16, 10))
    axes1 = axes1.flatten()

    # Process and plot all channels
    for ch in range(1, 20):
        data = df[df["channel"] == ch].copy()
        ax1 = axes1[ch - 1]

        if data.empty:
            ax1.set_title(f"Ch {ch} (no data)")
            continue

        x = np.array(data["threshold_mv"])
        y = np.array(data["rate_hz"].replace(33554431, 0)) / 1000.0
        sort_idx = np.argsort(x)
        x, y = x[sort_idx], y[sort_idx]
        dx = np.median(np.diff(x)) if len(x) > 1 else 1.0
        bar_width = abs(dx) * 0.9

        fit = fit_dict.get(ch, {})
        fit_ok = bool(fit) and np.isfinite(fit.get("mu", np.nan))
        mu = fit.get("mu", np.nan)
        sigma = fit.get("sigma", np.nan)
        fwhm = fit.get("fwhm", np.nan)
        peak_height = fit.get("peak_height", np.nan)

        # Plot 1: Rate vs Threshold
        ax1.bar(x, y, width=bar_width, color="salmon", alpha=0.5, edgecolor="black", linewidth=0.4)
        if fit_ok:
            x_fit = np.linspace(min(x), max(x), 300)
            ax1.plot(x_fit, gauss(x_fit, peak_height / 1000.0, mu, abs(sigma)), "r-", lw=1.5)
        ax1.set_title(f"Ch {ch:02d} — μ={mu:.1f}, σ={sigma:.1f}, FWHM={fwhm:.1f}", fontsize=9)
        ax1.set_xlabel("Threshold [mV]")
        ax1.set_ylabel("Rate [kHz]")
        ax1.grid(True, linestyle="--", alpha=0.5)
        if log_y:
            ax1.set_yscale("log")

    fig1.tight_layout(rect=[0, 0.05, 1, 0.95])
    # Save both figures
    if save:
        base, ext = os.path.splitext(save)
        file1 = save
        fig1.savefig(file1, dpi=150)
        plt.close(fig1)
        print(f"✅ Saved: {file1}")
        # assign both to channel=0 (shared overview)
        saved_files[0] = file1
        plt.close("all")
    else:
        plt.show()

    return saved_files


import uproot
import numpy as np
from tabulate import tabulate


def summarize_root_file(
        root_file: str,
        channels=range(1, 20),
):
    """
    Print per-channel summary:
      - number of hits
      - acquisition time span
      - mean hit rate [Hz]
      - mean ADC amplitude
    """
    tree_name = "pmt_events"
    ch_branch = "channel"
    adc_branch = "adc"
    pmt_time_branch = "pmt_time"
    coarse_branch = "time_coarse"
    fine_branch = "time_fine"
    tdc_branch = "tdc_start"
    # -----------------------------------------------------------------
    # Load ROOT and reconstruct time
    # -----------------------------------------------------------------
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise RuntimeError(f"TTree '{tree_name}' not found in {root_file}")

        tree = f[tree_name]
        ch = tree[ch_branch].array(library="np") + 1
        adc = tree[adc_branch].array(library="np")

        try:
            # Preferred: already-built PMT time
            pmt_time = tree[pmt_time_branch].array(library="np")
            time_s = pmt_time.astype(float) * 4e-9
        except Exception:
            # Fallback: reconstruct full timestamp
            coarse = tree[coarse_branch].array(library="np").astype(np.uint64)
            fine = tree[fine_branch].array(library="np").astype(np.uint64)
            tdc = tree[tdc_branch].array(library="np").astype(np.uint64)

            T = (coarse << 28) | (fine << 4) | tdc
            time_s = T.astype(float) * 0.25e-9

    # -----------------------------------------------------------------
    # Per-channel summary
    # -----------------------------------------------------------------
    table = []
    headers = ["Ch", "Hits", "Duration [s]", "Mean rate [Hz]", "Mean ADC"]

    for channel in channels:
        mask = ch == channel
        n_hits = int(np.sum(mask))

        if n_hits < 2:
            table.append([f"{channel:02d}", n_hits, "-", "-", "-"])
            continue

        t_ch = time_s[mask]
        adc_ch = adc[mask]

        duration = float(np.max(t_ch) - np.min(t_ch))
        rate = n_hits / duration if duration > 0 else np.nan

        table.append([
            f"{channel:02d}",
            n_hits,
            f"{duration:.2f}",
            f"{rate:.2f}",
            f"{np.mean(adc_ch):.1f}",
        ])

    print("\n📊 ROOT file summary")
    print(tabulate(table, headers=headers, tablefmt="fancy_grid"))


# ===============================================================
#  ROOT-BASED PLOTTING (ADC / TOT / SCATTER)
# ===============================================================
def plot_adc_histograms(
        root_file: str,
        tree_name: str = "pmt_events",
        channels=range(1, 20),
        bin_width: int = 1,
        bin_edges=None,
        fixed_range: bool = True,
        adc_branch: str = "adc",
        ch_branch: str = "channel",
        save: str | None = None,
        log_y: bool = False,
        fit: str = "1pe",
        separate: bool = False,
):
    """
    Plot ADC histograms per channel using optional Gaussian-based fits.

    Returns
    -------
    list[str]
        List of saved PNG file paths.
    """
    import uproot
    import matplotlib.pyplot as plt
    import numpy as np
    import os

    saved_files: dict[int, str] = {}

    # --- Load ROOT data ---
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")
        tree = f[tree_name]
        ch = tree[ch_branch].array(library="np") + 1
        adc = tree[adc_branch].array(library="np")

    # --- Perform Gaussian fits ---
    print("🔍 Performing ADC Gaussian fits before plotting...")
    fit = fit.lower().strip()
    fits = {}

    if fit == "1pe":
        fits = fit_1pe_distribution(root_file, bin_edges=bin_edges, bin_width=bin_width, verbose=False)
    elif fit == "gauss":
        fits = fit_adc_gauss(root_file, bin_edges=bin_edges, bin_width=bin_width, verbose=False)
    elif fit in ("none", "off", "skip", "", None):
        print("⚠️ Skipping fit overlay (fit='none').")
    else:
        print(f"⚠️ Unknown fit type '{fit}'. Supported options: '1pe', 'gauss', 'none'.")

    # ============================
    # SEPARATE MODE — per-channel
    # ============================
    if separate:
        base_dir = os.path.dirname(save) if save else os.path.dirname(root_file)
        base_name = os.path.splitext(os.path.basename(root_file))[0]
        os.makedirs(base_dir, exist_ok=True)

        for ch_id in channels:
            adc_ch = adc[ch == ch_id]
            if adc_ch.size == 0:
                continue

            mean_val = np.mean(adc_ch)
            std_val = np.std(adc_ch)
            max_val = np.max(adc_ch)
            min_val = np.min(adc_ch)
            # Adaptive binning
            if bin_edges is None:
                ch_min = np.min([min_val, int(mean_val - 3 * std_val)])
                ch_max = np.max([max_val, int(mean_val + 3 * std_val)])
                edges = np.arange(ch_min - 4 * bin_width, ch_max + 4 * bin_width, bin_width, dtype=int)
            else:
                edges = bin_edges
                bin_width = bin_edges[1] - bin_edges[0]

            counts, edges = np.histogram(adc_ch, bins=edges)
            centers = 0.5 * (edges[:-1] + edges[1:])

            fig, ax = plt.subplots(figsize=(6, 4))
            ax.bar(
                centers,
                counts,
                width=bin_width,
                color="salmon",
                alpha=0.5,
                edgecolor="black",
                linewidth=0.4,
                label="Data",
            )

            fit_res = fits.get(ch_id)

            mu = fit_res.get("fit_mu") if fit_res else None
            sigma = fit_res.get("fit_sigma") if fit_res else None
            amp = fit_res.get("fit_amplitude") if fit_res else None
            r2 = fit_res.get("fit_r2") if fit_res else None

            if (
                    isinstance(mu, (int, float)) and np.isfinite(mu) and
                    isinstance(sigma, (int, float)) and np.isfinite(sigma)
            ):
                xx = np.linspace(edges[0], edges[-1], 400)
                yy = amp * np.exp(-0.5 * ((xx - mu) / sigma) ** 2)
                ax.plot(xx, yy, "r--", lw=2, label="Gauss fit")

                title_extra = (
                    f"μ_fit={mu:.1f}, σ_fit={sigma:.1f}, R²={r2:.3f}\n"
                    f"μ_data={mean_val:.1f}, σ_data={std_val:.1f}, "
                    f"min={min_val:.1f}, max={max_val:.1f}\n"
                )
            else:
                title_extra = (
                    f"μ_data={mean_val:.1f}, σ_data={std_val:.1f} (no fit)"
                )

            ax.set_title(f"Ch {ch_id:02d} — {title_extra}", fontsize=9)
            ax.set_xlabel("ADC")
            ax.set_ylabel("Counts")
            ax.grid(True, linestyle="--", alpha=0.4)
            if log_y:
                ax.set_yscale("log")
            ax.legend(fontsize=8, loc="upper right")

            out_path = os.path.join(base_dir, f"{base_name}_ch{ch_id:02d}.png")
            fig.tight_layout()
            if save:
                fig.savefig(out_path, dpi=180)
                plt.close(fig)
                saved_files[ch_id] = out_path
                print(f"💾 Saved: {out_path}")
        if save:
            print(f"\n✅ Saved {len(saved_files)} per-channel histograms in: {base_dir}")
        return saved_files  # <--- RETURN LIST HERE

    # ============================
    # COMBINED MODE — 4×5 grid
    # ============================
    fig, axes = plt.subplots(4, 5, figsize=(16, 10), sharex=fixed_range, sharey=fixed_range)
    axes = axes.flatten()

    # --- Determine histogram range (global if needed) ---
    if np.size(adc) > 0:
        hist_min = int(np.min(adc) - 5 * bin_width)
        hist_max = int(np.max(adc) + 5 * bin_width)
    else:
        hist_min, hist_max = 0, 4095
    if bin_width is None:
        bin_width = 1

    bin_edges = np.arange(hist_min, hist_max, bin_width)
    if bin_edges is not None:
        bin_width = bin_edges[1] - bin_edges[0]

    for idx, ch_id in enumerate(channels):
        ax = axes[idx]
        adc_ch = adc[ch == ch_id]
        if adc_ch.size == 0:
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.4)
            continue

        mean_val = np.mean(adc_ch)
        std_val = np.std(adc_ch)

        counts, edges = np.histogram(adc_ch, bins=bin_edges)
        centers = 0.5 * (edges[:-1] + edges[1:])

        ax.bar(
            centers,
            counts,
            width=bin_width,
            color="salmon",
            alpha=0.5,
            edgecolor="black",
            linewidth=0.4,
            label="Data",
        )

        fit_res = fits.get(ch_id)
        if fit_res and np.isfinite(fit_res["mu"]) and np.isfinite(fit_res["sigma"]):
            xx = np.linspace(edges[0], edges[-1], 400)
            yy = fit_res["amplitude"] * np.exp(-0.5 * ((xx - fit_res["mu"]) / fit_res["sigma"]) ** 2)
            ax.plot(xx, yy, "r--", lw=2, label="Gauss fit")
            title_extra = (
                f"μ_fit={fit_res['mu']:.1f}, σ_fit={fit_res['sigma']:.1f},\n "
                f"μ_data={mean_val:.1f}, σ_data={std_val:.1f}, R²={fit_res['r2']:.3f}"
            )
        else:
            title_extra = f"μ_data={mean_val:.1f}, σ_data={std_val:.1f} (no fit)"

        ax.set_title(f"Ch {ch_id:02d} — {title_extra}", fontsize=8)
        ax.set_xlabel("ADC")
        ax.set_ylabel("Counts")
        ax.grid(True, linestyle="--", alpha=0.4)
        if log_y:
            ax.set_yscale("log")
        ax.legend(fontsize=6, loc="upper right")

    if len(axes) > 19:
        fig.delaxes(axes[19])

    fig.suptitle(
        f"ADC histograms with Gaussian fits — {os.path.basename(root_file)}"
        + (" (log Y)" if log_y else ""),
        y=0.98,
    )
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        fig.savefig(save, dpi=200, bbox_inches="tight")
        saved_files[0] = (save)
        print(f"💾 Saved combined figure → {save}")
    else:
        plt.show()

    return saved_files  # <--- return saved file paths


def plot_tot_histograms(
        root_file: str,
        channels=range(1, 20),
        bin_width: float = 2.0,
        bin_edges=None,
        log_y=None,
        fixed_range: bool = True,
        save: str | None = None,
        separate: bool = False,
):
    """
    Plot Time-over-Threshold (TOT) histograms for PMT channels from a ROOT file.

    - Default: 4×5 grid of all channels.
    - If separate=True: save one PNG per channel (names like *_tot_ch01.png).
    - Each title shows channel number, mean, and std of TOT distribution.

    Returns
    -------
    list[str]
        List of saved PNG file paths.
    """
    import os
    import numpy as np
    import matplotlib.pyplot as plt
    import uproot

    saved_files: dict[int, str] = {}

    tree_name = "pmt_events"
    tdc_coarse_branch = "tdc_coarse"
    tdc_start_branch = "tdc_start"
    tdc_stop_branch = "tdc_stop"
    ch_branch = "channel"

    # --- Load data ---
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")
        tree = f[tree_name]
        arr = tree.arrays(
            [ch_branch, tdc_coarse_branch, tdc_start_branch, tdc_stop_branch],
            library="np",
        )

    ch = arr[ch_branch] + 1
    tdc_coarse = arr[tdc_coarse_branch]
    tdc_start = arr[tdc_start_branch]
    tdc_stop = arr[tdc_stop_branch]

    tot_ns = (tdc_coarse - tdc_start / 15.0 + tdc_stop / 15.0) * 4.0
    valid_mask = np.isfinite(tot_ns) & (tot_ns > 0)
    ch = ch[valid_mask]
    tot_ns = tot_ns[valid_mask]

    # --- Global bin edges if not given ---
    if bin_edges is None:
        tot_min = np.min(tot_ns) if tot_ns.size else 0.0
        tot_max = np.max(tot_ns) if tot_ns.size else 100.0
        global_edges = np.arange(tot_min - bin_width, tot_max + bin_width, bin_width)
    else:
        global_edges = bin_edges

    # =========================
    # Separate mode: per-channel
    # =========================
    if separate:
        base_dir = os.path.dirname(save) if save else os.path.dirname(root_file)
        base_name = os.path.splitext(os.path.basename(root_file))[0]
        os.makedirs(base_dir or ".", exist_ok=True)

        for ch_id in channels:
            tot_ch = tot_ns[ch == ch_id]
            if tot_ch.size == 0:
                continue

            mean_val = np.mean(tot_ch)
            std_val = np.std(tot_ch)

            # Per-channel bin edges if not fixed_range
            if bin_edges is None and not fixed_range:
                e_min = np.min(tot_ch) - bin_width
                e_max = np.max(tot_ch) + bin_width
                edges = np.arange(e_min, e_max + bin_width, bin_width)
            else:
                edges = global_edges

            fig, ax = plt.subplots(figsize=(6, 4))
            ax.hist(
                tot_ch,
                bins=edges,
                histtype="stepfilled",
                alpha=0.6,
                color="#ff7f0e",
                label="Data",
            )

            ax.set_title(
                f"Ch {ch_id:02d} — μ={mean_val:.1f} ns, σ={std_val:.1f} ns",
                fontsize=10,
            )
            ax.set_xlabel("TOT [ns]")
            ax.set_ylabel("Counts")
            ax.grid(True, linestyle="--", alpha=0.4)
            if log_y:
                ax.set_yscale("log")
            ax.legend(fontsize=8, loc="upper right")
            fig.tight_layout()

            if save:
                out_path = os.path.join(base_dir, f"{base_name}_tot_ch{ch_id:02d}.png")
                fig.savefig(out_path, dpi=200, bbox_inches="tight")
                plt.close(fig)
                print(f"💾 Saved: {out_path}")
                saved_files[ch_id] = out_path

        if saved_files:
            print(f"✅ Saved {len(saved_files)} per-channel TOT plots to: {base_dir or '.'}")
        else:
            print("⚠️ No channels had valid TOT data; nothing saved.")
        return saved_files

    # =========================
    # Combined grid (default)
    # =========================
    fig, axes = plt.subplots(4, 5, figsize=(16, 10), sharex=fixed_range, sharey=fixed_range)
    axes = axes.flatten()

    for idx, ch_id in enumerate(channels):
        ax = axes[idx]
        tot_ch = tot_ns[ch == ch_id]

        if tot_ch.size == 0:
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.4)
            continue

        mean_val = np.mean(tot_ch)
        std_val = np.std(tot_ch)

        if bin_edges is None and not fixed_range:
            e_min = np.min(tot_ch) - bin_width
            e_max = np.max(tot_ch) + bin_width
            edges = np.arange(e_min, e_max + bin_width, bin_width)
        else:
            edges = global_edges

        ax.hist(
            tot_ch,
            bins=edges,
            histtype="stepfilled",
            alpha=0.6,
            color="#ff7f0e",
            label="Data",
        )
        ax.set_title(
            f"Ch {ch_id:02d} — μ={mean_val:.1f} ns, σ={std_val:.1f} ns",
            fontsize=9,
        )
        ax.set_xlabel("TOT [ns]")
        ax.set_ylabel("Counts")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(fontsize=7, loc="upper right")
        if log_y:
            ax.set_yscale("log")

    if len(axes) > 19:
        fig.delaxes(axes[19])

    fig.suptitle(f"TOT histograms per channel — {os.path.basename(root_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        fig.savefig(save, dpi=200, bbox_inches="tight")
        print(f"💾 Saved combined figure to: {save}")
        saved_files[0] = save
    else:
        plt.show()

    return saved_files


def plot_time_vs_index(
        root_file: str,
        channels=range(1, 20),
        time_unit: str = "s",  # "s", "ms", "us"
        save: str | None = None,
        separate: bool = False,
        time_relative: bool = True
):
    """
    Plot timestamp vs sample index (ordering check).
    Useful for detecting DAQ issues (non-monotonic time, buffer problems).
    """

    import uproot
    import matplotlib.pyplot as plt
    import numpy as np
    import os

    # ------------------------------
    # Output path
    # ------------------------------
    base_dir = os.path.dirname(root_file)
    base_name = os.path.splitext(os.path.basename(root_file))[0]

    if save is None:
        save = os.path.join(base_dir, f"{base_name}_time_index.png")

    tree_name = "pmt_events"
    ch_branch = "channel"
    coarse_branch = "time_coarse"
    fine_branch = "time_fine"
    tdc_branch = "tdc_start"
    pmt_time_branch = "pmt_time"

    # -------------------------------------------------------
    # Load ROOT data
    # -------------------------------------------------------
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")

        tree = f[tree_name]

        ch = tree[ch_branch].array(library="np") + 1

        try:
            pmt_time = tree[pmt_time_branch].array(library="np")
            has_pmt_time = True
        except Exception:
            has_pmt_time = False
            time_coarse = tree[coarse_branch].array(library="np")
            time_fine = tree[fine_branch].array(library="np")
            tdc_start = tree[tdc_branch].array(library="np")

    # -------------------------------------------------------
    # Build timestamp
    # -------------------------------------------------------
    if has_pmt_time:
        time_s = pmt_time.astype(np.float64) * 4e-9
    else:
        T_raw = ((time_coarse.astype(np.uint64) << 28) |
                 (time_fine.astype(np.uint64) << 4) |
                 tdc_start.astype(np.uint64))

        time_s = T_raw.astype(np.float64) * 0.25e-9

    if len(time_s) == 0:
        print("Warning: time_s is empty — skipping normalization")
        return

    if time_relative:
        time_s = time_s - np.nanmin(time_s)

    # -------------------------------------------------------
    # Time scaling
    # -------------------------------------------------------
    scale = {"s": 1.0, "ms": 1e3, "us": 1e6}[time_unit]
    time_scaled = time_s * scale
    unit_label = {"s": "s", "ms": "ms", "us": "µs"}[time_unit]

    saved_files = {}

    # -------------------------------------------------------
    # SEPARATE MODE
    # -------------------------------------------------------
    if separate:
        os.makedirs(base_dir, exist_ok=True)

        for ch_id in channels:
            mask = ch == ch_id
            if not np.any(mask):
                continue

            t_ch = time_scaled[mask]
            idx = np.arange(len(t_ch))

            fig, ax = plt.subplots(figsize=(6, 4))
            ax.plot(idx, t_ch, linewidth=1)

            ax.set_title(f"Time vs Sample Index — Ch {ch_id:02d}")
            ax.set_xlabel("Sample index")
            ax.set_ylabel(f"Time [{unit_label}]")
            ax.grid(True, linestyle="--", alpha=0.4)

            out_path = os.path.join(base_dir, f"{base_name}_time_index_ch{ch_id:02d}.png")
            fig.tight_layout()
            fig.savefig(out_path, dpi=180)
            plt.close(fig)

            saved_files[ch_id] = out_path
            print(f"💾 Saved: {out_path}")

        return saved_files

    # -------------------------------------------------------
    # COMBINED GRID
    # -------------------------------------------------------
    fig, axes = plt.subplots(4, 5, figsize=(16, 10))
    axes = axes.flatten()

    for idx_plot, ch_id in enumerate(channels):
        ax = axes[idx_plot]

        mask = ch == ch_id
        if not np.any(mask):
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.4)
            continue

        t_ch = time_scaled[mask]
        idx = np.arange(len(t_ch))

        ax.plot(idx, t_ch, linewidth=1)
        ax.set_title(f"Ch {ch_id:02d}")
        ax.set_xlabel("Sample index")
        ax.set_ylabel(f"Time [{unit_label}]")
        ax.grid(True, linestyle="--", alpha=0.4)

        # --- optional diagnostic ---
        if np.any(np.diff(t_ch) < 0):
            print(f"⚠️ Non-monotonic time in channel {ch_id}")

    if len(axes) > 19:
        fig.delaxes(axes[19])

    fig.suptitle(f"Time vs Sample Index — {os.path.basename(root_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        fig.savefig(save, dpi=200)
        saved_files[0] = save
        print(f"💾 Saved combined time-index figure → {save}")
    else:
        plt.show()

    return saved_files


def plot_hits_in_time(
        root_file: str,
        channels=range(1, 20),
        time_bin_s: float = 0.1,  # bin width in seconds
        time_unit: str = "s",  # "s", "ms", "us"
        save: str | None = None,
        separate: bool = False,
        time_relative: bool = True
):
    """
    Plot number of hits vs time in fixed bins (default 0.1 s).
    """
    import uproot
    import matplotlib.pyplot as plt
    import numpy as np
    import os
    if save is None:
        base_name = os.path.splitext(root_file)[0]
        save = os.path.join(root_file, f"{base_name}_hits_in_time.png")

    tree_name = "pmt_events"
    ch_branch = "channel"
    coarse_branch = "time_coarse"
    fine_branch = "time_fine"
    tdc_branch = "tdc_start"
    pmt_time_branch = "pmt_time"

    # -------------------------------------------------------
    # Load ROOT data
    # -------------------------------------------------------
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")

        tree = f[tree_name]

        ch = tree[ch_branch].array(library="np") + 1

        try:
            pmt_time = tree[pmt_time_branch].array(library="np")
            has_pmt_time = True
        except Exception:
            has_pmt_time = False
            time_coarse = tree[coarse_branch].array(library="np")
            time_fine = tree[fine_branch].array(library="np")
            tdc_start = tree[tdc_branch].array(library="np")

    # -------------------------------------------------------
    # Build timestamp (same as your function)
    # -------------------------------------------------------
    if has_pmt_time:
        time_s = pmt_time.astype(np.float64) * 4e-9
    else:
        T_raw = ((time_coarse.astype(np.uint64) << 28) |
                 (time_fine.astype(np.uint64) << 4) |
                 tdc_start.astype(np.uint64))

        time_s = T_raw.astype(np.float64) * 0.25e-9

    if len(time_s) == 0:
        print("Warning: time_s is empty — skipping normalization")
        return

    if time_relative:
        time_s = time_s - np.nanmin(time_s)

    # -------------------------------------------------------
    # Time scaling
    # -------------------------------------------------------
    scale = {"s": 1.0, "ms": 1e3, "us": 1e6}[time_unit]
    time_scaled = time_s * scale
    unit_label = {"s": "s", "ms": "ms", "us": "µs"}[time_unit]

    # bin width scaled
    bin_width = time_bin_s * scale

    saved_files = {}

    # -------------------------------------------------------
    # SEPARATE PER CHANNEL
    # -------------------------------------------------------
    if separate:
        base_dir = os.path.dirname(save) if save else os.path.dirname(root_file)
        base_name = os.path.splitext(os.path.basename(root_file))[0]
        os.makedirs(base_dir, exist_ok=True)

        for ch_id in channels:
            mask = ch == ch_id
            if not np.any(mask):
                continue

            t_ch = time_scaled[mask]

            # bins
            t_min = np.min(t_ch)
            t_max = np.max(t_ch)
            bins = np.arange(t_min, t_max + bin_width, bin_width)

            counts, edges = np.histogram(t_ch, bins=bins)
            centers = 0.5 * (edges[:-1] + edges[1:])

            fig, ax = plt.subplots(figsize=(6, 4))
            ax.step(centers, counts, where="mid")

            ax.set_title(f"Hits vs Time — Ch {ch_id:02d}")
            ax.set_xlabel(f"Time [{unit_label}]")
            ax.set_ylabel(f"Hits / {time_bin_s:.2f} s")
            ax.grid(True, linestyle="--", alpha=0.4)

            out_path = os.path.join(base_dir, f"{base_name}_hits_time_ch{ch_id:02d}.png")
            fig.tight_layout()
            fig.savefig(out_path, dpi=180)
            plt.close(fig)

            saved_files[ch_id] = out_path
            print(f"💾 Saved: {out_path}")

        return saved_files

    # -------------------------------------------------------
    # COMBINED GRID (same style as yours)
    # -------------------------------------------------------
    fig, axes = plt.subplots(4, 5, figsize=(16, 10))
    axes = axes.flatten()

    for idx, ch_id in enumerate(channels):
        ax = axes[idx]

        mask = ch == ch_id
        if not np.any(mask):
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.4)
            continue

        t_ch = time_scaled[mask]

        t_min = np.min(t_ch)
        t_max = np.max(t_ch)
        bins = np.arange(t_min, t_max + bin_width, bin_width)

        counts, edges = np.histogram(t_ch, bins=bins)
        centers = 0.5 * (edges[:-1] + edges[1:])

        ax.step(centers, counts, where="mid")
        ax.set_title(f"Ch {ch_id:02d}")
        ax.set_xlabel(f"Time [{unit_label}]")
        ax.set_ylabel("Hits")
        ax.grid(True, linestyle="--", alpha=0.4)

    if len(axes) > 19:
        fig.delaxes(axes[19])

    fig.suptitle(f"Hits vs Time — {os.path.basename(root_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        fig.savefig(save, dpi=200)
        saved_files[0] = save
        print(f"💾 Saved combined hits-vs-time figure → {save}")
    else:
        plt.show()

    return saved_files


def plot_adc_in_time(
        root_file: str,
        channels=range(1, 20),
        time_unit: str = "ms",  # "s", "ms", or "us"
        save: str | None = None,
        separate: bool = False,
        sample_size: int = 20000,  # plot firs n samples for visibility
        time_relative: bool = True
):
    """
    Plot ADC vs absolute time (full timestamp reconstructed).
    """
    import uproot
    import matplotlib.pyplot as plt
    import numpy as np
    import os
    if save is None:
        base_name = os.path.splitext(root_file)[0]
        save = os.path.join(root_file, f"{base_name}_adc_in_time.png")

    tree_name = "pmt_events"
    adc_branch = "adc"
    ch_branch = "channel"
    coarse_branch = "time_coarse"
    fine_branch = "time_fine"
    tdc_branch = "tdc_start"
    pmt_time_branch = "pmt_time"
    # -------------------------------------------------------
    # Load ROOT data
    # -------------------------------------------------------
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")

        tree = f[tree_name]

        ch = tree[ch_branch].array(library="np") + 1
        adc = tree[adc_branch].array(library="np")

        # Try reading full timestamp if present
        try:
            pmt_time = tree[pmt_time_branch].array(library="np")
            has_pmt_time = True
        except Exception:
            has_pmt_time = False
    has_pmt_time = False
    # -------------------------------------------------------
    # Build final timestamp
    # -------------------------------------------------------
    if has_pmt_time:
        # pmt_time LSB = 4 ns
        # Convert directly to seconds:
        time_s = pmt_time.astype(np.float64) * 4e-9
    else:
        time_coarse = tree[coarse_branch].array(library="np")
        time_fine = tree[fine_branch].array(library="np")
        tdc_start = tree[tdc_branch].array(library="np")
        # Full-resolution timestamp (0.25 ns per unit)
        T_raw = ((time_coarse.astype(np.uint64) << 28) |
                 (time_fine.astype(np.uint64) << 4) |
                 tdc_start.astype(np.uint64))

        time_s = T_raw.astype(np.float64) * 0.25e-9  # seconds

    if len(time_s) == 0:
        print("Warning: time_s is empty — skipping normalization")
        return

    if time_relative:
        time_s = time_s - np.nanmin(time_s)

    # Convert to time_unit
    scale = {"s": 1.0, "ms": 1e3, "us": 1e6}[time_unit]
    time_scaled = time_s * scale
    unit_label = {"s": "s", "ms": "ms", "us": "µs"}[time_unit]

    saved_files = {}

    # -------------------------------------------------------
    # SEPARATE PER-CHANNEL PLOTS
    # -------------------------------------------------------
    if separate:
        base_dir = os.path.dirname(save) if save else os.path.dirname(root_file)
        base_name = os.path.splitext(os.path.basename(root_file))[0]
        os.makedirs(base_dir, exist_ok=True)

        for ch_id in channels:
            mask = ch == ch_id
            if not np.any(mask):
                continue

            t_ch = time_scaled[mask]
            adc_ch = adc[mask]

            # Downsample
            if len(t_ch) > sample_size:
                t_ch = t_ch[0:sample_size]
                adc_ch = adc_ch[0:sample_size]

            fig, ax = plt.subplots(figsize=(6, 4))
            ax.scatter(t_ch, adc_ch, s=5, alpha=0.4, color="tab:blue")

            ax.set_title(f"ADC vs Time — Ch {ch_id:02d}")
            ax.set_xlabel(f"Time [{unit_label}]")
            ax.set_ylabel("ADC")
            ax.grid(True, linestyle="--", alpha=0.4)

            out_path = os.path.join(base_dir, f"{base_name}_adc_time_ch{ch_id:02d}.png")
            fig.tight_layout()
            fig.savefig(out_path, dpi=180)
            plt.close(fig)

            saved_files[ch_id] = out_path
            print(f"💾 Saved: {out_path}")

        return saved_files

    # -------------------------------------------------------
    # 4×5 COMBINED GRID
    # -------------------------------------------------------
    fig, axes = plt.subplots(4, 5, figsize=(16, 10))
    axes = axes.flatten()

    for idx, ch_id in enumerate(channels):
        ax = axes[idx]

        mask = ch == ch_id
        if not np.any(mask):
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.4)
            continue

        t_ch = time_scaled[mask]
        adc_ch = adc[mask]

        if len(t_ch) > sample_size:
            t_ch = t_ch[0:sample_size]
            adc_ch = adc_ch[0:sample_size]

        ax.scatter(t_ch, adc_ch, s=4, alpha=0.4, color="tab:blue")
        ax.set_title(f"Ch {ch_id:02d}")
        ax.set_xlabel(f"Time [{unit_label}]")
        ax.set_ylabel("ADC")
        ax.grid(True, linestyle="--", alpha=0.4)

    if len(axes) > 19:
        fig.delaxes(axes[19])

    fig.suptitle(f"ADC vs Time — {os.path.basename(root_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        fig.savefig(save, dpi=200)
        saved_files[0] = save
        print(f"💾 Saved combined ADC-vs-Time figure → {save}")
    else:
        plt.show()

    return saved_files


# ===============================================================
#  PER-CHANNEL FITS (ADC) + SUMMARY TABLE
# ===============================================================

def fit_adc_gauss(
        root_file: str,
        tree_name: str = "pmt_events",
        channels=range(1, 20),
        adc_branch: str = "adc",
        ch_branch: str = "channel",
        bin_width: int = 1,
        bin_edges=None,
        save_csv: bool = True,
        verbose: bool = True,
):
    """
    Simple Gaussian fitting of ADC histograms per channel.

    Fits a single Gaussian to the full ADC distribution for each channel.
    Handles empty or invalid data gracefully.

    Parameters
    ----------
    root_file : str
        Path to the input ROOT file.
    tree_name : str, optional
        Name of the TTree containing ADC data.
    channels : iterable[int], optional
        Channels to process (default: 1–19).
    adc_branch : str, optional
        ADC branch name.
    ch_branch : str, optional
        Channel branch name.
    bin_width : int, optional
        Histogram bin width in ADC units.
    bin_edges : np.ndarray or None, optional
        Custom bin edges; auto-generated if None.
    save_csv : bool, optional
        Save fit results to CSV (default: True).
    verbose : bool, optional
        Print summary table (default: True).
    """
    import numpy as np, pandas as pd, uproot, os
    from scipy.optimize import curve_fit, OptimizeWarning
    from tabulate import tabulate
    import warnings

    # --- Gaussian model ---
    def gauss(x, a, mu, sigma):
        return a * np.exp(-0.5 * ((x - mu) / sigma) ** 2)

    # --- Load data ---
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")
        tree = f[tree_name]
        ch = tree[ch_branch].array(library="np") + 1
        adc = tree[adc_branch].array(library="np")

        if verbose:
            unique_channels, counts = np.unique(ch, return_counts=True)
            print(f"\n📦 Loaded data from {root_file}")
            print(f"   Total entries: {len(adc):,}")
            for ch_id, count in zip(unique_channels, counts):
                print(f"     Ch {int(ch_id):02d}: {count:,} entries")
            print("------------------------------------------------------")

    results = {}

    # --- Fit per channel ---
    for ch_id in channels:
        adc_ch = adc[ch == ch_id]
        if adc_ch.size == 0:
            if verbose:
                print(f"⚠️  Skipping Ch {ch_id:02d} (no entries).")
            continue

        hist_min = int(np.min(adc_ch))
        hist_max_val = int(np.max(adc_ch))
        # Generate bin edges
        bins = bin_edges if bin_edges is not None else np.arange(hist_min - 5 * bin_width, hist_max_val + 5 * bin_width,
                                                                 bin_width)
        if len(bins) < 2:
            if verbose:
                print(f"⚠️  Skipping Ch {ch_id:02d} (invalid bin edges).")
            continue

        hist_vals, edges = np.histogram(adc_ch, bins=bins)
        if hist_vals.size == 0 or np.all(hist_vals == 0):
            if verbose:
                print(f"⚠️  Skipping Ch {ch_id:02d} (empty histogram).")
            continue

        centers = 0.5 * (edges[:-1] + edges[1:])

        # --- Initial guess ---
        a0 = np.max(hist_vals)
        mu0 = np.mean(adc_ch)
        sigma0 = np.std(adc_ch) if np.std(adc_ch) > 0 else 1.0
        p0 = [a0, mu0, sigma0]

        # --- Parameter bounds ---
        lower_bounds = [0, hist_min - 1, -np.inf]
        upper_bounds = [np.inf, hist_max_val + 1, np.inf]

        # --- Gaussian fit ---
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", OptimizeWarning)
                popt, _ = curve_fit(gauss, centers, hist_vals, p0=p0, maxfev=20000,
                                    bounds=(lower_bounds, upper_bounds), )
            a_fit, mu_fit, sigma_fit = map(float, popt)

            residuals = hist_vals - gauss(centers, *popt)
            ss_res = np.sum(residuals ** 2)
            ss_tot = np.sum((hist_vals - np.mean(hist_vals)) ** 2)
            r2 = 1 - ss_res / ss_tot if ss_tot != 0 else np.nan

        except Exception as e:
            if verbose:
                print(f"⚠️  Fit failed for Ch {ch_id:02d}: {e}")
            a_fit, mu_fit, sigma_fit, r2 = a0, mu0, sigma0, 0.0

        results[int(ch_id)] = {
            "mean_data": float(mu0),
            "std_data": float(sigma0),
            "mu": mu_fit,
            "sigma": abs(sigma_fit),
            "amplitude": a_fit,
            "r2": r2,
            "entries": int(len(adc_ch)),
            "min": float(np.min(adc_ch)),
            "max": float(np.max(adc_ch)),
        }

    # --- Save results ---
    if save_csv and results:
        csv_file = os.path.splitext(root_file)[0] + ".csv"
        pd.DataFrame.from_dict(results, orient="index").rename_axis("channel").to_csv(csv_file)
        print(f"💾 Results saved to {csv_file}")

    # --- Summary ---
    if verbose and results:
        print(f"\n✅ Gaussian fits completed for {len(results)} channels.\n")
        table_data = [
            [ch, f"{res['mu']:.1f}", f"{res['sigma']:.1f}", f"{res['r2']:.4f}", res["min"], res["max"], res["entries"]]
            for ch, res in sorted(results.items())
        ]
        print(tabulate(
            table_data,
            headers=["Ch", "μ (Mean)", "σ", "R²", "Min", "Max", "Entries"],
            tablefmt="fancy_grid",
            stralign="center",
            numalign="right",
        ))

    return results


def histogram_center_of_gravity(x_centers, counts, width_mode="fwhm"):
    """
    Compute center of gravity (COG) and estimate width (sigma).

    Parameters
    ----------
    x_centers : array-like
        Bin centers
    counts : array-like
        Bin counts
    width_mode : str
        "rms"  -> weighted RMS (recommended)
        "fwhm" -> estimate sigma from FWHM

    Returns
    -------
    cog_x : float
    cog_idx : int
    sigma : float
    """
    import numpy as np

    x = np.asarray(x_centers, dtype=float)
    y = np.asarray(counts, dtype=float)

    if np.sum(y) == 0:
        return np.nan, np.nan, np.nan

    # -----------------------------
    # Center of gravity
    # -----------------------------
    cog_x = np.sum(x * y) / np.sum(y)
    cog_idx = np.argmin(np.abs(x - cog_x))

    # -----------------------------
    # Width estimation
    # -----------------------------
    if width_mode == "rms":
        # Weighted RMS (best default)
        variance = np.sum(y * (x - cog_x) ** 2) / np.sum(y)
        sigma = np.sqrt(variance)

    elif width_mode == "fwhm":
        # Estimate FWHM → sigma
        half_max = np.max(y) / 2.0
        indices = np.where(y >= half_max)[0]

        if len(indices) >= 2:
            fwhm = x[indices[-1]] - x[indices[0]]
            sigma = fwhm / 2.355
        else:
            sigma = np.nan
    else:
        sigma = np.nan

    return cog_x, cog_idx, sigma


def truncated_stats(x, low=1, high=90):
    x = np.asarray(x)
    x = x[np.isfinite(x)]

    if x.size == 0:
        return np.nan, np.nan

    lo, hi = np.percentile(x, [low, high])
    x_cut = x[(x >= lo) & (x <= hi)]

    if x_cut.size == 0:
        return np.nan, np.nan

    return np.mean(x_cut), np.std(x_cut)


def fit_1pe_distribution(
        root_file: str,
        channels=range(1, 20),
        bin_width: int = 1,
        bin_edges=None,
        pedestals=None,
        hist_max: int = 4095,
        save_csv: bool = True,
        verbose: bool = True,
        led_freq_hz: int = 0,
        min_gain_adc: int = 20
):
    """
   Fit 1PE ADC distributions per channel using a Gaussian model.

    Processes PMT event data from a ROOT file and extracts signal parameters
    from ADC spectra. The fit is restricted to the signal region to suppress
    pedestal and noise contributions.

    Workflow
    --------
    1. Load ADC and channel data from TTree.
    2. For each channel:
    - Build ADC histogram.
    - Estimate signal position using histogram center of gravity (CoG).
    - Define fit region (right side of CoG, pedestal suppressed).
    - Perform Gaussian fit on the upper part of the distribution.
    3. Store fit and data statistics.
    4. Optionally save results to CSV and print summary.

    Parameters
    ----------
    root_file : str
        Path to input ROOT file.
    channels : iterable[int], optional
        Channels to process (default: 1–19).
    bin_width : int, optional
        Histogram bin width in ADC units.
    bin_edges : array-like, optional
        Custom histogram binning.
    pedestals : dict[int, float], optional
        Per-channel pedestal values (subtracted if provided).
    hist_max : int, optional
        Maximum ADC range.
    save_csv : bool, optional
        Save results to CSV.
    verbose : bool, optional
        Print summary output.
    led_freq_hz : int, optional
        LED trigger frequency for signal selection (0 = no filtering).
    min_gain_adc : int, optional
        Minimum valid fitted mean (below → fit rejected).

    Returns
    -------
    dict[int, dict]
        Per-channel results with keys:
            fit_mu            : Gaussian mean (ADC)
            fit_sigma         : Gaussian width (ADC)
            fit_amplitude     : Gaussian amplitude
            fit_r2            : fit quality (R²)
            data_mean         : mean ADC (corrected)
            data_std          : std ADC (raw)
            data_entries      : number of samples
            data_min/max      : ADC range
            cog_mean          : CoG estimate
            cog_sigma         :  width (initial estimate)
            fit_left_cut      : lower bound of fit region
            hist_left_edge    : histogram threshold edge
            pedestal_value    : pedestal used
            pedestal_applied  : bool

    Notes
    -----
    - CoG is used as a robust initial estimate of the signal position.
    - Fit is restricted to suppress pedestal and multi-PE contamination.
    - For low statistics or poor fits, fallback values may be used.
    """
    import numpy as np, pandas as pd, uproot, os
    from scipy.optimize import curve_fit
    from scipy.signal import find_peaks
    from tabulate import tabulate

    pmt_time_branch = "pmt_time"
    tree_name = "pmt_events"
    adc_branch = "adc"
    ch_branch = "channel"

    # --- Step 1: Load ROOT data ---
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")
        tree = f[tree_name]
        ch = tree[ch_branch].array(library="np") + 1
        adc = tree[adc_branch].array(library="np")
        pmt_time = tree[pmt_time_branch].array(library="np").astype(np.int64)
        # Summary printout
        unique_channels, counts = np.unique(ch, return_counts=True)
        if verbose:
            print(f"\n📦 Loaded data from {root_file}")
            print(f"   Total entries: {len(adc):,}")
            print("   Per-channel event counts:")
            for ch_id, count in zip(unique_channels, counts):
                print(f"     Ch {int(ch_id):02d}: {count:,} entries")
            print("   Missing channels:", [c for c in channels if c not in unique_channels])
            print("------------------------------------------------------")

    if verbose:
        print(
            f"Global Histogram bin edges:"
            f" min={np.min(bin_edges)}, max={np.max(bin_edges)}, bins={len(bin_edges) - 1},"
            f" width={bin_width}"
        )

    results = {}
    if pedestals is not None:
        print(f"Pedestals provided, adc is corrected.")
    # --- Step 2: Loop over all channels ---
    for ch_id in channels:
        adc_ch = adc[ch == ch_id]
        pmt_time_ch = pmt_time[ch == ch_id]
        # filter only led pulses
        led_mask, signal_phase = get_led_mask(pmt_time_ch, led_freq_hz)
        adc_ch = adc_ch[led_mask]

        if adc_ch.size == 0:
            continue

        if pedestals is not None:
            adc_ch = adc_ch - pedestals.get(ch_id)

        # --- Determine bin edges ---
        if bin_edges is None:
            hist_min = int(np.min(adc_ch) - 5 * bin_width)
            hist_max = int(np.max(adc_ch) + 5 * bin_width)
            bin_edges = np.arange(hist_min, hist_max + bin_width, bin_width, dtype=int)

        # --- Step 3: Build histogram ---
        hist_vals, edges = np.histogram(adc_ch, bins=bin_edges)
        centers = 0.5 * (edges[:-1] + edges[1:])

        if np.max(hist_vals) == 0:
            adc_min = np.min(adc_ch) if adc_ch.size > 0 else None
            adc_max = np.max(adc_ch) if adc_ch.size > 0 else None
            adc_mean = np.mean(adc_ch) if adc_ch.size > 0 else None
            adc_len = adc_ch.size

            print(
                f"Channel {ch_id}: histogram is empty (all zeros). Skipping...\n"
                f"    → ADC stats: len={adc_len}, min={adc_min}, max={adc_max}, mean={adc_mean}"
            )
            continue

        # Slight smoothing for stable peak/minimum detection
        smoothed = np.convolve(hist_vals, np.ones(5) / 5, mode="same")

        # --- Step 4: Locate main region via CoG ---
        cog_x, cog_idx, sigma0 = histogram_center_of_gravity(centers, hist_vals, width_mode="rms")

        # --- Step 6: Restrict to signal region using CoG ± FWHM ---
        data_mean, data_std = truncated_stats(adc_ch, 10, 90)
        fwhm = 2.355 * data_std

        fit_left = cog_x - 0.35 * fwhm
        fit_right = cog_x + 1.0 * fwhm

        mask = (centers >= fit_left) & (centers <= fit_right)

        x_fit = centers[mask]
        y_fit = hist_vals[mask]

        if y_fit is None or len(y_fit) == 0:
            print("⚠️ Empty fit data — skipping Gaussian fit")
            continue

            # --- Step 7: Select upper part of distribution ---
        y_max = np.max(y_fit)
        mask_top = y_fit >= 0.3 * y_max  # ≥ 50% of peak

        x_sel = x_fit[mask_top]
        y_sel = y_fit[mask_top]

        # Safety check
        if len(x_sel) < 3:
            x_sel = x_fit
            y_sel = y_fit

        # --- Step 8: Initial parameter estimates (on selected region) ---
        a0 = float(np.max(y_sel))
        mu0 = cog_x
        p0 = [a0, mu0, max(1e-6, sigma0)]

        # --- Step 9: Gaussian fit (only upper part) ---
        try:
            bounds = (
                [0.0, 0.0, 1e-6],  # a > 10, mu > 10, sigma > 0
                [2 * a0, 4000.0, np.inf]  # a < 1.2*max, mu < 4000, sigma < inf
            )

            popt, _ = curve_fit(
                gauss,
                x_sel,
                y_sel,
                p0=p0,
                bounds=bounds,
                maxfev=10000
            )

            a_fit, mu_fit, sigma_fit = map(float, popt)

            # Evaluate R² on FULL distribution (important!)
            residuals = y_fit - gauss(x_fit, *popt)
            ss_res = np.sum(residuals ** 2)
            ss_tot = np.sum((y_fit - np.mean(y_fit)) ** 2)
            r2 = 1 - (ss_res / ss_tot if ss_tot != 0 else np.inf)
            if mu_fit < min_gain_adc:
                mu_fit = np.nan
                sigma_fit = np.nan
                a_fit = np.nan
                r2 = np.nan
        except Exception as e:
            print(f"⚠️ Gaussian fit failed: {e}")
            mu_fit, sigma_fit, a_fit, r2 = mu0, sigma0, a0, 0.0

        left_edge = threshold_left(adc_ch, bin_width=1, frac=0.01)

        results[int(ch_id)] = {
            # --- Gaussian fit (1 PE signal) ---
            "fit_mu": mu_fit,
            "fit_sigma": abs(sigma_fit),
            "fit_amplitude": a_fit,
            "fit_r2": r2,

            # --- Data statistics (after pedestal subtraction if applied) ---
            "data_mean": data_mean,
            "data_std": data_std,
            "data_entries": int(adc_ch.size),
            "data_min": int(np.min(adc_ch)),
            "data_max": int(np.max(adc_ch)),

            # --- Histogram-based estimates ---
            "cog_mean": cog_x,
            "cog_sigma": sigma0,  # IMPORTANT: this is RMS, not FWHM

            # --- Fit region ---
            "fit_left_cut": float(fit_left),
            "fit_right_cut": float(fit_right),
            "hist_left_edge": left_edge,

            # --- Pedestal info ---
            "pedestal_value": pedestals.get(ch_id, 0) if pedestals is not None else 0,
            "pedestal_applied": pedestals is not None,
        }

    # --- Step 9: Save results ---
    if save_csv and results:
        csv_file = os.path.splitext(root_file)[0] + ".csv"

        df = pd.DataFrame.from_dict(results, orient="index")
        df.index.name = "channel"

        # Optional: nice column order
        cols_order = [
            "fit_mu", "fit_sigma", "fit_amplitude", "fit_r2",
            "cog_mean", "cog_sigma",
            "data_mean", "data_std", "data_entries", "data_min", "data_max",
            "fit_left_cut", "hist_left_edge",
            "pedestal_value", "pedestal_applied"
        ]
        df = df[[c for c in cols_order if c in df.columns]]

        df.to_csv(csv_file)
        print(f"💾 Results saved to {csv_file}")

    # --- Step 10: Print summary table ---
    if verbose and results:
        print(f"\n✅ Gaussian fits completed for {len(results)} channels.")

        table_data = []
        for ch_id, res in sorted(results.items()):

            r2_val = res.get("fit_r2", 0.0)
            if r2_val > 0.9:
                r2_str = f"\033[92m{r2_val:.4f}\033[0m"
            elif r2_val > 0.8:
                r2_str = f"\033[93m{r2_val:.4f}\033[0m"
            else:
                r2_str = f"\033[91m{r2_val:.4f}\033[0m"

            table_data.append([
                ch_id,
                f"{res.get('pedestal_value', 0):.1f}",
                f"{res.get('fit_mu', np.nan):.1f}",
                f"{res.get('fit_sigma', np.nan):.1f}",
                r2_str,
                f"{res.get('cog_mean', np.nan):.1f}",
                f"{res.get('cog_sigma', np.nan):.1f}",
                f"{res.get('data_mean', np.nan):.1f}",
                f"{res.get('data_std', np.nan):.1f}",
                f"{res.get('fit_left_cut', np.nan):.0f}",
                f"{res.get('data_entries', 0):,}",
                f"{res.get('data_min', np.nan):.0f}",
                f"{res.get('data_max', np.nan):.0f}",
            ])

        print("\n📊 ADC Gaussian Fit Summary (1 PE region)\n")
        print(tabulate(
            table_data,
            headers=[
                "Ch",
                "Pedestal",
                "μ_fit (ADC)",
                "σ_fit (ADC)",
                "R²",
                "cog_mean",
                "cog_sigma",
                "data_mean",
                "data_std",
                "Cut [ADC]",
                "Entries",
                "Min",
                "Max"
            ],
            tablefmt="fancy_grid",
            stralign="center",
            numalign="right",
        ))

    return results


# ===============================================================
#  ADC vs TOT SCATTER PLOTS
# ===============================================================

def plot_adc_vs_tot(
        root_file: str,
        tree_name: str = "pmt_events",
        channels=range(1, 20),
        adc_branch: str = "adc",
        ch_branch: str = "channel",
        tdc_coarse_branch: str = "tdc_coarse",
        tdc_start_branch: str = "tdc_start",
        tdc_stop_branch: str = "tdc_stop",
        fixed_range: bool = False,
        tot_max: float = 800.0,
        adc_max: float = 4095.0,
        sample_size: int = 5000,
        save: str | None = None,
        separate: bool = False,
):
    """
    Scatter plots of ADC vs TOT for each channel.

    TOT [ns] = (tdc_coarse - tdc_start/15 + tdc_stop/15) * 4

    Parameters
    ----------
    separate : bool, optional
        If True, save separate PNGs for each channel (only if save is provided).
        If False, save or show one combined 4x5 grid.
    Returns
    -------
    dict[int, str]
        Mapping of {channel: plot_path}, empty if not saved.
    """
    import os
    import numpy as np
    import matplotlib.pyplot as plt
    import uproot

    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise SystemExit(f"TTree '{tree_name}' not found in {root_file}")
        tree = f[tree_name]
        arr = tree.arrays(
            [ch_branch, adc_branch, tdc_coarse_branch, tdc_start_branch, tdc_stop_branch],
            library="np",
        )

    ch = arr[ch_branch] + 1
    adc = arr[adc_branch]
    tdc_coarse = arr[tdc_coarse_branch]
    tdc_start = arr[tdc_start_branch]
    tdc_stop = arr[tdc_stop_branch]
    tot_ns = (tdc_coarse - tdc_start / 15.0 + tdc_stop / 15.0) * 4.0

    valid = (
            np.isfinite(adc)
            & np.isfinite(tot_ns)
            & (adc > 0)
            & (adc < adc_max)
            & (tot_ns > 0)
            & (tot_ns < tot_max)
    )
    adc = adc[valid]
    tot_ns = tot_ns[valid]
    ch = ch[valid]

    saved_files: dict[int, str] = {}

    # -------------------------------
    # SEPARATE MODE
    # -------------------------------
    if separate and save:
        base_name = os.path.splitext(os.path.basename(save))[0]
        base_dir = os.path.dirname(save) or os.getcwd()
        os.makedirs(base_dir, exist_ok=True)

        for ch_id in channels:
            mask = ch == ch_id
            if not np.any(mask):
                continue

            adc_ch = adc[mask]
            tot_ch = tot_ns[mask]

            # Downsample for readability
            if len(adc_ch) > sample_size:
                sel = np.random.choice(len(adc_ch), sample_size, replace=False)
                adc_ch = adc_ch[sel]
                tot_ch = tot_ch[sel]

            fig, ax = plt.subplots(figsize=(5, 4))
            ax.scatter(tot_ch, adc_ch, s=3, alpha=0.5, color="#1f77b4")
            ax.set_title(f"Ch {ch_id:02d}", fontsize=10)
            ax.set_xlabel("TOT [ns]")
            ax.set_ylabel("ADC")
            ax.set_xlim(0, tot_max)
            ax.set_ylim(0, adc_max)
            ax.grid(True, linestyle="--", alpha=0.4)

            out_path = os.path.join(base_dir, f"{base_name}_ch{ch_id:02d}.png")
            fig.savefig(out_path, dpi=150, bbox_inches="tight")
            plt.close(fig)
            saved_files[ch_id] = out_path
            print(f"✅ Saved → {out_path}")

        print(f"📁 Saved {len(saved_files)} per-channel ADC vs TOT plots → {base_dir}")
        return saved_files

    # -------------------------------
    # COMBINED GRID MODE
    # -------------------------------
    fig, axes = plt.subplots(4, 5, figsize=(16, 10), sharex=fixed_range, sharey=fixed_range)
    axes = axes.flatten()

    for idx, ch_id in enumerate(channels):
        ax = axes[idx]
        mask = ch == ch_id
        if not np.any(mask):
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.4)
            continue

        adc_ch = adc[mask]
        tot_ch = tot_ns[mask]

        # Downsample
        if len(adc_ch) > sample_size:
            sel = np.random.choice(len(adc_ch), sample_size, replace=False)
            adc_ch = adc_ch[sel]
            tot_ch = tot_ch[sel]

        ax.scatter(tot_ch, adc_ch, s=3, alpha=0.5, color="#1f77b4")
        ax.set_title(f"Ch {ch_id:02d}", fontsize=9)
        ax.set_xlabel("TOT [ns]")
        ax.set_ylabel("ADC")
        ax.grid(True, linestyle="--", alpha=0.4)
        if fixed_range:
            ax.set_xlim(0, tot_max)
            ax.set_ylim(0, adc_max)

    if len(axes) > 19:
        fig.delaxes(axes[19])

    fig.suptitle(f"ADC vs TOT per channel — {os.path.basename(root_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        plt.savefig(save, dpi=200, bbox_inches="tight")
        plt.close(fig)
        print(f"💾 Saved combined ADC vs TOT plot → {save}")
        saved_files[0] = save
    else:
        plt.show()

    return saved_files


import os
import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
import uproot


def get_led_mask(pmt_time, led_freq_hz, phase_window=8):
    """
    Returns boolean mask selecting pulses within LED phase window.

    Parameters
    ----------
    pmt_time : np.ndarray
        PMT time (coarse, same as used in your code)
    led_freq_hz : float or None
        LED frequency. If 0 or None → all events are selected.
    phase_window : int
        Window in phase bins (4 ns units)

    Returns
    -------
    mask : np.ndarray (bool)
    signal_phase : int or None
    """

    # -----------------------------
    # NO LED MODE → take all
    # -----------------------------
    if led_freq_hz is None or led_freq_hz == 0:
        return np.ones_like(pmt_time, dtype=bool), None

    # -----------------------------
    # PERIOD (in 4 ns units)
    # -----------------------------
    PERIOD_4NS = int((1.0 / led_freq_hz) / 4e-9)

    if PERIOD_4NS <= 0:
        return np.ones_like(pmt_time, dtype=bool), None

    # -----------------------------
    # PHASE
    # -----------------------------
    phase = pmt_time % PERIOD_4NS

    # find dominant phase (LED peak)
    hist = np.bincount(phase, minlength=PERIOD_4NS)
    signal_phase = int(np.argmax(hist))

    # -----------------------------
    # WINDOW MASK
    # -----------------------------
    # handle wrap-around properly
    delta = np.abs(phase - signal_phase)
    delta = np.minimum(delta, PERIOD_4NS - delta)

    led_mask = delta <= phase_window

    return led_mask, signal_phase


def analyze_led_timing(
        root_file,
        led_freq_hz=10_000,
        tree_name="pmt_events",
        channels=range(1, 20),
        ch_branch="channel",
        pmt_time_branch="pmt_time",
        tdc_start_branch="tdc_start",
        adc_branch="adc",
        tdc_coarse_branch="tdc_coarse",
        tdc_stop_branch="tdc_stop",
        phase_window=8,
        pedestals={}
):
    def gauss(x, A, mu, sigma):
        return A * np.exp(-(x - mu) ** 2 / (2 * sigma ** 2))

    results = {}

    # -----------------------------
    # OUTPUT DIR
    # -----------------------------
    save_dir = os.path.join(os.path.dirname(root_file), "timing_plots")
    os.makedirs(save_dir, exist_ok=True)
    root_prefix = os.path.splitext(os.path.basename(root_file))[0]

    # -----------------------------
    # LOAD DATA
    # -----------------------------
    with uproot.open(root_file) as f:
        tree = f[tree_name]

        channel = tree[ch_branch].array(library="np") + 1
        pmt_time = tree[pmt_time_branch].array(library="np").astype(np.int64)
        tdc_start = tree[tdc_start_branch].array(library="np").astype(np.int64)
        adc = tree[adc_branch].array(library="np")

    # -----------------------------
    # TIME
    # -----------------------------
    T = (pmt_time << 4) + tdc_start
    t_ns = T * 0.25

    # -----------------------------
    # CLEAN: finite mask (APPLY TO ALL!)
    # -----------------------------
    mask = np.isfinite(t_ns)

    T = T[mask]
    t_ns = t_ns[mask]
    channel = channel[mask]
    pmt_time = pmt_time[mask]
    adc = adc[mask]

    if t_ns.size == 0:
        print("⚠️ No valid timestamps — filling results with NaN")

        for ch in channels:
            results[ch] = {
                "n_total": 0,
                "n_led": 0,
                "n_dark": 0,
                "dark_rate_Hz": np.nan,
                "measurement_time_s": 0.0,
                "led_frequency_Hz": led_freq_hz,
                "time_mean_ns": np.nan,
                "time_sigma_ns": np.nan,
                "TTS_FWHM_ns": np.nan,
                "lambda_true": np.nan,
                "occupancy": np.nan,
                "poisson_p0": np.nan,
                "poisson_p1": np.nan,
                "p_multi": np.nan,
                "led_fraction": np.nan,
                "adc_min": np.nan,
                "adc_mean": np.nan,
                "adc_max": np.nan,
                "valid": False  # 👈 VERY IMPORTANT FLAG
            }

        return results

    # -----------------------------
    # CLEAN: remove outliers (DMA / glitches)
    # -----------------------------
    def remove_time_outliers_v2(t_ns, threshold_ns=1e6):
        dt = np.abs(np.diff(t_ns))
        bad = dt > threshold_ns

        mask = np.ones_like(t_ns, dtype=bool)
        mask[:-1][bad] = False
        mask[1:][bad] = False

        return mask

    mask_clean = remove_time_outliers_v2(t_ns, threshold_ns=1e6)

    T = T[mask_clean]
    t_ns = t_ns[mask_clean]
    channel = channel[mask_clean]
    pmt_time = pmt_time[mask_clean]
    adc = adc[mask_clean]

    # debug
    n_removed = np.sum(~mask_clean)
    if n_removed > 0:
        print(f"⚠️ Removed {n_removed} time outliers")

    # -----------------------------
    # LOOP CHANNELS
    # -----------------------------
    for ch in channels:

        mask_ch = channel == ch
        if not np.any(mask_ch):
            continue

        pmt_ch = pmt_time[mask_ch]
        adc_ch = adc[mask_ch] - pedestals.get(ch, 0.0)
        T_ch = T[mask_ch].copy()

        led_mask, signal_phase = get_led_mask(pmt_ch, led_freq_hz, phase_window)
        dark_mask = ~led_mask

        n_led = np.count_nonzero(led_mask)
        n_dark = np.count_nonzero(dark_mask)
        n_total = len(pmt_ch)

        # -----------------------------
        # DURATION (ROBUST)
        # -----------------------------
        # use percentiles instead of min/max
        t_min = np.percentile(T_ch * 0.25, 1)
        t_max = np.percentile(T_ch * 0.25, 99)

        duration_s = (t_max - t_min) * 1e-9

        # -------------------------
        # TIMING RAW
        # -------------------------
        PERIOD_4NS = int((1.0 / led_freq_hz) / 4e-9)
        t_rel_raw = (T_ch % (PERIOD_4NS << 4)) * 0.25
        t_rel_raw = t_rel_raw - signal_phase * 4

        # -------------------------
        # TIMING CORRECTED
        # -------------------------
        t_rel_corr = apply_timewalk_lut(t_rel_raw.copy(), adc_ch)

        t_led_raw = t_rel_raw[led_mask]
        t_led_corr = t_rel_corr[led_mask]

        # -------------------------
        # ADC STATS (LED only)
        # -------------------------
        adc_led = adc_ch[led_mask]

        if adc_led.size > 0:
            adc_min = float(np.min(adc_led))
            adc_mean = float(np.mean(adc_led))
            adc_max = float(np.max(adc_led))
        else:
            adc_min, adc_mean, adc_max = np.nan, np.nan, np.nan

        # -------------------------
        # HISTOGRAM
        # -------------------------
        bins = np.arange(-10, 20, 0.25)

        hist_raw, edges = np.histogram(t_led_raw, bins=bins)
        hist_corr, _ = np.histogram(t_led_corr, bins=bins)
        centers = 0.5 * (edges[:-1] + edges[1:])

        # -------------------------
        # FIT (corrected)
        # -------------------------
        try:
            p0 = [np.max(hist_corr), np.mean(t_led_corr), np.std(t_led_corr)]
            popt, _ = curve_fit(gauss, centers, hist_corr, p0=p0)
            A, mu, sigma = popt
            sigma_fit = abs(sigma)
        except Exception:
            A, mu, sigma = np.nan, np.nan, np.nan
            sigma_fit = np.nan
            popt = None

        # -------------------------
        # PLOT
        # -------------------------
        plt.figure(figsize=(6, 4))

        # RAW
        plt.step(
            centers,
            hist_raw,
            where="mid",
            linestyle="--",
            label="raw"
        )

        # CORRECTED
        plt.step(
            centers,
            hist_corr,
            where="mid",
            label="corrected"
        )

        # FIT
        if popt is not None:
            x_fit = np.linspace(min(bins), max(bins), 400)
            y_fit = gauss(x_fit, *popt)
            plt.plot(
                x_fit,
                y_fit,
                lw=2,
                label=f"fit σ = {sigma_fit:.3f} ns"
            )

        plt.title(f"Timing distribution – Channel {ch}")
        plt.xlabel("Time [ns]")
        plt.ylabel("Counts")
        plt.grid(alpha=0.3)
        plt.legend()

        outfile = os.path.join(save_dir, f"{root_prefix}_timing_ch{ch:02d}.png")
        plt.savefig(outfile, dpi=150)
        plt.close()

        print(f"✔ Timing plot saved for ch{ch:02d} → {outfile}")

        # -------------------------
        # DARK RATE
        # -------------------------
        dark_rate = n_dark / duration_s if duration_s > 0 else np.nan

        # -------------------------
        # POISSON
        # -------------------------
        n_expected = duration_s * led_freq_hz
        occupancy = n_led / n_expected if n_expected > 0 else np.nan
        lambda_true = -np.log(1 - occupancy) if occupancy < 1 else np.nan

        p0 = np.exp(-lambda_true)
        p1 = lambda_true * np.exp(-lambda_true)
        p_multi = 1 - p0 - p1

        # -------------------------
        # RESULTS
        # -------------------------
        results[ch] = {
            "n_total": n_total,
            "n_led": n_led,
            "n_dark": n_dark,
            "dark_rate_Hz": dark_rate,
            "measurement_time_s": duration_s,
            "led_frequency_Hz": led_freq_hz,
            "time_mean_ns": mu,
            "time_sigma_ns": sigma_fit,
            "TTS_FWHM_ns": 2.355 * sigma_fit if np.isfinite(sigma_fit) else np.nan,
            "lambda_true": lambda_true,
            "occupancy": occupancy,
            "poisson_p0": p0,
            "poisson_p1": p1,
            "p_multi": p_multi,
            "led_fraction": n_led / n_total if n_total > 0 else np.nan,
            "adc_min": adc_min,
            "adc_mean": adc_mean,
            "adc_max": adc_max
        }

        print(f"Ch {ch:02d}: σ={sigma_fit:.3f} ns")

    return results


def plot_adc_tot(
        root_files,
        tree_name: str = "pmt_events",
        channels=range(1, 20),
        adc_branch: str = "adc",
        ch_branch: str = "channel",
        tdc_coarse_branch: str = "tdc_coarse",
        tdc_start_branch: str = "tdc_start",
        tdc_stop_branch: str = "tdc_stop",
        tot_max: float = 800.0,
        adc_max: float = 4095.0,
        sample_size: int = 5000,
        save_dir: str = ".",
        prefix: str = "adc_tot",
):
    """
    Per-channel ADC vs TOT plots.
    Supports single ROOT file or list of ROOT files.

    Returns
    -------
    dict[int, str]
        Mapping {channel: file_path}
    """

    import os
    import numpy as np
    import matplotlib.pyplot as plt
    import uproot

    # normalize input
    if isinstance(root_files, str):
        root_files = [root_files]

    os.makedirs(save_dir, exist_ok=True)

    # collect arrays from all files
    all_ch, all_adc, all_tot = [], [], []

    for root_file in root_files:
        print(f"📂 Loading: {root_file}")
        with uproot.open(root_file) as f:
            tree = f[tree_name]
            arr = tree.arrays(
                [ch_branch, adc_branch, tdc_coarse_branch, tdc_start_branch, tdc_stop_branch],
                library="np",
            )

        ch = arr[ch_branch] + 1
        adc = arr[adc_branch]
        tdc_coarse = arr[tdc_coarse_branch]
        tdc_start = arr[tdc_start_branch]
        tdc_stop = arr[tdc_stop_branch]

        tot_ns = (tdc_coarse - tdc_start / 15.0 + tdc_stop / 15.0) * 4.0

        valid = (
                np.isfinite(adc)
                & np.isfinite(tot_ns)
                & (adc > 0)
                & (adc < adc_max)
                & (tot_ns > 0)
                & (tot_ns < tot_max)
        )

        all_ch.append(ch[valid])
        all_adc.append(adc[valid])
        all_tot.append(tot_ns[valid])

    # merge all files
    ch = np.concatenate(all_ch)
    adc = np.concatenate(all_adc)
    tot_ns = np.concatenate(all_tot)

    print(f"📊 Total events: {len(adc)}")

    saved_files = {}

    # plotting per channel
    for ch_id in channels:
        mask = ch == ch_id
        if not np.any(mask):
            continue

        adc_ch = adc[mask]
        tot_ch = tot_ns[mask]

        # Downsample
        if len(adc_ch) > sample_size:
            sel = np.random.choice(len(adc_ch), sample_size, replace=False)
            adc_ch = adc_ch[sel]
            tot_ch = tot_ch[sel]

        fig, ax = plt.subplots(figsize=(5, 4))
        ax.scatter(tot_ch, adc_ch, s=3, alpha=0.5)
        ax.set_title(f"ADC vs TOT — Ch {ch_id:02d}")
        ax.set_xlabel("TOT [ns]")
        ax.set_ylabel("ADC")
        ax.set_xlim(0, tot_max)
        ax.set_ylim(0, adc_max)
        ax.grid(True, alpha=0.3)

        outfile = os.path.join(save_dir, f"{prefix}_ch{ch_id:02d}.png")
        fig.savefig(outfile, dpi=150, bbox_inches="tight")
        plt.close(fig)

        saved_files[ch_id] = outfile
        print(f"✔ Saved: {outfile}")

    print(f"Generated {len(saved_files)} plots from {len(root_files)} file(s)")

    return saved_files


# ===============================================================
#  HV RAMP VALIDATION PLOT
# ===============================================================

def linear_model(x, a, b):
    return a * x + b


def plot_hv_ramp(
        csv_file: str,
        channels=range(1, 20),
        save: str | None = None,
        hv_tolerance: float = 50.0,
        separate: bool = False,
):
    """
    Plot measured HV/current vs HV setpoint and check ΔV tolerance.

    Parameters
    ----------
    csv_file : str
        Path to the HV ramp measurement CSV file.
    channels : iterable[int], optional
        Channels to include (default: 1–19)
    save : str or None, optional
        If provided, save figure(s) to this path.
    hv_tolerance : float, optional
        Tolerance for ΔV check in Volts.
    separate : bool, optional
        If True, saves individual PNGs per channel (only if `save` is not None).

    Returns
    -------
    dict[int, str]
        Mapping {channel: plot_path} for saved files.
        Empty dict if save=None.
    """
    import os
    import numpy as np
    import pandas as pd
    import matplotlib.pyplot as plt
    from scipy.optimize import curve_fit
    from tabulate import tabulate

    def linear_model(x, a, b):
        return a * x + b

    try:
        df = pd.read_csv(csv_file)
    except Exception as e:
        raise SystemExit(f"❌ Failed to read {csv_file}: {e}")

    # Ensure numeric types
    for col in ["hv_set", "voltage", "current", "voltage_set"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    saved_files: dict[int, str] = {}
    summary_rows = []

    # ----------------------------------
    # SEPARATE MODE
    # ----------------------------------
    if separate and save:
        base_name = os.path.splitext(os.path.basename(save))[0]
        base_dir = os.path.dirname(save) or os.getcwd()
        os.makedirs(base_dir, exist_ok=True)

        for ch_id in channels:
            ch_data = df[df["channel"] == ch_id].sort_values("voltage_set")
            if ch_data.empty:
                print(f"⚠️  No data for channel {ch_id}")
                continue

            voltage_set = ch_data["voltage_set"].values
            voltage = ch_data["voltage"].values
            current = ch_data["current"].values

            # --- Fits ---
            def safe_fit(x, y):
                try:
                    popt, _ = curve_fit(linear_model, x, y)
                    return (*popt, True)
                except Exception:
                    return (np.nan, np.nan, False)

            a_v, b_v, fit_ok_v = safe_fit(voltage_set, voltage)
            a_i, b_i, fit_ok_i = safe_fit(voltage_set, current)

            # --- Plot ---
            fig, ax1 = plt.subplots(figsize=(6, 4))
            ax1.plot(voltage_set, voltage, "o-", label="Measured V", color="tab:blue")
            if fit_ok_v:
                v_fit = np.linspace(min(voltage_set), max(voltage_set), 100)
                ax1.plot(v_fit, linear_model(v_fit, a_v, b_v), "b--", alpha=0.6)
            ax1.set_xlabel("HV Set [V]")
            ax1.set_ylabel("Measured HV [V]", color="tab:blue")

            ax2 = ax1.twinx()
            ax2.plot(voltage_set, current, "s--", label="Current [uA]", color="tab:red")
            if fit_ok_i:
                ax2.plot(v_fit, linear_model(v_fit, a_i, b_i), "r:", alpha=0.6)
            ax2.set_ylabel("Current [uA]", color="tab:red")

            max_diff = float(np.max(np.abs(voltage - voltage_set)))
            warn = max_diff > hv_tolerance
            ax1.set_title(f"Ch {ch_id:02d} (ΔV={max_diff:.1f}V{' ⚠️' if warn else ''})")
            ax1.grid(True, linestyle="--", alpha=0.5)
            fig.tight_layout()

            # --- Save ---
            out_path = os.path.join(base_dir, f"{base_name}_ch{ch_id:02d}.png")
            fig.savefig(out_path, dpi=150)
            plt.close(fig)
            saved_files[ch_id] = out_path
            print(f"✅ Saved → {out_path}")

            fit_v_str = f"y = {a_v:.3f}x + {b_v:.2f}" if fit_ok_v else "—"
            fit_i_str = f"y = {a_i:.5f}x + {b_i:.3f}" if fit_ok_i else "—"
            status = "⚠️" if warn else "✅"
            summary_rows.append([ch_id, fit_v_str, fit_i_str, f"{max_diff:.1f}", status])

        print(f"📁 Saved {len(saved_files)} HV ramp plots → {base_dir}")

        # Save summary table
        df_summary = pd.DataFrame(summary_rows, columns=["channel", "hv_fit", "i_fit", "max_dV", "status"])
        csv_out = csv_file.replace(".csv", "_hv_check.csv")
        df_summary.to_csv(csv_out, index=False)
        print(f"💾 HV ramp fit summary saved → {csv_out}")

        print("\n📊 HV Ramp Linear Fit Summary\n")
        print(tabulate(
            summary_rows,
            headers=["Ch", "HV meas fit", "Current fit", "Max ΔV [V]", "Status"],
            tablefmt="fancy_grid",
            stralign="center",
            numalign="right"
        ))

        return saved_files

    # ----------------------------------
    # COMBINED GRID MODE
    # ----------------------------------
    fig, axes = plt.subplots(4, 5, figsize=(16, 10), sharex=False)
    axes = axes.flatten()

    for idx, ch_id in enumerate(channels):
        ax = axes[idx]
        ch_data = df[df["channel"] == ch_id].sort_values("voltage_set")

        if ch_data.empty:
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.5)
            summary_rows.append([ch_id, "—", "—", "—", "❌ No data"])
            continue

        voltage_set = ch_data["voltage_set"].values
        voltage = ch_data["voltage"].values
        current = ch_data["current"].values

        # Fit lines
        def safe_fit(x, y):
            try:
                popt, _ = curve_fit(linear_model, x, y)
                return (*popt, True)
            except Exception:
                return (np.nan, np.nan, False)

        a_v, b_v, fit_ok_v = safe_fit(voltage_set, voltage)
        a_i, b_i, fit_ok_i = safe_fit(voltage_set, current)

        ax.plot(voltage_set, voltage, "o-", label="Measured V", color="tab:blue")
        if fit_ok_v:
            v_fit = np.linspace(min(voltage_set), max(voltage_set), 100)
            ax.plot(v_fit, linear_model(v_fit, a_v, b_v), "b--", alpha=0.6)
        ax2 = ax.twinx()
        ax2.plot(voltage_set, current, "s--", label="Current [uA]", color="tab:red")
        if fit_ok_i:
            ax2.plot(v_fit, linear_model(v_fit, a_i, b_i), "r:", alpha=0.6)

        max_diff = float(np.max(np.abs(voltage - voltage_set)))
        warn = max_diff > hv_tolerance

        ax.set_title(f"Ch {ch_id:02d} (ΔV={max_diff:.1f}V{' ⚠️' if warn else ''})", fontsize=9)
        ax.set_xlabel("HV Set [V]")
        ax.set_ylabel("Measured HV [V]", color="tab:blue")
        ax2.set_ylabel("Current [uA]", color="tab:red")
        ax.grid(True, linestyle="--", alpha=0.5)

        fit_v_str = f"y = {a_v:.3f}x + {b_v:.2f}" if fit_ok_v else "—"
        fit_i_str = f"y = {a_i:.5f}x + {b_i:.3f}" if fit_ok_i else "—"
        status = "⚠️" if warn else "✅"
        summary_rows.append([ch_id, fit_v_str, fit_i_str, f"{max_diff:.1f}", status])

    # Hide unused panels
    for j in range(len(channels), len(axes)):
        axes[j].axis("off")

    fig.suptitle(f"HV Ramp Validation — {os.path.basename(csv_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        plt.savefig(save, dpi=150)
        plt.close(fig)
        print(f"✅ Saved HV ramp plot to: {save}")
        saved_files[0] = save
    else:
        plt.show()

    print("\n📊 HV Ramp Linear Fit Summary\n")
    print(tabulate(
        summary_rows,
        headers=["Ch", "HV meas fit", "Current fit", "Max ΔV [V]", "Status"],
        tablefmt="fancy_grid",
        stralign="center",
        numalign="right"
    ))

    df_summary = pd.DataFrame(summary_rows, columns=["channel", "hv_fit", "i_fit", "max_dV", "status"])
    csv_out = csv_file.replace(".csv", "_hv_check.csv")
    df_summary.to_csv(csv_out, index=False)
    print(f"💾 HV ramp fit summary saved → {csv_out}")

    return saved_files


# ===============================================================
#  RATE MONITOR PLOT (CSV)
# ===============================================================

def plot_rate_monitor(csv_file: str, sharex=True, sharey=True):
    """
    Plot time evolution of event rates for all PMT channels.

    This function visualizes the rate monitor data (typically produced
    by long-term stability or threshold scans) as 4×5 subplots, where
    each panel corresponds to one PMT channel.
    The rate in each channel is plotted versus timestamp to help identify
    noise bursts, dead channels, or rate drifts over time.

    **Workflow:**
      1. Load the provided CSV file containing rate measurements.
      2. Convert timestamps to datetime objects for proper axis formatting.
      3. Create a 4×5 grid of subplots (one per channel, channels 1–19).
      4. Plot `rate_hz` (converted to kHz) vs. `timestamp` for each channel.
      5. Save the resulting figure as `<csv_file>.png` in the same directory.

    **Parameters**
    ----------
    csv_file : str
        Path to the CSV file containing time-dependent rate data.
        The file must contain at least the columns:
        - `timestamp` : UTC or local time of measurement
        - `channel`   : PMT channel number (1–19)
        - `rate_hz`   : Measured rate in Hz
    sharex : bool, optional
        If True, all subplots share the same X-axis (time).
        This makes time alignment between channels easier to compare.
    sharey : bool, optional
        If True, all subplots share the same Y-axis (rate).
        Useful for comparing absolute rate magnitudes across channels.

    **Output**
    -------
    A PNG file saved automatically in the same folder as the input CSV:
        `<csv_file>.png`

    **Notes**
    -----
    - Empty or missing channels are labeled as “(no data)”.
    - The Y-axis is shown in **kHz** for better readability.
    - The function is designed to display 19 channels (mPMT layout).
    - Grid lines and consistent formatting make long-term drifts easier to detect.
    """
    df = pd.read_csv(csv_file)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    fig, axes = plt.subplots(4, 5, figsize=(16, 10), sharex=sharex, sharey=sharey)
    axes = axes.flatten()

    for ch in range(1, 20):
        ax = axes[ch - 1]
        data = df[df["channel"] == ch]
        if data.empty:
            ax.set_title(f"Ch {ch:02d} (no data)")
            ax.grid(True, linestyle="--", alpha=0.5)
            continue

        ax.plot(data["timestamp"], data["rate_hz"] / 1000.0, "r.-")
        ax.set_title(f"Ch {ch:02d}")
        ax.set_ylabel("Rate [kHz]")
        ax.grid(True, linestyle="--", alpha=0.5)

    if len(axes) > 19:
        fig.delaxes(axes[19])

    fig.suptitle(f"Rate vs Time — {os.path.basename(csv_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    png_file = csv_file.replace(".csv", ".png")
    plt.savefig(png_file, dpi=150)
    print(f"✅ Plot saved → {png_file}")


def exp_model(V, a, b):
    """Exponential gain model (no constant term c)."""
    return a * np.exp(b * V)


def analyze_hv_scan(
        summary_csv: str,
        pedestal_file: str | None = None,
        pedestals: dict | None = None,
        channels=range(1, 20),
        target_gain=100,
        bin_edges=range(1, 800),
        fit_adc=None,
        use_gaussian=True,
        led_freq_hz=10_000
):
    """
    Analyze HV scan data: compute mean ADC vs HV, fit exponential response,
    and optionally extract Gaussian 1PE fits for each HV point.

    Returns
    -------
    pd.DataFrame
        Fit summary per channel including exponential and (optional) 1PE Gaussian fits.
    """
    import os
    import numpy as np
    import pandas as pd
    import matplotlib.pyplot as plt
    from scipy.optimize import curve_fit
    from tabulate import tabulate

    bin_width = bin_edges[1] - bin_edges[0]
    df_summary = pd.read_csv(summary_csv)
    if df_summary.empty:
        raise SystemExit(f"No HV scan data found in {summary_csv}")

    if pedestals is None:
        pedestals = {}
        pedestals_dict = load_pedestal_file(pedestal_file)
        for ch in channels:
            pedestals[ch] = pedestals_dict.get(ch, {"mean": 0.0})["mean"]

    gauss_fit_results = {ch: [] for ch in channels}
    fit_results = []

    print(f"\n⚙️ Analyzing HV scan from {summary_csv}")
    if fit_adc is not None:
        print("🔍 Performing per-HV Gaussian fits using fit_1pe_distribution()")

    # ------------------------------
    # Collect mean ADC per HV step
    # ------------------------------
    for _, row in df_summary.iterrows():
        hv_set = row["hv_set"]
        root_file = row["root_file"]
        if not os.path.exists(root_file):
            print(f"⚠️ Missing ROOT file: {root_file}")
            continue

        gauss_fits = {}
        if fit_adc is not None:
            gauss_fits = fit_1pe_distribution(root_file, bin_edges=bin_edges, bin_width=bin_width, pedestals=pedestals,
                                              verbose=True, led_freq_hz=led_freq_hz)
        if led_freq_hz > 0:
            timing_result = analyze_led_timing(root_file, led_freq_hz=led_freq_hz, pedestals=pedestals)

        for ch_id in channels:
            if fit_adc is not None and ch_id in gauss_fits:
                fit_res = gauss_fits[ch_id]
                if led_freq_hz > 0:
                    time_res = timing_result[ch_id]
                else:
                    time_res = {}
                entry = {"hv_set": hv_set}

                # copy ALL fields from fit_1pe_distribution as-is
                entry.update(fit_res)

                # optionally add timing
                entry["time_sigma_ns"] = float(time_res.get("time_sigma_ns", np.nan))
                entry["lambda_true"] = float(time_res.get("lambda_true", np.nan))

                gauss_fit_results[ch_id].append(entry)

    # ------------------------------
    # Plot and fit per channel
    # ------------------------------
    fig, axes = plt.subplots(4, 5, figsize=(16, 10))
    axes = axes.flatten()

    def exp_model(v, a, b):
        return a * np.exp(b * v)

    for idx, ch_id in enumerate(channels):
        ax = axes[idx]
        gauss_fits_ch = sorted(gauss_fit_results[ch_id], key=lambda x: x["hv_set"])

        if len(gauss_fits_ch) < 3:
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            fit_results.append({
                "channel": ch_id,
                "a": np.nan, "b": np.nan, "hv_nominal": np.nan,
                "r2_exp": np.nan,
                "pedestal_mean": pedestals.get(ch_id),
                "n_points": 0, "fit_success": False,
                "gauss_fits": []
            })
            continue

        voltages = np.array([d["hv_set"] for d in gauss_fits_ch])
        # ✅ If Gaussian fits available, use mean_fit; otherwise use mean_adc
        if use_gaussian == True:
            means = np.array([d["fit_mu"] for d in gauss_fits_ch])
        else:
            means = np.array([d["data_mean"] for d in gauss_fits_ch])

        # --- Fit exponential model ---
        fit_ok = False
        r2_exp = np.nan
        try:
            # --- Clean data ---
            voltages = np.asarray(voltages)
            means = np.asarray(means)

            mask = (np.isfinite(means))

            voltages_f = voltages[mask]
            means_f = means[mask]
            log_means = np.log(means_f)

            coeffs = np.polyfit(voltages_f, log_means, 1)
            b = coeffs[0]
            a = np.exp(coeffs[1])
            fit_ok = True
            y_pred = exp_model(voltages_f, a, b)
            ss_res = np.sum((means_f - y_pred) ** 2)
            ss_tot = np.sum((means_f - np.mean(means_f)) ** 2)
            r2_exp = 1 - (ss_res / ss_tot) if ss_tot > 0 else np.nan
        except Exception as e:
            a, b = np.nan, np.nan
            print(f"⚠️ Exp fit failed for Ch {ch_id:02d}: {e}")

        # --- Plot results ---
        ax.scatter(voltages, means, color="blue", label="Data")
        if fit_ok:
            v_fit = np.linspace(min(voltages), max(voltages), 200)
            gain_fit = exp_model(v_fit, a, b)
            ax.plot(v_fit, gain_fit, "r--", label=f"Fit: a={a:.2e}, b={b:.3e}")
            try:
                hv_nominal = np.log(target_gain / a) / b
                if hv_nominal > 1500 or hv_nominal < 0:
                    hv_nominal = np.nan
            except Exception:
                hv_nominal = np.nan

            ax.axvline(hv_nominal, color="g", linestyle=":", label=f"HVₙₒₘ ≈ {hv_nominal:.0f}V")
        else:
            hv_nominal = np.nan

        ax.set_xlabel("HV [V]")
        ax.set_ylabel("Mean ADC (pedestal-corrected)")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.legend(fontsize=6)
        ax.set_title(f"Ch {ch_id:02d}")

        # --- Store results ---
        fit_results.append({
            "channel": ch_id,
            "a": a, "b": b,
            "hv_nominal": hv_nominal,
            "r2_exp": r2_exp,
            "pedestal_mean": pedestals.get(ch_id),
            "n_points": len(means),
            "fit_success": fit_ok,
            "gauss_fits": gauss_fits_ch  # ✅ includes all individual Gaussian fits per HV
        })

    plt.suptitle(
        f"HV Gain Curves (Target Gain = {target_gain})\nSummary: {os.path.basename(summary_csv)}",
        y=0.99,
    )
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    output_png = summary_csv.replace(".csv", "_gain_fit.png")
    plt.savefig(output_png, dpi=200)
    print(f"✅ Gain curves saved → {output_png}")

    # ------------------------------
    # Save and print fit summary
    # ------------------------------
    df_fit = pd.DataFrame(fit_results)
    output_csv = summary_csv.replace(".csv", "_fit_results.csv")
    df_fit.to_csv(output_csv, index=False)
    print(f"💾 Fit parameters saved → {output_csv}")

    # ------------------------------
    # Summary Table
    # ------------------------------
    table = []
    for _, row in df_fit.iterrows():
        hv_str = f"{row['hv_nominal']:.0f}" if row["fit_success"] and not np.isnan(row["hv_nominal"]) else "—"
        table.append([
            int(row["channel"]),
            f"{row['a']:.2e}" if not np.isnan(row['a']) else "—",
            f"{row['b']:.3e}" if not np.isnan(row['b']) else "—",
            hv_str,
            f"{row['r2_exp']:.3f}" if not np.isnan(row['r2_exp']) else "—",
            f"{row['pedestal_mean']:.2f}",
            int(row["n_points"]),
            "✅" if row["fit_success"] else "❌",
        ])

    print("\n📊 HV Scan Fit Summary\n")
    print(tabulate(
        table,
        headers=["Ch", "a", "b", "HV_nom [V]", "R²_exp", "Ped μ", "Points", "Fit OK"],
        tablefmt="fancy_grid",
        stralign="center",
        numalign="right"
    ))

    return df_fit


def plot_hv_scan_analysis_per_channel(
        df_fit: pd.DataFrame,
        summary_csv: str,
        pedestal_file: str | None = None,
        pedestals: dict | None = None,
        channels=range(1, 20),
        target_gain: float = 100.0,
        bin_edges: np.ndarray | None = None,
        output_dir: str | None = None,
        show: bool = False,
        save_histograms: bool = True,
        led_freq_hz: int = 0,
        use_gaussian=True
):
    """
    Generate one figure per channel:
      - Left: ADC histograms for each HV step (with 1PE Gaussian fits + R² values)
      - Right: Mean ADC vs HV using exponential fit from df_fit.
    Optionally also generate a second plot containing only ADC histograms
    for each HV step with Gaussian fit overlays and fit-range shading.
    """

    import os
    import numpy as np
    import matplotlib.pyplot as plt
    import pandas as pd
    import uproot
    from helper_functions import load_pedestal_file, exp_model

    df_summary = pd.read_csv(summary_csv)
    if df_summary.empty:
        raise SystemExit(f"No HV scan rows in {summary_csv}")

    if pedestals is None:
        pedestals = {}
        pedestals_dict = load_pedestal_file(pedestal_file)
        for ch in channels:
            pedestals[ch] = pedestals_dict.get(ch, {"mean": 0.0})["mean"]

    base_dir = output_dir or os.path.dirname(os.path.abspath(summary_csv))
    os.makedirs(base_dir, exist_ok=True)

    tree_name = "pmt_events"
    cache = {}
    saved_files: dict[int, str] = {}

    # --- Cache ROOT file ADC arrays ---
    for _, row in df_summary.iterrows():
        root_path = row["root_file"]
        if not os.path.exists(root_path) or root_path in cache:
            continue
        with uproot.open(root_path) as f:
            if tree_name not in f:
                cache[root_path] = {}
                continue
            t = f[tree_name]
            ch = t["channel"].array(library="np") + 1
            adc = t["adc"].array(library="np")
            pmt_time = t["pmt_time"].array(library="np")
            led_mask, signal_phase = get_led_mask(pmt_time, led_freq_hz)
            ch = ch[led_mask]
            adc = adc[led_mask]
            cache[root_path] = {cid: adc[ch == cid] for cid in range(1, 20)}

    # ================================
    # Per-channel analysis
    # ================================
    for ch_id in channels:
        ped_mean = pedestals.get(ch_id)

        # Extract Gaussian fits
        row_fit = df_fit[df_fit["channel"] == ch_id]

        # gauss_fits is a list of dicts per HV
        gauss_data = []
        if not row_fit.empty and "gauss_fits" in row_fit.columns:
            gauss_data = row_fit.iloc[0]["gauss_fits"]
            # convert string->list if loaded from CSV
            if isinstance(gauss_data, str):
                try:
                    gauss_data = eval(gauss_data)
                except Exception:
                    gauss_data = []

        # Gather ADC arrays per HV
        hv_adc = []
        hv_points = []

        for _, row in df_summary.iterrows():
            hv_set = float(row["hv_set"])
            root_path = row["root_file"]
            adc_arr = cache.get(root_path, {}).get(ch_id)
            # for mean vs HV
            matched = next((g for g in gauss_data if g["hv_set"] == hv_set), {})
            if use_gaussian:
                value = matched.get("fit_mu")
            else:
                value = matched.get("data_mean")
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = np.nan
            if np.isfinite(value):
                hv_points.append((hv_set, value))

            if adc_arr is None or len(adc_arr) < 10:
                continue

            adc_corr = adc_arr - ped_mean
            hv_adc.append((hv_set, adc_corr))

        if not hv_adc:
            continue

        # ============================================================
        # MAIN PLOT (Top: Histograms, Bottom: Exp Fit)
        # ============================================================

        fig, (axL, axR) = plt.subplots(2, 1, figsize=(5, 7), gridspec_kw={"hspace": 0.28})

        # Top PANEL — histograms w/ Gaussian fits
        for hv_set, adc_vals in sorted(hv_adc, key=lambda x: x[0]):
            label_text = f"{hv_set:.0f} V"
            g_fit = next((g for g in gauss_data if g["hv_set"] == hv_set), None)

            if g_fit and np.isfinite(g_fit.get("r2_fit", np.nan)):
                label_text += f" (μ={g_fit['mean_fit']:.1f}, R²={g_fit['r2_fit']:.2f})"

            axL.hist(adc_vals, bins=bin_edges, histtype="step", linewidth=1.4, label=label_text)

            # Overlay Gaussian
            if g_fit and np.isfinite(g_fit.get("fit_mu", np.nan)):
                mu = g_fit.get("fit_mu")
                sigma = g_fit.get("fit_sigma", 0)
                amp = g_fit.get("fit_amplitude", 0)
                xx = np.linspace(mu - 4 * sigma, mu + 4 * sigma, 200)
                yy = amp * np.exp(-0.5 * ((xx - mu) / sigma) ** 2)
                axL.plot(xx, yy, "r--", lw=0.8, alpha=0.6)

        axL.set_title(f"Ch {ch_id:02d} — ADC distributions (+fits)")
        axL.set_xlabel("ADC (pedestal-corrected)")
        axL.set_ylabel("Counts")
        axL.tick_params(axis="y", labelrotation=45)
        axL.axvline(
            target_gain,
            color="k",
            linestyle=":",
            linewidth=1.5,
            label=f"Target gain = {target_gain}"
        )

        axL.grid(True, linestyle="--", alpha=0.2)
        axL.legend(fontsize=7, ncol=2, loc="upper right")
        axL.set_xlim(-target_gain, 8 * target_gain)

        # Bottom PANEL — mean ADC vs HV (exp fit)
        if hv_points:
            hv_arr = np.array([v for v, _ in hv_points])
            mu_arr = np.array([m for _, m in hv_points])
            axR.scatter(hv_arr, mu_arr, s=18, label="Mean ADC")

            if not row_fit.empty and row_fit.iloc[0]["fit_success"]:
                a = row_fit.iloc[0]["a"]
                b = row_fit.iloc[0]["b"]
                hv_nom = row_fit.iloc[0]["hv_nominal"]
                r2_exp = row_fit.iloc[0].get("r2_exp", np.nan)

                v_fit = np.linspace(hv_arr.min(), hv_arr.max(), 300)
                axR.plot(v_fit, exp_model(v_fit, a, b), "r--",
                         lw=1.5, label=f"Exp fit (R²={r2_exp:.3f})")

                if np.isfinite(hv_nom):
                    axR.axvline(hv_nom, color="g", ls=":", lw=1.2,
                                label=f"HVₙₒₘ≈{hv_nom:.0f} V")
            else:
                axR.text(0.5, 0.5, "No valid fit", transform=axR.transAxes,
                         ha="center", va="center", color="red")

            axR.set_title(f"Ch {ch_id:02d} — Gain vs HV")
            axR.set_xlabel("HV set [V]")
            axR.set_ylabel("Mean ADC (ped-corr)")
            axR.grid(True, linestyle="--", alpha=0.4)
            axR.legend(fontsize=7, loc="upper left")

        fig.suptitle(f"HV scan — {os.path.basename(summary_csv)} — Ch {ch_id:02d}",
                     y=0.98, fontsize=9)
        plt.tight_layout(rect=[0, 0, 1, 0.96])

        # Save main figure
        if show or output_dir is None:
            plt.show()
        else:
            base = os.path.splitext(os.path.basename(summary_csv))[0]
            out_path = os.path.join(base_dir, f"{base}_ch{ch_id:02d}.png")
            fig.savefig(out_path, dpi=180)
            plt.close(fig)
            saved_files[ch_id] = out_path
            print(f"✅ Saved → {out_path}")

        # ============================================================
        # OPTIONAL: HISTOGRAM-ONLY MULTI-PANEL FIGURE
        # ============================================================
        if save_histograms:
            hv_sorted = sorted(hv_adc, key=lambda x: x[0])
            n = len(hv_sorted)
            ncols = 1 if n <= 12 else 2
            nrows = (n + ncols - 1) // ncols

            fig_h, axes = plt.subplots(
                nrows, ncols,
                figsize=(6 * ncols, 2.4 * nrows),
                sharex=False,
                sharey=False
            )
            axes = np.array(axes).reshape(nrows, ncols)

            for idx, (hv_set, adc_vals) in enumerate(hv_sorted):
                r = idx // ncols
                c = idx % ncols
                ax = axes[r, c]

                g_fit = next((g for g in gauss_data if g["hv_set"] == hv_set), None)

                ax.hist(adc_vals, bins=bin_edges, alpha=0.5, color="steelblue", label="Data")

                if g_fit:
                    mu = g_fit.get("fit_mu", np.nan)
                    sigma = abs(g_fit.get("fit_sigma", np.nan))
                    amp = g_fit.get("fit_amplitude", np.nan)

                    data_mean = g_fit.get("data_mean", np.nan)
                    cog_mean = g_fit.get("cog_mean", np.nan)
                    r2 = g_fit.get("fit_r2", np.nan)

                    # --- Gaussian ---
                    if np.isfinite(mu) and np.isfinite(sigma) and np.isfinite(amp):
                        xx = np.linspace(mu - 4 * sigma, mu + 4 * sigma, 200)
                        yy = amp * np.exp(-0.5 * ((xx - mu) / sigma) ** 2)
                        ax.plot(xx, yy, "r--", lw=1.2, label="Gaussian fit")

                    # --- Vertical markers ---
                    if np.isfinite(data_mean):
                        ax.axvline(data_mean, color="blue", linestyle=":", linewidth=1.3, label="data mean")

                    if np.isfinite(cog_mean):
                        ax.axvline(cog_mean, color="green", linestyle="-.", linewidth=1.3, label="CoG")

                    x_left = g_fit.get("fit_left_cut")
                    if isinstance(x_left, (int, float)) and np.isfinite(x_left):
                        ax.axvline(x_left, color="orange", linestyle="--", linewidth=1.3, label="fit cut")

                    # --- Title ---
                    info = (
                        f"HV={hv_set:.0f} V\n"
                        f"μ_fit={mu:.1f}, σ_fit={sigma:.1f}, R²={r2:.2f}\n"
                        f"μ_data={data_mean:.1f}, μ_cog={cog_mean:.1f}"
                    )

                else:
                    info = f"HV={hv_set:.0f} V\n(no fit)"

                ax.set_title(info, fontsize=9)
                ax.grid(True, linestyle="--", alpha=0.4)

                # --- Clean legend (remove duplicates) ---
                handles, labels = ax.get_legend_handles_labels()
                unique = dict(zip(labels, handles))
                if unique:
                    ax.legend(unique.values(), unique.keys(), fontsize=7)

            # Remove empty axes
            for i in range(n, nrows * ncols):
                r = i // ncols
                c = i % ncols
                axes[r, c].axis("off")

            fig_h.suptitle(f"ADC Histograms — Ch {ch_id:02d}", y=0.99)
            plt.tight_layout(rect=[0, 0, 1, 0.97])

            base = os.path.splitext(os.path.basename(summary_csv))[0]
            out_h_path = os.path.join(base_dir, f"hvscan_{base}_ch{ch_id:02d}_hists.png")
            fig_h.savefig(out_h_path, dpi=180)
            plt.close(fig_h)
            print(f"🧾 Saved histogram-only plot → {out_h_path}")

    return saved_files


def plot_led_pulser_rates(results: dict, output_dir: str) -> dict:
    import os
    import numpy as np
    import matplotlib.pyplot as plt

    os.makedirs(output_dir, exist_ok=True)
    out_dict = {}

    for ch, ch_entry in results.items():
        ch = int(ch)
        meas = ch_entry.get("measurements", [])

        if not meas:
            print(f"⚠️ Channel {ch}: no measurements, skipping plot.")
            continue

        pts = []
        lambda_pts = []

        for m in meas:
            att = m.get("attenuation")
            rate = m.get("rate")

            stats = m.get("stats") or {}
            lam = stats.get("lambda_true")

            if isinstance(att, (int, float)) and isinstance(rate, (int, float)):
                pts.append((att, rate))

                if isinstance(lam, (int, float)) and np.isfinite(lam):
                    lambda_pts.append((att, lam))
                else:
                    lambda_pts.append((att, np.nan))

                if not pts:
                    print(f"⚠️ Channel {ch}: no numeric data, skipping plot.")
                    continue

        # sort
        pts.sort(key=lambda x: x[0])
        lambda_pts.sort(key=lambda x: x[0])

        attenuations = [p[0] for p in pts]
        rates = [p[1] for p in pts]
        lambdas = [p[1] for p in lambda_pts]

        # --- plot ---
        fig, ax1 = plt.subplots(figsize=(10, 6))

        ax1.plot(attenuations, rates, "-o", linewidth=2, markersize=5, label="Rate [Hz]")
        ax1.set_xlabel("LED Attenuation")
        ax1.set_ylabel("Rate [Hz]")
        ax1.grid(True, linestyle="--", alpha=0.5)

        # --- second axis for lambda ---
        ax2 = ax1.twinx()
        ax2.plot(attenuations, lambdas, "--s", markersize=5, label="λ (occupancy)")
        ax2.set_ylabel("Poisson λ")

        # --- combined legend ---
        lines_1, labels_1 = ax1.get_legend_handles_labels()
        lines_2, labels_2 = ax2.get_legend_handles_labels()
        ax1.legend(lines_1 + lines_2, labels_1 + labels_2, loc="best")

        plt.title(f"Channel {ch} — Rate & λ vs LED Attenuation")

        # save
        fname = f"led_pulser_att_vs_rate_ch{ch:02d}.png"
        fpath = os.path.join(output_dir, fname)
        plt.savefig(fpath, dpi=160)
        plt.close()

        print(f"📈 Saved channel {ch} plot → {fpath}")
        out_dict[ch] = fpath

    return out_dict


def plot_sigma_vs_lambda(results: dict, output_dir: str) -> dict:
    """
    Plot timing sigma vs Poisson lambda for each channel.

    Returns
    -------
    dict
        { channel_number : filepath_to_png }
    """
    import os
    import numpy as np
    import matplotlib.pyplot as plt

    os.makedirs(output_dir, exist_ok=True)
    out_dict = {}

    for ch, ch_entry in results.items():
        ch = int(ch)
        meas = ch_entry.get("measurements", [])

        if not meas:
            print(f"⚠️ Channel {ch}: no measurements")
            continue

        pts = []
        for m in meas:
            if m.get("stats") is not None:
                stats = m.get("stats")
                lam = stats.get("lambda_true")
                sigma = stats.get("time_sigma_ns")

                if np.isfinite(lam) and np.isfinite(sigma):
                    pts.append((lam, sigma))

        if len(pts) < 2:
            print(f"⚠️ Channel {ch}: not enough valid points")
            continue

        # sort by lambda
        pts.sort(key=lambda x: x[0])
        lam_arr = np.array([p[0] for p in pts])
        sigma_arr = np.array([p[1] for p in pts])

        # --- Plot ---
        plt.figure(figsize=(6, 4))
        plt.plot(lam_arr, sigma_arr, "-o", markersize=5)

        plt.title(f"Channel {ch:02d} — Timing σ vs λ")
        plt.xlabel("Poisson λ (occupancy)")
        plt.ylabel("Timing σ [ns]")

        plt.grid(alpha=0.3)

        # Save
        fname = f"sigma_vs_lambda_ch{ch:02d}.png"
        fpath = os.path.join(output_dir, fname)
        plt.savefig(fpath, dpi=150)
        plt.close()

        print(f"📊 Saved σ vs λ plot → {fpath}")
        out_dict[ch] = fpath

    return out_dict


def flush_event_buffers(rc, data_port=5566, data_folder=None, duration=6):
    import time
    import os
    from helper_functions import start_event_receiver, stop_event_receiver

    if data_folder is None:
        data_folder = os.getenv("DATA_FOLDER", ".")

    print("\n🧹 Flushing internal data buffers...")

    proc = None
    logfile = None
    output_file = None

    try:
        # -----------------------------
        # Start evbuilder (safe)
        # -----------------------------
        try:
            if rc.process_isrunning():
                rc.process_evbuilder_stop()
                time.sleep(0.5)
        except Exception as e:
            print(f"⚠️ evbuilder pre-stop failed: {e}")

        rc.process_evbuilder_start(host=os.getenv("HOSTIP"), data_port=data_port)
        time.sleep(0.2)

        # -----------------------------
        # Start receiver
        # -----------------------------
        tmp_filename = "flush_buffer_tmp.root"
        proc, logfile, output_file = start_event_receiver(
            filename=tmp_filename,
            data_folder=data_folder,
            port=data_port
        )

        time.sleep(duration)

    finally:
        # -----------------------------
        # ALWAYS stop receiver first
        # -----------------------------
        try:
            if proc is not None:
                stop_event_receiver(proc)
                print("🛑 Receiver stopped")
        except Exception as e:
            print(f"⚠️ Receiver stop failed: {e}")

        # -----------------------------
        # Then stop evbuilder
        # -----------------------------
        try:
            rc.process_evbuilder_stop()
            print("🛑 Evbuilder stopped")
        except Exception as e:
            if "No process running" in str(e):
                print("⚠️ Evbuilder already stopped")
            else:
                print(f"⚠️ Evbuilder stop failed: {e}")

        # -----------------------------
        # Cleanup temp files
        # -----------------------------
        try:
            if output_file and os.path.exists(output_file):
                os.remove(output_file)
            if logfile and os.path.exists(logfile):
                os.remove(logfile)
        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")

    print("✅ Buffer flush complete.\n")


def collect_data(
        rc,
        channels,
        run_filename,
        run_dir,
        wait_for_data_s=60,
        desc="Run",
        flush_buff=True,
        data_port=5566
):
    import time
    import os
    import traceback

    warnings.filterwarnings("ignore", category=TqdmExperimentalWarning)

    proc = None
    logfile = None
    output_file = None

    # ------------------------------
    # Optional buffer flush
    # ------------------------------
    if flush_buff:
        try:
            flush_event_buffers(rc, data_port=data_port)
        except Exception as e:
            print(f"⚠️ Buffer flush failed: {e}")
            traceback.print_exc()

    print("---------------------------------------------------")
    print("🚀 Starting data acquisition...")

    try:
        # ------------------------------
        # Ensure evbuilder clean state
        # ------------------------------
        try:
            if rc.process_isrunning():
                rc.process_evbuilder_stop()
                time.sleep(0.5)
        except Exception as e:
            print(f"⚠️ Pre-stop evbuilder failed: {e}")

        # ------------------------------
        # Start evbuilder
        # ------------------------------
        rc.process_evbuilder_start(host=os.getenv("HOSTIP"), data_port=data_port)
        time.sleep(0.5)

        # ------------------------------
        # Start receiver
        # ------------------------------
        proc, logfile, output_file = start_event_receiver(
            filename=run_filename,
            data_folder=run_dir,
            port=data_port
        )
        time.sleep(0.5)

        # ------------------------------
        # Enable channels
        # ------------------------------
        try:
            rc.enable_channel(channels=channels)
        except Exception as e:
            print(f"⚠️ enable_channel failed: {e}")

        time.sleep(0.2)

        # ------------------------------
        # Data taking
        # ------------------------------
        print(f"⏳ Collecting data for {wait_for_data_s} s...")
        for _ in tqdm(range(wait_for_data_s), desc=desc, colour="green"):
            time.sleep(1)

    except Exception as e:
        print("\n❌ ERROR during data acquisition:")
        traceback.print_exc()

    finally:
        print("🛑 Stopping data acquisition...")

        # ------------------------------
        # Disable channels (safe)
        # ------------------------------
        try:
            rc.disable_channel(all_channels=True)
        except Exception as e:
            print(f"⚠️ disable_channel failed: {e}")

        time.sleep(0.5)

        # ------------------------------
        # ALWAYS stop receiver FIRST
        # ------------------------------
        try:
            if proc is not None:
                stop_event_receiver(proc)
                print("🛑 Receiver stopped")
        except Exception as e:
            print(f"⚠️ Receiver stop failed: {e}")

        time.sleep(0.5)

        # ------------------------------
        # Then stop evbuilder (safe)
        # ------------------------------
        try:
            rc.process_evbuilder_stop()
            print("🛑 Evbuilder stopped")
        except Exception as e:
            if "No process running" in str(e):
                print("⚠️ Evbuilder already stopped")
            else:
                print(f"⚠️ Evbuilder stop failed: {e}")

        print(f"✅ Run finished → {output_file}")
        print("---------------------------------------------------")

    return output_file


def plot_adc_histogram(ax, adc_corr, pedestal_mean, bin_edges, target_gain, target_threshold_pe):
    """
    Make a nice-looking histogram for a single channel.
    Adds target gain and threshold*gain vertical lines.
    """

    import numpy as np
    import matplotlib.pyplot as plt

    # --- pretty color palette ---
    color = "#4682B4"  # steelblue (nice, deep)
    vline_gain_color = "#D62728"  # red
    vline_thr_color = "#2CA02C"  # green

    # --- calculate threshold ADC ---
    thr_adc = target_threshold_pe * target_gain

    # --- histogram ---
    ax.hist(
        adc_corr,
        bins=bin_edges,
        density=False,
        alpha=0.55,
        color=color,
        edgecolor="none"  # <--- remove ugly borders
    )

    # --- vertical line: target gain ---
    ax.axvline(
        target_gain,
        color=vline_gain_color,
        linestyle="--",
        linewidth=2,
        label=f"Target Gain = {target_gain:.1f} ADC"
    )

    # --- vertical line: threshold * gain ---
    ax.axvline(
        thr_adc,
        color=vline_thr_color,
        linestyle="-.",
        linewidth=2,
        label=f"Thr = {target_threshold_pe} × Gain = {thr_adc:.1f} ADC"
    )

    # --- cosmetics ---
    ax.set_xlabel("ADC (pedestal-corrected)")
    ax.set_ylabel("Counts")
    ax.set_title(f"ADC Distribution (Mean = {np.mean(adc_corr):.1f})")
    ax.grid(True, linestyle=":", alpha=0.35)

    ax.legend(fontsize=8)


def threshold_left(adc_values, bin_width=1, frac=0.10):
    """
    Compute the left-side threshold where the histogram
    first exceeds frac × max_amplitude, using bin_width
    instead of a fixed number of bins.

    Parameters
    ----------
    adc_values : array-like
        ADC samples.
    bin_width : float
        Width of histogram bins in ADC units.
    frac : float
        Fraction of max amplitude (default = 0.10 → 10%).

    Returns
    -------
    float or None
        ADC value at rising 10% threshold.
    """
    adc = np.asarray(adc_values, dtype=float)
    if adc.size == 0:
        return None

    # Determine bin edges from min/max ADC with given bin_width
    adc_min = np.min(adc)
    adc_max = np.max(adc)

    edges = np.arange(adc_min, adc_max + bin_width, bin_width)
    hist, edges = np.histogram(adc, bins=edges)

    # --- handle empty histograms ---
    if hist is None or len(hist) == 0:
        return None

    max_amp = np.max(hist)
    if max_amp <= 0:
        return None

    target = frac * max_amp

    # Search rising edge from LEFT → RIGHT
    for i in range(len(hist)):
        if hist[i] >= target:
            return edges[i]  # left edge of this bin

    return None


def plot_hv_monitoring_per_channel(test_json, save_dir=None):
    """
    Plot per-channel HV monitoring:
      - Measured voltage vs HV set
      - Measured current vs HV set
      - Annotate each point with status/alarm

    Parameters
    ----------
    test_json : dict
        JSON structure containing "monitoring": [{hv_set, hv_mon: [...]}]
    save_dir : str or None
        If provided, each channel is saved to this folder.
        If None, show interactive plots instead.

    Returns
    -------
    dict
        {channel: filepath}
    """
    monitoring = test_json.get("monitoring", [])
    if not monitoring:
        print("⚠ No monitoring data found in JSON.")
        return {}

    # Storage for output files
    saved = {}

    # All channels present in monitoring data
    channels = sorted(set(ch["channel"] for block in monitoring for ch in block["hv_mon"]))

    # Build per-channel data
    data = {ch: {"hv_set": [], "voltage": [], "current": [], "status": [], "alarm": []}
            for ch in channels}

    for block in monitoring:
        hv_set = block.get("hv_set")
        for chmon in block.get("hv_mon", []):
            ch_id = chmon["channel"]
            data[ch_id]["hv_set"].append(hv_set)
            data[ch_id]["voltage"].append(chmon.get("voltage"))
            data[ch_id]["current"].append(chmon.get("current"))
            data[ch_id]["status"].append(chmon.get("status", ""))
            data[ch_id]["alarm"].append(chmon.get("alarm", ""))

    # Create output directory
    if save_dir is not None:
        os.makedirs(save_dir, exist_ok=True)

    # Plot for each channel
    for ch in channels:
        hv_set = data[ch]["hv_set"]
        voltage = data[ch]["voltage"]
        current = data[ch]["current"]
        status = data[ch]["status"]
        alarm = data[ch]["alarm"]

        fig, ax1 = plt.subplots(figsize=(6, 4))

        # ---- Voltage plot (left axis) ----
        ax1.plot(hv_set, voltage, "o-", color="tab:blue", label="Measured Voltage [V]")
        ax1.set_xlabel("HV Set [V]")
        ax1.set_ylabel("Voltage [V]", color="tab:blue")
        ax1.tick_params(axis="y", labelcolor="tab:blue")
        ax1.grid(True, linestyle="--", alpha=0.4)

        # ---- Current plot (right axis) ----
        ax2 = ax1.twinx()
        ax2.plot(hv_set, current, "s--", color="tab:red", label="Current [mA]")
        ax2.set_ylabel("Current [mA]", color="tab:red")
        ax2.tick_params(axis="y", labelcolor="tab:red")

        # ---- Annotate status ----
        for xs, ys, st, alm in zip(hv_set, voltage, status, alarm):
            txt = st if alm == "none" else f"{st} ({alm})"
            ax1.text(xs, ys, txt, fontsize=7, ha="left", va="bottom")

        fig.suptitle(f"Channel {ch} — HV Monitoring")

        fig.tight_layout()

        # Save or show
        if save_dir is not None:
            outpath = os.path.join(save_dir, f"hv_monitoring_ch{ch:02d}.png")
            fig.savefig(outpath, dpi=150)
            saved[ch] = outpath
            plt.close(fig)
        else:
            plt.show()

    return saved


def exp_decay(t, a, b, c):
    """Simple exponential decay model."""
    return a * np.exp(-b * t) + c


def plot_dark_rate_channel(t, y, popt, threshold_hz, ch, output_dir):
    """Generate, save and report a dark rate plot + histogram for a single channel."""

    os.makedirs(output_dir, exist_ok=True)
    plot_file = os.path.join(output_dir, f"ch{ch:02d}_dark_rate.png")

    fig, axes = plt.subplots(1, 1, figsize=(6, 6), sharex=False)
    ax1 = axes

    # Thin transparent line instead of markers
    ax1.plot(t, y, "-", linewidth=1, alpha=0.5, label="Data")

    # Fit curve if available — also thin + transparent
    if not any(np.isnan(popt)):
        t_fit = np.linspace(t[0], t[-1], 200)
        ax1.plot(
            t_fit,
            exp_decay(t_fit, *popt),
            "-",
            linewidth=1,
            alpha=0.6,
            label="Fit"
        )

    # Threshold line (kept solid, thin)
    ax1.axhline(threshold_hz, color="r", ls="--", linewidth=1, label=f"Threshold = {threshold_hz} Hz")

    # Cosmetics
    ax1.set_ylabel("Rate [Hz]")
    ax1.set_xlabel("Time [s]")
    ax1.set_title(f"Dark rate evolution – Channel {ch}")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(bottom=0)

    plt.tight_layout()

    # --- SAVE ---
    fig.savefig(plot_file, dpi=150)
    plt.close(fig)  # 🔥 VERY IMPORTANT in loops

    print(f"💾 Saved dark-rate plot for Ch {ch:02d} → {plot_file}")

    return plot_file


def extract_channel_parameters(test_results, output_dir=None):
    """
    Unified extractor working with both:
      - global test_results.json (multiple channels)
      - per-channel JSON (your new format)
      - ReportGenerator internal list

    Produces:
        channels_data[ch] = {
            test_name: { raw data ... },
            flat fields ... (hv_nominal, adc_sigma, thr_eq_mu, etc.)
        }
    """

    channels_data = {}

    # ------------------------------------------------------------
    # Normalize input -> list of (test_name, test_block)
    # ------------------------------------------------------------
    if isinstance(test_results, dict):
        iter_tests = [(name, block) for name, block in test_results.items()]

    elif isinstance(test_results, list):
        iter_tests = []
        for t in test_results:
            name = t.get("test_type", "unknown_test")
            iter_tests.append((name, t))

    else:
        raise TypeError("test_results must be dict or list")

    # ------------------------------------------------------------
    # MAIN EXTRACTION LOOP
    # ------------------------------------------------------------
    for test_name, block in iter_tests:

        # Skip irrelevant global fields
        if test_name in ("logs", "monitoring"):
            continue

        # --------------------------------------------------------
        # CASE A: OLD FORMAT (test_block["results"] exists)
        #        hv_scan_test → results → { "1": {...}, ... }
        # --------------------------------------------------------
        if "results" in block:

            for ch_str, ch_data in block["results"].items():

                try:
                    ch = int(ch_str)
                except:
                    continue

                # create channel container
                if ch not in channels_data:
                    channels_data[ch] = {}

                # ---------- COMMUNICATION TEST ----------
                if test_name == "communication_test":
                    reg = ch_data.get("mon_registers", {})
                    channels_data[ch]["comm_temperature"] = reg.get("T")
                    channels_data[ch]["comm_voltage_measured"] = reg.get("V")
                    channels_data[ch]["comm_voltage_set"] = reg.get("Vset")
                    channels_data[ch]["comm_current"] = reg.get("I")
                    channels_data[ch]["comm_threshold"] = reg.get("threshold")

                # ---------- THRESHOLD SCAN + EQUALISATION ----------
                if test_name == "threshold_scan_and_equalisation":
                    init = ch_data.get("initial_scan", {})
                    eq = ch_data.get("equalised_scan", {})
                    channels_data[ch]["thr_init_pos"] = init.get("mu")
                    channels_data[ch]["thr_init_fwhm"] = init.get("fwhm")
                    channels_data[ch]["thr_init_sigma"] = init.get("sigma")
                    channels_data[ch]["thr_eq_pos"] = eq.get("mu")
                    channels_data[ch]["thr_discr_mv"] = ch_data.get("new_discriminator_mv")

                # ---------- HV RAMP TEST ----------
                if test_name == "hv_ramp_test":
                    channels_data[ch]["hv_ramp_ok"] = ch_data.get("hv_ok")

                # ---------- PULSER TEST ----------
                if test_name == "pedestal_test":
                    channels_data[ch]["adc_pedestal"] = ch_data.get("mu")
                    channels_data[ch]["adc_sigma"] = ch_data.get("sigma")
                    channels_data[ch]["pulser_r2"] = ch_data.get("r2")
                    channels_data[ch]["pulser_amplitude"] = ch_data.get("amplitude")

                # ---------- HV SCAN TEST ----------
                if test_name == "hv_scan_test":
                    channels_data[ch]["hv_nominal"] = ch_data.get("hv_nominal")
                    channels_data[ch]["hv_scan_fit_ok"] = ch_data.get("fit_success")
                    channels_data[ch]["threshold_mv"] = ch_data.get("threshold_mv")

                # ---------- LED PULSER ----------
                if test_name == "led_pulser_test":
                    channels_data[ch]["led_max_rate"] = ch_data.get("max_rate")
                    channels_data[ch]["led_min_rate"] = ch_data.get("min_rate")

                # ---------- DATA TAKING RUN ----------
                if test_name == "data_taking_run":
                    g = ch_data.get("gauss_fit") or {}  # safe even if None
                    channels_data[ch]["run_mu"] = g.get("fit_mu")
                    channels_data[ch]["run_sigma"] = g.get("fit_sigma")
                    channels_data[ch]["run_mean"] = g.get("data_mean")
                    channels_data[ch]["run_std"] = g.get("data_std")
                    channels_data[ch]["sigma_ns"] = ch_data.get("sigma_ns")
                    channels_data[ch]["run_voltage"] = ch_data.get("voltage")

                # ---------- DARK RUN ----------
                if test_name == "dark_run":
                    channels_data[ch]["dark_rate"] = ch_data.get("rate_mean")
                    channels_data[ch]["dark_rate_init"] = ch_data.get("rate_init")
                    channels_data[ch]["dark_rate_final"] = ch_data.get("rate_final")
    # ------------------------------------------------------------
    # SAVE PER-CHANNEL JSON FILES (optional)
    # ------------------------------------------------------------
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        for ch, params in channels_data.items():
            fname = os.path.join(output_dir, f"channel_{ch:02d}.json")
            with open(fname, "w") as f:
                json.dump(params, f, indent=2)

    return channels_data


def format_time(seconds: int) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def analyze_rate_stability(t, y, popt):
    """
    Returns:
        rate_init
        rate_final
        rate_mean
        variance_raw      (variance of raw data)
        variance_detrended (variance after removing trend)
        details           (dict with extra info)
    """
    import statistics
    # Basic stats
    rate_init = float(y[0])
    rate_final = float(y[-1])
    rate_mean = float(np.mean(y))
    variance_raw = float(statistics.variance(y))

    # ---- 1) Detrend using fitted exponential (if fit successful)
    if not any(np.isnan(popt)):
        y_fit = exp_decay(t, *popt)
        residuals = y - y_fit
    else:
        # fallback: subtract median
        residuals = y - np.median(y)

    variance_detrended = float(statistics.variance(residuals))

    # ---- 2) Expected Poisson variance = mean
    poisson_variance = rate_mean

    # ---- 3) Detect abnormal jumps
    dy = np.diff(y)
    if poisson_variance > 0:
        max_factor_of_poisson = variance_detrended / poisson_variance
    else:
        max_factor_of_poisson = np.nan
    return {
        "rate_init": rate_init,
        "rate_final": rate_final,
        "rate_mean": rate_mean,
        "variance_raw": variance_raw,
        "variance_detrended": variance_detrended,
        "max_factor_of_poisson": max_factor_of_poisson
    }


def process_rate_channel(
        mon_log,
        ch: int,
        threshold_hz: float,
        output_dir: str,
        do_plot: bool = True,
):
    """
    Process ratemeter time series for one channel:
      - extract timestamps
      - extract rate time series
      - mask bad values
      - exponential fit (robust)
      - optional plot
      - return structured results

    Returns:
        {
            "fit_success": bool,
            "a": float|None,
            "b": float|None,
            "c": float|None,
            "plot_file": str|None,
            "t": ndarray,
            "y": ndarray,
        }
    """
    # ========== 1) Extract time vector ==========
    try:
        time_data = [
            dt.datetime.fromisoformat(entry["timestamp"]).timestamp()
            for entry in mon_log
        ]
        time_data = np.array(time_data, dtype=float)
        time_data -= time_data[0]
    except Exception:
        return {"fit_success": False, "plot_file": None, "t": [], "y": []}

    # ========== 2) Extract rate vector ==========
    y_raw = []
    for entry in mon_log:
        channels = entry.get("channels", {})
        ch_block = channels.get(ch) or channels.get(str(ch), {})
        y_raw.append(ch_block.get("r_mon", np.nan))

    y_raw = np.array(y_raw, dtype=float)
    mask = np.isfinite(y_raw)

    if np.sum(mask) < 3:
        return {"fit_success": False, "plot_file": None, "t": [], "y": []}

    t = time_data[mask]
    y = y_raw[mask]

    # ========== 3) Exponential fit (robust) ==========
    try:
        p0 = [y[0] - y[-1], 0.001, y[-1]]
        popt, _ = curve_fit(exp_decay, t, y, p0=p0, maxfev=20000)
        fit_success = True
    except Exception:
        popt = [np.nan, np.nan, np.nan]
        fit_success = False

    # ========== 4) Plot (optional) ==========
    plot_file = None
    if do_plot:
        thr_val = threshold_hz if threshold_hz is not None else 0
        plot_file = plot_dark_rate_channel(
            t=t,
            y=y,
            popt=popt,
            threshold_hz=thr_val,
            ch=ch,
            output_dir=output_dir,
        )
        print(f"Channel {ch} Dark rate plot saved to: {plot_file}")

    # ========== 5) Return structured result ==========
    return {
        "fit_success": fit_success,
        "a": float(popt[0]) if fit_success else None,
        "b": float(popt[1]) if fit_success else None,
        "c": float(popt[2]) if fit_success else None,
        "plot_file": plot_file,
        "t": t,
        "y": y,
    }


def plot_hit_time_differences(
        root_file: str,
        channels=range(1, 20),
        time_unit: str = "us",
        save: str | None = None,
        separate: bool = False,
        bins: int = 200,
        sample_size: int | None = None,
        log_y: bool = False,
        max_diff_s: float | None = None,
):
    """
    Compute Δt only once per channel, then plot either:
      - combined 4×5 grid
      - separate per-channel PNGs

    Fit: sum of two exponentials
        f(t) = A1 exp(-t/τ1) + A2 exp(-t/τ2)

    Returned:
        saved_plots, fit_results
    """

    import uproot
    import numpy as np
    import matplotlib.pyplot as plt
    import os
    from scipy.optimize import curve_fit

    # -----------------------------------------------------------------
    # Double exponential model
    # -----------------------------------------------------------------
    def double_exp(t, A1, tau1, A2, tau2):
        return A1 * np.exp(-t / tau1) + A2 * np.exp(-t / tau2)

    tree_name = "pmt_events"
    ch_branch = "channel"

    coarse_branch = "time_coarse"
    fine_branch = "time_fine"
    tdc_branch = "tdc_start"
    pmt_time_branch = "pmt_time"

    # -----------------------------------------------------------------
    # Load ROOT
    # -----------------------------------------------------------------
    with uproot.open(root_file) as f:
        if tree_name not in f:
            raise RuntimeError(f"TTree '{tree_name}' not found in {root_file}")

        tree = f[tree_name]
        ch = tree[ch_branch].array(library="np") + 1

        try:
            pmt_time = tree[pmt_time_branch].array(library="np")
            time_s = pmt_time.astype(float) * 4e-9
        except Exception:
            coarse = tree[coarse_branch].array(library="np").astype(np.uint64)
            fine = tree[fine_branch].array(library="np").astype(np.uint64)
            tdc = tree[tdc_branch].array(library="np").astype(np.uint64)

            T = (coarse << 28) | (fine << 4) | tdc
            time_s = T.astype(float) * 0.25e-9

    # Unit conversion
    scale_map = {"s": 1.0, "ms": 1e3, "us": 1e6, "ns": 1e9}
    label_map = {"s": "s", "ms": "ms", "us": "µs", "ns": "ns"}

    unit_scale = scale_map[time_unit]
    unit_label = label_map[time_unit]

    # Save directory
    root_dir = os.path.dirname(root_file) or "."
    if save:
        user_dir = os.path.dirname(save)
        base_dir = user_dir if user_dir else root_dir
    else:
        base_dir = root_dir

    os.makedirs(base_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(root_file))[0]

    saved_plots = {}
    fit_results = {}

    # -----------------------------------------------------------------
    # PRECOMPUTE Δt FOR EACH CHANNEL ONLY ONCE
    # -----------------------------------------------------------------
    dt_dict = {}

    for ch_id in channels:
        mask = (ch == ch_id)
        if np.sum(mask) < 2:
            continue

        t_ch = np.sort(time_s[mask])

        if sample_size and len(t_ch) > sample_size:
            t_ch = t_ch[:sample_size]

        # Compute Δt in seconds
        dt_raw = np.diff(t_ch)
        dt_raw = dt_raw[dt_raw >= 0]

        if max_diff_s is not None:
            dt_raw = dt_raw[dt_raw <= max_diff_s]

        dt = dt_raw * unit_scale
        dt = dt[np.isfinite(dt) & (dt >= 0)]

        if len(dt) < 2:
            continue

        dt_dict[ch_id] = dt

    # -----------------------------------------------------------------
    # Helper: Fit double exponential
    # -----------------------------------------------------------------
    def fit_double_exponential(dt, bins):
        if len(dt) < 20:
            return None

        counts, edges = np.histogram(dt, bins=bins)
        centers = 0.5 * (edges[:-1] + edges[1:])
        m = counts > 0
        if np.sum(m) < 4:
            return None

        x = centers[m]
        y = counts[m]

        # Initial guesses
        A1_0 = np.max(y)
        tau1_0 = np.mean(dt) * 0.5
        A2_0 = np.max(y) * 0.3
        tau2_0 = np.mean(dt) * 2.0

        try:
            popt, _ = curve_fit(
                double_exp,
                x, y,
                p0=[A1_0, tau1_0, A2_0, tau2_0],
                maxfev=50000
            )
            A1, tau1, A2, tau2 = map(float, popt)
            return A1, tau1, A2, tau2
        except Exception:
            return None

    # -----------------------------------------------------------------
    # SEPARATE FIGURES
    # -----------------------------------------------------------------
    if separate:
        for ch_id, dt in dt_dict.items():
            popt = fit_double_exponential(dt, bins)

            fig, ax = plt.subplots(figsize=(6, 4))
            ax.hist(dt, bins=bins, edgecolor="black", alpha=0.7)

            if popt:
                A1, tau1, A2, tau2 = popt
                xx = np.linspace(0, np.max(dt), 400)
                ax.plot(xx, double_exp(xx, A1, tau1, A2, tau2),
                        'r-', lw=1.2,
                        label=f"τ1={tau1:.2g} {unit_label}, τ2={tau2:.2g} {unit_label}")
            else:
                tau_mle = np.mean(dt)
                ax.axvline(tau_mle, color='red', ls='--',
                           label=f"⟨Δt⟩={tau_mle:.2g} {unit_label}")

            if log_y:
                ax.set_yscale("log")

            ax.set_title(f"Δt — Ch {ch_id:02d}")
            ax.set_xlabel(f"Δt [{unit_label}]")
            ax.set_ylabel("Counts")
            ax.grid(True, ls="--", alpha=0.3)
            ax.legend()

            out = os.path.join(base_dir, f"{base_name}_dt_ch{ch_id:02d}.png")
            fig.tight_layout()
            fig.savefig(out, dpi=180)
            plt.close(fig)

            saved_plots[ch_id] = out

            # Store fit results
            if popt:
                A1, tau1, A2, tau2 = popt
                fit_results[ch_id] = {
                    "tau1_unit": tau1,
                    "tau2_unit": tau2,
                    "tau1_s": tau1 / unit_scale,
                    "tau2_s": tau2 / unit_scale,
                    "fit_success": True,
                    "n_dt": len(dt),
                }
            else:
                tau_mle = np.mean(dt)
                fit_results[ch_id] = {
                    "tau_mle_unit": tau_mle,
                    "tau_mle_s": tau_mle / unit_scale,
                    "fit_success": False,
                    "n_dt": len(dt),
                }

        return saved_plots, fit_results

    # -----------------------------------------------------------------
    # COMBINED GRID
    # -----------------------------------------------------------------
    fig, axes = plt.subplots(4, 5, figsize=(16, 10))
    axes = axes.flatten()

    for ax, ch_id in zip(axes, channels):
        dt = dt_dict.get(ch_id, None)
        if dt is None:
            ax.set_title(f"Ch {ch_id:02d} (no data)")
            ax.grid(True, ls="--", alpha=0.3)
            continue

        popt = fit_double_exponential(dt, bins)

        ax.hist(dt, bins=bins, edgecolor="black", alpha=0.7)

        if popt:
            A1, tau1, A2, tau2 = popt
            xx = np.linspace(0, np.max(dt), 300)
            ax.plot(xx, double_exp(xx, A1, tau1, A2, tau2),
                    "r-", lw=1.2)
            text = f"τ1={tau1:.2g}, τ2={tau2:.2g} {unit_label}"
        else:
            tau_mle = np.mean(dt)
            ax.axvline(tau_mle, color='red', ls='--')
            text = f"⟨Δt⟩={tau_mle:.2g} {unit_label}"

        if log_y:
            ax.set_yscale("log")

        ax.set_title(f"Ch {ch_id:02d}  {text}", fontsize=9)
        ax.grid(True, ls="--", alpha=0.3)

    fig.suptitle(f"Hit Δt Distributions — {os.path.basename(root_file)}", y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    if save:
        out = save if os.path.dirname(save) else os.path.join(base_dir, save)
        fig.savefig(out, dpi=200)
        saved_plots["combined"] = out

    return saved_plots, fit_results


def timing_sigma_simple(
        root_file,
        tree_name="pmt_events",
        channels=range(1, 20),
        ch_branch="channel",
        pmt_time_branch="pmt_time",
        tdc_start_branch="tdc_start",
        adc_branch="adc",
        pulser_freq_hz=10000,
        use_timewalk_correction=True,
        pedestals={},
        phase_window=8
):
    save_dir = os.path.join(os.path.dirname(root_file), "timing_plots")
    os.makedirs(save_dir, exist_ok=True)
    root_prefix = os.path.splitext(os.path.basename(root_file))[0]
    PERIOD_4NS = int((1.0 / pulser_freq_hz) / 4e-9)

    print(f"Pulser frequency : {pulser_freq_hz:.1f} Hz")
    print(f"Pulser period    : {PERIOD_4NS} × 4 ns bins")
    print("--------------------------------------------------")

    # -----------------------------
    # Load ROOT
    # -----------------------------
    with uproot.open(root_file) as f:
        tree = f[tree_name]
        adc = tree[adc_branch].array(library="np")
        channel = tree[ch_branch].array(library="np") + 1
        pmt_time = tree[pmt_time_branch].array(library="np").astype(np.int64)
        tdc_start = tree[tdc_start_branch].array(library="np").astype(np.int64)

    # Full timestamp (0.25 ns resolution)
    T = (pmt_time << 4) + tdc_start

    results = {ch: [] for ch in channels}

    # -----------------------------
    # Global phase
    # -----------------------------
    g_phase = pmt_time % PERIOD_4NS
    phase_hist = np.bincount(g_phase, minlength=PERIOD_4NS)
    g_signal_phase = int(np.argmax(phase_hist)) - 3

    # -----------------------------
    # Channel loop
    # -----------------------------
    for ch in channels:

        mask_ch = channel == ch
        if not np.any(mask_ch):
            results[ch] = {
                "entries": 0,
                "std_ns": np.nan,
                "mean_adc": np.nan,
                "sigma_ns": np.nan,
                "fit_status": "FAIL",
                "use_timewalk_correction": use_timewalk_correction
            }
            continue

        phase = pmt_time[mask_ch] % PERIOD_4NS
        phase_hist = np.bincount(phase, minlength=PERIOD_4NS)
        signal_phase = int(np.argmax(phase_hist)) - 3

        sel = (
                mask_ch
                & (np.abs((pmt_time % PERIOD_4NS) - signal_phase) <= phase_window)
        )

        n_sel = np.count_nonzero(sel)
        T_chan = T[sel]
        # add time correction
        adc_chan = adc[sel] - pedestals[ch]

        # Relative timing
        t_rel_tdc = T_chan % (PERIOD_4NS << 4)
        t_rel_ns = t_rel_tdc * 0.25
        t_rel_ns = t_rel_ns - g_signal_phase * 4

        t_rel_ns_raw = t_rel_ns.copy()
        if use_timewalk_correction:
            print(f"Channel[{ch}] Using time walk correction on timing analysis...")
            t_rel_ns = apply_timewalk_lut(t_rel_ns, adc_chan)

        # RMS
        std_ns = np.nanstd(t_rel_ns)

        # Histogram for Gaussian fit
        bins = np.arange(-phase_window * 2 + 0.25, phase_window * 4 + 0.25, 0.25)
        hist_corr, edges = np.histogram(t_rel_ns, bins)
        hist_raw, _ = np.histogram(t_rel_ns_raw, bins)
        centers = 0.5 * (edges[:-1] + edges[1:])

        threshold = 0.5 * np.max(hist_corr)
        mask = hist_corr > threshold

        x_fit_data = centers[mask]
        y_fit_data = hist_corr[mask]

        # Gaussian fit
        try:
            p0 = [np.max(hist_corr), np.nanmean(t_rel_ns), std_ns]

            popt, _ = curve_fit(
                gauss,
                x_fit_data,
                y_fit_data,
                p0=p0
            )

            sigma_fit = abs(popt[2])
            fit_status = "OK"


        except Exception:
            sigma_fit = np.nan
            fit_status = "FAIL"
            popt = None

        # -----------------------------
        # Plot histogram
        # -----------------------------
        plt.figure(figsize=(6, 4))

        # RAW
        plt.step(
            centers,
            hist_raw,
            where="mid",
            label="raw",
            linestyle="--"
        )

        # CORRECTED
        plt.step(
            centers,
            hist_corr,
            where="mid",
            label="corrected"
        )

        # FIT
        if popt is not None:
            x_fit = np.linspace(min(bins), max(bins), 400)
            y_fit = gauss(x_fit, *popt)
            plt.plot(
                x_fit,
                y_fit,
                lw=2,
                label=f"fit σ = {sigma_fit:.3f} ns"
            )

        plt.title(f"Timing distribution – Channel {ch}")
        plt.xlabel("Time [ns]")
        plt.ylabel("Counts")
        plt.grid(alpha=0.3)
        plt.legend()

        outfile = os.path.join(save_dir, f"{root_prefix}_timing_ch{ch:02d}.png")
        plt.savefig(outfile, dpi=150)
        plt.close()

        print(f"✔ Timing plot saved for ch{ch:02d} → {outfile}")
        # -----------------------------
        # Store result
        # -----------------------------
        results[ch] = {
            "entries": n_sel,
            "std_ns": std_ns,
            "sigma_ns": sigma_fit,
            "mean_adc": np.mean(adc_chan),
            "fit_status": fit_status,
            "use_timewalk_correction": use_timewalk_correction
        }

    # -----------------------------
    # Print summary table
    # -----------------------------
    table_rows = [
        [ch, v["entries"], v["mean_adc"], v["std_ns"], v["sigma_ns"], v["fit_status"]]
        for ch, v in sorted(results.items())
    ]

    print("\nTiming Resolution Summary\n")

    print(tabulate(
        table_rows,
        headers=[
            "Ch",
            "Entries",
            "mean ADC",
            "STD [ns]",
            "Gauss σ [ns]",
            "Fit",
        ],
        tablefmt="fancy_grid",
        floatfmt=".4f",
    ))

    return results