//! Launching and supervising the Reflex backend.
//!
//! The desktop shell owns one child process: the Python backend. That backend
//! in turn owns FalkorDB, so taking down the whole process group on exit stops
//! both without the shell needing to know about the database at all.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

/// Where the backend serves once it is up. Kept in sync with
/// `configuration/config.prod.yaml`, which forces frontend and backend onto a
/// single port — Reflex refuses to start in prod mode when they differ.
pub const BACKEND_URL: &str = "http://127.0.0.1:8080";

/// The profile that selects `configuration/config.prod.yaml`.
///
/// `appkit_commons` calls `load_dotenv(override=True)` at import time, so a
/// `PROFILES` line in `.env` would beat this. The repo's `.env` deliberately
/// leaves `PROFILES` unset for exactly that reason.
const PROFILES: &str = "prod";

/// Generous on purpose: Reflex compiles the frontend on first run.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(180);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// How many lines of backend output to keep for the error screen.
const LOG_TAIL_LINES: usize = 60;

const TERMINATE_GRACE: Duration = Duration::from_secs(5);

/// Process group of the running backend, or 0.
///
/// Read from a signal handler, so it has to be an atomic rather than sitting
/// behind the `Mutex` in the app state.
#[cfg(unix)]
static BACKEND_PGID: AtomicI32 = AtomicI32::new(0);

/// Take the backend down when *this* process is signalled.
///
/// Tauri's `RunEvent::Exit` only fires on a graceful quit (⌘Q, closing the
/// window). A plain SIGTERM — which is what Ctrl-C on `task tauri:dev` and most
/// process supervisors send — kills the shell without running any `Drop`, and
/// the backend was deliberately put in its own process group so it does *not*
/// die with its parent. Without this handler that combination orphans both the
/// backend and the FalkorDB it started.
#[cfg(unix)]
pub fn install_signal_handlers() {
    // Go through the fn-pointer type first: casting a function *item* straight
    // to an integer is a lint error, and the intermediate makes the intent
    // explicit.
    let handler = on_terminate as extern "C" fn(libc::c_int) as libc::sighandler_t;
    unsafe {
        libc::signal(libc::SIGTERM, handler);
        libc::signal(libc::SIGINT, handler);
        libc::signal(libc::SIGHUP, handler);
    }
}

#[cfg(not(unix))]
pub fn install_signal_handlers() {}

/// Only `killpg` and `_exit` are called here — both are async-signal-safe.
#[cfg(unix)]
extern "C" fn on_terminate(signal: libc::c_int) {
    let pgid = BACKEND_PGID.load(Ordering::SeqCst);
    if pgid > 0 {
        unsafe {
            libc::killpg(pgid, libc::SIGTERM);
        }
    }
    unsafe { libc::_exit(128 + signal) }
}

#[derive(Debug)]
pub enum BackendError {
    /// The frozen-Python sidecar is not implemented yet.
    SidecarNotImplemented,
    Spawn(String),
    /// The backend process died before it ever served a request.
    Exited { status: String, tail: String },
    /// The backend is still running but never answered in time.
    TimedOut { tail: String },
}

impl std::fmt::Display for BackendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::SidecarNotImplemented => write!(
                f,
                "This build has no bundled Python backend. Run the app from the \
                 repository with `task tauri:dev`, or implement the frozen \
                 sidecar (see `task tauri:bundle:sidecar`)."
            ),
            Self::Spawn(message) => write!(f, "Could not start the backend: {message}"),
            Self::Exited { status, tail } => write!(
                f,
                "The backend exited during startup ({status}).\n\n{tail}"
            ),
            Self::TimedOut { tail } => write!(
                f,
                "The backend did not answer within {}s.\n\n{tail}",
                HEALTH_TIMEOUT.as_secs()
            ),
        }
    }
}

/// How this build gets a backend.
pub enum BackendLauncher {
    /// Run from the repository checkout via `uv`. This is what `task tauri:dev`
    /// and the current bundled build both use.
    Repo { root: PathBuf, env: ReflexEnv },
    /// Run a frozen, self-contained backend shipped as a Tauri sidecar.
    /// Not implemented — see `src-tauri/src/sidecar.rs`.
    Sidecar,
}

#[derive(Clone, Copy)]
pub enum ReflexEnv {
    /// Hot-reloads the backend and still serves a single port.
    Preview,
    Prod,
}

