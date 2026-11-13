//! Field values: [`Value`] to and from Python objects.
//!
//! `null` is `None`, booleans, integers (64-bit), floats and strings are Python's own, `!!binary`
//! is `bytes`, sequences are lists (tuples go out as sequences too), mappings are dicts, and a
//! value with a local tag is a [`Tagged`].

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyList, PyString, PyTuple};
use spirisynq_yaml::{MAX_DEPTH, Value};

/// A value with a local tag: `!Link robot/camera` is `Tagged("Link", "robot/camera")`.
#[pyclass(frozen, module = "SpiriSynq._core")]
pub struct Tagged {
    /// The tag without its leading `!`.
    #[pyo3(get)]
    tag: String,
    #[pyo3(get)]
    value: Py<PyAny>,
}

#[pymethods]
impl Tagged {
    #[new]
    fn new(tag: String, value: Py<PyAny>) -> Self {
        Tagged { tag, value }
    }

    fn __eq__(&self, py: Python<'_>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        let Ok(other) = other.cast::<Tagged>() else { return Ok(false) };
        let other = other.get();
        Ok(self.tag == other.tag && self.value.bind(py).eq(&other.value)?)
    }

    fn __hash__(&self, py: Python<'_>) -> PyResult<isize> {
        (self.tag.as_str(), &self.value).into_pyobject(py)?.hash()
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        Ok(format!(
            "Tagged({:?}, {})",
            self.tag,
            self.value.bind(py).repr()?
        ))
    }
}

/// A Python object for `value`.
pub fn to_py<'py>(py: Python<'py>, value: &Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        Value::Null => py.None().into_bound(py),
        Value::Bool(b) => PyBool::new(py, *b).to_owned().into_any(),
        Value::Int(i) => i.into_pyobject(py)?.into_any(),
        Value::Float(f) => PyFloat::new(py, *f).into_any(),
        Value::String(s) => PyString::new(py, s).into_any(),
        Value::Binary(b) => PyBytes::new(py, b).into_any(),
        Value::Seq(items) => {
            let items = items.iter().map(|v| to_py(py, v));
            PyList::new(py, items.collect::<PyResult<Vec<_>>>()?)?.into_any()
        }
        Value::Map(entries) => {
            let dict = PyDict::new(py);
            for (k, v) in entries {
                dict.set_item(to_py(py, k)?, to_py(py, v)?)?;
            }
            dict.into_any()
        }
        Value::Tagged(t) => {
            let value = to_py(py, &t.value)?.unbind();
            Bound::new(py, Tagged::new(t.tag.clone(), value))?.into_any()
        }
    })
}

/// The value a Python object stands for.
pub fn from_py(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    convert(obj, 0)
}

fn convert(obj: &Bound<'_, PyAny>, depth: usize) -> PyResult<Value> {
    if depth > MAX_DEPTH {
        return Err(PyValueError::new_err(format!(
            "nested more than {MAX_DEPTH} deep"
        )));
    }
    let inner = |item: &Bound<'_, PyAny>| convert(item, depth + 1);
    if obj.is_none() {
        Ok(Value::Null)
    } else if let Ok(b) = obj.cast::<PyBool>() {
        Ok(Value::Bool(b.is_true()))
    } else if obj.is_instance_of::<pyo3::types::PyInt>() {
        Ok(Value::Int(obj.extract()?))
    } else if let Ok(f) = obj.cast::<PyFloat>() {
        Ok(Value::Float(f.value()))
    } else if let Ok(s) = obj.cast::<PyString>() {
        Ok(Value::String(s.to_str()?.to_owned()))
    } else if let Ok(b) = obj.cast::<PyBytes>() {
        Ok(Value::Binary(b.as_bytes().to_vec()))
    } else if let Ok(b) = obj.cast::<PyByteArray>() {
        Ok(Value::Binary(b.to_vec()))
    } else if let Ok(t) = obj.cast::<Tagged>() {
        let t = t.get();
        Ok(Value::tagged(t.tag.clone(), inner(t.value.bind(obj.py()))?))
    } else if obj.is_instance_of::<PyList>() || obj.is_instance_of::<PyTuple>() {
        let items = obj.try_iter()?.map(|item| inner(&item?));
        Ok(Value::Seq(items.collect::<PyResult<_>>()?))
    } else if let Ok(d) = obj.cast::<PyDict>() {
        let entries = d.iter().map(|(k, v)| Ok((inner(&k)?, inner(&v)?)));
        Ok(Value::Map(entries.collect::<PyResult<_>>()?))
    } else {
        let kind = obj.get_type().name()?;
        Err(PyTypeError::new_err(format!(
            "a {kind} can't be a field value"
        )))
    }
}
