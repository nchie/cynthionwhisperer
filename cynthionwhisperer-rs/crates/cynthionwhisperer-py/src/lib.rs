use futures_lite::future::block_on;
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyType};
use pyo3::{Bound, Py};

use ::cynthionwhisperer as cw;
use cw::{
    CapturePoll, CaptureStream, PID, PowerConfig, Speed, TimestampedEvent, TriggerControl,
    TriggerStage,
};
use std::time::Duration;

#[pyclass(unsendable)]
struct Cynthion {
    inner: cw::Cynthion,
}

#[pymethods]
impl Cynthion {
    #[classmethod]
    fn open_first(_cls: &Bound<'_, PyType>) -> PyResult<Self> {
        let result = block_on(cw::Cynthion::open_first());
        result
            .map(|inner| Self { inner })
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))
    }

    fn start_capture(&self, speed: &Bound<'_, PyAny>) -> PyResult<Capture> {
        let speed = parse_speed(speed)?;
        let stream = self
            .inner
            .start_capture(speed)
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))?;
        Ok(Capture {
            inner: Some(stream),
        })
    }

    fn power_sources(&self) -> Option<Vec<String>> {
        self.inner
            .power_sources()
            .map(|sources| sources.iter().map(|source| source.to_string()).collect())
    }

    fn power_config(&self, py: Python<'_>) -> PyResult<Option<(usize, bool, bool, bool)>> {
        let config = py.detach(|| block_on(self.inner.power_config()));
        Ok(config.map(|config| {
            (
                config.source_index,
                config.on_now,
                config.start_on,
                config.stop_off,
            )
        }))
    }

    #[pyo3(signature = (source_index, on_now, start_on=false, stop_off=false))]
    fn set_power_config(
        &mut self,
        py: Python<'_>,
        source_index: usize,
        on_now: bool,
        start_on: bool,
        stop_off: bool,
    ) -> PyResult<()> {
        let config = PowerConfig {
            source_index,
            on_now,
            start_on,
            stop_off,
        };
        py.detach(|| block_on(self.inner.set_power_config(config)))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))
    }

    fn trigger_caps(&self, py: Python<'_>) -> PyResult<(u8, u8, u16)> {
        let caps = py
            .detach(|| block_on(self.inner.trigger_caps()))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))?;
        Ok((
            caps.max_stages,
            caps.max_pattern_len,
            caps.stage_payload_len,
        ))
    }

    #[pyo3(signature = (enable, stage_count, output_enable=true))]
    fn set_trigger_control(
        &mut self,
        py: Python<'_>,
        enable: bool,
        stage_count: u8,
        output_enable: bool,
    ) -> PyResult<()> {
        let control = TriggerControl {
            enable,
            output_enable,
            stage_count,
        };
        py.detach(|| block_on(self.inner.set_trigger_control(control)))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))
    }

    #[pyo3(signature = (stage_index, offset, pattern, mask=None, length=None))]
    fn set_trigger_stage(
        &mut self,
        py: Python<'_>,
        stage_index: u8,
        offset: u16,
        pattern: &Bound<'_, PyAny>,
        mask: Option<&Bound<'_, PyAny>>,
        length: Option<u8>,
    ) -> PyResult<()> {
        let pattern = pattern
            .extract::<Vec<u8>>()
            .map_err(|_| PyTypeError::new_err("pattern must be bytes-like"))?;
        if pattern.len() > 32 {
            return Err(PyValueError::new_err("pattern must be at most 32 bytes"));
        }

        let mask = if let Some(mask) = mask {
            let parsed = mask
                .extract::<Vec<u8>>()
                .map_err(|_| PyTypeError::new_err("mask must be bytes-like"))?;
            if parsed.len() != pattern.len() {
                return Err(PyValueError::new_err(
                    "mask length must match pattern length",
                ));
            }
            parsed
        } else {
            vec![0xFF; pattern.len()]
        };

        let requested_length = length.unwrap_or(pattern.len().try_into().unwrap_or(u8::MAX));
        if usize::from(requested_length) > pattern.len() {
            return Err(PyValueError::new_err("length cannot exceed pattern length"));
        }

        let stage = TriggerStage {
            offset,
            length: requested_length,
            pattern,
            mask,
        };
        py.detach(|| block_on(self.inner.set_trigger_stage(stage_index, &stage)))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))
    }

    fn arm_trigger(&mut self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| block_on(self.inner.arm_trigger()))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))
    }

    fn disarm_trigger(&mut self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| block_on(self.inner.disarm_trigger()))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))
    }

    fn trigger_status(&self, py: Python<'_>) -> PyResult<(bool, bool, bool, bool, u8, u16, u8)> {
        let status = py
            .detach(|| block_on(self.inner.trigger_status()))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))?;
        Ok((
            status.enable,
            status.armed,
            status.output_enable,
            status.output_state,
            status.sequence_stage,
            status.fire_count,
            status.stage_count,
        ))
    }

    fn get_trigger_stage(
        &self,
        py: Python<'_>,
        stage_index: u8,
    ) -> PyResult<(u16, u8, Vec<u8>, Vec<u8>)> {
        let stage = py
            .detach(|| block_on(self.inner.get_trigger_stage(stage_index)))
            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))?;
        Ok((stage.offset, stage.length, stage.pattern, stage.mask))
    }
}

