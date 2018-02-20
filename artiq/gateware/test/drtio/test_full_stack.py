import unittest
from types import SimpleNamespace
import random

from migen import *

from artiq.gateware.drtio import *
from artiq.gateware.drtio import rt_serializer
from artiq.gateware import rtio
from artiq.gateware.rtio import rtlink
from artiq.gateware.rtio.phy import ttl_simple
from artiq.coredevice.exceptions import *


class DummyTransceiverPair:
    def __init__(self, nwords):
        a2b_k = [Signal() for _ in range(nwords)]
        a2b_d = [Signal(8) for _ in range(nwords)]
        b2a_k = [Signal() for _ in range(nwords)]
        b2a_d = [Signal(8) for _ in range(nwords)]

        self.alice = SimpleNamespace(
            encoder=SimpleNamespace(k=a2b_k, d=a2b_d),
            decoders=[SimpleNamespace(k=k, d=d) for k, d in zip(b2a_k, b2a_d)],
            rx_ready=1
        )
        self.bob = SimpleNamespace(
            encoder=SimpleNamespace(k=b2a_k, d=b2a_d),
            decoders=[SimpleNamespace(k=k, d=d) for k, d in zip(a2b_k, a2b_d)],
            rx_ready=1
        )


class DummyRXSynchronizer:
    def resync(self, signal):
        return signal


class SimpleIOPHY(Module):
    def __init__(self, o_width, i_width):
        self.rtlink = rtlink.Interface(
            rtlink.OInterface(o_width),
            rtlink.IInterface(i_width, timestamped=True))
        self.received_data = Signal(o_width)
        self.sync.rio_phy += If(self.rtlink.o.stb,
            self.received_data.eq(self.rtlink.o.data))


class DUT(Module):
    def __init__(self, nwords):
        self.ttl0 = Signal()
        self.ttl1 = Signal()
        self.transceivers = DummyTransceiverPair(nwords)

        self.submodules.master = DRTIOMaster(self.transceivers.alice,
                                             fine_ts_width=0)
        self.submodules.master_ki = rtio.KernelInitiator(self.master.cri)

        rx_synchronizer = DummyRXSynchronizer()
        self.submodules.phy0 = ttl_simple.Output(self.ttl0)
        self.submodules.phy1 = ttl_simple.Output(self.ttl1)
        self.submodules.phy2 = SimpleIOPHY(512, 32)  # test wide output data
        rtio_channels = [
            rtio.Channel.from_phy(self.phy0),
            rtio.Channel.from_phy(self.phy1),
            rtio.Channel.from_phy(self.phy2),
        ]
        self.submodules.satellite = DRTIOSatellite(
            self.transceivers.bob, rtio_channels, rx_synchronizer,
            lane_count=4, fifo_depth=8, fine_ts_width=0)
        self.satellite.reset.storage.reset = 0
        self.satellite.reset.storage_full.reset = 0
        self.satellite.reset_phy.storage.reset = 0
        self.satellite.reset_phy.storage_full.reset = 0


class OutputsTestbench:
    def __init__(self):
        self.dut = DUT(2)
        self.now = 0

    def init(self):
        yield from self.dut.master.rt_controller.csrs.underflow_margin.write(100)
        while not (yield from self.dut.master.link_layer.link_status.read()):
            yield
        yield from self.get_buffer_space()

    def get_buffer_space(self):
        csrs = self.dut.master.rt_controller.csrs
        yield from csrs.o_get_buffer_space.write(1)
        yield
        while (yield from csrs.o_wait.read()):
            yield
        r = (yield from csrs.o_dbg_buffer_space.read())
        return r

    def delay(self, dt):
        self.now += dt

    def sync(self):
        t = self.now + 15
        while (yield self.dut.master.cri.counter) < t:
            yield

    def write(self, channel, data):
        kcsrs = self.dut.master_ki
        yield from kcsrs.chan_sel.write(channel)
        yield from kcsrs.timestamp.write(self.now)
        yield from kcsrs.o_data.write(data)
        yield from kcsrs.o_we.write(1)
        yield
        status = 1
        wlen = 0
        while status:
            status = yield from kcsrs.o_status.read()
            if status & 2:
                raise RTIOUnderflow
            yield
            wlen += 1
        return wlen

    @passive
    def check_ttls(self, ttl_changes):
        cycle = 0
        old_ttls = [0, 0]
        while True:
            ttls = [(yield self.dut.ttl0), (yield self.dut.ttl1)]
            for n, (old_ttl, ttl) in enumerate(zip(old_ttls, ttls)):
                if ttl != old_ttl:
                    ttl_changes.append((cycle, n))
            old_ttls = ttls
            yield
            cycle += 1


