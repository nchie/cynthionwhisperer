#!/usr/bin/env python3
# pylint: disable=maybe-no-member
#
# This file is part of Cynthion.
#
# Copyright (c) 2020-2023 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause

""" Generic USB analyzer backend generator for LUNA. """

from enum import IntEnum, IntFlag

from amaranth                            import Signal, Elaboratable, Module, ResetInserter, C, Mux, Array, Cat
from amaranth.build.res                  import ResourceError
from usb_protocol.emitters               import DeviceDescriptorCollection
from usb_protocol.types                  import USBRequestType, USBRequestRecipient

from luna.usb2                           import USBDevice, USBStreamInEndpoint
from luna                                import top_level_cli

from luna.gateware.usb.request.control   import ControlRequestHandler
from luna.gateware.usb.stream            import USBInStreamInterface
from luna.gateware.stream.generator      import StreamSerializer
from luna.gateware.architecture.car      import LunaECP5DomainGenerator
from luna.gateware.architecture.flash_sn import ECP5FlashUIDStringDescriptor
from luna.gateware.interface.ulpi        import UTMITranslator
from luna.gateware.usb.usb2              import USBSpeed
from luna.gateware.usb.usb2.control      import USBControlEndpoint
from luna.gateware.usb.request.standard  import StandardRequestHandler
from luna.gateware.usb.request.windows   import MicrosoftOS10DescriptorCollection, MicrosoftOS10RequestHandler

from apollo_fpga.gateware.advertiser     import ApolloAdvertiser, ApolloAdvertiserRequestHandler

from usb_protocol.emitters.descriptors.standard import get_string_descriptor
from usb_protocol.types.descriptors.microsoft10 import RegistryTypes

from .analyzer                           import USBAnalyzer
from .fifo                               import Stream16to8, StreamFIFO, AsyncFIFOReadReset, HyperRAMPacketFIFO
from .speed_detection                    import USBAnalyzerSpeedDetector
from .event_detection                    import USBHighSpeedEventDetector, USBFullSpeedEventDetector, USBLowSpeedEventDetector
from .speeds                             import USBAnalyzerSpeed
from .events                             import USBAnalyzerEvent

import cynthion


USB_VENDOR_ID        = cynthion.shared.usb.bVendorId.cynthion
USB_PRODUCT_ID       = cynthion.shared.usb.bProductId.cynthion

BULK_ENDPOINT_NUMBER  = 1
BULK_ENDPOINT_ADDRESS = 0x80 | BULK_ENDPOINT_NUMBER
MAX_BULK_PACKET_SIZE  = 512

# Minor version of the protocol supported by the analyzer.
# The major version is specified in bInterfaceProtocol.
MINOR_VERSION = 2

TRIGGER_MAX_STAGES = 8
TRIGGER_MAX_PATTERN_BYTES = 32
TRIGGER_CONTROL_PAYLOAD_LEN = 2
TRIGGER_STAGE_PAYLOAD_LEN = 4 + TRIGGER_MAX_PATTERN_BYTES + TRIGGER_MAX_PATTERN_BYTES
TRIGGER_CAPS_PAYLOAD_LEN = 4
TRIGGER_STATUS_PAYLOAD_LEN = 5


class USBAnalyzerTriggerConfig:
    """Container for trigger configuration and runtime status signals."""

    def __init__(self, max_stages=TRIGGER_MAX_STAGES, max_pattern=TRIGGER_MAX_PATTERN_BYTES):
        self.max_stages = max_stages
        self.max_pattern = max_pattern
        self.pattern_bits = (max_pattern - 1).bit_length()
        self.stage_bits = (max_stages - 1).bit_length()

        self.enable = Signal(reset=0)
        self.armed = Signal(reset=0)
        self.output_enable = Signal(reset=1)
        self.stage_count = Signal(range(max_stages + 1), reset=0)

        self.stage_offsets = Array(
            Signal(16, name=f"trigger_stage_{i}_offset")
            for i in range(max_stages)
        )
        self.stage_lengths = Array(
            Signal(8, name=f"trigger_stage_{i}_length")
            for i in range(max_stages)
        )

        pattern_flat = []
        mask_flat = []
        for stage in range(max_stages):
            for index in range(max_pattern):
                pattern_flat.append(
                    Signal(8, name=f"trigger_stage_{stage}_byte_{index}")
                )
                mask_flat.append(
                    Signal(8, reset=0xFF, name=f"trigger_stage_{stage}_mask_{index}")
                )
        self.patterns_flat = Array(pattern_flat)
        self.masks_flat = Array(mask_flat)

        # Host control strobes.
        self.arm_strobe = Signal()
        self.disarm_strobe = Signal()

        # Runtime status from analyzer.
        self.sequence_stage = Signal(range(max_stages + 1))
        self.trigger_out = Signal()
        self.fire_count = Signal(16)

