#[macro_use]
extern crate bitfield;

pub mod backend;
pub mod capture;
pub mod event;
pub mod usb;
pub mod util;

use anyhow::{Context, Error};

use crate::backend::cynthion::{CynthionDevice, CynthionHandle, VID_PID};
use crate::backend::{BackendHandle, BackendStop, EventIterator, EventResult};

pub type Result<T> = std::result::Result<T, Error>;

pub struct Cynthion {
    handle: CynthionHandle,
}

impl Cynthion {
    pub async fn open_first() -> Result<Self> {
        let devices = nusb::list_devices()
            .await
            .context("Failed to list USB devices")?;
        let mut matches = devices
            .filter(|info| (info.vendor_id(), info.product_id()) == VID_PID);
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
}

pub struct CaptureStream {
    events: Box<dyn EventIterator>,
    stop: Option<BackendStop>,
}

impl CaptureStream {
    pub fn stop(mut self) -> Result<()> {
        if let Some(stop) = self.stop.take() {
            stop.stop()?;
        }
        Ok(())
    }
}

impl Iterator for CaptureStream {
    type Item = EventResult;

    fn next(&mut self) -> Option<Self::Item> {
        self.events.next()
    }
}

pub use crate::usb::validate_packet;
pub use crate::{event::EventType, backend::TimestampedEvent, usb::PID, usb::Speed, capture::CaptureMetadata};
