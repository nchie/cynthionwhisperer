//! USB capture backend for Cynthion.

use std::cmp::Ordering;
use std::collections::VecDeque;
use std::num::NonZeroU32;
use std::ops::DerefMut;
use std::sync::mpsc::RecvTimeoutError;
use std::sync::{Arc, mpsc};
use std::time::Duration;

use anyhow::{Context as ErrorContext, Error, bail};
use async_lock::Mutex;
use async_trait::async_trait;
use nusb::{
    self, DeviceInfo, Interface,
    transfer::{Buffer, Bulk, ControlIn, ControlOut, ControlType, In, Recipient},
};

use super::{
    BackendDevice, BackendHandle, EventIterator, EventPoll, EventResult, EventType, PowerConfig,
    Speed, TimestampedEvent, TransferQueue, claim_interface,
};

use crate::capture::CaptureMetadata;

pub const VID_PID: (u16, u16) = (0x1d50, 0x615b);
const CLASS: u8 = 0xff;
const SUBCLASS: u8 = 0x10;
const PROTOCOL: u8 = 0x01;
const ENDPOINT: u8 = 0x81;
const READ_LEN: usize = 0x4000;
const NUM_TRANSFERS: usize = 4;

const REQUEST_GET_STATE: u8 = 0;
const REQUEST_SET_STATE: u8 = 1;
const REQUEST_GET_SPEEDS: u8 = 2;
const REQUEST_SET_TEST_CONFIG: u8 = 3;
const REQUEST_GET_MINOR_VERSION: u8 = 4;
const REQUEST_GET_TRIGGER_CAPS: u8 = 5;
const REQUEST_SET_TRIGGER_CONTROL: u8 = 6;
const REQUEST_SET_TRIGGER_STAGE: u8 = 7;
const REQUEST_GET_TRIGGER_STATUS: u8 = 9;
const REQUEST_ARM_TRIGGER: u8 = 10;
const REQUEST_DISARM_TRIGGER: u8 = 11;
const REQUEST_GET_TRIGGER_STAGE: u8 = 12;

const TRIGGER_STAGE_PAYLOAD_LEN: usize = 4 + 32 + 32;
const TRIGGER_CONTROL_PAYLOAD_LEN: usize = 2;
const TRIGGER_CAPS_PAYLOAD_LEN: usize = 4;
const TRIGGER_STATUS_PAYLOAD_LEN: usize = 5;
const TRIGGER_MAX_PATTERN_LEN: usize = 32;

bitfield! {
    #[derive(Copy, Clone)]
    struct State(u8);
    bool, enable, set_enable: 0;
    u8, from into Speed, speed, set_speed: 2, 1;
    bool, target_c_vbus_en, set_target_c_vbus_en: 3;
    bool, control_vbus_en, set_control_vbus_en: 4;
    bool, aux_vbus_en, set_aux_vbus_en: 5;
    bool, target_a_discharge, set_target_a_discharge: 6;
    bool, power_control_enable, set_power_control_enable: 7;
}

bitfield! {
    #[derive(Copy, Clone)]
    struct TestConfig(u8);
    bool, connect, set_connect: 0;
    u8, from into Speed, speed, set_speed: 2, 1;
}

impl TestConfig {
    fn new(speed: Option<Speed>) -> TestConfig {
        let mut config = TestConfig(0);
        match speed {
            Some(speed) => {
                config.set_connect(true);
                config.set_speed(speed);
            }
            None => {
                config.set_connect(false);
            }
        };
        config
    }
}

#[derive(Clone, Debug)]
pub struct TriggerCaps {
    pub max_stages: u8,
    pub max_pattern_len: u8,
    pub stage_payload_len: u16,
}

#[derive(Clone, Debug)]
pub struct TriggerControl {
    pub enable: bool,
    pub output_enable: bool,
    pub stage_count: u8,
}

