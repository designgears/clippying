use once_cell::sync::OnceCell;
use pyo3::prelude::*;
use std::fs;
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::time::{Duration, Instant};
use url::Url;

static EMBEDDED_EXE_PATH: OnceCell<String> = OnceCell::new();
static EMBEDDED_EXE_BYTES: &[u8] = include_bytes!(env!("CLIPPYING_EMBED_BIN"));

fn parse_ws_addr(url: &str) -> Result<SocketAddr, String> {
    let parsed = Url::parse(url).map_err(|e| format!("invalid websocket url: {e}"))?;
    let host = parsed
        .host_str()
        .ok_or_else(|| "missing host in websocket url".to_string())?;
    let port = parsed
        .port_or_known_default()
        .ok_or_else(|| "missing port in websocket url".to_string())?;
    let mut addrs = (host, port)
        .to_socket_addrs()
        .map_err(|e| format!("failed to resolve {host}:{port}: {e}"))?;
    addrs
        .next()
        .ok_or_else(|| format!("no address resolved for {host}:{port}"))
}

fn tcp_up(url: &str) -> bool {
    let Ok(addr) = parse_ws_addr(url) else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok()
}

fn ensure_embedded_exe() -> Result<String, String> {
    if let Some(p) = EMBEDDED_EXE_PATH.get() {
        return Ok(p.clone());
    }

    let out_path = PathBuf::from(format!("/tmp/clippying-native-{}", env!("CARGO_PKG_VERSION")));
    let should_write = match fs::metadata(&out_path) {
        Ok(meta) => meta.len() != EMBEDDED_EXE_BYTES.len() as u64,
        Err(_) => true,
    };

    if should_write {
        fs::write(&out_path, EMBEDDED_EXE_BYTES).map_err(|e| format!("write embedded daemon failed: {e}"))?;
        fs::set_permissions(&out_path, fs::Permissions::from_mode(0o755))
            .map_err(|e| format!("chmod embedded daemon failed: {e}"))?;
    }

    let p = out_path.to_string_lossy().to_string();
    let _ = EMBEDDED_EXE_PATH.set(p.clone());
    Ok(p)
}

#[pyfunction]
fn resolve_exe(exe: String) -> PyResult<String> {
    if exe.trim().is_empty() || exe == "__embedded__" {
        ensure_embedded_exe().map_err(pyo3::exceptions::PyRuntimeError::new_err)
    } else {
        Ok(exe)
    }
}

#[pyfunction]
fn api_is_up(url: String) -> bool {
    tcp_up(&url)
}

#[pyfunction]
#[pyo3(signature = (url, exe, wait_ms=3000))]
fn ensure_api(url: String, exe: String, wait_ms: u64) -> PyResult<bool> {
    let resolved = if exe.trim().is_empty() || exe == "__embedded__" {
        ensure_embedded_exe().map_err(pyo3::exceptions::PyRuntimeError::new_err)?
    } else {
        exe
    };
    clippying::daemon::start_manager_in_thread(&resolved)
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;

    let deadline = Instant::now() + Duration::from_millis(wait_ms);
    while Instant::now() < deadline {
        if tcp_up(&url) {
            return Ok(true);
        }
        std::thread::sleep(Duration::from_millis(100));
    }

    Ok(false)
}

#[pyfunction]
fn stop_api(exe: String) -> PyResult<(bool, String)> {
    if exe.trim().is_empty() || exe == "__embedded__" {
        let _ = ensure_embedded_exe().map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    }
    clippying::daemon::stop_manager_in_thread();
    Ok((true, String::new()))
}

#[pymodule]
fn _native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(api_is_up, m)?)?;
    m.add_function(wrap_pyfunction!(ensure_api, m)?)?;
    m.add_function(wrap_pyfunction!(stop_api, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_exe, m)?)?;
    Ok(())
}
