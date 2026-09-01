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

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::{Manager, RunEvent};

use backend::{
    install_signal_handlers, Backend, BackendError, BackendLauncher, ReflexEnv,
    Vendored, BACKEND_URL,
};

/// Points the backend at the vendored redis-server and FalkorDB module.
const FALKORDB_RESOURCE_PATH: &str = "resources/falkordb";

/// The vendored `uv` the backend runs under.
const UV_RESOURCE_PATH: &str = "resources/uv/uv";

/// Set by `task tauri:dev` so a dev run finds the checkout.
const REPO_ROOT_ENV_VAR: &str = "MAIL_ARCHIVE_ROOT";

struct AppState {
    backend: Mutex<Option<Backend>>,
    /// Why startup failed, once it has. The splash polls this instead of
    /// listening for an event: a fast failure — a missing `uv`, say — is
    /// reported within milliseconds, long before the webview has run the
    /// script that would register the listener, and an emit with no listener
    /// is simply dropped.
    failure: Mutex<Option<String>>,
}

/// Polled by the splash. `None` means startup is still in progress, or
/// succeeded — in which case the webview has already been navigated away.
#[tauri::command]
fn startup_failure(state: tauri::State<'_, AppState>) -> Option<String> {
    state.failure.lock().ok().and_then(|failure| failure.clone())
}

pub fn run() {
    init_logging();
    // Tauri's exit events only cover a graceful quit; this covers SIGTERM/INT.
    install_signal_handlers();
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            backend: Mutex::new(None),
            failure: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![startup_failure])
        .setup(|app| {
            let handle = app.handle().clone();
            let vendored = resolve_vendored(app);
            let launcher = match resolve_launcher(app) {
                Ok(launcher) => launcher,
                Err(message) => {
                    // The splash polls `startup_failure`, so recording the
                    // failure here surfaces it exactly like a backend one.
                    show_error(&handle, &message);
                    return Ok(());
                }
            };

            // Boot the backend off-thread so the splash window paints at once.
            std::thread::spawn(move || {
                match start_backend(&launcher, &vendored) {
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
    vendored: &Vendored,
) -> Result<Backend, BackendError> {
    let mut backend = Backend::start(launcher, vendored)?;
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

/// Leave the splash up and record what went wrong, for the splash to pick up.
fn show_error(handle: &tauri::AppHandle, message: &str) {
    log::error!("{message}");
    if let Some(state) = handle.try_state::<AppState>() {
        if let Ok(mut failure) = state.failure.lock() {
            *failure = Some(message.to_owned());
        }
    }
}

/// Where this build's vendored runtimes live.
fn resolve_vendored(app: &tauri::App) -> Vendored {
    Vendored {
        uv: vendored_path(app, UV_RESOURCE_PATH, Path::is_file),
        falkordb: vendored_path(app, FALKORDB_RESOURCE_PATH, Path::is_dir),
    }
}

/// Resolve one vendored path, bundle first.
///
/// In a bundled app these are Tauri resources; in a dev run they sit in the
/// checkout, where `task tauri:vendor` wrote them.
fn vendored_path(
    app: &tauri::App,
    relative: &str,
    exists: fn(&Path) -> bool,
) -> Option<PathBuf> {
    if let Ok(resources) = app.path().resource_dir() {
        let bundled = resources.join(relative);
        if exists(&bundled) {
            return Some(bundled);
        }
    }
    let from_repo = repo_root()?.join("src-tauri").join(relative);
    exists(&from_repo).then_some(from_repo)
}

fn resolve_launcher(app: &tauri::App) -> Result<BackendLauncher, String> {
    let Some(root) = repo_root() else {
        // No checkout to run from: this is where the frozen sidecar would take
        // over. Until it exists, fail loudly rather than silently doing nothing.
        return Ok(BackendLauncher::Sidecar);
    };
    if cfg!(debug_assertions) {
        // Hot-reloads the backend and still serves one port. A dev run keeps
        // its data in the checkout's `.state/`, exactly as before.
        return Ok(BackendLauncher::Repo {
            root,
            env: ReflexEnv::Preview,
            data_dir: None,
        });
    }
    // A production launch keeps the user's mail in the per-user application
    // data directory, never in the checkout. Prepared here, before the backend
    // exists to race it.
    let data_dir = prepare_data_dir(app)?;
    Ok(BackendLauncher::Repo {
        root,
        env: ReflexEnv::Prod,
        data_dir: Some(data_dir),
    })
}

/// Create the per-user data directory and make it private to the owner.
///
/// Resolves through Tauri's path resolver, so on macOS this is
/// `~/Library/Application Support/de.rehpoehler.mailarc` — derived from the
/// bundle identifier in `tauri.conf.json`. The archive in it is somebody's
/// actual mail, so the permissions are set with an explicit chmod rather than
/// a creation mode: a permissive umask, or a directory an earlier version left
/// behind, must not leave it readable to other users.
fn prepare_data_dir(app: &tauri::App) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("Could not resolve the per-user data directory: {error}"))?;
    std::fs::create_dir_all(&dir).map_err(|error| {
        format!(
            "Could not create the data directory {}: {error}",
            dir.display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).map_err(
            |error| {
                format!(
                    "Could not make the data directory {} private: {error}",
                    dir.display()
                )
            },
        )?;
    }
    Ok(dir)
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