#[derive(Clone, Debug)]
pub struct TriggerStage {
    pub offset: u16,
    pub length: u8,
    pub pattern: Vec<u8>,
    pub mask: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct TriggerStatus {
    pub enable: bool,
    pub armed: bool,
    pub output_enable: bool,
    pub output_state: bool,
    pub sequence_stage: u8,
    pub fire_count: u16,
    pub stage_count: u8,
}

/// A Cynthion device attached to the system.
#[derive(Clone)]
pub struct CynthionDevice {
    pub device_info: DeviceInfo,
}

/// Fields of CynthionHandle to be stored in a mutex.
struct CynthionInner {
    interface: Interface,
    state: State,
    power: Option<PowerConfig>,
}

/// A handle to an open Cynthion device.
#[derive(Clone)]
pub struct CynthionHandle {
    inner: Arc<Mutex<CynthionInner>>,
    speeds: Vec<Speed>,
    metadata: CaptureMetadata,
    power_sources: Option<&'static [&'static str]>,
    protocol_minor: u8,
}

/// Converts from received data bytes to timestamped packets.
pub struct CynthionStream {
    data_rx: mpsc::Receiver<Buffer>,
    reuse_tx: mpsc::Sender<Buffer>,
    buffer: VecDeque<u8>,
    padding_due: bool,
    total_clk_cycles: u64,
}

/// Convert 60MHz clock cycles to nanoseconds, rounding down.
fn clk_to_ns(clk_cycles: u64) -> u64 {
    const TABLE: [u64; 3] = [0, 16, 33];
    let quotient = clk_cycles / 3;
    let remainder = clk_cycles % 3;
    quotient * 50 + TABLE[remainder as usize]
}

/// Probe a Cynthion device.
pub fn probe(device_info: DeviceInfo) -> Result<Box<dyn BackendDevice>, Error> {
    Ok(Box::new(CynthionDevice { device_info }))
}

