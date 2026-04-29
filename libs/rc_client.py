import os
import socket
import json
import math
import time

class RunControlClient:
    def __init__(self, host='localhost', port=9000, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.file = self.sock.makefile('rwb')

    def _send_command(self, command, args=None):
        if args is None:
            args = {}
        req = {"command": command, "args": args}
        self.file.write((json.dumps(req) + '\n').encode('utf-8'))
        self.file.flush()
        line = self.file.readline()
        if not line:
            raise ConnectionError("Connection closed by server")
        resp = json.loads(line.decode('utf-8'))
        if resp.get('status') != 'ok':
            raise RuntimeError(resp.get('error', 'Unknown error'))
        return resp

    def read(self, address):
        resp = self._send_command('read', {"address": address})
        return resp['value']

    def write(self, address, value):
        self._send_command('write', {"address": address, "value": value})

    def read_sensors(self):
        resp = self._send_command('read_sensors')
        return resp['values']

    def status(self):
        resp = self._send_command('status')
        return resp
    
    def do_defaults(self, defaults_file=None, verbose=False):
        """
        Reset all registers to their default values from defaults.json.
        
        :param defaults_file: path to defaults.json (default = same folder as this file)
        :param confirm: if True, ask user for confirmation before writing
        """
        if defaults_file is None:
            defaults_file = os.path.join(os.path.dirname(__file__), "defaults.json")

        if not os.path.exists(defaults_file):
            print(f"Error Defaults file not found: {defaults_file}")
            return

        with open(defaults_file) as f:
            def_reg = json.load(f)

        for add, value in def_reg.items():
            addr_int = int(add)
            if verbose:
                print(f"Writing Reg{addr_int}: {value} (0x{value:08x})")
            self.write(addr_int, value)

        print("✅ All registers set to default values.")
        
    # --------------------------
    # Turn on/off channels
    # --------------------------
    def turn_on(self, channels=None, all_channels=False):
        """Turn on channels (register 1) only if acquisition is disabled."""
        acq_reg = self.read(0)  # acquisition register

        if all_channels:
            # Only enable channels that are currently disabled for acquisition
            allowed = [ch for ch in range(1, 20) if not (acq_reg & (1 << (ch-1)))]
            if not allowed:
                print("No channels can be turned on: all have acquisition enabled")
                return
            current = self.read(1)
            for ch in allowed:
                current |= (1 << (ch - 1))
            self.write(1, current)
            print(f"Channels turned ON: {allowed}")
        elif channels:
            current = self.read(1)
            allowed = []
            for ch in channels:
                if 1 <= ch <= 19:
                    if not (acq_reg & (1 << (ch-1))):
                        current |= (1 << (ch-1))
                        allowed.append(ch)
            self.write(1, current)
            skipped = set(channels) - set(allowed)
            if allowed:
                print(f"Channels turned ON: {allowed}")
            if skipped:
                print(f"Skipped channels (acquisition enabled): {list(skipped)}")

    def turn_off(self, channels=None, all_channels=False):
        """Turn off channels (register 1)."""
        if all_channels:
            self.write(1, 0)
        elif channels:
            current = self.read(1)
            for ch in channels:
                if 1 <= ch <= 19:
                    current &= ~(1 << (ch - 1))
            self.write(1, current)

    def is_on(self, ch: int) -> bool:
        """
        Return True if channel 'ch' is ON according to register 1.
        ON = bit (ch-1) == 1.
        """
        if not (1 <= ch <= 19):
            raise ValueError("Channel must be between 1 and 19")

        reg1 = self.read(1)
        return bool(reg1 & (1 << (ch - 1)))

    # --------------------------
    # Clear channels
    # --------------------------
    def clear_channel(self, channels=None, all_channels=False):
        """Clear channels (register 5)."""
        if all_channels:
            self.write(5, 0x7FFFF)
            time.sleep(0.5)
            self.write(5, 0)
        elif channels:
            for ch in channels:
                if 1 <= ch <= 19:
                    self.write(5, 1 << (ch - 1))
                    time.sleep(0.5)
                    self.write(5, 0)
                    
    # --------------------------
    # Channel acquisition
    # --------------------------
    def enable_channel(self, channels=None, all_channels=False):
        """Enable channel acquisition for given channels or all (1–19)."""
        if all_channels:
            self.write(0, 0x7FFFF)  # enable all 19 channels
        elif channels:
            current = self.read(0)
            for ch in channels:
                if 1 <= ch <= 19:
                    current |= (1 << (ch - 1))
            self.write(0, current)

    def disable_channel(self, channels=None, all_channels=False):
        """Disable channel acquisition for given channels or all (1–19)."""
        if all_channels:
            self.write(0, 0)  # disable all channels
        elif channels:
            current = self.read(0)
            for ch in channels:
                if 1 <= ch <= 19:
                    current &= ~(1 << (ch - 1))
            self.write(0, current)
            
    # --------------------------
    # Trigger control functions
    # --------------------------
    def enable_trigger(self, channels=None, all_channels=False):
        """Enable trigger for given channels or all (1–19)."""
        if all_channels:
            self.write(58, 0x7FFFF)  # enable all 19 channels
        elif channels:
            current = self.read(58)
            for ch in channels:
                if 1 <= ch <= 19:
                    current |= (1 << (ch - 1))
            self.write(58, current)
               
    def disable_trigger(self, channels=None, all_channels=False):
        """Disable trigger for given channels or all (1–19)."""
        if all_channels:
            self.write(58, 0)  # disable all
        elif channels:
            current = self.read(58)
            for ch in channels:
                if 1 <= ch <= 19:
                    current &= ~(1 << (ch - 1))
            self.write(58, current)
            
    # --------------------------
    # Print all registers
    # --------------------------
    def print_all(self):
        """Print all 64 registers in groups of 8 per row."""
        for row in range(8):
            regs = []
            for col in range(8):
                addr = row * 8 + col
                value = self.read(addr)
                regs.append(f"Register{addr:02}: {value:08x}")
            print("  ".join(regs))       
            
    # --------------------------
    # Rate threshold
    # --------------------------
    def set_threshold(self, value: int, channels=None, all_channels=False):
        """
        Set rate threshold for one or more channels (1–19), or all channels.
        Each register (46–55) stores thresholds for two channels:
          - lower 16 bits = odd channel
          - upper 16 bits = even channel
        """
        if not (1 <= value <= 65535):
            raise ValueError("Threshold value must be between 1 and 65535")

        if all_channels:
            for i in range(46, 56):  # registers 46–55
                self.write(i, (value << 16) | value)
            print(f"Threshold set to {value} for all channels")
        elif channels:
            for channel in channels:
                if not (1 <= channel <= 19):
                    raise ValueError("Channel must be in range 1–19")
                chaddr = math.floor((channel - 1) / 2) + 46
                cleanreg = self.read(chaddr)
                if channel % 2 == 0:  # even channel -> upper 16 bits
                    newval = (value << 16) | (cleanreg & 0xFFFF)
                else:  # odd channel -> lower 16 bits
                    newval = (cleanreg & 0xFFFF0000) | value
                self.write(chaddr, newval)
                print(f"Threshold for channel {channel} set to {value}")     
   
    def set_spi_speed(self, speed: int, verbose: bool = True):
        """
        Set SPI clock frequency mode in register 4 (bits 19–20).

        SPI clock modes:
            3 -> fast
            2 -> mid
            1 -> slow
            0 -> extra slow

        Corresponds to modifying bits [20:19] in register 4.
        """
        if speed not in (0, 1, 2, 3):
            raise ValueError("SPI speed must be 0, 1, 2, or 3 (0=extra slow, 3=fast)")

        reg_addr = 4
        reg_val = self.read(reg_addr)

        # Clear bits 19 and 20
        reg_val &= ~(0b11 << 19)

        # Insert new 2-bit speed value
        reg_val |= (speed & 0b11) << 19

        self.write(reg_addr, reg_val)
        if verbose:
            print(f"SPI speed set to {speed} (0=extra slow … 3=fast)")
                    
    def print_threshold(self):
        """
        Print thresholds for all channels (1–19).
        Registers 46–55 hold thresholds, 2 channels per register.
        """
        for ch in range(1, 20):
            chaddr = math.floor((ch - 1) / 2) + 46
            regval = self.read(chaddr)
            if ch % 2 == 0:
                threshold = (regval >> 16) & 0xFFFF
            else:
                threshold = regval & 0xFFFF
            print(f"Channel {ch:02}: threshold = {threshold}")
            
     
    def plot_rates(self):
        """
        Draw a horizontal bar plot in the console for 19 channels based on rate values.
        Uses a fixed-width scale for easier reading.
        """
        rates = [self.read(i) for i in range(8, 27)]

        max_rate = max(rates)
        if max_rate == 0:
            print("No rates to plot.")
            return

        max_width = 80  # max number of characters per bar
        scale_step = max_rate / max_width if max_rate > max_width else 1

        print("\nChannel Rates (horizontal)")
        for i, rate in enumerate(rates, start=1):
            bar_length = int(rate / scale_step + 0.5)
            bar = "█" * bar_length
            print(f"Ch {i:02}: {bar:<{max_width}} {rate}")

        print(f"Max rate: {max_rate}")
    
    def print_status(self):
        """Show 19 channel status with acquisition and on/off info."""
        acq_reg = format(self.read(0), '019b')       # acquisition register
        on_reg = format(self.read(1), '019b')        # channel on/off register
        ratemeters = [self.read(i) for i in range(8, 27)]
        deadtime = round((65535 - self.read(27)) / 65535 * 100)

        def ch(channel):
            a = acq_reg[18-channel] == '1'
            o = on_reg[18-channel] == '1'
            # two-char status: acquisition + on
            if a and o:
                return f"{channel+1:02}AO"  # on + acquisition enabled
            elif a:
                return f"{channel+1:02}A."  # acquisition only
            elif o:
                return f"{channel+1:02}O."  # on only
            else:
                return f"{channel+1:02}.."  # off

        status_scheme = [
            f"      {ch(11)}  {ch(0)}  {ch(1)}",
            f"   {ch(10)}  {ch(17)}  {ch(12)}  {ch(2)}",
            f"{ch(9)}   {ch(16)}  {ch(18)}  {ch(13)}  {ch(3)}",
            f"   {ch(8)}  {ch(15)}  {ch(14)}  {ch(4)}",
            f"      {ch(7)}  {ch(6)}  {ch(5)}",
        ]

        ratemeters_scheme = [
            f"              {ratemeters[11]:08} {ratemeters[0]:08} {ratemeters[1]:08}",
            f"         {ratemeters[10]:08} {ratemeters[17]:08} {ratemeters[12]:08} {ratemeters[2]:08}",
            f"{ratemeters[9]:08} {ratemeters[16]:08} {ratemeters[18]:08} {ratemeters[13]:08} {ratemeters[3]:08}",
            f"         {ratemeters[8]:08} {ratemeters[15]:08} {ratemeters[14]:08} {ratemeters[4]:08}",
            f"              {ratemeters[7]:08} {ratemeters[6]:08} {ratemeters[5]:08}",
        ]

        print("Number+Status: 'AO'=on+acquisition, 'A.'=acquisition only, 'O.'=on only, '..'=off")
        for status, rate in zip(status_scheme, ratemeters_scheme):
            print(status, " ", rate)
        print(f"Deadtime: {deadtime}%")
                 
    def process_evbuilder_start(self, host="192.168.16.70", data_port = 5555, disable_rc=True, path="/opt/mpmt-readout/build/evproducer"):
        """
        Start the evbuilder process on the server.
        
        Sends the command:
        {"command": "start_process", "args": {"path": ..., "params": [...]}}
        """
        params = ["--host", host, "--port", str(data_port)]

        if disable_rc:
            params.append("--disable-rc")

        command_args = {"path": path, "params": params}
        print(command_args)
        resp = self._send_command("start_process", command_args)
        print("Process started:", resp)
        return resp
    
    def process_evbuilder_stop(self):
        """
        Stop the evbuilder process on the server.

        Sends the command:
        {"command": "stop_process", "args": {}}
        """
        print("\n🛑 Stopping event builder...")
        resp = self._send_command("stop_process", {})
        print("Process stopped:", resp)
        return resp     
       
    def process_program_febs(
        self,
        firmware="HKL031V4B.hex",
        baud="115200",
        febs="all",
        port="/dev/ttyPS1",
        directory="/opt/mpmt-board-cli/utils/FEB_firmware",
        script="reprogram_FEBs.py",
        path="python3",
    ):
        """
        Start the FEB programming process on the server.

        Runs:
        cd /opt/mpmt-board-cli/utils/FEB_firmware
        python3 reprogram_FEBs.py -f <firmware> -b <baud> -n <febs> -p <port>
        
        Parameters
        ----------
        firmware : str
            Firmware file (.hex) to upload (default: HKL031V4B.hex).
        baud : str
            Baudrate for programming (default: 115200).
        febs : str
            FEBs to program (e.g., "all" or "1,2,3").
        port : str
            Serial port device (default: /dev/ttyPS1).
        path : str
            Path to reprogram_FEBs.py script (default: /opt/mpmt-board-cli/utils/FEB_firmware/reprogram_FEBs.py).
        """
        params = [
            os.path.join(directory,script),
            "-f", os.path.join(directory,firmware),
            "-b", str(baud),
            "-n", febs,
            "-p", port
        ]
        command_args = {"path": path, "params": params}
        print(command_args)
        resp = self._send_command("start_process", command_args)
        print("FEB programming started:", resp)
        return resp

    def read_log(self) -> str:
        """
        Return last 100 lines of process log (stdout/stderr).
        """
        try:
            resp = self._send_command("log", {})
            return resp.get("log")
        except Exception:
            return "Error"
        
    def process_isrunning(self) -> bool:
        """
        Return True if the any process is currently running on the server.
        Falls back to False on any communication error.
        """
        try:
            resp = self._send_command("process_status", {})
            return bool(resp.get("running", False))
        except Exception:
            return False    
               
    def close(self):
        try:
            self.file.close()
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    # --------------------------
    # Pulser control
    # --------------------------
    def pulser_set_frequency(self, hz: float | int, verbose: bool = True) -> None:
        """
        Set global pulser frequency via register 7.
        - If hz <= 0  -> pulser OFF (write 0)
        - If hz >= 1_000_000 -> write the raw value (compat with original logic)
        - Else -> write period = int(1_000_000 / hz)

        Notes:
            The firmware expects a period in microseconds in register 7.
        """
        try:
            hz = float(hz)
        except Exception:
            print("Invalid pulser value")
            return

        if hz <= 0:
            self.write(7, 0)
            if verbose:
                print("Pulser OFF")
        elif hz >= 1_000_000:
            # Keep original behavior: write the given value directly
            self.write(7, int(hz))
            if verbose:
                print("Pulser OFF (raw value ≥ 1e6 written)")
        else:
            period = int(1_000_000 / hz)
            self.write(7, period)
            if verbose:
                print(f"Pulser set to {hz:g} Hz (period={period})")

    def pulser_add_subhits(self, pulses: int) -> None:
        """
        Add 'subhits' (extra pulses) via register 60.
        Equivalent to CLI: `pulser sub <pulses>`.
        """
        try:
            pulses = int(pulses)
        except Exception:
            print("Invalid number of pulses")
            return

        if pulses < 0:
            print("Invalid number of pulses")
            return

        self.write(60, pulses)
        print(f"Subhits added: {pulses}")

    # --------------------------
    # Channel pulser enable mask (register 59)
    # --------------------------
    def enable_pulser(self, channels: list[int] | None = None, all_channels: bool = False) -> None:
        """
        Enable channel pulser (bitmask in register 59).
        - all_channels=True -> enable for all 1..19
        - channels=[...]    -> enable only those channels
        """
        if all_channels:
            self.write(59, 0x7FFFF)
            print("Pulser enabled on all channels")
            return

        if not channels:
            print("No channels specified")
            return

        current = self.read(59)
        changed = []
        for ch in channels:
            if 1 <= ch <= 19:
                mask = 1 << (ch - 1)
                if not (current & mask):
                    current |= mask
                    changed.append(ch)
        self.write(59, current)
        if changed:
            print(f"Pulser enabled on channels: {changed}")

    def disable_pulser(self, channels: list[int] | None = None, all_channels: bool = False) -> None:
        """
        Disable channel pulser (bitmask in register 59).
        - all_channels=True -> disable for all 1..19
        - channels=[...]    -> disable only those channels
        """
        if all_channels:
            self.write(59, 0)
            print("Pulser disabled on all channels")
            return

        if not channels:
            print("No channels specified")
            return

        current = self.read(59)
        changed = []
        for ch in channels:
            if 1 <= ch <= 19:
                mask = 1 << (ch - 1)
                if current & mask:
                    current &= ~mask
                    changed.append(ch)
        self.write(59, current)
        if changed:
            print(f"Pulser disabled on channels: {changed}")

    # --------------------------
    # Channel reset enable mask (register 5)
    # --------------------------
    def lock_channel(self, channels: list[int] | None = None, all_channels: bool = False) -> None:
        """
        Enable channel pulser (bitmask in register 59).
        - all_channels=True -> enable for all 1..19
        - channels=[...]    -> enable only those channels
        """
        if all_channels:
            self.write(5, 0x7FFFF)
            print("Pulser enabled on all channels")
            return

        if not channels:
            print("No channels specified")
            return

        current = self.read(5)
        changed = []
        for ch in channels:
            if 1 <= ch <= 19:
                mask = 1 << (ch - 1)
                if not (current & mask):
                    current |= mask
                    changed.append(ch)
        self.write(5, current)
        if changed:
            print(f"Pulser enabled on channels: {changed}")

    def free_channel(self, channels: list[int] | None = None, all_channels: bool = False) -> None:
        """
        Disable channel pulser (bitmask in register 59).
        - all_channels=True -> disable for all 1..19
        - channels=[...]    -> disable only those channels
        """
        if all_channels:
            self.write(5, 0)
            print("Pulser disabled on all channels")
            return

        if not channels:
            print("No channels specified")
            return

        current = self.read(5)
        changed = []
        for ch in channels:
            if 1 <= ch <= 19:
                mask = 1 << (ch - 1)
                if current & mask:
                    current &= ~mask
                    changed.append(ch)
        self.write(5, current)
        if changed:
            print(f"Pulser disabled on channels: {changed}")
            
    # --------------------------
    # Ratemeter threshold (regs 46..55, two channels per register)
    # --------------------------
    def set_rate_threshold(self, value: int, channels: list[int] | None = None,
                           all_channels: bool = False, verbose: bool = True) -> None:
        """
        Set ratemeter threshold(s).
        - value: 1..65535   (firmware interprets this as time-to-peak; ~ value*8 ns)
        - channels: list of channels in 1..19; if None with all_channels=False -> do nothing
        - all_channels: set same value for all channels (1..19)
        """
        if not (1 <= int(value) <= 65535):
            raise ValueError("Threshold value must be between 1 and 65535")

        if all_channels:
            for reg in range(46, 56):  # 46..55 inclusive
                self.write(reg, (value << 16) | value)
            if verbose:
                print(f"Ratemeter threshold set to {value} ({value*8} ns) for ALL channels")
            return

        if not channels:
            if verbose:
                print("No channels specified.")
            return

        for ch in channels:
            if not (1 <= ch <= 19):
                if verbose:
                    print(f"Skip invalid channel {ch} (valid: 1..19)")
                continue
            reg = (ch - 1) // 2 + 46                         # 46..55
            current = self.read(reg)
            if (ch % 2) == 0:
                # odd channel -> upper 16 bits
                newval = (value << 16) | (current & 0xFFFF)
            else:
                # even channel -> lower 16 bits
                newval = (current & 0xFFFF0000) | value
            self.write(reg, newval)
            if verbose:
                print(f"Ch {ch:02d}: ratemeter threshold ← {value} ({value*8} ns) [reg {reg}]")

    def get_rate_thresholds(self) -> dict[int, int]:
        """
        Read ratemeter thresholds for all channels (1..19).
        Returns: {channel: value}
        """
        out: dict[int, int] = {}
        for ch in range(1, 20):
            reg = (ch - 1) // 2 + 46
            regval = self.read(reg)
            if ch % 2 == 0:
                thr = (regval >> 16) & 0xFFFF
            else:
                thr = regval & 0xFFFF
            out[ch] = thr
        return out
    
    # --------------------------
    # Time to Peak (regs 28..37, two channels per register)
    # --------------------------
    def set_time_to_peak(self, value: int, channels: list[int] | None = None,
                         all_channels: bool = False, verbose: bool = True) -> None:
        """
        Set time-to-peak parameter (TTP) for specified channels.
        - value: 1..4096   (each unit = 8 ns)
        - channels: list of channels (1..19)
        - all_channels: if True, apply same value to all channels (1..19)
        """
        if not (1 <= int(value) <= 4096):
            raise ValueError("Time-to-peak value must be between 1 and 4096")

        if all_channels:
            for reg in range(28, 38):  # 28..37 inclusive
                self.write(reg, (value << 12) | value)
            if verbose:
                print(f"⏱️  Time-to-peak set to {value} ({value*8} ns) for ALL channels")
            return

        if not channels:
            if verbose:
                print("No channels specified.")
            return

        for ch in channels:
            if not (1 <= ch <= 19):
                if verbose:
                    print(f"Skip invalid channel {ch} (valid: 1..19)")
                continue
            reg = (ch - 1) // 2 + 28
            current = self.read(reg)
            if ch % 2 == 0:
                newval = (value << 12) | (current & 0xFFF)
            else:
                newval = (current & 0xFFF000) | value
            self.write(reg, newval)
            if verbose:
                print(f"Ch {ch:02d}: time-to-peak ← {value} ({value*3.7} ns) [reg {reg}]")

    def get_time_to_peak(self) -> dict[int, int]:
        """
        Read time-to-peak parameter for all channels (1..19).
        Returns: {channel: value}
        """
        out: dict[int, int] = {}
        for ch in range(1, 20):
            reg = (ch - 1) // 2 + 28
            regval = self.read(reg)
            if ch % 2 == 0:
                ttp = (regval >> 12) & 0xFFF
            else:
                ttp = regval & 0xFFF
            out[ch] = ttp
        return out

    # --------------------------
    # Measure Delay (regs 38..42, four channels per register)
    # --------------------------
    def set_measure_delay(self, value: int, channels: list[int] | None = None,
                          all_channels: bool = False, verbose: bool = True) -> None:
        """
        Set additional delay per measurement.
        - value: 1..255  (each unit = 8 ns)
        - channels: list of channels (1..19)
        - all_channels: if True, apply same value to all channels
        """
        if not (1 <= int(value) <= 255):
            raise ValueError("Delay value must be between 1 and 255")

        if all_channels:
            for reg in range(38, 43):  # 38..42 inclusive
                packed = (value << 24) | (value << 16) | (value << 8) | value
                self.write(reg, packed)
            if verbose:
                print(f"⏳ Delay set to {value} ({value*8} ns) for ALL channels")
            return

        if not channels:
            if verbose:
                print("No channels specified.")
            return

        for ch in channels:
            if not (1 <= ch <= 19):
                if verbose:
                    print(f"Skip invalid channel {ch} (valid: 1..19)")
                continue
            reg = (ch - 1) // 4 + 38
            byte_pos = 3 - ((ch - 1) % 4)
            shift = byte_pos * 8
            mask = 0xFF << shift
            current = self.read(reg)
            newval = (current & ~mask) | ((value & 0xFF) << shift)
            self.write(reg, newval)
            if verbose:
                print(f"Ch {ch:02d}: delay ← {value} ({value*8} ns) [reg {reg}]")

    def get_measure_delay(self) -> dict[int, int]:
        """
        Read measurement delay for all channels (1..19).
        Returns: {channel: value}
        """
        out: dict[int, int] = {}
        for ch in range(1, 20):
            reg = (ch - 1) // 4 + 38
            regval = self.read(reg)
            byte_pos = 3 - ((ch - 1) % 4)
            shift = byte_pos * 8
            delay = (regval >> shift) & 0xFF
            out[ch] = delay
        return out

    # --------------------------
    # FIFO / DMA reset toggles (reg 4)
    # --------------------------
    _REG_CTRL  = 4
    _BIT_DMA   = 0x1000   # DMA reset/free bit
    _BIT_FIFO  = 0x0200   # FIFO reset/free bit

    def _set_bits(self, addr: int, mask: int) -> None:
        val = self.read(addr)
        self.write(addr, val | mask)

    def _clear_bits(self, addr: int, mask: int) -> None:
        val = self.read(addr)
        self.write(addr, val & ~mask)

    def reset_dma(self, verbose: bool = True) -> None:
        """
        Toggle DMA reset bit (reg 4, bit 0x1000).
        - If set  -> clear it  (print 'DMA reset')
        - If clear-> set it    (print 'DMA free')
        Mirrors original CLI semantics.
        """
        val = self.read(self._REG_CTRL)
        if val & self._BIT_DMA:
            self._clear_bits(self._REG_CTRL, self._BIT_DMA)
            if verbose:
                print("🔄 DMA reset")
        else:
            self._set_bits(self._REG_CTRL, self._BIT_DMA)
            if verbose:
                print("🧹 DMA free")

    def reset_fifo(self, verbose: bool = True) -> None:
        """
        Toggle FIFO reset bit (reg 4, bit 0x0200).
        - If set  -> clear it  (print 'FIFO free')
        - If clear-> set it    (print 'FIFO reset')
        Mirrors original CLI semantics.
        """
        val = self.read(self._REG_CTRL)
        if val & self._BIT_FIFO:
            self._clear_bits(self._REG_CTRL, self._BIT_FIFO)
            if verbose:
                print("🧹 FIFO free")
        else:
            self._set_bits(self._REG_CTRL, self._BIT_FIFO)
            if verbose:
                print("🔄 FIFO reset")

    def reset(self, which: str, verbose: bool = True) -> None:
        """
        Unified entrypoint:
          which in {'DMA','fifo'}
        """
        if which == "DMA":
            self.reset_dma(verbose=verbose)
        elif which == "fifo":
            self.reset_fifo(verbose=verbose)
        else:
            raise ValueError("Invalid subcommand for reset(): use 'DMA' or 'fifo'")
        
# Example usage:
# rc = RunControlClient('127.0.0.1', 9000)
# print(rc.read(0))
# rc.write(0, 123)
# print(rc.status())
# rc.close()
