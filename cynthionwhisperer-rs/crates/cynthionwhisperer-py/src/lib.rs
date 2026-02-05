use futures_lite::future::block_on;
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyType};
use pyo3::{Bound, Py};

use ::cynthionwhisperer as cw;
use cw::{CaptureStream, Speed, TimestampedEvent};

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
            .map_err(|err| PyRuntimeError::new_err(err.to_string()))
    }

    fn start_capture(&self, speed: &Bound<'_, PyAny>) -> PyResult<Capture> {
        let speed = parse_speed(speed)?;
        let stream = self
            .inner
            .start_capture(speed)
            .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
        Ok(Capture { inner: Some(stream) })
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
        let Some(stream) = slf.inner.as_mut() else {
            return Ok(None);
        };
        let next = stream.next();
        match next {
            Some(Ok(event)) => event_to_pyobject(py, event).map(Some),
            Some(Err(err)) => Err(PyRuntimeError::new_err(err.to_string())),
            None => {
                slf.inner.take();
                Ok(None)
            }
        }
    }

    fn stop(&mut self) -> PyResult<()> {
        if let Some(stream) = self.inner.take() {
            stream
                .stop()
                .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
        }
        Ok(())
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

fn event_to_pyobject(py: Python<'_>, event: TimestampedEvent) -> PyResult<Py<PyAny>> {
    match event {
        TimestampedEvent::Packet { timestamp_ns, bytes } => {
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