impl CynthionDevice {
    /// Open this device.
    pub async fn open(&self) -> Result<CynthionHandle, Error> {
        use Speed::*;

        // Check we can open the device.
        let device = self
            .device_info
            .open()
            .await
            .context("Failed to open device")?;

        // Read the active configuration.
        let config = device
            .active_configuration()
            .context("Failed to retrieve active configuration")?;

        // Iterate over the interfaces...
        for interface in config.interfaces() {
            let interface_number = interface.interface_number();

            // ...and alternate settings...
            for alt_setting in interface.alt_settings() {
                let alt_setting_number = alt_setting.alternate_setting();

                // Ignore if this is not our supported target.
                if alt_setting.class() != CLASS || alt_setting.subclass() != SUBCLASS {
                    continue;
                }

                // Check protocol version.
                let protocol = alt_setting.protocol();
                #[allow(clippy::absurd_extreme_comparisons)]
                match PROTOCOL.cmp(&protocol) {
                    Ordering::Less => bail!(
                        "Analyzer gateware is newer (v{}) than supported by this version of Packetry (v{}). Please update Packetry.",
                        protocol,
                        PROTOCOL
                    ),
                    Ordering::Greater => bail!(
                        "Analyzer gateware is older (v{}) than supported by this version of Packetry (v{}). Please update gateware.",
                        protocol,
                        PROTOCOL
                    ),
                    Ordering::Equal => {}
                }

                // Try to claim the interface.
                let interface = claim_interface(&device, interface_number).await?;

                // Select the required alternate, if not the default.
                if alt_setting_number != 0 {
                    interface
                        .set_alt_setting(alt_setting_number)
                        .await
                        .context("Failed to select alternate setting")?;
                }

                // Read the state register.
                let mut state = State(read_byte(&interface, REQUEST_GET_STATE).await?);

                // Fetch the available speeds.
                let mut speeds = Vec::new();
                let speed_byte = read_byte(&interface, REQUEST_GET_SPEEDS).await?;
                for speed in [Auto, High, Full, Low] {
                    if speed_byte & speed.mask() != 0 {
                        speeds.push(speed);
                    }
                }

                // Fetch the minor protocol version.
                let protocol_minor = read_byte(&interface, REQUEST_GET_MINOR_VERSION)
                    .await
                    .unwrap_or(0);

                let metadata = CaptureMetadata {
                    iface_desc: Some("Cynthion USB Analyzer".to_string()),
                    iface_hardware: Some({
                        let bcd = self.device_info.device_version();
                        let major = bcd >> 8;
                        let minor = bcd as u8;
                        format!("Cynthion r{major}.{minor}")
                    }),
                    iface_os: Some(format!("USB Analyzer v{protocol}.{protocol_minor}")),
                    iface_snaplen: Some(NonZeroU32::new(0xFFFF).unwrap()),
                    ..Default::default()
                };

                // Translate the power configuration.
                let power = if (protocol, protocol_minor) < (1, 1) {
                    // Analyzer does not support power control.
                    state.set_power_control_enable(false);
                    None
                } else {
                    // Analyzer supports power control.
                    let (source_index, on_now) = if !state.power_control_enable() {
                        // Power control has not yet been set up.
                        // Set the initial configuration.
                        state.set_power_control_enable(true);
                        state.set_target_c_vbus_en(true);
                        state.set_control_vbus_en(false);
                        state.set_aux_vbus_en(false);
                        state.set_target_a_discharge(false);
                        (0, true)
                    } else if state.target_c_vbus_en() {
                        (0, true)
                    } else if state.control_vbus_en() {
                        (1, true)
                    } else if state.aux_vbus_en() {
                        (2, true)
                    } else {
                        (0, false)
                    };
                    Some(PowerConfig {
                        source_index,
                        on_now,
                        start_on: false,
                        stop_off: false,
                    })
                };

                let power_sources: Option<&[&str]> = if power.is_none() {
                    None
                } else if self.device_info.device_version() >= 0x0006 {
                    Some(&["TARGET-C", "CONTROL", "AUX"])
                } else {
                    Some(&["TARGET-C", "HOST"])
                };

                // Now we have a usable device.
                return Ok(CynthionHandle {
                    inner: Arc::new(Mutex::new(CynthionInner {
                        interface,
                        state,
                        power,
                    })),
                    speeds,
                    metadata,
                    power_sources,
                    protocol_minor,
                });
            }
        }

        bail!("No supported analyzer interface found");
    }
}

#[async_trait]
impl BackendDevice for CynthionDevice {
    async fn open_as_generic(&self) -> Result<Box<dyn BackendHandle>, Error> {
        Ok(Box::new(self.open().await?))
    }

    fn duplicate(&self) -> Box<dyn BackendDevice> {
        Box::new(self.clone())
    }
}

#[async_trait(?Send)]
impl BackendHandle for CynthionHandle {
    fn supported_speeds(&self) -> &[Speed] {
        &self.speeds
    }

    fn metadata(&self) -> &CaptureMetadata {
        &self.metadata
    }

    fn power_sources(&self) -> Option<&'static [&'static str]> {
        self.power_sources
    }

    async fn power_config(&self) -> Option<PowerConfig> {
        self.inner().await.power.clone()
    }

    async fn set_power_config(&mut self, power: PowerConfig) -> Result<(), Error> {
        self.inner().await.set_power_config(power).await
    }

    async fn begin_capture(
        &mut self,
        speed: Speed,
        data_tx: mpsc::Sender<Buffer>,
    ) -> Result<TransferQueue, Error> {
        let mut inner = self.inner().await;

        let endpoint = match inner.interface.endpoint::<Bulk, In>(ENDPOINT) {
            Ok(endpoint) => endpoint,
            Err(_) => bail!("Failed to claim endpoint {ENDPOINT}"),
        };

        inner.start_capture(speed).await?;

        Ok(TransferQueue::new(
            endpoint,
            data_tx,
            NUM_TRANSFERS,
            READ_LEN,
        ))
    }

    async fn end_capture(&mut self) -> Result<(), Error> {
        self.inner().await.stop_capture().await
    }

    async fn post_capture(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn timestamped_events(
        &self,
        data_rx: mpsc::Receiver<Buffer>,
        reuse_tx: mpsc::Sender<Buffer>,
    ) -> Box<dyn EventIterator> {
        Box::new(CynthionStream {
            data_rx,
            reuse_tx,
            buffer: VecDeque::new(),
            padding_due: false,
            total_clk_cycles: 0,
        })
    }

    fn duplicate(&self) -> Box<dyn BackendHandle> {
        Box::new(self.clone())
    }
}