class USBAnalyzerRegister(Elaboratable):

    def __init__(self, reset=0x00):
        self.current = Signal(8, reset=reset)
        self.next = Signal(8)
        self.write = Signal()

    def elaborate(self, platform):
        m = Module()
        with m.If(self.write):
            m.d.sync += self.current.eq(self.next)
        return m


class USBAnalyzerVendorRequests(IntEnum):
    GET_STATE = 0
    SET_STATE = 1
    GET_SPEEDS = 2
    SET_TEST_CONFIG = 3
    GET_MINOR_VERSION = 4
    GET_TRIGGER_CAPS = 5
    SET_TRIGGER_CONTROL = 6
    SET_TRIGGER_STAGE = 7
    GET_TRIGGER_STATUS = 9
    ARM_TRIGGER = 10
    DISARM_TRIGGER = 11
    GET_TRIGGER_STAGE = 12


# Bit numbers of state register bits.
class USBAnalyzerState:
    # Enable capture. Used to start/stop the analyzer.
    ENABLE = 0

    # Capture speed selection.
    # 0b00 = HS, 0b01 = FS, 0b11 = LS
    SPEED = slice(1, 3)

    # Enable VBUS passthrough from TARGET-C to TARGET-A.
    VBUS_FROM_TARGET_C = 3

    # Enable VBUS passthrough from CONTROL/HOST to TARGET-A.
    VBUS_FROM_CONTROL_HOST = 4

    # Enable VBUS passthrough from AUX to TARGET-A.
    VBUS_FROM_AUX = 5

    # Enable VBUS discharge on TARGET-A.
    VBUS_TARGET_A_DISCHARGE = 6

    # Enable power control.
    # 0: VBUS passthrough is enabled from TARGET-C to TARGET-A.
    # 1: VBUS distribution is controlled by bits 3-6.
    POWER_CONTROL_ENABLE = 7


class USBAnalyzerSupportedSpeeds(IntFlag):
    USB_SPEED_AUTO = 0b0001
    USB_SPEED_LOW  = 0b0010
    USB_SPEED_FULL = 0b0100
    USB_SPEED_HIGH = 0b1000


