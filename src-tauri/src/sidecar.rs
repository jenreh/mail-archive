//! Frozen-Python backend sidecar — NOT IMPLEMENTED.
//!
//! Today the bundled app still shells out to `uv` against the repository
//! checkout, so it is self-contained for FalkorDB (redis-server, the FalkorDB
//! module and its OpenSSL dylibs are all vendored) but not for Python.
//!
//! Filling this in is what makes the `.app` runnable on a Mac with no Python,
//! no uv, and no checkout. The shape it needs to take:
//!
//! 1. `reflex export --frontend-only` to produce the static frontend, and
//!    freeze the backend (PyInstaller or a relocatable CPython from
//!    `uv python install`) into a single executable.
//! 2. Drop that executable at `src-tauri/binaries/mail-archive-backend-<triple>`
//!    and uncomment `bundle.externalBin` in `tauri.conf.json`.
//! 3. Return a `Command` here built from `app.shell().sidecar(...)` instead of
//!    the `uv` invocation in `backend.rs`.
//!
//! Known obstacles, so the next person does not rediscover them:
//! - Reflex re-runs its init/compile step at startup unless the exported build
//!   is present and `REFLEX_SKIP_COMPILE` is set.
//! - Reflex's asset paths assume a writable working directory.
//! - Every `.so` in the frozen `site-packages` needs signing for arm64 macOS,
//!   the same requirement that already forces the re-sign in
//!   `scripts/vendor_falkordb.py`.
//!
//! `task tauri:bundle:sidecar` prints this plan and exits non-zero.

/// Marker for the not-yet-implemented sidecar path.
///
/// Deliberately unused until the sidecar exists; it documents the contract the
/// implementation has to satisfy.
///
/// `BackendLauncher::Sidecar` returns `BackendError::SidecarNotImplemented`
/// rather than silently falling back, so a broken bundle fails loudly.
#[allow(dead_code)]
pub const NOT_IMPLEMENTED_HINT: &str =
    "Frozen Python sidecar not implemented — see src-tauri/src/sidecar.rs";