impl CynthionInner {
    async fn start_capture(&mut self, speed: Speed) -> Result<(), Error> {
        self.state.set_speed(speed);
        self.state.set_enable(true);
        if let Some(power) = &mut self.power {
            if power.start_on {
                let index = power.source_index;
                self.state.set_target_c_vbus_en(index == 0);
                self.state.set_control_vbus_en(index == 1);
                self.state.set_aux_vbus_en(index == 2);
                self.state.set_target_a_discharge(false);
                power.on_now = true;
            }
        }
        self.write_request(REQUEST_SET_STATE, self.state.0).await
    }

    async fn stop_capture(&mut self) -> Result<(), Error> {
        self.state.set_enable(false);
        if let Some(power) = &mut self.power {
            if power.stop_off {
                self.state.set_target_c_vbus_en(false);
                self.state.set_control_vbus_en(false);
                self.state.set_aux_vbus_en(false);
                self.state.set_target_a_discharge(true);
                power.on_now = false;
            }
        }
        self.write_request(REQUEST_SET_STATE, self.state.0).await
    }

    async fn set_power_config(&mut self, power: PowerConfig) -> Result<(), Error> {
        let index = power.source_index;
        let on = power.on_now;
        self.state.set_power_control_enable(true);
        self.state.set_target_c_vbus_en(on && index == 0);
        self.state.set_control_vbus_en(on && index == 1);
        self.state.set_aux_vbus_en(on && index == 2);
        self.state.set_target_a_discharge(!on);
        self.power = Some(power);
        self.write_request(REQUEST_SET_STATE, self.state.0).await
    }

    async fn write_request(&mut self, request: u8, value: u8) -> Result<(), Error> {
        self.write_request_with_data(request, u16::from(value), &[])
            .await
    }

    async fn write_request_with_data(
        &mut self,
        request: u8,
        value: u16,
        data: &[u8],
    ) -> Result<(), Error> {
        let control = ControlOut {
            control_type: ControlType::Vendor,
            recipient: Recipient::Interface,
            request,
            value,
            index: self.interface.interface_number() as u16,
            data,
        };
        let timeout = Duration::from_secs(1);
        self.interface
            .control_out(control, timeout)
            .await
            .context("Write request failed")?;
        Ok(())
    }

    async fn read_request(
        &mut self,
        request: u8,
        value: u16,
        length: usize,
    ) -> Result<Vec<u8>, Error> {
        let control = ControlIn {
            control_type: ControlType::Vendor,
            recipient: Recipient::Interface,
            request,
            value,
            index: self.interface.interface_number() as u16,
            length: u16::try_from(length).unwrap_or(u16::MAX),
        };
        let timeout = Duration::from_secs(1);
        let buf = self
            .interface
            .control_in(control, timeout)
            .await
            .context("Read request failed")?;
        Ok(buf.to_vec())
    }
}