class USBAnalyzerVendorRequestHandler(ControlRequestHandler):

    def __init__(self, state, test_config, trigger):
        self.state = state
        self.test_config = test_config
        self.trigger = trigger
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        interface = self.interface

        # Create convenience aliases for our interface components.
        setup = interface.setup

        stage_index_raw = setup.value[0:8]
        stage_index = Signal(range(self.trigger.max_stages))
        valid_stage_index = Signal()
        rx_count = Signal(range(TRIGGER_STAGE_PAYLOAD_LEN + 1))
        control_flags = Signal(8)
        control_stage_count = Signal(8)

        status_flags = Signal(8)
        m.d.comb += status_flags.eq(Cat(
            self.trigger.enable,
            self.trigger.armed,
            self.trigger.output_enable,
            self.trigger.trigger_out,
            C(0, 4),
        ))

        # This handler emits momentary strobes for arm/disarm operations.
        m.d.comb += [
            self.trigger.arm_strobe.eq(0),
            self.trigger.disarm_strobe.eq(0),
            stage_index.eq(stage_index_raw[0:self.trigger.stage_bits]),
            valid_stage_index.eq(stage_index_raw < self.trigger.max_stages),
        ]

        # Transmitter for all constant-size and stage readback responses.
        m.submodules.transmitter = transmitter = StreamSerializer(
            data_length=TRIGGER_STAGE_PAYLOAD_LEN,
            domain="usb",
            stream_type=USBInStreamInterface,
            max_length_width=7,
        )

        # Handle vendor requests to our interface.
        with m.If(
                (setup.type == USBRequestType.VENDOR) &
                (setup.recipient == USBRequestRecipient.INTERFACE) &
                (setup.index == 0)):

            m.d.comb += interface.claim.eq(
                (setup.request == USBAnalyzerVendorRequests.GET_STATE) |
                (setup.request == USBAnalyzerVendorRequests.SET_STATE) |
                (setup.request == USBAnalyzerVendorRequests.GET_SPEEDS)|
                (setup.request == USBAnalyzerVendorRequests.SET_TEST_CONFIG) |
                (setup.request == USBAnalyzerVendorRequests.GET_MINOR_VERSION) |
                (setup.request == USBAnalyzerVendorRequests.GET_TRIGGER_CAPS) |
                (setup.request == USBAnalyzerVendorRequests.SET_TRIGGER_CONTROL) |
                (setup.request == USBAnalyzerVendorRequests.SET_TRIGGER_STAGE) |
                (setup.request == USBAnalyzerVendorRequests.GET_TRIGGER_STATUS) |
                (setup.request == USBAnalyzerVendorRequests.ARM_TRIGGER) |
                (setup.request == USBAnalyzerVendorRequests.DISARM_TRIGGER) |
                (setup.request == USBAnalyzerVendorRequests.GET_TRIGGER_STAGE))

            with m.FSM(domain="usb"):

                # IDLE -- not handling any active request
                with m.State('IDLE'):

                    # If we've received a new setup packet, handle it.
                    with m.If(setup.received):

                        # Select which vendor we're going to handle.
                        with m.Switch(setup.request):

                            with m.Case(USBAnalyzerVendorRequests.GET_STATE):
                                m.next = 'GET_STATE'
                            with m.Case(USBAnalyzerVendorRequests.SET_STATE):
                                m.next = 'SET_STATE'
                            with m.Case(USBAnalyzerVendorRequests.GET_SPEEDS):
                                m.next = 'GET_SPEEDS'
                            with m.Case(USBAnalyzerVendorRequests.SET_TEST_CONFIG):
                                m.next = 'SET_TEST_CONFIG'
                            with m.Case(USBAnalyzerVendorRequests.GET_MINOR_VERSION):
                                m.next = 'GET_MINOR_VERSION'
                            with m.Case(USBAnalyzerVendorRequests.GET_TRIGGER_CAPS):
                                m.next = 'GET_TRIGGER_CAPS'
                            with m.Case(USBAnalyzerVendorRequests.SET_TRIGGER_CONTROL):
                                m.d.usb += [
                                    rx_count.eq(0),
                                    control_flags.eq(0),
                                    control_stage_count.eq(0),
                                ]
                                m.next = 'SET_TRIGGER_CONTROL'
                            with m.Case(USBAnalyzerVendorRequests.SET_TRIGGER_STAGE):
                                m.d.usb += rx_count.eq(0)
                                m.next = 'SET_TRIGGER_STAGE'
                            with m.Case(USBAnalyzerVendorRequests.GET_TRIGGER_STATUS):
                                m.next = 'GET_TRIGGER_STATUS'
                            with m.Case(USBAnalyzerVendorRequests.ARM_TRIGGER):
                                m.next = 'ARM_TRIGGER'
                            with m.Case(USBAnalyzerVendorRequests.DISARM_TRIGGER):
                                m.next = 'DISARM_TRIGGER'
                            with m.Case(USBAnalyzerVendorRequests.GET_TRIGGER_STAGE):
                                m.next = 'GET_TRIGGER_STAGE'

                # GET_STATE -- Fetch the device's state
                with m.State('GET_STATE'):
                    self.handle_simple_data_request(m, transmitter, self.state.current, length=1)

                # SET_STATE -- The host is trying to set our state
                with m.State('SET_STATE'):
                    self.handle_register_write_request(m, self.state.next, self.state.write)

                # GET_SPEEDS -- Fetch the device's supported USB speeds
                with m.State('GET_SPEEDS'):
                    supported_speeds = \
                        USBAnalyzerSupportedSpeeds.USB_SPEED_LOW | \
                        USBAnalyzerSupportedSpeeds.USB_SPEED_FULL | \
                        USBAnalyzerSupportedSpeeds.USB_SPEED_HIGH

                    # Automatic speed detection is only supported on Cynthion r0.6+.
                    if platform.version >= (0, 6):
                        supported_speeds |= \
                            USBAnalyzerSupportedSpeeds.USB_SPEED_AUTO

                    self.handle_simple_data_request(m, transmitter, supported_speeds, length=1)

                # SET_TEST_CONFIG -- The host is trying to configure our test device
                with m.State('SET_TEST_CONFIG'):
                    self.handle_register_write_request(m, self.test_config.next, self.test_config.write)

                # GET_STATE -- Fetch the device's state
                with m.State('GET_MINOR_VERSION'):
                    self.handle_simple_data_request(m, transmitter, C(MINOR_VERSION), length=1)

                # GET_TRIGGER_CAPS -- Trigger engine capabilities.
                with m.State('GET_TRIGGER_CAPS'):
                    caps = Cat(
                        C(self.trigger.max_stages, 8),
                        C(self.trigger.max_pattern, 8),
                        C(TRIGGER_STAGE_PAYLOAD_LEN & 0xFF, 8),
                        C((TRIGGER_STAGE_PAYLOAD_LEN >> 8) & 0xFF, 8),
                    )
                    self.handle_simple_data_request(
                        m,
                        transmitter,
                        caps,
                        length=TRIGGER_CAPS_PAYLOAD_LEN,
                    )

                # SET_TRIGGER_CONTROL -- Configure trigger globals.
                with m.State('SET_TRIGGER_CONTROL'):
                    stage_count_clamped = Mux(
                        control_stage_count > self.trigger.max_stages,
                        self.trigger.max_stages,
                        control_stage_count,
                    )

                    m.d.comb += interface.claim.eq(1)

                    with m.If(interface.rx.valid & interface.rx.next):
                        with m.Switch(rx_count):
                            with m.Case(0):
                                m.d.usb += control_flags.eq(interface.rx.payload)
                            with m.Case(1):
                                m.d.usb += control_stage_count.eq(interface.rx.payload)
                        with m.If(rx_count < TRIGGER_STAGE_PAYLOAD_LEN):
                            m.d.usb += rx_count.eq(rx_count + 1)

                    with m.If(interface.rx_ready_for_response):
                        with m.If(rx_count >= TRIGGER_CONTROL_PAYLOAD_LEN):
                            m.d.comb += interface.handshakes_out.ack.eq(1)
                            m.d.usb += [
                                self.trigger.enable.eq(control_flags[0]),
                                self.trigger.output_enable.eq(control_flags[1]),
                                self.trigger.stage_count.eq(stage_count_clamped),
                            ]
                            with m.If(~control_flags[0]):
                                m.d.usb += self.trigger.armed.eq(0)
                                m.d.comb += self.trigger.disarm_strobe.eq(1)
                        with m.Else():
                            m.d.comb += interface.handshakes_out.stall.eq(1)

                    with m.If(interface.status_requested):
                        with m.If(rx_count >= TRIGGER_CONTROL_PAYLOAD_LEN):
                            m.d.comb += self.send_zlp()
                        with m.Else():
                            m.d.comb += interface.handshakes_out.stall.eq(1)
                        m.next = 'IDLE'

                # SET_TRIGGER_STAGE -- Configure one trigger stage.
                with m.State('SET_TRIGGER_STAGE'):
                    m.d.comb += interface.claim.eq(1)

                    with m.If(interface.rx.valid & interface.rx.next):
                        with m.If(valid_stage_index):
                            with m.Switch(rx_count):
                                with m.Case(0):
                                    m.d.usb += self.trigger.stage_offsets[stage_index][0:8].eq(interface.rx.payload)
                                with m.Case(1):
                                    m.d.usb += self.trigger.stage_offsets[stage_index][8:16].eq(interface.rx.payload)
                                with m.Case(2):
                                    m.d.usb += self.trigger.stage_lengths[stage_index].eq(
                                        Mux(
                                            interface.rx.payload > self.trigger.max_pattern,
                                            self.trigger.max_pattern,
                                            interface.rx.payload,
                                        )
                                    )
                                with m.Case(3):
                                    pass
                                for i in range(TRIGGER_MAX_PATTERN_BYTES):
                                    with m.Case(4 + i):
                                        flat_index = Cat(C(i, self.trigger.pattern_bits), stage_index)
                                        m.d.usb += self.trigger.patterns_flat[flat_index].eq(interface.rx.payload)
                                for i in range(TRIGGER_MAX_PATTERN_BYTES):
                                    with m.Case(4 + TRIGGER_MAX_PATTERN_BYTES + i):
                                        flat_index = Cat(C(i, self.trigger.pattern_bits), stage_index)
                                        m.d.usb += self.trigger.masks_flat[flat_index].eq(interface.rx.payload)
                        with m.If(rx_count < TRIGGER_STAGE_PAYLOAD_LEN):
                            m.d.usb += rx_count.eq(rx_count + 1)

                    with m.If(interface.rx_ready_for_response):
                        with m.If(valid_stage_index & (rx_count >= TRIGGER_STAGE_PAYLOAD_LEN)):
                            m.d.comb += interface.handshakes_out.ack.eq(1)
                        with m.Else():
                            m.d.comb += interface.handshakes_out.stall.eq(1)

                    with m.If(interface.status_requested):
                        with m.If(valid_stage_index & (rx_count >= TRIGGER_STAGE_PAYLOAD_LEN)):
                            m.d.comb += self.send_zlp()
                        with m.Else():
                            m.d.comb += interface.handshakes_out.stall.eq(1)
                        m.next = 'IDLE'

                # GET_TRIGGER_STATUS -- Read trigger runtime state.
                with m.State('GET_TRIGGER_STATUS'):
                    status = Cat(
                        status_flags,
                        self.trigger.sequence_stage,
                        self.trigger.fire_count[0:8],
                        self.trigger.fire_count[8:16],
                        self.trigger.stage_count,
                    )
                    self.handle_simple_data_request(
                        m,
                        transmitter,
                        status,
                        length=TRIGGER_STATUS_PAYLOAD_LEN,
                    )

                # ARM_TRIGGER -- Enable armed matching state.
                with m.State('ARM_TRIGGER'):
                    m.d.comb += interface.claim.eq(1)
                    with m.If(interface.status_requested):
                        m.d.usb += self.trigger.armed.eq(1)
                        m.d.comb += [
                            self.trigger.arm_strobe.eq(1),
                            self.send_zlp(),
                        ]
                        m.next = 'IDLE'

                # DISARM_TRIGGER -- Disable matching state and reset sequence.
                with m.State('DISARM_TRIGGER'):
                    m.d.comb += interface.claim.eq(1)
                    with m.If(interface.status_requested):
                        m.d.usb += self.trigger.armed.eq(0)
                        m.d.comb += [
                            self.trigger.disarm_strobe.eq(1),
                            self.send_zlp(),
                        ]
                        m.next = 'IDLE'

                # GET_TRIGGER_STAGE -- Read trigger stage config.
                with m.State('GET_TRIGGER_STAGE'):
                    m.d.comb += [
                        interface.claim.eq(1),
                        transmitter.stream.attach(interface.tx),
                        transmitter.max_length.eq(TRIGGER_STAGE_PAYLOAD_LEN),
                        transmitter.data[0].eq(self.trigger.stage_offsets[stage_index][0:8]),
                        transmitter.data[1].eq(self.trigger.stage_offsets[stage_index][8:16]),
                        transmitter.data[2].eq(self.trigger.stage_lengths[stage_index]),
                        transmitter.data[3].eq(C(0, 8)),
                    ]
                    for i in range(TRIGGER_MAX_PATTERN_BYTES):
                        flat_index = Cat(C(i, self.trigger.pattern_bits), stage_index)
                        m.d.comb += transmitter.data[4 + i].eq(self.trigger.patterns_flat[flat_index])
                    for i in range(TRIGGER_MAX_PATTERN_BYTES):
                        flat_index = Cat(C(i, self.trigger.pattern_bits), stage_index)
                        m.d.comb += transmitter.data[4 + TRIGGER_MAX_PATTERN_BYTES + i].eq(
                            self.trigger.masks_flat[flat_index]
                        )

                    with m.If(interface.data_requested):
                        with m.If(valid_stage_index):
                            m.d.comb += transmitter.start.eq(1)
                        with m.Else():
                            m.d.comb += interface.handshakes_out.stall.eq(1)
                            m.next = 'IDLE'

                    with m.If(interface.status_requested):
                        with m.If(valid_stage_index):
                            m.d.comb += interface.handshakes_out.ack.eq(1)
                        with m.Else():
                            m.d.comb += interface.handshakes_out.stall.eq(1)
                        m.next = 'IDLE'

        return m