class TestFullStack(unittest.TestCase):
    clocks = {"sys": 8, "rtio": 5, "rtio_rx": 5,
              "rio": 5, "rio_phy": 5,
              "sys_with_rst": 8, "rtio_with_rst": 5}

    def test_pulses(self):
        tb = OutputsTestbench()
        ttl_changes = []
        correct_ttl_changes = [
            (208, 0),
            (213, 0),
            (213, 1),
            (219, 1),
        ]

        def test():
            yield from tb.init()
            tb.delay(200)
            yield from tb.write(0, 1)
            tb.delay(5)
            yield from tb.write(0, 0)
            yield from tb.write(1, 1)
            tb.delay(6)
            yield from tb.write(1, 0)
            yield from tb.sync()

        run_simulation(tb.dut,
            {"sys": test(), "rtio": tb.check_ttls(ttl_changes)}, self.clocks)
        self.assertEqual(ttl_changes, correct_ttl_changes)

    def test_underflow(self):
        tb = OutputsTestbench()

        def test():
            yield from tb.init()
            with self.assertRaises(RTIOUnderflow):
                yield from tb.write(0, 0)

        run_simulation(tb.dut, {"sys": test()}, self.clocks)

    def test_large_data(self):
        tb = OutputsTestbench()

        def test():
            yield from tb.init()
            correct_large_data = random.Random(0).randrange(2**512-1)
            self.assertNotEqual((yield tb.dut.phy2.received_data), correct_large_data)
            tb.delay(200)
            yield from tb.write(2, correct_large_data)
            yield from tb.sync()
            self.assertEqual((yield tb.dut.phy2.received_data), correct_large_data)

        run_simulation(tb.dut, {"sys": test()}, self.clocks)

    def test_buffer_space(self):
        tb = OutputsTestbench()
        ttl_changes = []
        correct_ttl_changes = [(258 + 40*i, 0) for i in range(10)]

        def test():
            yield from tb.init()
            tb.delay(250)
            max_wlen = 0
            for i in range(10):
                wlen = yield from tb.write(0, (i + 1) % 2)
                max_wlen = max(max_wlen, wlen)
                tb.delay(40)
            # check that some writes caused buffer space requests
            self.assertGreater(max_wlen, 5)
            yield from tb.sync()

        run_simulation(tb.dut,
            {"sys": test(), "rtio": tb.check_ttls(ttl_changes)}, self.clocks)
        self.assertEqual(ttl_changes, correct_ttl_changes)

    def test_tsc_error(self):
        tb = OutputsTestbench()

        def test():
            saterr = tb.dut.satellite.rt_errors
            csrs = tb.dut.master.rt_controller.csrs
            yield from tb.init()
            errors = yield from saterr.protocol_error.read()
            self.assertEqual(errors, 0)
            yield from csrs.tsc_correction.write(100000000)
            yield from csrs.set_time.write(1)
            for i in range(15):
               yield
            tb.delay(10000)
            yield from tb.write(0, 1)
            for i in range(12):
               yield
            errors = yield from saterr.protocol_error.read()
            self.assertEqual(errors, 4)  # write underflow
            yield from saterr.protocol_error.write(errors)
            yield
            errors = yield from saterr.protocol_error.read()
            self.assertEqual(errors, 0)

        run_simulation(tb.dut, {"sys": test()}, self.clocks)

    def test_inputs(self):
        dut = DUT(2)
        kcsrs = dut.master_ki

        def get_input(timeout):
            yield from kcsrs.chan_sel.write(2)
            yield from kcsrs.timestamp.write(10)
            yield from kcsrs.i_request.write(1)
            yield
            status = yield from kcsrs.i_status.read()
            while status & 0x4:
                yield
                status = yield from kcsrs.i_status.read()
            if status & 0x1:
                return "timeout"
            if status & 0x2:
                return "overflow"
            return ((yield from kcsrs.i_data.read()),
                    (yield from kcsrs.i_timestamp.read()))

        def test():
            while not (yield from dut.master.link_layer.link_status.read()):
                yield

            i1 = yield from get_input(10)
            i2 = yield from get_input(20)
            self.assertEqual(i1, (0x600d1dea, 6))
            self.assertEqual(i2, "timeout")

        def generate_input():
            for i in range(5):
                yield
            yield dut.phy2.rtlink.i.data.eq(0x600d1dea)
            yield dut.phy2.rtlink.i.stb.eq(1)
            yield
            yield dut.phy2.rtlink.i.data.eq(0)
            yield dut.phy2.rtlink.i.stb.eq(0)

        run_simulation(dut,
            {"sys": test(), "rtio": generate_input()}, self.clocks)

    def test_echo(self):
        dut = DUT(2)
        csrs = dut.master.rt_controller.csrs
        mgr = dut.master.rt_manager

        def test():
            while not (yield from dut.master.link_layer.link_status.read()):
                yield

            yield from mgr.update_packet_cnt.write(1)
            yield
            self.assertEqual((yield from mgr.packet_cnt_tx.read()), 0)
            self.assertEqual((yield from mgr.packet_cnt_rx.read()), 0)

            yield from mgr.request_echo.write(1)

            for i in range(15):
                yield

            yield from mgr.update_packet_cnt.write(1)
            yield
            self.assertEqual((yield from mgr.packet_cnt_tx.read()), 1)
            self.assertEqual((yield from mgr.packet_cnt_rx.read()), 1)

        run_simulation(dut, test(), self.clocks)