impl CynthionHandle {
    async fn inner(&self) -> impl DerefMut<Target = CynthionInner> + use<'_> {
        self.inner.lock().await
    }

    fn ensure_trigger_supported(&self) -> Result<(), Error> {
        if self.protocol_minor < 2 {
            bail!("Trigger configuration not supported by this gateware version.")
        }
        Ok(())
    }

    pub async fn configure_test_device(&mut self, speed: Option<Speed>) -> Result<(), Error> {
        let test_config = TestConfig::new(speed);
        self.inner()
            .await
            .write_request(REQUEST_SET_TEST_CONFIG, test_config.0)
            .await
            .context("Failed to set test device configuration")
    }

    pub async fn trigger_caps(&self) -> Result<TriggerCaps, Error> {
        self.ensure_trigger_supported()?;
        let mut inner = self.inner().await;
        let data = inner
            .read_request(REQUEST_GET_TRIGGER_CAPS, 0, 64)
            .await
            .context("Failed to read trigger capabilities")?;
        if data.len() != TRIGGER_CAPS_PAYLOAD_LEN {
            bail!(
                "Expected {TRIGGER_CAPS_PAYLOAD_LEN}-byte trigger caps response, got {}",
                data.len()
            );
        }
        Ok(TriggerCaps {
            max_stages: data[0],
            max_pattern_len: data[1],
            stage_payload_len: u16::from_le_bytes([data[2], data[3]]),
        })
    }

    pub async fn set_trigger_control(&mut self, control: TriggerControl) -> Result<(), Error> {
        self.ensure_trigger_supported()?;

        let max_stages = self.trigger_caps().await?.max_stages;
        let stage_count = control.stage_count.min(max_stages);
        let mut flags = 0u8;
        if control.enable {
            flags |= 0b0000_0001;
        }
        if control.output_enable {
            flags |= 0b0000_0010;
        }
        let payload = [flags, stage_count];
        debug_assert_eq!(payload.len(), TRIGGER_CONTROL_PAYLOAD_LEN);

        let mut inner = self.inner().await;
        inner
            .write_request_with_data(REQUEST_SET_TRIGGER_CONTROL, 0, &payload)
            .await
            .context("Failed to set trigger control")
    }

    pub async fn set_trigger_stage(
        &mut self,
        stage_index: u8,
        stage: &TriggerStage,
    ) -> Result<(), Error> {
        self.ensure_trigger_supported()?;

        let caps = self.trigger_caps().await?;
        if stage_index >= caps.max_stages {
            bail!(
                "Stage index {} exceeds supported stage count {}",
                stage_index,
                caps.max_stages
            );
        }

        let max_len = usize::from(caps.max_pattern_len).min(TRIGGER_MAX_PATTERN_LEN);
        if stage.pattern.len() < usize::from(stage.length) {
            bail!(
                "Stage pattern length ({}) is shorter than stage length ({})",
                stage.pattern.len(),
                stage.length
            );
        }
        if stage.mask.len() < usize::from(stage.length) {
            bail!(
                "Stage mask length ({}) is shorter than stage length ({})",
                stage.mask.len(),
                stage.length
            );
        }

        let clamped_len = usize::from(stage.length).min(max_len);
        let mut payload = vec![0u8; TRIGGER_STAGE_PAYLOAD_LEN];
        let [offset_lo, offset_hi] = stage.offset.to_le_bytes();
        payload[0] = offset_lo;
        payload[1] = offset_hi;
        payload[2] = u8::try_from(clamped_len).unwrap_or(u8::MAX);
        payload[3] = 0;
        payload[4..(4 + clamped_len)].copy_from_slice(&stage.pattern[..clamped_len]);
        payload[(4 + TRIGGER_MAX_PATTERN_LEN)..(4 + TRIGGER_MAX_PATTERN_LEN + clamped_len)]
            .copy_from_slice(&stage.mask[..clamped_len]);
        for index in clamped_len..TRIGGER_MAX_PATTERN_LEN {
            payload[4 + TRIGGER_MAX_PATTERN_LEN + index] = 0xFF;
        }

        let mut inner = self.inner().await;
        inner
            .write_request_with_data(REQUEST_SET_TRIGGER_STAGE, u16::from(stage_index), &payload)
            .await
            .context("Failed to set trigger stage")
    }

    pub async fn get_trigger_stage(&self, stage_index: u8) -> Result<TriggerStage, Error> {
        self.ensure_trigger_supported()?;
        let mut inner = self.inner().await;
        let data = inner
            .read_request(REQUEST_GET_TRIGGER_STAGE, u16::from(stage_index), 256)
            .await
            .context("Failed to read trigger stage")?;
        if data.len() != TRIGGER_STAGE_PAYLOAD_LEN {
            bail!(
                "Expected {TRIGGER_STAGE_PAYLOAD_LEN}-byte trigger stage response, got {}",
                data.len()
            );
        }

        let offset = u16::from_le_bytes([data[0], data[1]]);
        let length = data[2].min(TRIGGER_MAX_PATTERN_LEN as u8);
        let stage_len = usize::from(length);
        let pattern = data[4..(4 + stage_len)].to_vec();
        let mask =
            data[(4 + TRIGGER_MAX_PATTERN_LEN)..(4 + TRIGGER_MAX_PATTERN_LEN + stage_len)].to_vec();
        Ok(TriggerStage {
            offset,
            length,
            pattern,
            mask,
        })
    }

    pub async fn trigger_status(&self) -> Result<TriggerStatus, Error> {
        self.ensure_trigger_supported()?;
        let mut inner = self.inner().await;
        let data = inner
            .read_request(REQUEST_GET_TRIGGER_STATUS, 0, 64)
            .await
            .context("Failed to read trigger status")?;
        if data.len() != TRIGGER_STATUS_PAYLOAD_LEN {
            bail!(
                "Expected {TRIGGER_STATUS_PAYLOAD_LEN}-byte trigger status response, got {}",
                data.len()
            );
        }
        let flags = data[0];
        Ok(TriggerStatus {
            enable: (flags & 0b0000_0001) != 0,
            armed: (flags & 0b0000_0010) != 0,
            output_enable: (flags & 0b0000_0100) != 0,
            output_state: (flags & 0b0000_1000) != 0,
            sequence_stage: data[1],
            fire_count: u16::from_le_bytes([data[2], data[3]]),
            stage_count: data[4],
        })
    }

    pub async fn arm_trigger(&mut self) -> Result<(), Error> {
        self.ensure_trigger_supported()?;
        let mut inner = self.inner().await;
        inner
            .write_request_with_data(REQUEST_ARM_TRIGGER, 0, &[])
            .await
            .context("Failed to arm trigger")
    }

    pub async fn disarm_trigger(&mut self) -> Result<(), Error> {
        self.ensure_trigger_supported()?;
        let mut inner = self.inner().await;
        inner
            .write_request_with_data(REQUEST_DISARM_TRIGGER, 0, &[])
            .await
            .context("Failed to disarm trigger")
    }
}

