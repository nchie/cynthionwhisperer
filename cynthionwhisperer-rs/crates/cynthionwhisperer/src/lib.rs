#[macro_use]
extern crate bitfield;

pub mod backend;
pub mod capture;
pub mod event;
pub mod usb;
pub mod util;

use anyhow::{Context, Error};
use std::time::Duration;

use crate::backend::cynthion::{CynthionDevice, CynthionHandle, VID_PID};
use crate::backend::{BackendHandle, BackendStop, EventIterator, EventPoll, EventResult};

pub type Result<T> = std::result::Result<T, Error>;

pub struct Cynthion {
    handle: CynthionHandle,
}

impl Cynthion {
    pub async fn open_first() -> Result<Self> {
        let devices = nusb::list_devices()
            .await
            .context("Failed to list USB devices")?;
        let mut matches = devices.filter(|info| (info.vendor_id(), info.product_id()) == VID_PID);
        let device_info = matches
            .next()
            .ok_or_else(|| Error::msg("No Cynthion devices found"))?;
        let device = CynthionDevice { device_info };
        let handle = device
            .open()
            .await
            .context("Failed to open Cynthion device")?;
        Ok(Self { handle })
    }

    pub async fn open(info: nusb::DeviceInfo) -> Result<Self> {
        let device = CynthionDevice { device_info: info };
        let handle = device
            .open()
            .await
            .context("Failed to open Cynthion device")?;
        Ok(Self { handle })
    }

    pub fn supported_speeds(&self) -> &[Speed] {
        self.handle.supported_speeds()
    }

    pub fn metadata(&self) -> &CaptureMetadata {
        self.handle.metadata()
    }

    pub fn start_capture(&self, speed: Speed) -> Result<CaptureStream> {
        let (events, stop) = self.handle.start(
            speed,
            Box::new(|result| {
                if let Err(error) = result {
                    eprintln!("Capture worker error: {error}");
                }
            }),
        )?;
        Ok(CaptureStream {
            events,
            stop: Some(stop),
        })
    }

    pub async fn trigger_caps(&self) -> Result<crate::backend::cynthion::TriggerCaps> {
        self.handle.trigger_caps().await
    }

    pub async fn set_trigger_control(
        &mut self,
        control: crate::backend::cynthion::TriggerControl,
    ) -> Result<()> {
        self.handle.set_trigger_control(control).await
    }

    pub async fn set_trigger_stage(
        &mut self,
        stage_index: u8,
        stage: &crate::backend::cynthion::TriggerStage,
    ) -> Result<()> {
        self.handle.set_trigger_stage(stage_index, stage).await
    }

    pub async fn get_trigger_stage(
        &self,
        stage_index: u8,
    ) -> Result<crate::backend::cynthion::TriggerStage> {
        self.handle.get_trigger_stage(stage_index).await
    }

    pub async fn trigger_status(&self) -> Result<crate::backend::cynthion::TriggerStatus> {
        self.handle.trigger_status().await
    }

    pub async fn arm_trigger(&mut self) -> Result<()> {
        self.handle.arm_trigger().await
    }

    pub async fn disarm_trigger(&mut self) -> Result<()> {
        self.handle.disarm_trigger().await
    }
}

pub struct CaptureStream {
    events: Box<dyn EventIterator>,
    stop: Option<BackendStop>,
}

pub enum CapturePoll {
    Event(EventResult),
    Timeout,
    Ended,
}

impl CaptureStream {
    pub fn stop(mut self) -> Result<()> {
        if let Some(stop) = self.stop.take() {
            stop.stop()?;
        }
        Ok(())
    }

    pub fn poll_next(&mut self, timeout: Duration) -> CapturePoll {
        match self.events.poll_next(timeout) {
            EventPoll::Event(event) => CapturePoll::Event(event),
            EventPoll::Timeout => CapturePoll::Timeout,
            EventPoll::Ended => CapturePoll::Ended,
        }
    }
}

impl Iterator for CaptureStream {
    type Item = EventResult;

    fn next(&mut self) -> Option<Self::Item> {
        self.events.next()
    }
}

pub use crate::usb::validate_packet;
pub use crate::{
    backend::TimestampedEvent,
    backend::cynthion::{TriggerCaps, TriggerControl, TriggerStage, TriggerStatus},
    capture::CaptureMetadata,
    event::EventType,
    usb::PID,
    usb::Speed,
};
