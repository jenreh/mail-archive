//! The mail-archive desktop shell.
//!
//! Responsibilities, in order:
//!   1. show a splash window immediately, so the user is never staring at
//!      nothing while Reflex compiles its frontend;
//!   2. start the Python backend and wait for it to answer;
//!   3. point the webview at it — or show why it never came up;
//!   4. take the whole process group down on quit.
//!
//! The shell knows nothing about FalkorDB. The backend owns that, and killing
//! the process group stops it along with everything else.

mod backend;
mod sidecar;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Emitter, Manager, RunEvent};

use backend::{
    install_signal_handlers, Backend, BackendError, BackendLauncher, ReflexEnv,
    BACKEND_URL,
};

/// Points the backend at the vendored redis-server and FalkorDB module.
const FALKORDB_RESOURCE_PATH: &str = "resources/falkordb";

/// Set by `task tauri:dev` so a dev run finds the checkout.
const REPO_ROOT_ENV_VAR: &str = "MAIL_ARCHIVE_ROOT";

struct AppState {
    backend: Mutex<Option<Backend>>,
}

pub fn run() {
    init_logging();
    // Tauri's exit events only cover a graceful quit; this covers SIGTERM/INT.
    install_signal_handlers();
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            backend: Mutex::new(None),
        })
        .setup(|app| {
            let handle = app.handle().clone();
            let falkordb_dir = resolve_falkordb_dir(app);
            let launcher = resolve_launcher();

            // Boot the backend off-thread so the splash window paints at once.
            std::thread::spawn(move || {
                match start_backend(&launcher, falkordb_dir) {
                    Ok(ready) => {
                        if let Some(state) = handle.try_state::<AppState>() {
                            *state.backend.lock().unwrap() = Some(ready);
                        }
                        show_app(&handle);
                    }
                    Err(error) => show_error(&handle, &error.to_string()),
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the mail-archive desktop shell")
        .run(|handle, event| {
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                if let Some(state) = handle.try_state::<AppState>() {
                    if let Some(backend) = state.backend.lock().unwrap().as_mut() {
                        backend.stop();
                    }
                }
            }
        });
}

fn start_backend(
    launcher: &BackendLauncher,
    falkordb_dir: Option<PathBuf>,
) -> Result<Backend, BackendError> {
    let mut backend = Backend::start(launcher, falkordb_dir)?;
    backend.wait_until_ready()?;
    Ok(backend)
}

/// Swap the splash for the running app.
fn show_app(handle: &tauri::AppHandle) {
    let Some(window) = handle.get_webview_window("main") else {
        log::error!("main window is missing");
        return;
    };
    match BACKEND_URL.parse() {
        Ok(url) => {
            if let Err(error) = window.navigate(url) {
                log::error!("could not navigate to the backend: {error}");
            }
        }
        Err(error) => log::error!("invalid backend url: {error}"),
    }
}

/// Leave the splash up and tell it what went wrong.
fn show_error(handle: &tauri::AppHandle, message: &str) {
    log::error!("{message}");
    if let Err(error) = handle.emit("backend-failed", message) {
        log::error!("could not report the failure to the splash: {error}");
    }
}

/// Where the vendored FalkorDB binaries live for this build.
fn resolve_falkordb_dir(app: &tauri::App) -> Option<PathBuf> {
    // In a bundled app they are Tauri resources; in a dev run they sit in the
    // checkout, where `task tauri:vendor` wrote them.
    if let Ok(resources) = app.path().resource_dir() {
        let bundled = resources.join(FALKORDB_RESOURCE_PATH);
        if bundled.is_dir() {
            return Some(bundled);
        }
    }
    let from_repo = repo_root()?.join("src-tauri").join(FALKORDB_RESOURCE_PATH);
    from_repo.is_dir().then_some(from_repo)
}

fn resolve_launcher() -> BackendLauncher {
    match repo_root() {
        Some(root) => BackendLauncher::Repo {
            root,
            env: if cfg!(debug_assertions) {
                // Hot-reloads the backend and still serves one port.
                ReflexEnv::Preview
            } else {
                ReflexEnv::Prod
            },
        },
        // No checkout to run from: this is where the frozen sidecar would take
        // over. Until it exists, fail loudly rather than silently doing nothing.
        None => BackendLauncher::Sidecar,
    }
}

/// Minimal stderr logger.
///
/// Without one installed, every `log::` call in this crate is silently
/// dropped — a backend that fails to start would print nothing at all. A full
/// logging plugin is not worth the compile time just for that.
fn init_logging() {
    static LOGGER: StderrLogger = StderrLogger;
    let level = if cfg!(debug_assertions) {
        log::LevelFilter::Debug
    } else {
        log::LevelFilter::Info
    };
    let _ = log::set_logger(&LOGGER).map(|()| log::set_max_level(level));
}

struct StderrLogger;

impl log::Log for StderrLogger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &log::Record) {
        if self.enabled(record.metadata()) {
            eprintln!("[{}] {}", record.level(), record.args());
        }
    }

    fn flush(&self) {}
}

fn repo_root() -> Option<PathBuf> {
    if let Ok(root) = std::env::var(REPO_ROOT_ENV_VAR) {
        let path = PathBuf::from(root);
        if path.join("rxconfig.py").is_file() {
            return Some(path);
        }
    }
    // `cargo tauri dev` runs with src-tauri as the manifest dir.
    let from_manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    from_manifest
        .join("rxconfig.py")
        .is_file()
        .then(|| from_manifest.clone())
}
