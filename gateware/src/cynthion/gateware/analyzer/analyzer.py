#
# This file is part of Cynthion.
#
# Copyright (c) 2020-2023 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause

""" Low-level USB analyzer gateware. """

import unittest

from amaranth          import Signal, Module, Elaboratable, Memory, Record, Mux, Cat, C, Array

from luna.gateware.stream import StreamInterface
from luna.gateware.test   import LunaGatewareTestCase, usb_domain_test_case

from .fifo import Stream16to8, StreamFIFO, AsyncFIFOReadReset
from .speeds import USBAnalyzerSpeed
from .events import USBAnalyzerEvent


TRIGGER_MAX_STAGES = 8
TRIGGER_MAX_PATTERN_BYTES = 32


class _DefaultTriggerConfig:
    """Fallback trigger config used when top-level trigger wiring is omitted."""

    def __init__(self):
        self.max_stages = TRIGGER_MAX_STAGES
        self.max_pattern = TRIGGER_MAX_PATTERN_BYTES
        self.pattern_bits = (self.max_pattern - 1).bit_length()
        self.stage_bits = (self.max_stages - 1).bit_length()

        self.enable = Signal(reset=0)
        self.armed = Signal(reset=0)
        self.output_enable = Signal(reset=0)
        self.stage_count = Signal(range(self.max_stages + 1), reset=0)

        self.stage_offsets = Array(
            Signal(16, name=f"default_trigger_stage_{i}_offset")
            for i in range(self.max_stages)
        )
        self.stage_lengths = Array(
            Signal(8, name=f"default_trigger_stage_{i}_length")
            for i in range(self.max_stages)
        )
        self.patterns_flat = Array(
            Signal(8, name=f"default_trigger_pattern_{i}")
            for i in range(self.max_stages * self.max_pattern)
        )
        self.masks_flat = Array(
            Signal(8, reset=0xFF, name=f"default_trigger_mask_{i}")
            for i in range(self.max_stages * self.max_pattern)
        )

        flat_depth = self.max_stages * self.max_pattern
        self.pattern_write_en = Signal()
        self.pattern_write_addr = Signal(range(flat_depth))
        self.pattern_write_data = Signal(8)
        self.mask_write_en = Signal()
        self.mask_write_addr = Signal(range(flat_depth))
        self.mask_write_data = Signal(8)

        self.arm_strobe = Signal()
        self.disarm_strobe = Signal()


