//! Code describing the USB standard and its data types.

use crc::{Crc, CRC_16_USB};
use num_enum::{IntoPrimitive, FromPrimitive};

fn crc16(bytes: &[u8]) -> u16 {
    const CRC16: Crc<u16> = Crc::<u16>::new(&CRC_16_USB);
    CRC16.checksum(bytes)
}

// We can't use the CRC_5_USB implementation, because we need to
// compute the CRC over either 11 or 19 bits of data, rather than
// over an integer number of bytes.

pub fn crc5(mut input: u32, num_bits: u32) -> u8 {
    let mut state: u32 = 0x1f;
    for _ in 0..num_bits {
        let cmp = input & 1 != state & 1;
        input >>= 1;
        state >>= 1;
        if cmp {
            state ^= 0x14;
        }
    }
    (state ^ 0x1f) as u8
}

#[derive(Copy, Clone, Debug, FromPrimitive, IntoPrimitive, PartialEq)]
#[repr(u8)]
pub enum Speed {
    #[default]
    High = 0,
    Full = 1,
    Low  = 2,
    Auto = 3,
}

#[allow(clippy::upper_case_acronyms)]
#[derive(Copy, Clone, Debug, Default, IntoPrimitive, FromPrimitive, PartialEq, Eq)]
#[repr(u8)]
pub enum PID {
    RSVD  = 0xF0,
    OUT   = 0xE1,
    ACK   = 0xD2,
    DATA0 = 0xC3,
    PING  = 0xB4,
    SOF   = 0xA5,
    NYET  = 0x96,
    DATA2 = 0x87,
    SPLIT = 0x78,
    IN    = 0x69,
    NAK   = 0x5A,
    DATA1 = 0x4B,
    ERR   = 0x3C,
    SETUP = 0x2D,
    STALL = 0x1E,
    MDATA = 0x0F,
    #[default]
    Malformed = 0,
}

impl std::fmt::Display for PID {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

impl From<&u8> for PID {
    fn from(byte: &u8) -> PID {
        PID::from(*byte)
    }
}

pub fn validate_packet(packet: &[u8]) -> Result<PID, Option<PID>> {
    use PID::*;

    match packet.first().map(PID::from) {
        // A zero-byte packet is always invalid, and has no PID.
        None => Err(None),

        // Otherwise, check validity according to PID.
        Some(pid) => {
            let len = packet.len();
            let valid = match pid {

                // SOF and tokens must be three bytes, with a valid CRC5.
                SOF | SETUP | IN | OUT | PING if len == 3 => {
                    let data = u32::from_le_bytes(
                        [packet[1], packet[2] & 0x07, 0, 0]);
                    let crc = packet[2] >> 3;
                    crc == crc5(data, 11)
                }

                // SPLIT packets must be four bytes, with a valid CRC5.
                SPLIT if len == 4 => {
                    let data = u32::from_le_bytes(
                        [packet[1], packet[2], packet[3] & 0x07, 0]);
                    let crc = packet[3] >> 3;
                    crc == crc5(data, 19)
                },

                // Data packets must be 3 to 1027 bytes, with a valid CRC16.
                DATA0 | DATA1 | DATA2 | MDATA if (3..=1027).contains(&len) => {
                    let data = &packet[1..(len - 2)];
                    let crc = u16::from_le_bytes([packet[len - 2], packet[len - 1]]);
                    crc == crc16(data)
                }

                // Handshake packets must be a single byte.
                ACK | NAK | NYET | STALL | ERR if len == 1 => true,

                // Anything else is invalid.
                _ => false
            };

            if valid {
                // Packet is valid.
                Ok(pid)
            } else {
                // Invalid, but has a (possibly wrong or malformed) PID byte.
                Err(Some(pid))
            }
        }
    }
}

impl Speed {
    /// How this speed setting should be displayed.
    pub fn description(&self) -> &'static str {
        use Speed::*;
        match self {
            Auto => "Auto",
            High => "High (480Mbps)",
            Full => "Full (12Mbps)",
            Low => "Low (1.5Mbps)",
        }
    }
}