#[pyclass(unsendable)]
struct Capture {
    inner: Option<CaptureStream>,
}

#[pymethods]
impl Capture {
    fn __iter__(slf: PyRef<Self>) -> PyRef<Self> {
        slf
    }

    fn __next__(mut slf: PyRefMut<Self>, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        loop {
            py.check_signals()?;
            let next = {
                let Some(stream) = slf.inner.as_mut() else {
                    return Ok(None);
                };
                py.detach(|| stream.poll_next(Duration::from_millis(100)))
            };
            py.check_signals()?;
            match next {
                CapturePoll::Event(Ok(event)) => return event_to_pyobject(py, event).map(Some),
                CapturePoll::Event(Err(err)) => {
                    return Err(PyRuntimeError::new_err(format!("{err:#}")));
                }
                CapturePoll::Timeout => continue,
                CapturePoll::Ended => {
                    slf.inner.take();
                    return Ok(None);
                }
            }
        }
    }

    fn stop(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Some(stream) = self.inner.take() {
            py.detach(|| stream.stop())
                .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))?;
        }
        Ok(())
    }

    #[pyo3(signature = (direction, pattern, data_pid=None))]
    fn capture_until(
        mut slf: PyRefMut<Self>,
        py: Python<'_>,
        direction: &str,
        pattern: &Bound<'_, PyAny>,
        data_pid: Option<&str>,
    ) -> PyResult<Option<Py<PyAny>>> {
        let direction = parse_direction(direction)?;
        let pattern = pattern
            .extract::<Vec<u8>>()
            .map_err(|_| PyTypeError::new_err("pattern must be bytes-like (e.g. b\"\\x20\")"))?;
        let data_pid = data_pid.map(parse_data_pid).transpose()?;
        let mut last_token_direction: Option<Direction> = None;

        loop {
            py.check_signals()?;
            let next = {
                let stream = match slf.inner.as_mut() {
                    Some(stream) => stream,
                    None => return Ok(None),
                };
                py.detach(|| stream.poll_next(Duration::from_millis(100)))
            };
            py.check_signals()?;

            match next {
                CapturePoll::Timeout => continue,
                CapturePoll::Ended => {
                    slf.inner.take();
                    return Ok(None);
                }
                CapturePoll::Event(Ok(TimestampedEvent::Event { .. })) => continue,
                CapturePoll::Event(Ok(TimestampedEvent::Packet {
                    timestamp_ns,
                    bytes,
                })) => {
                    let Some(pid) = packet_pid(&bytes) else {
                        continue;
                    };

                    if pid == PID::IN {
                        last_token_direction = Some(Direction::In);
                        continue;
                    }
                    if pid == PID::OUT {
                        last_token_direction = Some(Direction::Out);
                        continue;
                    }
                    if !is_data_pid(pid) {
                        continue;
                    }
                    if direction != Direction::Any {
                        // Best effort: if we have not observed an IN/OUT token yet,
                        // do not reject on direction alone.
                        if let Some(observed_direction) = last_token_direction {
                            if observed_direction != direction {
                                continue;
                            }
                        }
                    }
                    if let Some(expected_pid) = data_pid {
                        if expected_pid != pid {
                            continue;
                        }
                    }

                    let Some(payload) = payload_from_data_packet(&bytes) else {
                        continue;
                    };
                    if !payload.starts_with(&pattern) {
                        continue;
                    }

                    if let Some(stream) = slf.inner.take() {
                        py.detach(|| stream.stop())
                            .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))?;
                    }

                    let packet = Packet {
                        timestamp_ns,
                        bytes,
                    };
                    return Py::new(py, packet).map(|obj| Some(obj.into_any()));
                }
                CapturePoll::Event(Err(err)) => {
                    return Err(PyRuntimeError::new_err(format!("{err:#}")));
                }
            }
        }
    }
}