class USBAnalyzer(Elaboratable):
    """ Core USB analyzer; backed by a small ringbuffer in FPGA block RAM.

    If you're looking to instantiate a full analyzer, you'll probably want to grab
    one of the DRAM-based ringbuffer variants (which are currently forthcoming).

    If you're looking to use this with a ULPI PHY, rather than the FPGA-convenient UTMI interface,
    grab the UTMITranslator from `luna.gateware.interface.ulpi`.

    Attributes
    ----------
    stream: StreamInterface(), output stream
        Stream that carries USB analyzer data.

    idle: Signal(), output
        Asserted iff the analyzer is not currently receiving data.
    stopped: Signal(), output
        Asserted iff the analyzer is stopped and not capturing packets.
    overrun: Signal(), output
        Asserted iff the analyzer has received more data than it can store in its internal buffer.
        Occurs if :attr:``stream`` is not being read quickly enough.
    capturing: Signal(), output
        Asserted iff the analyzer is currently capturing a packet.
    starting: Signal(), output
        Asserted iff the analyzer is starting capture on this cycle.


    Parameters
    ----------
    utmi_interface: UTMIInterface()
        The UTMI interface that carries the data to be analyzed.
    mem_depth: int, default=8192
        The depth of the analyzer's local ringbuffer, in bytes.
        Must be a power of 2.
    """

    # Header is 16-bit length and 16-bit timestamp.
    HEADER_SIZE_BYTES = 4
    HEADER_SIZE_WORDS = HEADER_SIZE_BYTES // 2

    # Event is 0xFF marker, 8-bit event code and 16-bit timestamp.
    EVENT_SIZE_BYTES = 4
    EVENT_SIZE_WORDS = HEADER_SIZE_BYTES // 2

    # Support a maximum payload size of 1024B, plus a 1-byte PID and a 2-byte CRC16.
    MAX_PACKET_SIZE_BYTES = 1024 + 1 + 2

    def __init__(
        self,
        utmi_interface,
        session_valid,
        speed_selection,
        event_strobe,
        event_code,
        mem_depth=4096,
        trigger=None,
    ):
        """
        Parameters:
            utmi_interface -- A record or elaboratable that presents a UTMI interface.
        """

        self.utmi = utmi_interface
        self.session_valid = session_valid
        self.speed_selection = speed_selection
        self.event_strobe = event_strobe
        self.event_code = event_code
        self.trigger = trigger if trigger is not None else _DefaultTriggerConfig()

        assert (mem_depth % 2) == 0, "mem_depth must be a power of 2"

        # Internal storage memory.
        self.mem = Memory(width=16, depth=mem_depth, name="analysis_ringbuffer")
        self.mem_size_words = mem_depth
        self.mem_size_bytes = 2 * mem_depth

        #
        # I/O port
        #
        self.stream         = StreamInterface(payload_width=16)

        self.capture_enable = Signal()
        self.idle           = Signal()
        self.stopped        = Signal()
        self.overrun        = Signal()
        self.capturing      = Signal()
        self.starting       = Signal()

        self.trigger_toggle_out = Signal()
        self.trigger_fire_strobe = Signal()
        self.trigger_sequence_stage = Signal(range(self.trigger.max_stages + 1))
        self.trigger_fire_count = Signal(16)


    def elaborate(self, platform):
        m = Module()

        # Memory read and write ports.
        m.submodules.read  = mem_read_port  = self.mem.read_port(domain="sync", transparent=False)
        m.submodules.write = mem_write_port = self.mem.write_port(domain="sync", granularity=8)

        # FIFO write addresses point to bytes.
        write_byte_addr  = Signal(range(self.mem_size_bytes))

        # Memory addresses point to words
        header_word_addr = Signal.like(mem_write_port.addr)
        write_word_addr  = Signal.like(mem_write_port.addr)
        read_word_addr   = Signal.like(mem_read_port.addr)
        fifo_word_count  = Signal.like(mem_read_port.addr)
        write_odd        = Signal()
        m.d.comb += Cat(write_odd, write_word_addr).eq(write_byte_addr)

        # Current receive status.
        packet_size     = Signal(16)
        packet_time     = Signal(16)
        event_code      = Signal(8)

        # Triggers for memory write operations.
        new_packet      = Signal()
        write_packet    = Signal()
        write_header    = Signal()
        write_event     = Signal()

        # Trigger matching state.
        packet_byte_index = Signal(range(self.MAX_PACKET_SIZE_BYTES + 1))
        stage_mismatch = Signal()
        stage_match_count = Signal(range(self.trigger.max_pattern + 1))
        seq_expected_stage = Signal(range(self.trigger.max_stages + 1))
        pending_trigger_event = Signal()
        pending_stage_compare = Signal()
        pending_stage_byte = Signal(8)

        active_stage_index = Signal(range(self.trigger.max_stages))
        active_stage_offset = Signal(16)
        active_stage_len_raw = Signal(8)
        active_stage_len = Signal(8)
        active_stage_valid = Signal()

        stage_window_hit = Signal()
        pattern_index = Signal(range(self.trigger.max_pattern))
        pattern_flat_index = Signal(range(self.trigger.max_stages * self.trigger.max_pattern))
        pending_stage_mismatch = Signal()
        stage_mismatch_effective = Signal()

        # Pattern/mask memories let synthesis map match tables into BRAM
        # rather than wide mux trees from dynamic Array indexing.
        flat_depth = self.trigger.max_stages * self.trigger.max_pattern
        pattern_memory = Memory(width=8, depth=flat_depth, init=[0] * flat_depth)
        mask_memory = Memory(width=8, depth=flat_depth, init=[0xFF] * flat_depth)
        m.submodules.pattern_read_port = pattern_read_port = \
            pattern_memory.read_port(domain="usb", transparent=False)
        m.submodules.mask_read_port = mask_read_port = \
            mask_memory.read_port(domain="usb", transparent=False)
        m.submodules.pattern_write_port = pattern_write_port = \
            pattern_memory.write_port(domain="usb")
        m.submodules.mask_write_port = mask_write_port = \
            mask_memory.write_port(domain="usb")

        m.d.comb += [
            active_stage_index.eq(seq_expected_stage[0:self.trigger.stage_bits]),
            active_stage_offset.eq(self.trigger.stage_offsets[active_stage_index]),
            active_stage_len_raw.eq(self.trigger.stage_lengths[active_stage_index]),
            active_stage_len.eq(
                Mux(
                    active_stage_len_raw > self.trigger.max_pattern,
                    self.trigger.max_pattern,
                    active_stage_len_raw,
                )
            ),
            active_stage_valid.eq(
                self.trigger.enable &
                self.trigger.armed &
                (seq_expected_stage < self.trigger.stage_count) &
                (seq_expected_stage < self.trigger.max_stages)
            ),
            stage_window_hit.eq(
                active_stage_valid &
                (packet_byte_index >= active_stage_offset) &
                (packet_byte_index < (active_stage_offset + active_stage_len))
            ),
            pattern_index.eq(packet_byte_index - active_stage_offset),
            pattern_flat_index.eq(active_stage_index * self.trigger.max_pattern + pattern_index),
            pattern_read_port.en.eq(stage_window_hit),
            pattern_read_port.addr.eq(pattern_flat_index),
            mask_read_port.en.eq(stage_window_hit),
            mask_read_port.addr.eq(pattern_flat_index),
            pattern_write_port.en.eq(self.trigger.pattern_write_en),
            pattern_write_port.addr.eq(self.trigger.pattern_write_addr),
            pattern_write_port.data.eq(self.trigger.pattern_write_data),
            mask_write_port.en.eq(self.trigger.mask_write_en),
            mask_write_port.addr.eq(self.trigger.mask_write_addr),
            mask_write_port.data.eq(self.trigger.mask_write_data),
            pending_stage_mismatch.eq(
                (pending_stage_byte & mask_read_port.data) !=
                (pattern_read_port.data & mask_read_port.data)
            ),
            stage_mismatch_effective.eq(stage_mismatch | (pending_stage_compare & pending_stage_mismatch)),
            self.trigger_sequence_stage.eq(seq_expected_stage),
        ]

        # Use the FIFO as our stream source.
        m.d.comb += self.stream.payload.eq(mem_read_port.data)
        with m.If(~self.stream.valid | self.stream.ready):
            # The stream produces the next word when there is data in the FIFO.
            m.d.comb += [
                mem_read_port.en    .eq(fifo_word_count != 0)
            ]
            m.d.sync += [
                self.stream.valid   .eq(mem_read_port.en),
                self.stream.last    .eq(fifo_word_count == 1),
            ]
        with m.Else():
            m.d.comb += mem_read_port.en.eq(0)

        # When a word is read from the FIFO, move to the next address.
        m.d.comb += mem_read_port.addr.eq(read_word_addr)
        with m.If(mem_read_port.en):
            m.d.sync += read_word_addr.eq(read_word_addr + 1)

        #
        # FIFO count handling.
        #

        # Number of words popped from the FIFO this cycle.
        fifo_words_popped = Signal(1)

        # Number of uncommitted words and its push trigger.
        fifo_words_pending = Signal(11)
        data_commit  = Signal()

        # One word is popped if the FIFO stream is read.
        m.d.comb += fifo_words_popped.eq(mem_read_port.en)

        # On startup, set counts to zero.
        with m.If(self.starting & ~data_commit):
            m.d.sync += [
                self.stream.valid.eq(0),
                write_byte_addr.eq(0),
                fifo_word_count.eq(0),
                read_word_addr.eq(0),
                fifo_words_pending.eq(0),
            ]
        # Otherwise, update the count acording to words pushed and popped.
        with m.Else():
            fifo_next_count = fifo_word_count - fifo_words_popped
            with m.If(data_commit):
                m.d.sync += fifo_word_count.eq(fifo_next_count + fifo_words_pending)
            with m.Else():
                m.d.sync += fifo_word_count.eq(fifo_next_count)


        # Timestamp counter.
        current_time = Signal(16)
        m.d.usb += [
            current_time.eq(current_time + 1),
            self.trigger_fire_strobe.eq(0),
        ]

        #
        # Core analysis FSM.
        #
        with m.FSM(domain="usb") as f:
            m.d.comb += [
                self.idle      .eq(f.ongoing("AWAIT_START") | f.ongoing("AWAIT_PACKET")),
                self.stopped   .eq(f.ongoing("AWAIT_START")),
                self.overrun   .eq(f.ongoing("OVERRUN")),
                self.capturing .eq(f.ongoing("CAPTURE_PACKET")),
                self.starting  .eq(self.stopped & self.capture_enable),
            ]

            # AWAIT_START: wait for capture to be enabled, but don't start mid-packet.
            with m.State("AWAIT_START"):
                m.d.usb += [
                    current_time.eq(0),
                    packet_byte_index.eq(0),
                    stage_mismatch.eq(0),
                    stage_match_count.eq(0),
                    pending_stage_compare.eq(0),
                    seq_expected_stage.eq(0),
                    pending_trigger_event.eq(0),
                ]
                with m.If(self.capture_enable & ~self.utmi.rx_active):
                    # Capture is being started.
                    m.next = "AWAIT_PACKET"
                    m.d.usb += current_time.eq(0)
                    # Log a start event indicating the speed in use.
                    start_event = USBAnalyzerEvent.CAPTURE_START_BASE \
                                      | self.speed_selection
                    m.d.comb += [
                        write_event .eq(1),
                        event_code  .eq(start_event),
                    ]


            # AWAIT_PACKET: capture is enabled, wait for a packet to start.
            with m.State("AWAIT_PACKET"):
                with m.If(
                    self.trigger.disarm_strobe |
                    ~self.trigger.enable |
                    ~self.trigger.armed
                ):
                    m.d.usb += seq_expected_stage.eq(0)

                with m.If(self.trigger.arm_strobe):
                    m.d.usb += seq_expected_stage.eq(0)

                with m.If(~self.capture_enable):
                    # Capture is being stopped.
                    m.next = "AWAIT_START"
                    # Log a stop event.
                    m.d.comb += [
                        write_event        .eq(1),
                        event_code         .eq(USBAnalyzerEvent.CAPTURE_STOP_NORMAL),
                    ]
                with m.Elif(pending_trigger_event):
                    m.d.comb += [
                        write_event        .eq(1),
                        event_code         .eq(USBAnalyzerEvent.TRIGGER_FIRED),
                    ]
                    m.d.usb += [
                        pending_trigger_event.eq(0),
                        current_time.eq(1),
                    ]
                with m.Elif(
                    self.utmi.rx_active &
                    self.session_valid &
                    (self.speed_selection != USBAnalyzerSpeed.AUTO)
                ):
                    m.d.comb += new_packet.eq(1)
                    m.next = "CAPTURE_PACKET"
                    m.d.usb += [
                        packet_size        .eq(0),
                        packet_time        .eq(current_time),
                        current_time       .eq(0),
                        packet_byte_index  .eq(0),
                        stage_mismatch     .eq(0),
                        stage_match_count  .eq(0),
                        pending_stage_compare.eq(0),
                    ]
                with m.Elif(self.event_strobe):
                    # Event detected externally.
                    m.d.comb += [
                        write_event        .eq(1),
                        event_code         .eq(self.event_code),
                    ]
                    # After writing the event, reset our timestamp counter.
                    # An event that occurs next cycle will have a timestamp
                    # one cycle after the current event.
                    m.d.usb += current_time.eq(1)
                with m.Elif(current_time == 0xFFFF):
                    # The timestamp is about to wrap. Write a dummy event.
                    m.d.comb += [
                        write_event        .eq(1),
                        event_code         .eq(USBAnalyzerEvent.NONE),
                    ]


            # Capture data until the packet is complete.
            with m.State("CAPTURE_PACKET"):

                byte_received = self.utmi.rx_valid & self.utmi.rx_active

                m.d.usb += pending_stage_compare.eq(0)
                with m.If(pending_stage_compare & pending_stage_mismatch):
                    m.d.usb += stage_mismatch.eq(1)

                # Capture data whenever RxValid is asserted.
                m.d.comb += [
                    write_packet    .eq(byte_received),
                ]

                # Advance the write pointer each time we receive a bit.
                with m.If(byte_received):
                    m.d.usb += [
                        packet_size        .eq(packet_size + 1),
                        packet_byte_index  .eq(packet_byte_index + 1),
                    ]

                    with m.If(stage_window_hit):
                        m.d.usb += [
                            stage_match_count.eq(stage_match_count + 1),
                            pending_stage_compare.eq(1),
                            pending_stage_byte.eq(self.utmi.rx_data),
                        ]

                    # If this would be filling up our data memory,
                    # move to the OVERRUN state.
                    with m.If(fifo_word_count + fifo_words_pending == self.mem_size_words - 1):
                        m.next = "OVERRUN"

                # If we've stopped receiving, write header.
                with m.If(~self.utmi.rx_active):
                    m.d.comb += [
                        write_header .eq(1),
                    ]

                    with m.If(
                        active_stage_valid &
                        (active_stage_len > 0) &
                        ~stage_mismatch_effective &
                        (stage_match_count == active_stage_len) &
                        (packet_size >= (active_stage_offset + active_stage_len))
                    ):
                        with m.If((seq_expected_stage + 1) >= self.trigger.stage_count):
                            m.d.usb += [
                                seq_expected_stage.eq(0),
                                self.trigger_fire_strobe.eq(1),
                                self.trigger_fire_count.eq(self.trigger_fire_count + 1),
                                pending_trigger_event.eq(1),
                            ]
                            with m.If(self.trigger.output_enable):
                                m.d.usb += self.trigger_toggle_out.eq(~self.trigger_toggle_out)
                        with m.Else():
                            m.d.usb += seq_expected_stage.eq(seq_expected_stage + 1)

                    m.next = "AWAIT_PACKET"


            # BABBLE -- handles the case in which we've received a packet beyond
            # the allowable size in the USB spec
            with m.State("BABBLE"):

                # Trap here, for now.
                pass


            with m.State("OVERRUN"):
                # TODO: we should probably set an overrun flag and then emit an EOP, here?

                # If capture is stopped by the host, reset back to the ready state.
                with m.If(~self.capture_enable):
                    m.next = "AWAIT_START"


        #
        # Buffer write FSM.
        #
        next_byte_addr_aligned = Mux(self.starting, 0, write_byte_addr + write_odd)
        next_word_addr = next_byte_addr_aligned[1:]

        with m.FSM(domain="sync"):
            # START: Begin write operation when requested.
            with m.State("START"):
                with m.If(new_packet):
                    # Allocate new packet header.
                    m.d.sync += [
                        header_word_addr     .eq(next_word_addr),
                        fifo_words_pending   .eq(self.HEADER_SIZE_WORDS),
                        write_byte_addr      .eq(next_byte_addr_aligned + self.HEADER_SIZE_BYTES),
                    ]
                    m.next = "IDLE"
                with m.Elif(write_packet):
                    # Write packet byte.
                    m.d.comb += [
                        mem_write_port.addr  .eq(write_word_addr),
                        mem_write_port.data  .eq(self.utmi.rx_data.replicate(2)),
                        mem_write_port.en    .eq(Mux(write_odd, 0b01, 0b10)),
                    ]
                    m.d.sync += [
                        fifo_words_pending   .eq(fifo_words_pending + ~write_odd),
                        write_byte_addr      .eq(write_byte_addr + 1),
                    ]
                    m.next = "IDLE"
                with m.Elif(write_header):
                    # Write first word of header.
                    m.d.comb += [
                        mem_write_port.addr  .eq(header_word_addr),
                        mem_write_port.data  .eq(packet_size),
                        mem_write_port.en    .eq(0b11)
                    ]
                    m.next = "FINISH_HEADER"
                with m.Elif(write_event):
                    # Write event identifier and event code.
                    m.d.comb += [
                        mem_write_port.addr  .eq(next_word_addr),
                        mem_write_port.data  .eq(Cat(event_code, C(0xFF, 8))),
                        mem_write_port.en    .eq(0b11),
                    ]
                    m.d.sync += [
                        header_word_addr     .eq(next_word_addr),
                        fifo_words_pending   .eq(self.EVENT_SIZE_WORDS),
                        write_byte_addr      .eq(next_byte_addr_aligned + self.EVENT_SIZE_BYTES),
                    ]
                    m.next = "FINISH_EVENT"

            # FINISH_HEADER: Write second word of header.
            with m.State("FINISH_HEADER"):
                m.d.comb += [
                        mem_write_port.addr  .eq(header_word_addr + 1),
                        mem_write_port.data  .eq(packet_time),
                        mem_write_port.en    .eq(0b11),
                        data_commit          .eq(1),
                ]
                m.next = "START"

            # FINISH_EVENT: Write second word of event.
            with m.State("FINISH_EVENT"):
                m.d.comb += [
                        mem_write_port.addr  .eq(header_word_addr + 1),
                        mem_write_port.data  .eq(current_time),
                        mem_write_port.en    .eq(0b11),
                        data_commit          .eq(1),
                ]
                m.next = "START"

            # IDLE: Nothing to do this cycle.
            with m.State("IDLE"):
                m.next = "START"


        return m