async fn read_byte(interface: &Interface, request: u8) -> Result<u8, Error> {
    let control = ControlIn {
        control_type: ControlType::Vendor,
        recipient: Recipient::Interface,
        request,
        value: 0,
        index: interface.interface_number() as u16,
        length: 64,
    };
    let timeout = Duration::from_secs(1);
    let buf = interface
        .control_in(control, timeout)
        .await
        .context("Failed retrieving supported speeds from device")?;
    let size = buf.len();
    if size != 1 {
        bail!("Expected 1-byte response, got {size}");
    }
    Ok(buf[0])
}

enum WaitResult {
    Received,
    Timeout,
    Ended,
}

impl EventIterator for CynthionStream {
    fn poll_next(&mut self, timeout: Duration) -> EventPoll {
        loop {
            match self.next_buffered_event() {
                Some(event) => return EventPoll::Event(Ok(event)),
                None => match self.wait_for_next_buffer(Some(timeout)) {
                    WaitResult::Received => continue,
                    WaitResult::Timeout => return EventPoll::Timeout,
                    WaitResult::Ended => return EventPoll::Ended,
                },
            }
        }
    }
}

impl Iterator for CynthionStream {
    type Item = EventResult;
    fn next(&mut self) -> Option<EventResult> {
        loop {
            // Do we have another event already in the buffer?
            match self.next_buffered_event() {
                // Yes; return the event.
                Some(event) => return Some(Ok(event)),
                // No; wait for more data from the capture thread.
                None => match self.wait_for_next_buffer(None) {
                    WaitResult::Received => continue,
                    WaitResult::Timeout => continue,
                    WaitResult::Ended => return None,
                },
            }
        }
    }
}