impl ReflexEnv {
    fn as_str(self) -> &'static str {
        match self {
            Self::Preview => "preview",
            Self::Prod => "prod",
        }
    }
}

/// A running backend, plus the tail of everything it has printed.
pub struct Backend {
    child: Option<Child>,
    output_tail: Arc<Mutex<Vec<String>>>,
}

impl Backend {
    pub fn start(
        launcher: &BackendLauncher,
        falkordb_dir: Option<PathBuf>,
    ) -> Result<Self, BackendError> {
        let (root, env) = match launcher {
            BackendLauncher::Sidecar => return Err(BackendError::SidecarNotImplemented),
            BackendLauncher::Repo { root, env } => (root, *env),
        };

        let mut command = Command::new("uv");
        command
            .arg("run")
            .arg("reflex")
            .arg("run")
            .arg("--env")
            .arg(env.as_str())
            .current_dir(root)
            .env("PROFILES", PROFILES)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        if let Some(dir) = falkordb_dir {
            // Tells the Python side where the vendored redis-server and
            // FalkorDB module live inside the bundle.
            command.env("MAIL_ARCHIVE_FALKORDB_DIR", dir);
        }

        #[cfg(unix)]
        {
            // Own process group, so `stop` can take down granian's workers and
            // the FalkorDB the backend started, not just the `uv` wrapper.
            command.process_group(0);
        }

        let mut child = command
            .spawn()
            .map_err(|error| BackendError::Spawn(error.to_string()))?;

        #[cfg(unix)]
        BACKEND_PGID.store(child.id() as i32, Ordering::SeqCst);

        let output_tail = Arc::new(Mutex::new(Vec::new()));
        if let Some(stdout) = child.stdout.take() {
            drain(stdout, Arc::clone(&output_tail));
        }
        if let Some(stderr) = child.stderr.take() {
            drain(stderr, Arc::clone(&output_tail));
        }

        Ok(Self {
            child: Some(child),
            output_tail,
        })
    }

    /// Block until the backend answers, or give up and report what it printed.
    pub fn wait_until_ready(&mut self) -> Result<(), BackendError> {
        let deadline = Instant::now() + HEALTH_TIMEOUT;
        while Instant::now() < deadline {
            if let Some(child) = self.child.as_mut() {
                if let Ok(Some(status)) = child.try_wait() {
                    log::error!("Backend exited during startup with {status}");
                    return Err(BackendError::Exited {
                        status: status.to_string(),
                        tail: self.tail(),
                    });
                }
            }
            if is_serving() {
                log::info!("Backend ready at {BACKEND_URL}");
                return Ok(());
            }
            std::thread::sleep(HEALTH_POLL_INTERVAL);
        }
        Err(BackendError::TimedOut { tail: self.tail() })
    }

    pub fn tail(&self) -> String {
        self.output_tail
            .lock()
            .map(|lines| lines.join("\n"))
            .unwrap_or_else(|_| "<backend output unavailable>".to_owned())
    }

    /// Stop the backend and everything it started. Safe to call twice.
    pub fn stop(&mut self) {
        #[cfg(unix)]
        BACKEND_PGID.store(0, Ordering::SeqCst);

        let Some(mut child) = self.child.take() else {
            return;
        };
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }

        log::info!("Stopping backend (pid {})", child.id());
        #[cfg(unix)]
        {
            signal_group(child.id(), libc::SIGTERM);
            let deadline = Instant::now() + TERMINATE_GRACE;
            while Instant::now() < deadline {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            log::warn!("Backend ignored SIGTERM, sending SIGKILL");
            signal_group(child.id(), libc::SIGKILL);
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(unix)]
fn signal_group(pid: u32, signal: i32) {
    // The child was spawned with `process_group(0)`, so its pid is its pgid.
    unsafe {
        libc::killpg(pid as libc::pid_t, signal);
    }
}

/// Whether the backend answers an HTTP request yet.
fn is_serving() -> bool {
    ureq::get(BACKEND_URL)
        .timeout(Duration::from_secs(2))
        .call()
        .is_ok()
}

fn drain<R: std::io::Read + Send + 'static>(reader: R, tail: Arc<Mutex<Vec<String>>>) {
    std::thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            log::debug!("backend: {line}");
            if let Ok(mut lines) = tail.lock() {
                if lines.len() == LOG_TAIL_LINES {
                    lines.remove(0);
                }
                lines.push(line);
            }
        }
    });
}
