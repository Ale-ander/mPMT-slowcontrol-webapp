import math
import struct
from sys import exit

import pymodbus.client as ModbusClient
from pymodbus import FramerType
from pymodbus.exceptions import ModbusIOException


class HVModbus:
    def __init__(self, param):
        self.devset = [None] * 21  # 1...20 for new boards default address (20)
        self.dev = None
        self.client = None
        self.address = None
        self.param = param

        if self.param.mode == 'tcp':
            self.client = ModbusClient.ModbusTcpClient(self.param.host, port=502, framer=FramerType.SOCKET)
            if not self.client.connect():
                print(f'E: host not reachable or mbusd not running ({self.param.host})')
                exit(1)
        elif self.param.mode == 'rtu':
            self.client = ModbusClient.ModbusSerialClient(
                self.param.port,
                framer=FramerType.RTU,
                baudrate=115200,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.5
            )
            if not self.client.connect():
                print(f'E: port not available ({self.param.port})')
                exit(1)

    def open(self, addr):
        try:
            rr = self.client.read_holding_registers(address=0, count=1, device_id=addr)
        except ModbusIOException:
            return False

        if rr.isError():
            return False

        self.address = addr
        return True

    def isConnected(self):
        return self.address is not None

    def getAddress(self):
        return self.address

    def getStatus(self):
        rr = self.client.read_holding_registers(address=6, count=1, device_id=self.address)
        return rr.registers[0]

    def getVoltage(self):
        rr = self.client.read_holding_registers(address=0x2A, count=2, device_id=self.address)
        rr.registers.reverse()
        return self.client.convert_from_registers(rr.registers, data_type=self.client.DATATYPE.INT32) / 1000

    def getVoltageSet(self):
        rr = self.client.read_holding_registers(address=0x26, count=1, device_id=self.address)
        return rr.registers[0]

    def setVoltageSet(self, value):
        self.client.write_register(address=0x26, value=value, device_id=self.address)

    def getCurrent(self):
        rr = self.client.read_holding_registers(address=0x28, count=2, device_id=self.address)
        rr.registers.reverse()
        return self.client.convert_from_registers(rr.registers, data_type=self.client.DATATYPE.INT32) / 1000

    def getTemperature(self):
        rr = self.client.read_holding_registers(address=0x7, count=1, device_id=self.address)
        return self.convertTemperature(rr.registers[0])

    def getRate(self, fmt=str):
        rr = self.client.read_holding_registers(address=0x23, count=2, device_id=self.address)
        rup = rr.registers[0]
        rdn = rr.registers[1]
        if fmt == str:
            return f'{rup}/{rdn}'
        else:
            return rup, rdn

    def setRateRampup(self, value):
        self.client.write_register(address=0x23, value=value, device_id=self.address)

    def setRateRampdown(self, value):
        self.client.write_register(address=0x24, value=value, device_id=self.address)

    def getLimit(self, fmt=str):
        rr = self.client.read_holding_registers(address=0, count=48, device_id=self.address)
        lv = rr.registers[0x27]
        li = rr.registers[0x25]
        lt = rr.registers[0x2F]
        ltt = rr.registers[0x22]

        if fmt == str:
            return f'{lv}/{li}/{lt}/{ltt}'
        else:
            return lv, li, lt, ltt

    def setLimitVoltage(self, value):
        self.client.write_register(address=0x27, value=value, device_id=self.address)

    def setLimitCurrent(self, value):
        self.client.write_register(address=0x25, value=value, device_id=self.address)

    def setLimitTemperature(self, value):
        self.client.write_register(address=0x2F, value=value, device_id=self.address)

    def setLimitTriptime(self, value):
        self.client.write_register(address=0x22, value=value, device_id=self.address)

    def setThreshold(self, value):
        if value.is_integer():
            self.client.write_register(address=0x2D, value=int(value), device_id=self.address)
            self.client.write_register(address=0x35, value=0, device_id=self.address)
        else:
            self.client.write_register(address=0x2D, value=math.floor(value), device_id=self.address)
            self.client.write_register(address=0x35, value=int(value * 10) % 10, device_id=self.address)

    def getThreshold(self):
        rr = self.client.read_holding_registers(address=0x2D, count=1, device_id=self.address)
        return rr.registers[0]

    def getAlarm(self):
        rr = self.client.read_holding_registers(address=0x2E, count=1, device_id=self.address)
        return rr.registers[0]

    def getVref(self):
        rr = self.client.read_holding_registers(address=0x2C, count=1, device_id=self.address)
        return rr.registers[0] / 10

    def powerOn(self):
        rr = self.client.write_coil(address=1, value=True, device_id=self.address)
        return not rr.isError()

    def powerOnAll(self):
        self.client.write_coil(address=1, value=True, device_id=0, no_response_expected=True)

    def powerOff(self):
        rr = self.client.write_coil(address=1, value=False, device_id=self.address)
        return not rr.isError()

    def powerOffAll(self):
        self.client.write_coil(address=1, value=False, device_id=0, no_response_expected=True)

    def reset(self):
        rr = self.client.write_coil(address=2, value=True, device_id=self.address)
        return not rr.isError()

    def getInfo(self):
        l = self.client.read_holding_registers(address=0x02, count=1, device_id=self.address).registers
        fwver = struct.pack(f'>{len(l)}h', *l).decode()
        l = self.client.read_holding_registers(address=0x08, count=6, device_id=self.address).registers
        pmtsn = struct.pack(f'>{len(l)}h', *l).decode()
        l = self.client.read_holding_registers(address=0x0E, count=6, device_id=self.address).registers
        hvsn = struct.pack(f'>{len(l)}h', *l).decode()
        l = self.client.read_holding_registers(address=0x04, count=2, device_id=self.address).registers
        devid = (l[1] << 16) + l[0]
        return fwver, pmtsn, hvsn, devid

    def safe_write_registers(self, address, values):
        base_addr = address

        for i, val in enumerate(values):
            addr = base_addr + i
            try:
                self.client.write_register(address=addr, value=val, device_id=self.address)
            except Exception as e:
                print(f"Exception at reg 0x{addr:02X}: {e}")

    @staticmethod
    def _pack_sn_to_registers(sn: str):
        b = sn.encode("utf-8")[:12].ljust(12, b"\x00")
        return list(struct.unpack(">6H", b))

    def setPMTSerialNumber(self, sn):
        data = self._pack_sn_to_registers(sn)
        self.safe_write_registers(address=0x08, values=data)

    def setHVSerialNumber(self, sn):
        data = self._pack_sn_to_registers(sn)
        self.safe_write_registers(address=0x0E, values=data)

    def setFEBSerialNumber(self, sn):
        data = self._pack_sn_to_registers(sn)
        self.safe_write_registers(address=0x14, values=data)

    def setModbusAddress(self, addr):
        self.client.write_register(address=0x00, value=addr, device_id=self.address)

    def readMonRegisters(self):
        monData = {}
        rr = self.client.read_holding_registers(address=0, count=54, device_id=self.address)

        if rr.isError():
            return None

        monData['status'] = rr.registers[0x0006]
        monData['Vset'] = rr.registers[0x0026]
        monData['V'] = ((rr.registers[0x002B] << 16) + rr.registers[0x002A]) / 1000
        monData['I'] = ((rr.registers[0x0029] << 16) + rr.registers[0x0028]) / 1000
        monData['T'] = self.convertTemperature(rr.registers[0x0007])
        monData['rateUP'] = rr.registers[0x0023]
        monData['rateDN'] = rr.registers[0x0024]
        monData['limitV'] = rr.registers[0x0027]
        monData['limitI'] = rr.registers[0x0025]
        monData['limitT'] = rr.registers[0x002F]
        monData['limitTRIP'] = rr.registers[0x0022]
        monData['thresholdm'] = rr.registers[0x002D]
        monData['thresholdq'] = rr.registers[0x0035]
        monData['alarm'] = rr.registers[0x002E]

        return monData

    @staticmethod
    def convertTemperature(value):
        q = (value & 0xFF) / 1000
        i = (value >> 8) & 0xFF
        return round(q + i, 1)

    def readCalibRegisters(self):
        rr = self.client.read_holding_registers(address=0x30, count=5, device_id=self.address)
        mlsb = rr.registers[0]
        mmsb = rr.registers[1]
        qlsb = rr.registers[2]
        qmsb = rr.registers[3]
        calibt = rr.registers[4]

        calibm = ((mmsb << 16) + mlsb)
        calibm = struct.unpack('l', struct.pack('L', calibm & 0xffffffff))[0]
        calibm = calibm / 10000

        calibq = ((qmsb << 16) + qlsb)
        calibq = struct.unpack('l', struct.pack('L', calibq & 0xffffffff))[0]
        calibq = calibq / 10000

        calibt = calibt / 1.6890722

        return calibm, calibq, calibt

    def writeCalibSlope(self, slope):
        slope = int(slope * 10000)
        lsb = (slope & 0xFFFF)
        msb = (slope >> 16) & 0xFFFF
        self.client.write_registers(address=0x30, values=[lsb, msb], device_id=self.address)

    def writeCalibOffset(self, offset):
        offset = int(offset * 10000)
        lsb = (offset & 0xFFFF)
        msb = (offset >> 16) & 0xFFFF
        self.client.write_registers(address=0x32, values=[lsb, msb], device_id=self.address)

    def writeCalibDiscr(self, discr):
        discr = int(discr * 1.6890722)
        self.client.write_register(address=0x34, value=discr, device_id=self.address)