impl CynthionStream {
    fn wait_for_next_buffer(&mut self, timeout: Option<Duration>) -> WaitResult {
        let recv_result = match timeout {
            Some(timeout) => match self.data_rx.recv_timeout(timeout) {
                Ok(buffer) => Ok(buffer),
                Err(RecvTimeoutError::Timeout) => return WaitResult::Timeout,
                Err(RecvTimeoutError::Disconnected) => return WaitResult::Ended,
            },
            None => self
                .data_rx
                .recv()
                .map_err(|_| RecvTimeoutError::Disconnected),
        };

        match recv_result {
            Ok(buffer) => {
                self.buffer.extend(buffer.iter());
                // Buffer can now be reused.
                let _ = self.reuse_tx.send(buffer);
                WaitResult::Received
            }
            Err(RecvTimeoutError::Timeout) => WaitResult::Timeout,
            Err(RecvTimeoutError::Disconnected) => WaitResult::Ended,
        }
    }

    fn next_buffered_event(&mut self) -> Option<TimestampedEvent> {
        use TimestampedEvent::*;

        // Are we waiting for a padding byte?
        if self.padding_due {
            if self.buffer.is_empty() {
                return None;
            } else {
                self.buffer.pop_front();
                self.padding_due = false;
            }
        }

        // Loop over any non-packet events, until we get to a packet.
        loop {
            // Do we have the length and timestamp for the next packet/event?
            if self.buffer.len() < 4 {
                return None;
            }

            if self.buffer[0] == 0xFF {
                // This is an event.
                let event_code = self.buffer[1];

                // Update our cycle count.
                self.update_cycle_count();

                // Remove event from buffer.
                self.buffer.drain(0..4);

                if let Some(event_type) = EventType::from_code(event_code) {
                    return Some(Event {
                        timestamp_ns: clk_to_ns(self.total_clk_cycles),
                        event_type,
                    });
                }
            } else {
                // This is a packet, handle it below.
                break;
            }
        }

        // Do we have all the data for the next packet?
        let packet_len = u16::from_be_bytes([self.buffer[0], self.buffer[1]]) as usize;
        if self.buffer.len() <= 4 + packet_len {
            return None;
        }

        // Update our cycle count.
        self.update_cycle_count();

        // Remove the length and timestamp from the buffer.
        self.buffer.drain(0..4);

        // If packet length is odd, we will need to skip a padding byte after.
        if packet_len % 2 == 1 {
            self.padding_due = true;
        }

        // Remove the rest of the packet from the buffer and return it.
        Some(Packet {
            timestamp_ns: clk_to_ns(self.total_clk_cycles),
            bytes: self.buffer.drain(0..packet_len).collect(),
        })
    }

    fn update_cycle_count(&mut self) {
        // Decode the cycle count.
        let clk_cycles = u16::from_be_bytes([self.buffer[2], self.buffer[3]]);

        // Update our running total.
        self.total_clk_cycles += clk_cycles as u64;
    }
}

impl Speed {
    pub fn mask(&self) -> u8 {
        use Speed::*;
        match self {
            Auto => 0b0001,
            Low => 0b0010,
            Full => 0b0100,
            High => 0b1000,
        }
    }
}
