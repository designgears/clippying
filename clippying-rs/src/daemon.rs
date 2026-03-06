use std::collections::{HashMap, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::TcpListener;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::IntoRawFd;
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc,
    Arc, Mutex,
};
use std::thread;
use std::time::Duration;

use log::{debug, error, info, warn};
use serde::{Deserialize, Serialize};
use tungstenite::Message;

use nix::unistd::{fork, setsid, ForkResult};
use pulse::sample::{Format, Spec};
use pulse::stream::Direction;
use psimple::Simple;

const SAMPLE_RATE: u32 = 48000;
const CHANNELS: u8 = 2;
const BUFFER_SECS: usize = 30;
const BUFFER_SAMPLES: usize = BUFFER_SECS * SAMPLE_RATE as usize * CHANNELS as usize;
const INPUT_GAIN: f32 = 1.0;

const MANAGER_WS_PORT: u16 = 17373;
const PID_FILE: &str = "/tmp/clippying.pid";

static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

pub const LOG_FILE: &str = "/tmp/clippying.log";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum DaemonRequest {
    #[serde(alias = "start")]
    Monitor { source: String },
    Stop { source: String },
    StopAll,
    Clip { source: String, #[serde(default)] preview_sink: Option<String> },
    Status,
    Sources,
    Sinks,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct DaemonStatus {
    source: String,
    sample_rate: u32,
    channels: u8,
    buffer_secs: usize,
    buffered_samples: usize,
    ws_port: u16,
    last_clip: Option<ClipSaved>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SourceEntry {
    name: String,
    description: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SinkEntry {
    name: String,
    description: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ClipSaved {
    path: String,
}

fn broadcast_ws(ws_clients: &Arc<Mutex<Vec<mpsc::Sender<String>>>>, msg: &str) {
    let Ok(mut clients) = ws_clients.lock() else { return };
    clients.retain(|tx| tx.send(msg.to_string()).is_ok());
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum DaemonResponse {
    Ok,
    Error { message: String },
    Status { statuses: Vec<DaemonStatus> },
    Sources { sources: Vec<SourceEntry> },
    Sinks { sinks: Vec<SinkEntry> },
}

pub fn get_sources() -> Vec<(String, String)> {
    use pulse::context::Context;
    use pulse::mainloop::standard::Mainloop;
    use std::cell::RefCell;
    use std::rc::Rc;

    let mainloop = Rc::new(RefCell::new(match Mainloop::new() {
        Some(m) => m,
        None => return Vec::new(),
    }));

    let context = Rc::new(RefCell::new(match Context::new(&*mainloop.borrow(), "clippying-sources") {
        Some(c) => c,
        None => return Vec::new(),
    }));

    if context.borrow_mut().connect(None, pulse::context::FlagSet::NOFLAGS, None).is_err() {
        return Vec::new();
    }

    // Wait for context ready
    loop {
        match mainloop.borrow_mut().iterate(true) {
            pulse::mainloop::standard::IterateResult::Success(_) => {}
            _ => return Vec::new(),
        }
        match context.borrow().get_state() {
            pulse::context::State::Ready => break,
            pulse::context::State::Failed | pulse::context::State::Terminated => return Vec::new(),
            _ => {}
        }
    }

    let sources: Rc<RefCell<Vec<(String, String)>>> = Rc::new(RefCell::new(Vec::new()));
    let sources_clone = sources.clone();

    let op = context.borrow().introspect().get_source_info_list(move |result| {
        if let pulse::callbacks::ListResult::Item(info) = result {
            let name = info.name.as_ref().map(|s| s.to_string()).unwrap_or_default();
            let desc = info.description.as_ref().map(|s| s.to_string()).unwrap_or_default();
            sources_clone.borrow_mut().push((name, desc));
        }
    });

    // Wait for operation to complete
    while op.get_state() == pulse::operation::State::Running {
        if let pulse::mainloop::standard::IterateResult::Err(_) = mainloop.borrow_mut().iterate(true) {
            break;
        }
    }

    Rc::try_unwrap(sources).unwrap_or_default().into_inner()
}

pub fn get_sinks() -> Vec<(String, String)> {
    use pulse::context::Context;
    use pulse::mainloop::standard::Mainloop;
    use std::cell::RefCell;
    use std::rc::Rc;

    let mainloop = Rc::new(RefCell::new(match Mainloop::new() {
        Some(m) => m,
        None => return Vec::new(),
    }));

    let context = Rc::new(RefCell::new(match Context::new(&*mainloop.borrow(), "clippying-sinks") {
        Some(c) => c,
        None => return Vec::new(),
    }));

    if context
        .borrow_mut()
        .connect(None, pulse::context::FlagSet::NOFLAGS, None)
        .is_err()
    {
        return Vec::new();
    }

    // Wait for context ready
    loop {
        match mainloop.borrow_mut().iterate(true) {
            pulse::mainloop::standard::IterateResult::Success(_) => {}
            _ => return Vec::new(),
        }
        match context.borrow().get_state() {
            pulse::context::State::Ready => break,
            pulse::context::State::Failed | pulse::context::State::Terminated => return Vec::new(),
            _ => {}
        }
    }

    let sinks: Rc<RefCell<Vec<(String, String)>>> = Rc::new(RefCell::new(Vec::new()));
    let sinks_clone = sinks.clone();

    let op = context
        .borrow()
        .introspect()
        .get_sink_info_list(move |result| {
            if let pulse::callbacks::ListResult::Item(info) = result {
                let name = info.name.as_ref().map(|s| s.to_string()).unwrap_or_default();
                let desc = info.description.as_ref().map(|s| s.to_string()).unwrap_or_default();
                sinks_clone.borrow_mut().push((name, desc));
            }
        });

    // Wait for operation to complete
    while op.get_state() == pulse::operation::State::Running {
        if let pulse::mainloop::standard::IterateResult::Err(_) = mainloop.borrow_mut().iterate(true) {
            break;
        }
    }

    Rc::try_unwrap(sinks).unwrap_or_default().into_inner()
}

fn bind_manager_listener() -> std::io::Result<TcpListener> {
    TcpListener::bind(("127.0.0.1", MANAGER_WS_PORT))
}

fn write_pidfile() {
    let _ = fs::write(PID_FILE, format!("{}\n", std::process::id()));
}

fn remove_pidfile() {
    let _ = fs::remove_file(PID_FILE);
}

extern "C" fn handle_shutdown_signal(_: i32) {
    SHUTDOWN_REQUESTED.store(true, Ordering::Relaxed);
}

fn install_signal_handlers() {
    unsafe {
        let mut sa: libc::sigaction = std::mem::zeroed();
        sa.sa_sigaction = handle_shutdown_signal as usize;
        sa.sa_flags = 0;
        libc::sigemptyset(&mut sa.sa_mask);
        libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
        libc::sigaction(libc::SIGINT, &sa, std::ptr::null_mut());
    }
}

pub fn stop_manager() {
    let pid = match fs::read_to_string(PID_FILE)
        .ok()
        .and_then(|s| s.trim().parse::<i32>().ok())
    {
        Some(p) => p,
        None => {
            eprintln!("clippying: not running");
            return;
        }
    };

    info!("stopping manager (pid={pid})");
    let rc = unsafe { libc::kill(pid, libc::SIGTERM) };
    if rc != 0 {
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() == Some(libc::ESRCH) {
            remove_pidfile();
            eprintln!("clippying: not running");
        } else {
            eprintln!("clippying: failed to stop: {}", err);
        }
    }
}

pub fn daemonize_manager(exe_path: &str) {
    let ws_listener = match bind_manager_listener() {
        Ok(l) => l,
        Err(e) if e.kind() == std::io::ErrorKind::AddrInUse => {
            eprintln!("clippying: already running (ws://127.0.0.1:{})", MANAGER_WS_PORT);
            return;
        }
        Err(e) => {
            eprintln!(
                "clippying: failed to start (could not bind ws://127.0.0.1:{}): {}",
                MANAGER_WS_PORT, e
            );
            return;
        }
    };

    info!("daemonizing manager (ws_port={MANAGER_WS_PORT})");

    match unsafe { fork() } {
        Ok(ForkResult::Parent { .. }) => return,
        Ok(ForkResult::Child) => {
            let _ = setsid();
            match unsafe { fork() } {
                Ok(ForkResult::Parent { .. }) => std::process::exit(0),
                Ok(ForkResult::Child) => {
                    redirect_stdio();
                    write_pidfile();
                    run_manager(exe_path, ws_listener);
                }
                Err(_) => std::process::exit(1),
            }
        }
        Err(_) => eprintln!("Fork failed"),
    }
}

fn redirect_stdio() {
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o644)
        .open(LOG_FILE)
        .expect("Failed to open log file");
    let fd = log.into_raw_fd();
    unsafe {
        libc::dup2(fd, libc::STDOUT_FILENO);
        libc::dup2(fd, libc::STDERR_FILENO);
        libc::close(fd);
    }
}

#[derive(Clone)]
struct WorkerState {
    stop_requested: Arc<AtomicBool>,
    buffer: Arc<Mutex<VecDeque<i16>>>,
    last_clip: Arc<Mutex<Option<ClipSaved>>>,
}

fn start_worker(
    source: &str,
    workers: &Arc<Mutex<HashMap<String, WorkerState>>>,
) -> Result<(), String> {
    {
        let Ok(map) = workers.lock() else {
            return Err("worker map lock".to_string());
        };
        if map.contains_key(source) {
            return Ok(());
        }
    }

    let stop_requested = Arc::new(AtomicBool::new(false));
    let buffer: Arc<Mutex<VecDeque<i16>>> = Arc::new(Mutex::new(VecDeque::with_capacity(BUFFER_SAMPLES)));
    let last_clip: Arc<Mutex<Option<ClipSaved>>> = Arc::new(Mutex::new(None));

    let state = WorkerState {
        stop_requested: stop_requested.clone(),
        buffer: buffer.clone(),
        last_clip: last_clip.clone(),
    };

    {
        let Ok(mut map) = workers.lock() else { return Err("worker map lock".to_string()) };
        map.insert(source.to_string(), state.clone());
    }

    let workers = workers.clone();
    let source_s = source.to_string();
    thread::spawn(move || {
        let spec = Spec {
            format: Format::S16NE,
            channels: CHANNELS,
            rate: SAMPLE_RATE,
        };

        let pa = match Simple::new(
            None,
            "clippying",
            Direction::Record,
            Some(&source_s),
            "capture",
            &spec,
            None,
            None,
        ) {
            Ok(s) => s,
            Err(e) => {
                error!("PulseAudio error ({}): {}", source_s, e);
                if let Ok(mut map) = workers.lock() {
                    map.remove(&source_s);
                }
                return;
            }
        };

        info!("worker started (source={})", source_s);

        let mut audio_buf = vec![0i16; 96 * CHANNELS as usize];
        while !stop_requested.load(Ordering::Relaxed) {
            let bytes = bytemuck::cast_slice_mut::<i16, u8>(&mut audio_buf);
            if let Err(e) = pa.read(bytes) {
                warn!("read error ({}): {}", source_s, e);
                break;
            }

            let Ok(mut b) = buffer.lock() else { break };
            for &sample in &audio_buf {
                let boosted = (sample as f32 * INPUT_GAIN).clamp(-32768.0, 32767.0) as i16;
                b.push_back(boosted);
            }
            while b.len() > BUFFER_SAMPLES {
                b.pop_front();
            }
        }

        if let Ok(mut map) = workers.lock() {
            map.remove(&source_s);
        }
        info!("worker stopped (source={})", source_s);
    });

    Ok(())
}

fn stop_worker(source: &str, workers: &Arc<Mutex<HashMap<String, WorkerState>>>) -> Result<(), String> {
    let Ok(mut map) = workers.lock() else { return Err("worker map lock".to_string()) };
    let Some(w) = map.remove(source) else { return Err("not running".to_string()) };
    w.stop_requested.store(true, Ordering::Relaxed);
    Ok(())
}

fn stop_all_workers(workers: &Arc<Mutex<HashMap<String, WorkerState>>>) {
    let Ok(mut map) = workers.lock() else { return };
    for (_, w) in map.drain() {
        w.stop_requested.store(true, Ordering::Relaxed);
    }
}

pub fn run_manager(exe_path: &str, ws_listener: TcpListener) {
    SHUTDOWN_REQUESTED.store(false, Ordering::Relaxed);
    install_signal_handlers();

    if let Err(e) = ws_listener.set_nonblocking(true) {
        error!("failed to set WS listener nonblocking: {e}");
        return;
    }

    let ws_clients: Arc<Mutex<Vec<mpsc::Sender<String>>>> = Arc::new(Mutex::new(Vec::new()));
    let workers: Arc<Mutex<HashMap<String, WorkerState>>> = Arc::new(Mutex::new(HashMap::new()));

    info!("manager started (ws_port={MANAGER_WS_PORT})");

    {
        let ws_clients = ws_clients.clone();
        let workers = workers.clone();
        let exe_path = exe_path.to_string();
        thread::spawn(move || {
            loop {
                if SHUTDOWN_REQUESTED.load(Ordering::Relaxed) {
                    break;
                }
                match ws_listener.accept() {
                    Ok((stream, _)) => {
                        debug!("ws client connected");
                        let (tx, rx) = mpsc::channel::<String>();
                        if let Ok(mut clients) = ws_clients.lock() {
                            clients.push(tx);
                        }

                        let ws_clients = ws_clients.clone();
                        let workers = workers.clone();
                        let exe_path = exe_path.clone();
                        thread::spawn(move || {
                            let mut ws = match tungstenite::accept(stream) {
                                Ok(ws) => ws,
                                Err(e) => {
                                    debug!("ws handshake error: {e}");
                                    return;
                                }
                            };

                            let _ = ws.get_mut().set_read_timeout(Some(Duration::from_millis(100)));

                            loop {
                                while let Ok(msg) = rx.try_recv() {
                                    if ws.send(Message::Text(msg)).is_err() {
                                        return;
                                    }
                                }

                                let msg = match ws.read() {
                                    Ok(m) => m,
                                    Err(tungstenite::Error::Io(e))
                                        if e.kind() == std::io::ErrorKind::WouldBlock
                                            || e.kind() == std::io::ErrorKind::TimedOut =>
                                    {
                                        continue;
                                    }
                                    Err(_) => break,
                                };

                                let payload = match msg {
                                    Message::Text(s) => {
                                        if s.trim().is_empty() {
                                            continue;
                                        }
                                        s.into_bytes()
                                    }
                                    Message::Binary(b) => b,
                                    Message::Close(_) => break,
                                    _ => continue,
                                };

                                let resp = handle_request_bytes(
                                    &payload,
                                    &workers,
                                    &ws_clients,
                                    &exe_path,
                                );

                                let out = serde_json::to_string(&resp).unwrap_or_else(|_| "{}".to_string());
                                let _ = ws.send(Message::Text(out));
                            }
                        });
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(25));
                    }
                    Err(_) => thread::sleep(Duration::from_millis(25)),
                }
            }
        });
    }

    while !SHUTDOWN_REQUESTED.load(Ordering::Relaxed) {
        thread::sleep(Duration::from_secs(1));
    }

    info!("manager shutdown requested");
    stop_all_workers(&workers);
    remove_pidfile();
}

fn handle_request_bytes(
    bytes: &[u8],
    workers: &Arc<Mutex<HashMap<String, WorkerState>>>,
    ws_clients: &Arc<Mutex<Vec<mpsc::Sender<String>>>>,
    exe_path: &str,
) -> DaemonResponse {
    let req = match serde_json::from_slice::<DaemonRequest>(bytes) {
        Ok(r) => r,
        Err(e) => {
            debug!("bad request: {e}");
            return DaemonResponse::Error { message: format!("bad request: {}", e) };
        }
    };

    match req {
        DaemonRequest::Monitor { source } => match start_worker(&source, workers) {
            Ok(()) => DaemonResponse::Ok,
            Err(message) => DaemonResponse::Error { message },
        },
        DaemonRequest::Stop { source } => match stop_worker(&source, workers) {
            Ok(()) => DaemonResponse::Ok,
            Err(message) => DaemonResponse::Error { message },
        },
        DaemonRequest::StopAll => {
            stop_all_workers(workers);
            DaemonResponse::Ok
        }
        DaemonRequest::Clip { source, preview_sink } => {
            info!("clip requested (source={})", source);
            let worker = {
                let Ok(map) = workers.lock() else {
                    return DaemonResponse::Error { message: "worker map lock".to_string() };
                };
                map.get(&source).cloned()
            };

            let Some(w) = worker else {
                return DaemonResponse::Error { message: "not running".to_string() };
            };

            clip_buffer(
                &source,
                &w.buffer,
                &w.last_clip,
                ws_clients,
                exe_path,
                preview_sink.as_deref(),
            );
            DaemonResponse::Ok
        }
        DaemonRequest::Status => {
            let statuses = {
                let Ok(map) = workers.lock() else {
                    return DaemonResponse::Error { message: "worker map lock".to_string() };
                };

                map.iter()
                    .map(|(source, w)| daemon_status(source, &w.buffer, &w.last_clip))
                    .collect::<Vec<_>>()
            };

            DaemonResponse::Status { statuses }
        }
        DaemonRequest::Sources => {
            let sources = get_sources()
                .into_iter()
                .map(|(name, description)| SourceEntry { name, description })
                .collect::<Vec<_>>();
            DaemonResponse::Sources { sources }
        }
        DaemonRequest::Sinks => {
            let sinks = get_sinks()
                .into_iter()
                .map(|(name, description)| SinkEntry { name, description })
                .collect::<Vec<_>>();
            DaemonResponse::Sinks { sinks }
        }
    }
}

fn daemon_status(
    source: &str,
    buffer: &Arc<Mutex<VecDeque<i16>>>,
    last_clip: &Arc<Mutex<Option<ClipSaved>>>,
) -> DaemonStatus {
    let buffered_samples = buffer.lock().map(|b| b.len()).unwrap_or(0);
    let last_clip = last_clip.lock().ok().and_then(|c| c.clone());
    DaemonStatus {
        source: source.to_string(),
        sample_rate: SAMPLE_RATE,
        channels: CHANNELS,
        buffer_secs: BUFFER_SECS,
        buffered_samples,
        ws_port: MANAGER_WS_PORT,
        last_clip,
    }
}

fn clip_buffer(
    source: &str,
    buffer: &Arc<Mutex<VecDeque<i16>>>,
    last_clip: &Arc<Mutex<Option<ClipSaved>>>,
    ws_clients: &Arc<Mutex<Vec<mpsc::Sender<String>>>>,
    exe_path: &str,
    preview_sink: Option<&str>,
) {
    let mono: Vec<i16> = {
        let b = match buffer.lock() {
            Ok(b) => b,
            Err(_) => return,
        };
        b.iter()
            .copied()
            .collect::<Vec<_>>()
            .chunks_exact(CHANNELS as usize)
            .map(|frame| frame[0])
            .collect()
    };

    info!("spawning trimmer (source={}, samples={})", source, mono.len());
    let mut cmd = Command::new(exe_path);
    cmd.arg("--stdin-pcm")
        .arg(SAMPLE_RATE.to_string())
        .arg("1"); // mono

    if let Some(sink) = preview_sink.map(|s| s.trim()).filter(|s| !s.is_empty()) {
        cmd.env("PULSE_SINK", sink);
        cmd.arg("--preview-sink").arg(sink);
    }

    let mut child = match cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            error!("failed to spawn trimmer: {e}");
            return;
        }
    };

    if let Some(stdout) = child.stdout.take() {
        let last_clip = last_clip.clone();
        let ws_clients = ws_clients.clone();
        let source = source.to_string();
        thread::spawn(move || {
            use std::io::{BufRead, BufReader};
            let reader = BufReader::new(stdout);
            for line in reader.lines().flatten() {
                let Ok(mut v) = serde_json::from_str::<serde_json::Value>(&line) else { continue };
                let Some(t) = v.get("type").and_then(|x| x.as_str()) else { continue };
                if t != "clip_saved" { continue; }
                let path = v.get("path").and_then(|x| x.as_str()).unwrap_or("").to_string();
                if !path.is_empty() {
                    if let Ok(mut slot) = last_clip.lock() {
                        *slot = Some(ClipSaved { path });
                    }
                }

                if let Some(obj) = v.as_object_mut() {
                    obj.insert("source".to_string(), serde_json::Value::String(source.clone()));
                }
                let out = serde_json::to_string(&v).unwrap_or(line);
                broadcast_ws(&ws_clients, &out);
            }
        });
    }

    if let Some(stdin) = child.stdin.as_mut() {
        let bytes = bytemuck::cast_slice::<i16, u8>(&mono);
        if let Err(e) = stdin.write_all(bytes) {
            warn!("failed to write PCM to trimmer: {e}");
        }
    }
    drop(child.stdin.take());

    // Ensure the child is reaped so we don't accumulate zombies.
    thread::spawn(move || {
        let _ = child.wait();
    });
}