class USBAnalyzerTestBase(LunaGatewareTestCase):

    SYNC_CLOCK_FREQUENCY = 120e6
    USB_CLOCK_FREQUENCY = 60e6

    def expect_data(self, expected_data):
        # Check the stream reports data available.
        self.assertEqual((yield self.stream.valid), 1)

        # Check that the expected data is set up.
        self.assertEqual((yield self.stream.payload), expected_data[0])

        # Signal that we are ready to receive data.
        yield self.stream.ready.eq(1)
        yield

        # Validate that we get all of the bytes we expected.
        received_data = []
        for datum in expected_data:
            if (yield self.stream.valid):
                received_data.append((yield self.stream.payload))
                yield
            else:
                # Data ended early.
                break
        self.assertEqual(received_data, expected_data)

        if len(expected_data) % 2 == 1:
            # There should then be one padding byte.
            self.assertEqual((yield self.stream.valid), 1)
            yield

        # There should then be no data left.
        self.assertEqual((yield self.stream.valid), 0)


class USBAnalyzerTest(USBAnalyzerTestBase):

    def instantiate_dut(self):

        from amaranth import DomainRenamer, ResetInserter

        self.utmi = Record([
            ('tx_data',     8),
            ('rx_data',     8),

            ('rx_valid',    1),
            ('rx_active',   1),
            ('rx_error',    1),
            ('rx_complete', 1),
        ])

        session_valid = True
        speed = C(0x00, 2)
        speed_changing = False
        next_speed = speed

        m = Module()
        m.submodules.analyzer = self.analyzer = USBAnalyzer(
            self.utmi, session_valid, speed, speed_changing, next_speed, mem_depth=128)
        reset_on_start = ResetInserter(self.analyzer.starting)
        m.submodules.s16to8 = s16to8 = reset_on_start(Stream16to8())
        m.submodules.clk_conv = clk_conv = StreamFIFO(
            AsyncFIFOReadReset(width=8, depth=4, r_domain="usb", w_domain="sync"))
        m.d.comb += [
            s16to8.input.stream_eq(self.analyzer.stream),
            clk_conv.input.stream_eq(s16to8.output),
            clk_conv.fifo.ext_rst.eq(self.analyzer.starting),
        ]
        self.stream = clk_conv.output
        return m


    def advance_stream(self, value):
        yield self.utmi.rx_data.eq(value)
        yield


    @usb_domain_test_case
    def test_single_packet(self):
        # Enable capture
        yield self.analyzer.capture_enable.eq(1)
        yield

        # Ensure we're not capturing until a transaction starts.
        self.assertEqual((yield self.analyzer.capturing), 0)

        # Apply our first input, and validate that we start capturing.
        yield self.utmi.rx_active.eq(1)
        yield self.utmi.rx_valid.eq(1)
        yield self.utmi.rx_data.eq(0)
        yield
        yield

        # Provide some data.
        for i in range(1, 10):
            yield from self.advance_stream(i)
            self.assertEqual((yield self.analyzer.capturing), 1)

        # Ensure we're still capturing, _and_ that we have
        # data available.
        self.assertEqual((yield self.analyzer.capturing), 1)

        # End our packet.
        yield self.utmi.rx_active.eq(0)
        yield from self.advance_stream(10)

        # Idle for several cycles.
        yield from self.advance_cycles(5)
        self.assertEqual((yield self.analyzer.capturing), 0)

        # First we should get a start event with a timestamp of zero.
        start_event = [0xFF, 0x04, 0x00, 0x00]

        # Next, we should get a header with the total data length.
        # This should be 0x00, 0x0a; as we captured 10 bytes.
        #
        # Next, we should get a timestamp with the cycle count at which
        # the packet started. This should be zero.
        packet = [0x00, 0x0a, 0x00, 0x00] + list(range(0, 10))

        yield from self.expect_data(start_event + packet)


    @usb_domain_test_case
    def test_slow_packet(self):
        # Enable capture
        yield self.analyzer.capture_enable.eq(1)
        yield

        # Start a packet.
        yield self.utmi.rx_active.eq(1)
        yield

        # Provide a byte every 40 cycles, as in full speed capture.
        for byte in range(0, 10):
            yield from self.advance_cycles(39)
            yield self.utmi.rx_data.eq(byte)
            yield self.utmi.rx_valid.eq(1)
            yield
            yield self.utmi.rx_valid.eq(0)

        # End our packet.
        yield self.utmi.rx_active.eq(0)

        # Idle for several cycles.
        yield from self.advance_cycles(5)

        # First we should get a start event with a timestamp of zero.
        start_event = [0xFF, 0x04, 0x00, 0x00]

        # First, we should get a header with the total data length.
        # This should be 0x00, 0x0a; as we captured 10 bytes.
        #
        # Next, we should get a timestamp with the cycle count at which
        # the packet started. This should be zero.
        #
        # Finally, there should be the 10 packet bytes.
        packet = [0x00, 0x0a, 0x00, 0x00] + list(range(0, 10))

        yield from self.expect_data(start_event + packet)


    @usb_domain_test_case
    def test_short_packet(self):
        # Enable capture
        yield self.analyzer.capture_enable.eq(1)
        yield

        # Apply our first input, and validate that we start capturing.
        yield self.utmi.rx_active.eq(1)
        yield self.utmi.rx_valid.eq(1)
        yield self.utmi.rx_data.eq(0)
        yield

        # Provide some data.
        yield from self.advance_stream(0xAB)

        # End our packet.
        yield self.utmi.rx_active.eq(0)
        yield from self.advance_stream(10)

        # Idle for several cycles.
        yield from self.advance_cycles(5)
        self.assertEqual((yield self.analyzer.capturing), 0)

        # First we should get a start event with a timestamp of zero.
        start_event = [0xFF, 0x04, 0x00, 0x00]

        # Next, we should get a header with the total data length.
        # This should be 0x00, 0x01; as we captured 1 byte.
        # Then, we should get a timestamp with the cycle count at which
        # the packet started. This should be 0x00, 0x00.
        packet = [0x00, 0x01, 0x00, 0x00, 0xab]

        yield from self.expect_data(start_event + packet)


    @usb_domain_test_case
    def test_timestamp_wrap(self):
        # Enable capture.
        yield self.analyzer.capture_enable.eq(1)
        yield

        # Nothing happens for 0x10123 cycles.
        yield from self.advance_cycles(0x10123)

        # Then there's a one-byte packet.
        yield self.utmi.rx_active.eq(1)
        yield self.utmi.rx_valid.eq(1)
        yield self.utmi.rx_data.eq(0)
        yield
        yield from self.advance_stream(0xAB)
        yield self.utmi.rx_active.eq(0)
        yield from self.advance_stream(10)

        # First we should get a start event with a timestamp of zero.
        start_event = [0xFF, 0x04, 0x00, 0x00]

        # Then, we should get an event with code zero, timestamp 0xFFFF.
        rollover_event = [0xFF, 0x00, 0xFF, 0xFF]

        # Next we should get the packet, with length 1 and timestamp 0x0123.
        packet = [0x00, 0x01, 0x01, 0x23, 0xAB]

        yield from self.expect_data(start_event + rollover_event + packet)


    @usb_domain_test_case
    def test_stop_event(self):

        # Start capture.
        yield self.analyzer.capture_enable.eq(1)
        yield

        # Nothing happens for 0x123 cycles.
        yield from self.advance_cycles(0x123)

        # Stop capture.
        yield self.analyzer.capture_enable.eq(0)
        yield

        # First we should get a start event with a timestamp of zero.
        start_event = [0xFF, 0x04, 0x00, 0x00]

        # Then, we should get an stop event with a timestamp of 0x123.
        stop_event = [0xFF, 0x01, 0x01, 0x23]

        # Validate that we get all of the expected bytes.
        yield from self.expect_data(start_event + stop_event)


    @usb_domain_test_case
    def test_trigger_single_stage_match(self):
        # Configure one trigger stage:
        # Match bytes [AA BB CC] starting at packet offset 1.
        yield self.analyzer.trigger.enable.eq(1)
        yield self.analyzer.trigger.armed.eq(1)
        yield self.analyzer.trigger.output_enable.eq(1)
        yield self.analyzer.trigger.stage_count.eq(1)
        yield self.analyzer.trigger.stage_offsets[0].eq(1)
        yield self.analyzer.trigger.stage_lengths[0].eq(3)
        for i, value in enumerate((0xAA, 0xBB, 0xCC)):
            yield self.analyzer.trigger.pattern_write_addr.eq(i)
            yield self.analyzer.trigger.pattern_write_data.eq(value)
            yield self.analyzer.trigger.pattern_write_en.eq(1)
            yield
        yield self.analyzer.trigger.pattern_write_en.eq(0)
        yield

        # Start capture and send a matching packet.
        yield self.analyzer.capture_enable.eq(1)
        yield
        yield self.utmi.rx_active.eq(1)
        yield self.utmi.rx_valid.eq(1)
        yield self.utmi.rx_data.eq(0x10)
        yield
        yield
        yield from self.advance_stream(0xAA)
        yield from self.advance_stream(0xBB)
        yield from self.advance_stream(0xCC)
        yield self.utmi.rx_active.eq(0)
        yield self.utmi.rx_valid.eq(0)
        yield
        yield from self.advance_cycles(3)

        # Trigger should have fired exactly once and toggled output high.
        self.assertEqual((yield self.analyzer.trigger_toggle_out), 1)
        self.assertEqual((yield self.analyzer.trigger_fire_count), 1)


    @usb_domain_test_case
    def test_trigger_single_stage_mismatch(self):
        # Configure one trigger stage:
        # Match bytes [AA BB CC] starting at packet offset 1.
        yield self.analyzer.trigger.enable.eq(1)
        yield self.analyzer.trigger.armed.eq(1)
        yield self.analyzer.trigger.output_enable.eq(1)
        yield self.analyzer.trigger.stage_count.eq(1)
        yield self.analyzer.trigger.stage_offsets[0].eq(1)
        yield self.analyzer.trigger.stage_lengths[0].eq(3)
        for i, value in enumerate((0xAA, 0xBB, 0xCC)):
            yield self.analyzer.trigger.pattern_write_addr.eq(i)
            yield self.analyzer.trigger.pattern_write_data.eq(value)
            yield self.analyzer.trigger.pattern_write_en.eq(1)
            yield
        yield self.analyzer.trigger.pattern_write_en.eq(0)
        yield

        # Start capture and send a non-matching packet.
        yield self.analyzer.capture_enable.eq(1)
        yield
        yield self.utmi.rx_active.eq(1)
        yield self.utmi.rx_valid.eq(1)
        yield self.utmi.rx_data.eq(0x10)
        yield
        yield
        yield from self.advance_stream(0xAA)
        yield from self.advance_stream(0x99)  # mismatch at second compared byte
        yield from self.advance_stream(0xCC)
        yield self.utmi.rx_active.eq(0)
        yield self.utmi.rx_valid.eq(0)
        yield
        yield from self.advance_cycles(3)

        # Trigger should not fire.
        self.assertEqual((yield self.analyzer.trigger_toggle_out), 0)
        self.assertEqual((yield self.analyzer.trigger_fire_count), 0)