class USBAnalyzerApplet(Elaboratable):
    """ Gateware that serves as a generic USB analyzer backend.

    WARNING: This is _incomplete_! It's missing:
        - DRAM backing for analysis
    """

    def create_descriptors(self, platform, sharing):
        """ Create the descriptors we want to use for our device. """

        major, minor = platform.version
        descriptors = DeviceDescriptorCollection()

        #
        # We'll add the major components of the descriptors we we want.
        # The collection we build here will be necessary to create a standard endpoint.
        #

        # We'll need a device descriptor...
        with descriptors.DeviceDescriptor() as d:
            d.idVendor           = USB_VENDOR_ID
            d.idProduct          = USB_PRODUCT_ID

            d.iManufacturer      = "Cynthion Project"
            d.iProduct           = "USB Analyzer"
            d.iSerialNumber      = ECP5FlashUIDStringDescriptor
            d.bcdDevice          = major + (minor * 0.01)

            d.bNumConfigurations = 1


        # ... and a description of the USB configuration we'll provide.
        with descriptors.ConfigurationDescriptor() as c:

            with c.InterfaceDescriptor() as i:
                i.bInterfaceNumber = 0
                i.bInterfaceClass = 0xFF
                i.bInterfaceSubclass = cynthion.shared.usb.bInterfaceSubClass.analyzer
                i.bInterfaceProtocol = cynthion.shared.usb.bInterfaceProtocol.analyzer

                with i.EndpointDescriptor() as e:
                    e.bEndpointAddress = BULK_ENDPOINT_ADDRESS
                    e.wMaxPacketSize   = MAX_BULK_PACKET_SIZE

            # Include Apollo stub interface, if using a shared port.
            if sharing is not None:
                with c.InterfaceDescriptor() as i:
                    i.bInterfaceNumber = 1
                    i.bInterfaceClass = 0xFF
                    i.bInterfaceSubclass = cynthion.shared.usb.bInterfaceSubClass.apollo
                    i.bInterfaceProtocol = ApolloAdvertiserRequestHandler.PROTOCOL_VERSION

        return descriptors


    def elaborate(self, platform):
        m = Module()

        # State register
        m.submodules.state = state = USBAnalyzerRegister()
        speed_selection = state.current[USBAnalyzerState.SPEED]

        # Test config register
        m.submodules.test_config = test_config = USBAnalyzerRegister(reset=0x01)

        # Trigger configuration and status registers.
        trigger = USBAnalyzerTriggerConfig()

        # Generate our clock domains.
        clocking = LunaECP5DomainGenerator()
        m.submodules.clocking = clocking

        # Create our UTMI translator.
        ulpi = platform.request("target_phy")
        m.submodules.utmi = utmi = UTMITranslator(ulpi=ulpi)

        # Add event detectors for fixed speeds.
        m.submodules.hs_event = hs_event_detector = USBHighSpeedEventDetector()
        m.submodules.fs_event = fs_event_detector = USBFullSpeedEventDetector()
        m.submodules.ls_event = ls_event_detector = USBLowSpeedEventDetector()
        m.d.comb += [
            hs_event_detector.reset.eq(state.write),
            fs_event_detector.reset.eq(state.write),
            ls_event_detector.reset.eq(state.write),
            fs_event_detector.line_state.eq(utmi.line_state),
            ls_event_detector.line_state.eq(utmi.line_state),
            hs_event_detector.vbus_connected.eq(utmi.session_valid),
            fs_event_detector.vbus_connected.eq(utmi.session_valid),
            ls_event_detector.vbus_connected.eq(utmi.session_valid),
        ]

        # Connect our power controls. The power_control_enable bit must be set
        # to use this feature, otherwise the default pass-through is enabled.
        power_control_enable = state.current[USBAnalyzerState.POWER_CONTROL_ENABLE]
        if platform.version >= (0, 6):
            m.d.comb += [
                # Connect all the VBUS switch controls.
                platform.request("target_c_vbus_en").o.eq(
                    Mux(power_control_enable,
                        state.current[USBAnalyzerState.VBUS_FROM_TARGET_C], True)),
                platform.request("control_vbus_en").o.eq(
                    Mux(power_control_enable,
                        state.current[USBAnalyzerState.VBUS_FROM_CONTROL_HOST], False)),
                platform.request("aux_vbus_en").o.eq(
                    Mux(power_control_enable,
                        state.current[USBAnalyzerState.VBUS_FROM_AUX], False)),

                # And the TARGET-A discharge control.
                platform.request("target_a_discharge").o.eq(
                    Mux(power_control_enable,
                        state.current[USBAnalyzerState.VBUS_TARGET_A_DISCHARGE], False)),
            ]

            # Tap the D+/D- signals for speed detection.
            usb_dp = Signal()
            usb_dm = Signal()
            usb_dp_input = platform.request("target_usb_dp_chirp").i
            usb_dm_input = platform.request("target_usb_dm_chirp").i
            m.d.usb += [
                usb_dp.eq(usb_dp_input),
                usb_dm.eq(usb_dm_input),
            ]

            # Add a speed detector and use it when selected.
            m.submodules.speed = speed_detector = USBAnalyzerSpeedDetector()
            phy_speed = Mux(
                speed_selection == USBAnalyzerSpeed.AUTO,
                speed_detector.phy_speed,
                speed_selection)
            detected_speed = Mux(
                speed_selection == USBAnalyzerSpeed.AUTO,
                speed_detector.detected_speed,
                speed_selection)
            auto_event_strobe = speed_detector.event_strobe
            auto_event_code = speed_detector.event_code

            # Provide the necessary signals for speed detection.
            m.d.comb += [
                speed_detector.reset.eq(state.write),
                speed_detector.line_state.eq(utmi.line_state),
                speed_detector.usb_dp.eq(usb_dp),
                speed_detector.usb_dm.eq(usb_dm),
                speed_detector.vbus_connected.eq(utmi.session_valid),
            ]

        else:
            m.d.comb += [
                # On the r0.1 to r0.5 boards, power switching is different.

                # `pass_through_vbus` is equivalent to `target_c_vbus_en`
                # and controls VBUS from TARGET-C to TARGET-A.
                platform.request("pass_through_vbus").o.eq(
                    Mux(power_control_enable,
                        state.current[USBAnalyzerState.VBUS_FROM_TARGET_C], True)),

                # `power_a_port` controls VBUS from HOST to TARGET-A.
                platform.request("power_a_port").o.eq(
                    Mux(power_control_enable,
                        state.current[USBAnalyzerState.VBUS_FROM_CONTROL_HOST], False)),

                # There is no way of powering TARGET-A from the SIDEBAND
                # port, and no discharge capability on TARGET-A.
            ]

            # Speed selection is manual only.
            phy_speed = detected_speed = next_speed = speed_selection
            auto_event_strobe = False
            auto_event_code = USBAnalyzerEvent.NONE

        # Choose the appropriate event source according to speed selection.
        event_strobes = Array([
            hs_event_detector.event_strobe,
            fs_event_detector.event_strobe,
            ls_event_detector.event_strobe,
            auto_event_strobe,
        ])

        event_codes = Array([
            hs_event_detector.event_code,
            fs_event_detector.event_code,
            ls_event_detector.event_code,
            auto_event_code,
        ])

        event_strobe = event_strobes[speed_selection]
        event_code = event_codes[speed_selection]

        # Set up our parameters.
        m.d.comb += [
            # Set PHY mode to non-driving and to the desired speed.
            #
            # `dp_pulldown`, `dm_pulldown` and `term_select` do not need to be
            # configured as these values are "don't cares" for this specific
            # `op_mode` (see ULPI Specification rev. 1.1 Table 41).
            utmi.op_mode     .eq(0b01),
            utmi.xcvr_select .eq(phy_speed),
        ]

        # Select the appropriate PHY according to platform version.
        if platform.version >= (0, 6):
            phy_name = "control_phy"

            # Also set up a test device on the AUX PHY.
            m.submodules += AnalyzerTestDevice(test_config)
        else:
            phy_name = "host_phy"

        # Check how the port is shared with Apollo.
        sharing = platform.port_sharing(phy_name)

        # Create our USB uplink interface...
        uplink_ulpi = platform.request(phy_name)
        m.submodules.usb = usb = USBDevice(bus=uplink_ulpi)

        # Create descriptors.
        descriptors = self.create_descriptors(platform, sharing)

        # Add Microsoft OS 1.0 descriptors for Windows compatibility.
        descriptors.add_descriptor(get_string_descriptor("MSFT100\xee"), index=0xee)
        msft_descriptors = MicrosoftOS10DescriptorCollection()
        with msft_descriptors.ExtendedCompatIDDescriptor() as c:
            with c.Function() as f:
                f.bFirstInterfaceNumber = 0
                f.compatibleID          = 'WINUSB'
            if sharing is not None:
                with c.Function() as f:
                    f.bFirstInterfaceNumber = 1
                    f.compatibleID          = 'WINUSB'
        with msft_descriptors.ExtendedPropertiesDescriptor() as d:
            with d.Property() as p:
                p.dwPropertyDataType = RegistryTypes.REG_SZ
                p.PropertyName       = "DeviceInterfaceGUID"
                p.PropertyData       = "{88bae032-5a81-49f0-bc3d-a4ff138216d6}"

        # Add our standard control endpoint to the device.
        control_endpoint = usb.add_standard_control_endpoint(descriptors)

        # Add handler for Microsoft descriptors.
        msft_handler = MicrosoftOS10RequestHandler(msft_descriptors, request_code=0xee)
        control_endpoint.add_request_handler(msft_handler)

        # Add our vendor request handler to the control endpoint.
        vendor_request_handler = USBAnalyzerVendorRequestHandler(state, test_config, trigger)
        control_endpoint.add_request_handler(vendor_request_handler)

        # If needed, create an advertiser and add its request handler.
        if sharing == "advertising":
            adv = m.submodules.adv = ApolloAdvertiser()
            control_endpoint.add_request_handler(adv.default_request_handler(1))

        # Add a stream endpoint to our device.
        stream_ep = USBStreamInEndpoint(
            endpoint_number=BULK_ENDPOINT_NUMBER,
            max_packet_size=MAX_BULK_PACKET_SIZE
        )
        usb.add_endpoint(stream_ep)

        # Create a USB analyzer.
        m.submodules.analyzer = analyzer = USBAnalyzer(
            utmi, utmi.session_valid, detected_speed, event_strobe, event_code, trigger=trigger)

        # Follow this with a HyperRAM FIFO for additional buffering.
        reset_on_start = ResetInserter(analyzer.starting)
        m.submodules.psram_fifo = psram_fifo = reset_on_start(
            HyperRAMPacketFIFO(out_fifo_depth=128))

        # Convert the 16-bit stream into an 8-bit one for output.
        m.submodules.s16to8 = s16to8 = reset_on_start(Stream16to8())

        # Add a special stream clock converter for 'sync' to 'usb' crossing.
        m.submodules.clk_conv = clk_conv = StreamFIFO(
            AsyncFIFOReadReset(width=8, depth=4, r_domain="usb", w_domain="sync"))

        m.d.comb += [
            # Connect enable signal to host-controlled state register.
            analyzer.capture_enable     .eq(state.current[USBAnalyzerState.ENABLE]),

            # Trigger status exported to host over vendor requests.
            trigger.sequence_stage      .eq(analyzer.trigger_sequence_stage),
            trigger.trigger_out         .eq(analyzer.trigger_toggle_out),
            trigger.fire_count          .eq(analyzer.trigger_fire_count),

            # Flush endpoint when analyzer is idle with capture disabled.
            stream_ep.flush             .eq(analyzer.idle & ~analyzer.capture_enable),

            # Discard old data buffered by endpoint when the analyzer starts.
            stream_ep.discard           .eq(analyzer.starting),

            # USB stream pipeline.
            psram_fifo.input            .stream_eq(analyzer.stream),
            s16to8.input                .stream_eq(psram_fifo.output),
            clk_conv.input              .stream_eq(s16to8.output),
            clk_conv.fifo.ext_rst       .eq(analyzer.starting),
            stream_ep.stream            .stream_eq(clk_conv.output),

            usb.connect                 .eq(1),

            # LED indicators.
            platform.request("led", 0).o  .eq(analyzer.capturing),
            platform.request("led", 1).o  .eq(stream_ep.stream.valid),
            platform.request("led", 2).o  .eq(analyzer.overrun),

            platform.request("led", 3).o  .eq(utmi.session_valid),
            platform.request("led", 4).o  .eq(utmi.rx_active),
            platform.request("led", 5).o  .eq(utmi.rx_error),
        ]

        # Route trigger output to the dedicated interrupt pin when available.
        if sharing != "advertising":
            try:
                trigger_int = platform.request("int", 0)
                m.d.comb += trigger_int.o.eq(trigger.output_enable & analyzer.trigger_toggle_out)
            except ResourceError:
                pass

        # Return our elaborated module.
        return m


