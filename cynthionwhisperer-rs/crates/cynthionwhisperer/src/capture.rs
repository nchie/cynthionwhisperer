//! Capture metadata types.

use std::num::NonZeroU32;
use std::time::Duration;

use merge::Merge;

use crate::usb::Speed;

/// Metadata about the capture.
#[derive(Clone, Default, Merge)]
pub struct CaptureMetadata {
    // Fields corresponding to PCapNG section header.
    pub application: Option<String>,
    pub os: Option<String>,
    pub hardware: Option<String>,
    pub comment: Option<String>,

    // Fields corresponding to PcapNG interface description.
    pub iface_desc: Option<String>,
    pub iface_hardware: Option<String>,
    pub iface_os: Option<String>,
    pub iface_speed: Option<Speed>,
    pub iface_snaplen: Option<NonZeroU32>,

    // Fields corresponding to PcapNG interface statistics.
    pub start_time: Option<Duration>,
    pub end_time: Option<Duration>,
    pub dropped: Option<u64>,
}