class USBAnalyzerStackTest(USBAnalyzerTestBase):
    """ Test that evaluates a full-stack USB analyzer setup. """

    def instantiate_dut(self):

        from luna.gateware.interface.ulpi import UTMITranslator
        from amaranth import DomainRenamer, ResetInserter

        self.ulpi = Record([
            ('data', [
                ('i',  8),
                ('o',  8),
                ('oe', 8),
            ]),
            ('nxt', [('i', 1)]),
            ('stp', [('o', 1)]),
            ('dir', [('i', 1)]),
            ('clk', [('o', 1)]),
            ('rst', [('o', 1)]),
        ])

        session_valid = True
        speed = C(0x00, 2)
        speed_changing = False
        next_speed = speed

        # Create a stack of our UTMITranslator and our USBAnalyzer.
        # We'll wrap the both in a module to establish a synthetic hierarchy.
        m = Module()
        m.submodules.translator = self.translator = UTMITranslator(ulpi=self.ulpi, handle_clocking=False)
        m.submodules.analyzer   = self.analyzer = USBAnalyzer(
            self.translator, session_valid, speed, speed_changing, next_speed, mem_depth=128)
        reset_on_start = ResetInserter(self.analyzer.starting)
        m.submodules.s16to8 = s16to8 = reset_on_start(Stream16to8())
        m.submodules.clk_conv = clk_conv = StreamFIFO(
            AsyncFIFOReadReset(width=8, depth=4, r_domain="usb", w_domain="sync"))
        m.d.comb += [
            s16to8.input.stream_eq(self.analyzer.stream),
            clk_conv.input.stream_eq(s16to8.output),
            clk_conv.fifo.ext_rst.eq(self.analyzer.starting),
        ]
        self.stream = clk_conv.output
        return m


    def initialize_signals(self):

        # Ensure the translator doesn't need to perform any register reads/writes
        # by default, so we can focus on packet Rx.
        yield self.translator.xcvr_select.eq(1)
        yield self.translator.dm_pulldown.eq(1)
        yield self.translator.dp_pulldown.eq(1)
        yield self.translator.use_external_vbus_indicator.eq(0)


    @usb_domain_test_case
    def test_simple_analysis(self):
        # Enable capture
        yield self.analyzer.capture_enable.eq(1)
        yield from self.advance_cycles(10)

        # Start a new packet.
        yield self.ulpi.dir.i.eq(1)
        yield self.ulpi.nxt.i.eq(1)

        # Bus turnaround packet.
        yield self.ulpi.data.i.eq(0x80)
        yield

        # Provide some data to be captured.
        for i in [0x2d, 0x00, 0x10]:
            yield self.ulpi.data.i.eq(i)
            yield

        # Mark our packet as complete.
        yield self.ulpi.dir.i.eq(0)
        yield self.ulpi.nxt.i.eq(0)
        yield

        # Wait for a few cycles, for realism.
        yield from self.advance_cycles(10)

        # First we should get a start event with a timestamp of zero.
        start_event = [0xFF, 0x04, 0x00, 0x00]

        # Validate that we got the correct packet out; plus headers.
        # We waited 10 cycles before starting the packet, so the
        # timestamp should be 0x00, 0x0a.
        packet = [0x00, 0x03, 0x00, 0x0a, 0x2d, 0x00, 0x10]

        yield from self.expect_data(start_event + packet)


if __name__ == "__main__":
    unittest.main()
