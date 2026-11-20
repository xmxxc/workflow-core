//! Extracted PTY kernel. Private stdio protocol; lifecycle containment belongs to WFC runtime.
use anyhow::{bail, ensure, Context, Result};
use ghostty_vt::Terminal;
use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{self, BufRead, Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use uuid::Uuid;

enum Event {
    Command(String),
    Output(String, String, Vec<u8>),
    End(String, String, Option<String>),
    WriteComplete(Option<Value>, bool),
    Disconnect,
}

// 64 output events hold at most 512 KiB of copied PTY bytes. Backpressure
// reaches the PTY reader instead of growing an unbounded output backlog.
const EVENT_CAPACITY: usize = 64;
const WRITE_CAPACITY: usize = 32;
const INPUT_LIMIT: usize = 2 * 1024 * 1024;

struct WriteRequest {
    bytes: Vec<u8>,
    response: Option<Value>,
}

#[derive(Clone)]
struct Writer {
    tx: mpsc::SyncSender<WriteRequest>,
    overflow: Arc<AtomicBool>,
}

impl Writer {
    fn enqueue(&self, bytes: Vec<u8>, response: Option<Value>) -> Result<()> {
        self.tx
            .try_send(WriteRequest { bytes, response })
            .map_err(|_| anyhow::anyhow!("pty_writer_queue_full_or_closed"))
    }
}

struct Unit {
    nonce: String,
    child: Box<dyn Child + Send + Sync>,
    master: Box<dyn MasterPty + Send>,
    writer: Writer,
    terminal: Terminal,
    cols: u16,
    rows: u16,
    exit: Option<portable_pty::ExitStatus>,
    eof: bool,
    read_error: Option<String>,
}

impl Unit {
    fn poll(&mut self) -> Result<()> {
        if self.exit.is_none() {
            self.exit = self.child.try_wait()?;
        }
        Ok(())
    }

    fn close(&mut self) -> Result<()> {
        self.poll()?;
        if self.exit.is_none() {
            terminate(&mut *self.child)?;
            self.exit = Some(self.child.wait()?);
        }
        Ok(())
    }
}

impl Drop for Unit {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

// portable-pty starts the Unix command with setsid(). Constrain the signal to
// our still-live, verified group leader. Escaped groups are explicitly unsupported.
#[cfg(unix)]
fn terminate(child: &mut dyn Child) -> Result<()> {
    let pid = i32::try_from(child.process_id().context("missing child PID")?)?;
    ensure!(pid > 1, "invalid child PID");
    let group = unsafe { libc::getpgid(pid) };
    if group == pid {
        if unsafe { libc::kill(-group, libc::SIGKILL) } != 0 {
            return Err(io::Error::last_os_error().into());
        }
    } else {
        child.kill()?;
    }
    Ok(())
}

#[cfg(not(unix))]
fn terminate(child: &mut dyn Child) -> Result<()> {
    // No extracted Windows Job Object ownership: direct child termination only.
    child.kill()?;
    Ok(())
}

fn string<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .with_context(|| format!("missing string {key}"))
}

fn size(v: &Value) -> Result<(u16, u16)> {
    let cols = u16::try_from(v["cols"].as_u64().context("missing cols")?)?;
    let rows = u16::try_from(v["rows"].as_u64().context("missing rows")?)?;
    ensure!(
        (1..=1000).contains(&cols) && (1..=1000).contains(&rows),
        "invalid size"
    );
    Ok((cols, rows))
}

fn pty_size(cols: u16, rows: u16) -> PtySize {
    PtySize {
        cols,
        rows,
        pixel_width: 0,
        pixel_height: 0,
    }
}

fn spawn(v: &Value, tx: mpsc::SyncSender<Event>) -> Result<Unit> {
    let (cols, rows) = size(v)?;
    let argv = v["argv"].as_array().context("missing argv")?;
    ensure!(!argv.is_empty(), "empty argv");
    let mut args = Vec::new();
    for arg in argv {
        args.push(arg.as_str().context("argv must contain strings")?);
    }
    let mut cmd = CommandBuilder::new(args[0]);
    cmd.args(&args[1..]);
    cmd.env("TERM", ghostty_vt::TERM);
    if let Some(cwd) = v["cwd"].as_str() {
        cmd.cwd(cwd);
    }
    let pair = native_pty_system().openpty(pty_size(cols, rows))?;
    let mut reader = pair.master.try_clone_reader()?;
    let mut pty_writer = pair.master.take_writer()?;
    let (write_tx, write_rx) = mpsc::sync_channel::<WriteRequest>(WRITE_CAPACITY);
    let writer = Writer {
        tx: write_tx,
        overflow: Arc::new(AtomicBool::new(false)),
    };
    let write_events = tx.clone();
    thread::spawn(move || {
        while let Ok(request) = write_rx.recv() {
            // Only this thread writes the PTY. Each input/paste and terminal
            // query response is one FIFO item; their bytes cannot interleave.
            let success = pty_writer
                .write_all(&request.bytes)
                .and_then(|_| pty_writer.flush())
                .is_ok();
            let response = request.response.map(|mut response| {
                response["ok"] = json!(success);
                if success {
                    response["result"] = json!({"written": true});
                } else {
                    response["error"] = json!("pty_write_failed");
                }
                response
            });
            if write_events
                .send(Event::WriteComplete(response, success))
                .is_err()
                || !success
            {
                break;
            }
        }
    });
    let mut terminal = Terminal::new(cols, rows, 1000)?;
    let reply_writer = writer.clone();
    terminal.set_write_pty_callback(move |bytes| {
        // Parsing never waits for PTY input capacity. Saturation is an explicit
        // kernel failure, observed/contained by the owning runtime.
        if reply_writer.enqueue(bytes.to_vec(), None).is_err() {
            reply_writer.overflow.store(true, Ordering::Release);
        }
    })?;
    let child = pair.slave.spawn_command(cmd)?;
    drop(pair.slave);
    let nonce = Uuid::new_v4().to_string();
    let unit = string(v, "unit")?.to_string();
    let output_nonce = nonce.clone();
    thread::spawn(move || {
        let mut buffer = [0; 8192];
        let error = loop {
            match reader.read(&mut buffer) {
                Ok(0) => break None,
                Ok(n) => {
                    if tx
                        .send(Event::Output(
                            unit.clone(),
                            output_nonce.clone(),
                            buffer[..n].to_vec(),
                        ))
                        .is_err()
                    {
                        return;
                    }
                }
                // Linux PTY EIO after slave closure is EOF, not lost terminal data.
                #[cfg(target_os = "linux")]
                Err(e) if e.raw_os_error() == Some(libc::EIO) => break None,
                Err(e) => break Some(e.to_string()),
            }
        };
        let _ = tx.send(Event::End(unit, output_nonce, error));
    });
    Ok(Unit {
        nonce,
        child,
        master: pair.master,
        writer,
        terminal,
        cols,
        rows,
        exit: None,
        eof: false,
        read_error: None,
    })
}

fn handle(
    v: &Value,
    generation: &str,
    units: &mut HashMap<String, Unit>,
    tx: &mpsc::SyncSender<Event>,
) -> Result<Option<Value>> {
    ensure!(string(v, "generation")? == generation, "stale_generation");
    let name = string(v, "unit")?;
    let op = string(v, "op")?;
    if op == "spawn" {
        ensure!(!units.contains_key(name), "unit_exists");
        let unit = spawn(v, tx.clone())?;
        let result = json!({"nonce": unit.nonce, "pid": unit.child.process_id()});
        units.insert(name.to_string(), unit);
        return Ok(Some(result));
    }
    let unit = units.get_mut(name).context("unknown_unit")?;
    ensure!(string(v, "nonce")? == unit.nonce, "wrong_nonce");
    unit.poll()?;
    match op {
        "read" => Ok(Some(json!({
            "text": unit.terminal.read_text_viewport((0, 0), (unit.cols - 1, u32::from(unit.rows - 1)), false)?,
            "screen": format!("{:?}", unit.terminal.active_screen()?),
            "cols": unit.cols, "rows": unit.rows, "eof": unit.eof, "read_error": unit.read_error,
            "exit": unit.exit.as_ref().map(|s| json!({"code": s.exit_code(), "signal": s.signal()}))
        }))),
        "input" => {
            ensure!(unit.exit.is_none(), "unit_exited");
            let text = string(v, "text")?;
            ensure!(text.len() <= INPUT_LIMIT, "input_too_large");
            unit.writer.enqueue(
                text.as_bytes().to_vec(),
                Some(json!({
                    "id": v["id"], "generation": generation
                })),
            )?;
            // Completion arrives as an event. The parser keeps consuming output
            // while input is blocked, so bounded output backpressure cannot deadlock it.
            Ok(None)
        }

        "resize" => {
            ensure!(unit.exit.is_none(), "unit_exited");
            let (cols, rows) = size(v)?;
            unit.master.resize(pty_size(cols, rows))?;
            unit.terminal.resize(cols, rows, 1, 1)?;
            unit.cols = cols;
            unit.rows = rows;
            Ok(Some(json!({"cols": cols, "rows": rows})))
        }
        "close" => {
            unit.close()?;
            units.remove(name);
            Ok(Some(json!({"closed": true})))
        }
        _ => bail!("unknown_op"),
    }
}

fn emit(value: Value) -> Result<()> {
    let mut out = io::stdout().lock();
    serde_json::to_writer(&mut out, &value)?;
    writeln!(out)?;
    out.flush()?;
    Ok(())
}

fn main() -> Result<()> {
    let generation = Uuid::new_v4().to_string();
    emit(
        json!({"event": "hello", "generation": generation, "recovery": "lost", "transport": "private-stdio-ndjson", "protocol": 1, "output_queue_chunks": EVENT_CAPACITY, "output_chunk_bytes": 8192, "write_queue_items": WRITE_CAPACITY}),
    )?;
    let (tx, rx) = mpsc::sync_channel(EVENT_CAPACITY);
    let stdin_tx = tx.clone();
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(line) => {
                    if stdin_tx.send(Event::Command(line)).is_err() {
                        return;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = stdin_tx.send(Event::Disconnect);
    });
    let mut units: HashMap<String, Unit> = HashMap::new();
    while let Ok(event) = rx.recv() {
        match event {
            Event::Disconnect => break,
            Event::WriteComplete(response, success) => {
                if let Some(response) = response {
                    emit(response)?;
                }
                ensure!(success, "PTY writer failed");
            }
            Event::Output(name, nonce, bytes) => {
                if let Some(unit) = units.get_mut(&name).filter(|u| u.nonce == nonce) {
                    unit.terminal.write(&bytes);
                    ensure!(
                        !unit.writer.overflow.load(Ordering::Acquire),
                        "PTY query reply queue saturated"
                    );
                }
            }
            Event::End(name, nonce, error) => {
                if let Some(unit) = units.get_mut(&name).filter(|u| u.nonce == nonce) {
                    unit.eof = true;
                    unit.read_error = error;
                }
            }
            Event::Command(line) => {
                let request = serde_json::from_str::<Value>(&line);
                let id = request.as_ref().ok().map(|v| v["id"].clone());
                let result = request
                    .map_err(anyhow::Error::from)
                    .and_then(|v| handle(&v, &generation, &mut units, &tx));
                let response = match result {
                    Ok(None) => continue,
                    Ok(Some(result)) => {
                        json!({"id": id, "generation": generation, "ok": true, "result": result})
                    }
                    Err(error) => {
                        json!({"id": id, "generation": generation, "ok": false, "error": error.to_string()})
                    }
                };
                emit(response)?;
            }
        }
    }
    // Dropping units closes only processes launched by this server instance.
    drop(units);
    Ok(())
}