#[pyclass]
struct Packet {
    #[pyo3(get)]
    timestamp_ns: u64,
    bytes: Vec<u8>,
}

#[pymethods]
impl Packet {
    #[getter]
    fn bytes<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.bytes)
    }
}

#[pyclass]
struct Event {
    #[pyo3(get)]
    timestamp_ns: u64,
    #[pyo3(get)]
    event_type: String,
}

#[pymodule]
fn cynthionwhisperer(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Cynthion>()?;
    m.add_class::<Capture>()?;
    m.add_class::<Packet>()?;
    m.add_class::<Event>()?;
    Ok(())
}

fn parse_speed(speed: &Bound<'_, PyAny>) -> PyResult<Speed> {
    if let Ok(speed_str) = speed.extract::<&str>() {
        let normalized = speed_str.to_ascii_lowercase();
        match normalized.as_str() {
            "auto" => Ok(Speed::Auto),
            "high" | "hs" => Ok(Speed::High),
            "full" | "fs" => Ok(Speed::Full),
            "low" | "ls" => Ok(Speed::Low),
            _ => Err(PyValueError::new_err(
                "speed must be one of: auto, high, full, low",
            )),
        }
    } else if let Ok(speed_val) = speed.extract::<u8>() {
        Ok(Speed::from(speed_val))
    } else {
        Err(PyTypeError::new_err(
            "speed must be a string like 'auto' or a u8 value",
        ))
    }
}

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
enum Direction {
    Any,
    In,
    Out,
}

fn parse_direction(direction: &str) -> PyResult<Direction> {
    match direction.to_ascii_lowercase().as_str() {
        "any" => Ok(Direction::Any),
        "in" | "incoming" => Ok(Direction::In),
        "out" | "outgoing" => Ok(Direction::Out),
        _ => Err(PyValueError::new_err(
            "direction must be one of: any, in, out",
        )),
    }
}

fn parse_data_pid(data_pid: &str) -> PyResult<PID> {
    match data_pid.to_ascii_lowercase().as_str() {
        "data0" => Ok(PID::DATA0),
        "data1" => Ok(PID::DATA1),
        "data2" => Ok(PID::DATA2),
        "mdata" => Ok(PID::MDATA),
        _ => Err(PyValueError::new_err(
            "data_pid must be one of: data0, data1, data2, mdata",
        )),
    }
}

fn packet_pid(bytes: &[u8]) -> Option<PID> {
    match cw::validate_packet(bytes) {
        Ok(pid) => Some(pid),
        Err(Some(pid)) => Some(pid),
        Err(None) => None,
    }
}

fn is_data_pid(pid: PID) -> bool {
    matches!(pid, PID::DATA0 | PID::DATA1 | PID::DATA2 | PID::MDATA)
}

fn payload_from_data_packet(bytes: &[u8]) -> Option<&[u8]> {
    // Data packets are PID + payload + CRC16.
    if bytes.len() < 3 {
        None
    } else {
        Some(&bytes[1..(bytes.len() - 2)])
    }
}

fn event_to_pyobject(py: Python<'_>, event: TimestampedEvent) -> PyResult<Py<PyAny>> {
    match event {
        TimestampedEvent::Packet {
            timestamp_ns,
            bytes,
        } => {
            let packet = Packet {
                timestamp_ns,
                bytes,
            };
            Py::new(py, packet).map(|obj| obj.into_any())
        }
        TimestampedEvent::Event {
            timestamp_ns,
            event_type,
        } => {
            let event = Event {
                timestamp_ns,
                event_type: event_type.to_string(),
            };
            Py::new(py, event).map(|obj| obj.into_any())
        }
    }
}