class AnalyzerTestDevice(Elaboratable):
    """ Built-in example device that can be used to test the analyzer. """

    SPEEDS = (USBSpeed.HIGH, USBSpeed.FULL, USBSpeed.LOW)

    EP0_MAX_SIZE = {
        USBSpeed.HIGH: 64,
        USBSpeed.FULL: 64,
        USBSpeed.LOW: 8,
    }

    INT_EP_MAX_SIZE = {
        USBSpeed.HIGH: 512,
        USBSpeed.FULL: 64,
        USBSpeed.LOW: 8,
    }

    INT_EP_NUM = {
        USBSpeed.HIGH: 1,
        USBSpeed.FULL: 2,
        USBSpeed.LOW: 3,
    }

    def __init__(self, config):
        self.config = config

    def create_descriptors(self, speed):
        descriptors = DeviceDescriptorCollection()

        with descriptors.DeviceDescriptor() as d:
            d.idVendor           = cynthion.shared.usb.bVendorId.example
            d.idProduct          = cynthion.shared.usb.bProductId.analyzer_test
            d.iManufacturer      = "Cynthion Project"
            d.iProduct           = "USB Analyzer Test Device"
            d.bcdDevice          = 0.01
            d.bNumConfigurations = 1
            d.bMaxPacketSize0    = self.EP0_MAX_SIZE[speed]

        with descriptors.ConfigurationDescriptor() as c:
            with c.InterfaceDescriptor() as i:
                i.bInterfaceNumber = 0
                with i.EndpointDescriptor() as e:
                    e.bEndpointAddress = 0x80 | self.INT_EP_NUM[speed]
                    e.bmAttributes     = 0x03 # Interrupt endpoint
                    e.wMaxPacketSize   = self.INT_EP_MAX_SIZE[speed]
                    e.bInterval        = 0x05 # 5ms interval

        descriptors.add_descriptor(
                get_string_descriptor("MSFT100\xee"), index=0xee)

        return descriptors

    def elaborate(self, platform):
        m = Module()

        # Create a USB device and connect it as required.
        m.submodules.usb = usb = USBDevice(bus=platform.request("aux_phy"))
        current_speed = self.config.current[1:3]
        m.d.comb += [
            usb.connect.eq(self.config.current[0]),
            usb.low_speed_only.eq(current_speed == USBSpeed.LOW),
            usb.full_speed_only.eq(current_speed == USBSpeed.FULL),
        ]

        # Create control endpoint.
        control_ep = USBControlEndpoint(utmi=usb.utmi)

        # Add standard request handlers for each speed.
        for speed in self.SPEEDS:
            handler = StandardRequestHandler(
                self.create_descriptors(speed),
                self.EP0_MAX_SIZE[speed],
                blacklist=[lambda setup,speed=speed: current_speed != speed])
            control_ep.add_request_handler(handler)

        # Add Microsoft descriptors for Windows compatibility.
        msft_descriptors = MicrosoftOS10DescriptorCollection()
        with msft_descriptors.ExtendedCompatIDDescriptor() as c:
            with c.Function() as f:
                f.bFirstInterfaceNumber = 0
                f.compatibleID          = 'WINUSB'

        # Add handler for Microsoft descriptors.
        msft_handler = MicrosoftOS10RequestHandler(
                msft_descriptors, request_code=0xee)
        control_ep.add_request_handler(msft_handler)

        # Add control endpoint.
        usb.add_endpoint(control_ep)

        # Add IN endpoints for each speed.
        for speed in self.SPEEDS:
            in_ep = USBStreamInEndpoint(
                endpoint_number=self.INT_EP_NUM[speed],
                max_packet_size=self.INT_EP_MAX_SIZE[speed])
            usb.add_endpoint(in_ep)

            # Output a counter to the endpoint.
            counter = Signal(8)
            m.d.comb += [
                in_ep.stream.valid.eq(1),
                in_ep.stream.payload.eq(counter),
            ]
            with m.If(in_ep.stream.ready):
                m.d.usb += counter.eq(counter + 1)

        return m


if __name__ == "__main__":
    top_level_cli(USBAnalyzerApplet)
