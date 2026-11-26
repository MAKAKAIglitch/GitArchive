use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{ExitStatus, Output, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::process::Command;

#[derive(Debug, Parser)]
#[command(name = "gitarchive-core", version, about = "GitArchive mirror engine")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Create or update a bare mirror without pruning old remote refs.
    Sync {
        #[arg(long)]
        url: String,
        #[arg(long)]
        destination: PathBuf,
        #[arg(long, default_value = "folder")]
        storage_format: String,
        #[arg(long, default_value = "medium")]
        compression_level: String,
        #[arg(long, default_value_t = false)]
        force_repack: bool,
        #[arg(long)]
        seven_zip: Option<PathBuf>,
    },
    /// Restore a normal working copy from a local GitArchive mirror.
    Restore {
        #[arg(long)]
        archive: PathBuf,
        #[arg(long)]
        destination: PathBuf,
        #[arg(long, default_value = "folder")]
        storage_format: String,
        #[arg(long)]
        seven_zip: Option<PathBuf>,
    },
    /// Extract a compressed mirror into a new temporary directory for local inspection.
    Materialize {
        #[arg(long)]
        archive: PathBuf,
        #[arg(long)]
        destination: PathBuf,
        #[arg(long)]
        storage_format: String,
        #[arg(long)]
        seven_zip: Option<PathBuf>,
    },
}

#[derive(Clone, Copy, Debug)]
struct ProcessPriority {
    cpu: CpuPriority,
    memory: MemoryPriority,
}

#[derive(Clone, Copy, Debug)]
enum CpuPriority {
    Idle,
    BelowNormal,
    Normal,
}

#[derive(Clone, Copy, Debug)]
enum MemoryPriority {
    VeryLow,
    Low,
    Normal,
}

impl ProcessPriority {
    fn from_environment() -> Self {
        let cpu = match std::env::var("GITARCHIVE_CPU_PRIORITY")
            .unwrap_or_else(|_| "normal".to_owned())
            .to_ascii_lowercase()
            .as_str()
        {
            "idle" => CpuPriority::Idle,
            "below_normal" => CpuPriority::BelowNormal,
            _ => CpuPriority::Normal,
        };
        let memory = match std::env::var("GITARCHIVE_MEMORY_PRIORITY")
            .unwrap_or_else(|_| "normal".to_owned())
            .to_ascii_lowercase()
            .as_str()
        {
            "very_low" => MemoryPriority::VeryLow,
            "low" => MemoryPriority::Low,
            _ => MemoryPriority::Normal,
        };
        Self { cpu, memory }
    }

    fn cpu_class(self) -> u32 {
        match self.cpu {
            CpuPriority::Idle => 0x00000040,        // IDLE_PRIORITY_CLASS
            CpuPriority::BelowNormal => 0x00004000, // BELOW_NORMAL_PRIORITY_CLASS
            CpuPriority::Normal => 0x00000020,      // NORMAL_PRIORITY_CLASS
        }
    }

    fn memory_value(self) -> u32 {
        match self.memory {
            MemoryPriority::VeryLow => 1, // MEMORY_PRIORITY_VERY_LOW
            MemoryPriority::Low => 2,     // MEMORY_PRIORITY_LOW
            MemoryPriority::Normal => 5,  // MEMORY_PRIORITY_NORMAL
        }
    }

    fn apply_current(self) {
        #[cfg(windows)]
        unsafe {
            let process = GetCurrentProcess();
            if SetPriorityClass(process, self.cpu_class()) == 0 {
                eprintln!("WARNING: Could not set the GitArchive engine CPU priority.");
            }
            if !set_memory_priority(process, self.memory_value()) {
                eprintln!("WARNING: Could not set the GitArchive engine memory priority.");
            }
        }
        #[cfg(not(windows))]
        let _ = self;
    }

    fn apply_to_pid(self, pid: u32) {
        #[cfg(windows)]
        unsafe {
            if self.memory_value() >= 5 {
                return;
            }
            let process = OpenProcess(PROCESS_SET_INFORMATION, 0, pid);
            if process.is_null() {
                return;
            }
            let _ = set_memory_priority(process, self.memory_value());
            let _ = CloseHandle(process);
        }
        #[cfg(not(windows))]
        {
            let _ = self;
            let _ = pid;
        }
    }
}

#[cfg(windows)]
const PROCESS_SET_INFORMATION: u32 = 0x0200;
#[cfg(windows)]
const PROCESS_MEMORY_PRIORITY_CLASS: i32 = 0; // ProcessMemoryPriority

#[cfg(windows)]
#[repr(C)]
struct MemoryPriorityInformation {
    memory_priority: u32,
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentProcess() -> *mut std::ffi::c_void;
    fn SetPriorityClass(process: *mut std::ffi::c_void, priority_class: u32) -> i32;
    fn SetProcessInformation(
        process: *mut std::ffi::c_void,
        information_class: i32,
        information: *const std::ffi::c_void,
        information_size: u32,
    ) -> i32;
    fn OpenProcess(
        desired_access: u32,
        inherit_handle: i32,
        process_id: u32,
    ) -> *mut std::ffi::c_void;
    fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
}

#[cfg(windows)]
unsafe fn set_memory_priority(process: *mut std::ffi::c_void, value: u32) -> bool {
    let information = MemoryPriorityInformation { memory_priority: value };
    SetProcessInformation(
        process,
        PROCESS_MEMORY_PRIORITY_CLASS,
        &information as *const _ as *const std::ffi::c_void,
        std::mem::size_of::<MemoryPriorityInformation>() as u32,
    ) != 0
}

async fn run_background_status(mut command: Command) -> Result<ExitStatus> {
    let priority = ProcessPriority::from_environment();
    #[cfg(windows)]
    command.creation_flags(priority.cpu_class());

    let mut child = command.spawn().context("Could not start a background process")?;
    if let Some(pid) = child.id() {
        priority.apply_to_pid(pid);
    }
    child.wait().await.context("Could not wait for a background process")
}

async fn run_background_output(mut command: Command) -> Result<Output> {
    let priority = ProcessPriority::from_environment();
    #[cfg(windows)]
    command.creation_flags(priority.cpu_class());

    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let child = command.spawn().context("Could not start a background process")?;
    if let Some(pid) = child.id() {
        priority.apply_to_pid(pid);
    }
    child
        .wait_with_output()
        .await
        .context("Could not collect background process output")
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    ProcessPriority::from_environment().apply_current();
    if let Err(error) = run().await {
        eprintln!("ERROR: {error:#}");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Sync {
            url,
            destination,
            storage_format,
            compression_level,
            force_repack,
            seven_zip,
        } => sync_mirror(
            &url,
            &destination,
            &storage_format,
            &compression_level,
            force_repack,
            seven_zip.as_deref(),
        )
        .await,
        Commands::Restore {
            archive,
            destination,
            storage_format,
            seven_zip,
        } => restore_with_format(
            &archive,
            &destination,
            &storage_format,
            seven_zip.as_deref(),
        )
        .await,
        Commands::Materialize {
            archive,
            destination,
            storage_format,
            seven_zip,
        } => materialize_mirror(
            &archive,
            &destination,
            &storage_format,
            seven_zip.as_deref(),
        )
        .await,
    }
}

struct TempDirectory {
    path: PathBuf,
}

impl TempDirectory {
    fn create(parent: &Path, label: &str) -> Result<Self> {
        for _ in 0..8 {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            let path = parent.join(format!(
                ".{label}-{}-{nonce}",
                std::process::id()
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Ok(Self { path }),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => {
                    return Err(error).with_context(|| {
                        format!("Cannot create temporary directory {}", path.display())
                    });
                }
            }
        }
        bail!("Could not allocate a unique temporary directory.");
    }
}

impl Drop for TempDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn parent_directory(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

fn valid_storage_format(value: &str) -> Result<&'static str> {
    match value.to_ascii_lowercase().as_str() {
        "folder" | "directory" | "git" => Ok("folder"),
        "zip" => Ok("zip"),
        "7z" => Ok("7z"),
        _ => bail!("Unsupported storage format. Use folder, zip or 7z."),
    }
}

fn compression_code(value: &str) -> Result<u8> {
    match value.to_ascii_lowercase().as_str() {
        "min" | "minimum" => Ok(1),
        "medium" | "mid" => Ok(5),
        "max" | "maximum" => Ok(9),
        _ => bail!("Unsupported compression level. Use min, medium or max."),
    }
}

async fn sync_mirror(
    url: &str,
    destination: &Path,
    requested_format: &str,
    requested_level: &str,
    force_repack: bool,
    seven_zip_path: Option<&Path>,
) -> Result<()> {
    validate_git_url(url)?;
    check_git().await?;
    let storage_format = valid_storage_format(requested_format)?;

    if storage_format == "folder" {
        return sync_folder_mirror(url, destination).await;
    }

    let level = compression_code(requested_level)?;
    sync_compressed_mirror(url, destination, storage_format, level, force_repack, seven_zip_path).await
}

async fn sync_folder_mirror(url: &str, destination: &Path) -> Result<()> {
    if let Some(parent) = destination.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .with_context(|| format!("Cannot create archive directory {}", parent.display()))?;
    }

    if destination.exists() {
        if !is_bare_mirror(destination) {
            bail!(
                "Destination exists but is not a bare Git repository: {}",
                destination.display()
            );
        }
        fetch_existing_mirror(destination).await?;
    } else {
        clone_mirror(url, destination).await?;
    }

    println!("SUCCESS: Mirror is up to date.");
    Ok(())
}

async fn clone_mirror(url: &str, destination: &Path) -> Result<()> {
    println!("INFO: Creating the first bare mirror.");
    let mut command = Command::new("git");
    command
        .env("GIT_TERMINAL_PROMPT", "0")
        .args(["clone", "--mirror", "--progress"])
        .arg(url)
        .arg(destination);
    let status = run_background_status(command)
        .await
        .context("Could not start Git clone")?;
    if !status.success() {
        if destination.exists() {
            let _ = tokio::fs::remove_dir_all(destination).await;
        }
        bail!("Git clone failed (exit code: {:?}).", status.code());
    }
    Ok(())
}

fn is_bare_mirror(path: &Path) -> bool {
    path.join("HEAD").is_file() && path.join("objects").is_dir()
}

async fn sync_compressed_mirror(
    url: &str,
    destination: &Path,
    format: &str,
    level: u8,
    force_repack: bool,
    seven_zip_path: Option<&Path>,
) -> Result<()> {
    let parent = parent_directory(destination);
    tokio::fs::create_dir_all(parent)
        .await
        .with_context(|| format!("Cannot create archive directory {}", parent.display()))?;
    recover_archive_backup(destination)?;

    if destination.exists() && !destination.is_file() {
        bail!("Compressed archive path exists but is not a file: {}", destination.display());
    }

    let seven_zip = find_7zip(seven_zip_path)?;
    let stage = TempDirectory::create(parent, "gitarchive-stage")?;
    let mirror = stage.path.join("mirror.git");

    if destination.is_file() {
        println!("INFO: Extracting the previous compressed mirror into a temporary workspace.");
        extract_compressed_archive(&seven_zip, destination, &stage.path).await?;
        if !is_bare_mirror(&mirror) {
            bail!("The compressed archive does not contain a valid mirror.git bare repository.");
        }
        println!("INFO: Preserving old refs and fetching remote changes.");
        let refs_changed = fetch_existing_mirror(&mirror).await?;
        if !refs_changed && !force_repack {
            println!("INFO: No remote refs changed; keeping the compressed archive untouched.");
            return Ok(());
        }
    } else {
        clone_mirror(url, &mirror).await?;
    }

    println!("INFO: Repacking the updated mirror as {format} (single-threaded).");
    let temporary_archive = temporary_archive_path(destination, format);
    if let Err(error) =
        create_compressed_archive(&seven_zip, &stage.path, &temporary_archive, format, level).await
    {
        let _ = fs::remove_file(&temporary_archive);
        return Err(error);
    }
    if !temporary_archive.is_file() || temporary_archive.metadata()?.len() == 0 {
        let _ = fs::remove_file(&temporary_archive);
        bail!("7-Zip did not produce a valid non-empty archive.");
    }

    if let Err(error) = promote_archive(&temporary_archive, destination) {
        let _ = fs::remove_file(&temporary_archive);
        return Err(error);
    }

    println!("SUCCESS: Compressed mirror is up to date; the previous archive was preserved until replacement completed.");
    Ok(())
}

async fn restore_with_format(
    archive: &Path,
    destination: &Path,
    requested_format: &str,
    seven_zip_path: Option<&Path>,
) -> Result<()> {
    let storage_format = valid_storage_format(requested_format)?;
    if storage_format == "folder" {
        return restore_mirror(archive, destination).await;
    }
    if !archive.is_file() {
        bail!("Compressed GitArchive mirror not found: {}", archive.display());
    }
    let seven_zip = find_7zip(seven_zip_path)?;
    let parent = parent_directory(archive);
    let stage = TempDirectory::create(parent, "gitarchive-restore")?;
    extract_compressed_archive(&seven_zip, archive, &stage.path).await?;
    let mirror = stage.path.join("mirror.git");
    if !is_bare_mirror(&mirror) {
        bail!("The compressed archive does not contain a valid mirror.git bare repository.");
    }
    restore_mirror(&mirror, destination).await
}

async fn materialize_mirror(
    archive: &Path,
    destination: &Path,
    requested_format: &str,
    seven_zip_path: Option<&Path>,
) -> Result<()> {
    let storage_format = valid_storage_format(requested_format)?;
    if storage_format == "folder" {
        bail!("Materialize is only used for compressed archives.");
    }
    if !archive.is_file() {
        bail!("Compressed GitArchive mirror not found: {}", archive.display());
    }
    if destination.exists() {
        if !destination.is_dir()
            || fs::read_dir(destination)?.next().is_some()
        {
            bail!("Materialize destination must be a new or empty directory.");
        }
    } else {
        fs::create_dir_all(destination)
            .with_context(|| format!("Cannot create {}", destination.display()))?;
    }

    let result = async {
        let seven_zip = find_7zip(seven_zip_path)?;
        extract_compressed_archive(&seven_zip, archive, destination).await?;
        let mirror = destination.join("mirror.git");
        if !is_bare_mirror(&mirror) {
            bail!("The compressed archive does not contain a valid mirror.git bare repository.");
        }
        Ok(())
    }
    .await;

    if result.is_err() {
        // The destination may be a user-supplied empty directory. Remove only
        // the extraction subtree created by this operation, not its parent.
        let _ = fs::remove_dir_all(destination.join("mirror.git"));
    }
    result
}

async fn restore_mirror(archive: &Path, destination: &Path) -> Result<()> {
    if !is_bare_mirror(archive) {
        bail!(
            "Source is not a valid bare GitArchive mirror: {}",
            archive.display()
        );
    }
    if destination.exists() {
        bail!(
            "Restore destination already exists; choose a new empty path: {}",
            destination.display()
        );
    }
    if let Some(parent) = destination.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .with_context(|| format!("Cannot create restore directory {}", parent.display()))?;
    }

    println!("INFO: Creating a working copy from the local archive.");
    let mut clone_command = Command::new("git");
    clone_command
        .args(["clone", "--progress"])
        .arg(archive)
        .arg(destination);
    let status = run_background_status(clone_command)
        .await
        .context("Could not start Git restore clone")?;
    if !status.success() {
        if destination.exists() {
            let _ = tokio::fs::remove_dir_all(destination).await;
        }
        bail!("Git restore clone failed (exit code: {:?}).", status.code());
    }

    println!("INFO: Restoring preserved historical refs.");
    let mut fetch_command = Command::new("git");
    fetch_command
        .arg("-C")
        .arg(destination)
        .args(["fetch", "--no-tags", "--force", "--progress"])
        .arg(archive)
        .arg("+refs/gitarchive/snapshots/*:refs/gitarchive/snapshots/*");
    let status = run_background_status(fetch_command)
        .await
        .context("Could not fetch preserved historical refs")?;
    if !status.success() {
        bail!(
            "Working copy was created, but archived historical refs could not be fetched (exit code: {:?}). The source archive is unchanged.",
            status.code()
        );
    }

    println!("SUCCESS: Working copy restored; historical refs are available under refs/gitarchive/snapshots/.");
    Ok(())
}

fn find_7zip(explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        if path.is_file() {
            return Ok(path.to_path_buf());
        }
        bail!("The configured 7-Zip executable does not exist: {}", path.display());
    }

    let names: &[&str] = if cfg!(windows) {
        &["7z.exe", "7zz.exe", "7z", "7zz"]
    } else {
        &["7zz", "7z"]
    };

    if let Some(path_value) = std::env::var_os("PATH") {
        for directory in std::env::split_paths(&path_value) {
            for name in names {
                let candidate = directory.join(name);
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
        }
    }

    if cfg!(windows) {
        for variable in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"] {
            if let Some(root) = std::env::var_os(variable) {
                let root = PathBuf::from(root);
                let candidate = if variable == "LOCALAPPDATA" {
                    root.join("Programs").join("7-Zip").join("7z.exe")
                } else {
                    root.join("7-Zip").join("7z.exe")
                };
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
        }
    }

    bail!("7-Zip command-line executable was not found. Install 7-Zip or add 7z.exe to PATH.")
}

fn temporary_archive_path(destination: &Path, format: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let filename = destination
        .file_name()
        .unwrap_or_else(|| std::ffi::OsStr::new("archive"));
    let mut temporary_name = OsString::from(".");
    temporary_name.push(filename);
    temporary_name.push(format!(".tmp-{}-{nonce}.{format}", std::process::id()));
    parent_directory(destination).join(temporary_name)
}

fn backup_archive_path(destination: &Path) -> PathBuf {
    let filename = destination
        .file_name()
        .unwrap_or_else(|| std::ffi::OsStr::new("archive"));
    let mut backup_name = OsString::from(".");
    backup_name.push(filename);
    backup_name.push(".gitarchive-backup");
    parent_directory(destination).join(backup_name)
}

fn recover_archive_backup(destination: &Path) -> Result<()> {
    let backup = backup_archive_path(destination);
    if backup.is_file() && !destination.exists() {
        fs::rename(&backup, destination).with_context(|| {
            format!("Could not recover the previous archive from {}", backup.display())
        })?;
    } else if backup.is_file() && destination.is_file() {
        // A completed new archive was promoted before the previous process exited.
        fs::remove_file(&backup).with_context(|| {
            format!("Could not remove stale archive backup {}", backup.display())
        })?;
    }
    Ok(())
}

fn promote_archive(temporary: &Path, destination: &Path) -> Result<()> {
    let backup = backup_archive_path(destination);
    if backup.exists() {
        if destination.exists() {
            fs::remove_file(&backup)?;
        } else {
            fs::rename(&backup, destination)?;
        }
    }

    let had_previous = destination.is_file();
    if had_previous {
        fs::rename(destination, &backup).with_context(|| {
            format!("Could not move the previous archive to {}", backup.display())
        })?;
    }

    if let Err(error) = fs::rename(temporary, destination) {
        if had_previous && backup.exists() {
            let _ = fs::rename(&backup, destination);
        }
        return Err(error).with_context(|| {
            format!("Could not promote the new archive to {}", destination.display())
        });
    }

    if backup.is_file() {
        if let Err(error) = fs::remove_file(&backup) {
            eprintln!(
                "WARNING: New archive is ready, but old backup cleanup failed ({}): {error}",
                backup.display()
            );
            // Keep the backup. recover_archive_backup() will clean it up on
            // the next sync after confirming that the destination exists.
        }
    }
    Ok(())
}

async fn create_compressed_archive(
    seven_zip: &Path,
    source_parent: &Path,
    destination: &Path,
    format: &str,
    level: u8,
) -> Result<()> {
    let level_arg = format!("-mx={level}");
    let mut command = Command::new(seven_zip);
    command
        .current_dir(source_parent)
        .arg("a")
        .arg(if format == "zip" { "-tzip" } else { "-t7z" })
        .arg(&level_arg)
        .arg("-mmt=1")
        .arg("-y");

    if format == "zip" {
        command.arg("-mm=Deflate");
    } else {
        let dictionary = match level {
            1 => "16m",
            5 => "32m",
            _ => "64m",
        };
        command
            .arg("-m0=LZMA2")
            .arg(format!("-md={dictionary}"));
    }

    command.arg(destination).arg("mirror.git");
    let status = run_background_status(command)
        .await
        .with_context(|| format!("Could not start 7-Zip at {}", seven_zip.display()))?;
    if !status.success() {
        bail!("7-Zip archive creation failed (exit code: {:?}).", status.code());
    }
    Ok(())
}

async fn extract_compressed_archive(
    seven_zip: &Path,
    archive: &Path,
    destination: &Path,
) -> Result<()> {
    validate_archive_paths(seven_zip, archive).await?;
    let output_path = format!("-o{}", destination.display());
    let mut command = Command::new(seven_zip);
    command
        .arg("x")
        .arg(archive)
        .arg(output_path)
        .args(["-y", "-mmt=1"]);
    let status = run_background_status(command)
        .await
        .with_context(|| format!("Could not start 7-Zip at {}", seven_zip.display()))?;
    if !status.success() {
        bail!(
            "7-Zip extraction failed (exit code: {:?}); the original compressed archive was not modified.",
            status.code()
        );
    }
    Ok(())
}

async fn validate_archive_paths(seven_zip: &Path, archive: &Path) -> Result<()> {
    let mut command = Command::new(seven_zip);
    command.args(["l", "-slt", "-sccUTF-8"]).arg(archive);
    let output = run_background_output(command)
        .await
        .context("Could not inspect the compressed archive before extraction")?;
    if !output.status.success() {
        bail!("7-Zip could not read the archive directory.");
    }

    let listing = String::from_utf8_lossy(&output.stdout);
    let mut after_separator = false;
    let mut entry_count = 0usize;

    for line in listing.lines() {
        if line.starts_with("----------") {
            after_separator = true;
            continue;
        }
        if !after_separator {
            continue;
        }
        if line.starts_with("Symbolic Link =")
            || line.starts_with("Hard Link =")
            || line.starts_with("Reparse Point =")
        {
            bail!("The compressed archive contains a link entry; extraction was refused.");
        }
        if let Some(raw_path) = line.strip_prefix("Path = ") {
            // The archive filename is listed before the separator. Every Path
            // after it is an archive member and must be checked, including the
            // first entry (which may be the mirror.git directory itself).
            validate_archive_entry_path(raw_path)?;
            entry_count += 1;
        }
    }

    if entry_count == 0 {
        bail!("The archive is empty or its file list could not be validated.");
    }
    Ok(())
}

fn validate_archive_entry_path(raw_path: &str) -> Result<()> {
    let normalized = raw_path.replace('\\', "/");
    if normalized.starts_with('/') || normalized.starts_with("//") {
        bail!("The compressed archive contains an absolute path; extraction was refused.");
    }
    let parts: Vec<&str> = normalized.split('/').filter(|part| !part.is_empty()).collect();
    if parts.first() != Some(&"mirror.git")
        || parts.iter().any(|part| {
            *part == "."
                || *part == ".."
                || part.contains(':')
                || part.ends_with('.')
                || part.ends_with(' ')
                || part.chars().any(char::is_control)
        })
    {
        bail!("The compressed archive contains an unsafe or unexpected path; extraction was refused.");
    }
    Ok(())
}

async fn check_git() -> Result<()> {
    let mut command = Command::new("git");
    command.arg("--version");
    let output = run_background_output(command)
        .await
        .context("Git for Windows was not found. Install Git for Windows and restart GitArchive.")?;
    if !output.status.success() {
        bail!("Git is installed but could not run. Check the Git for Windows installation.");
    }
    let version = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    println!("INFO: {version}");
    Ok(())
}

async fn list_remote_refs(repository: &Path) -> Result<String> {
    let mut command = Command::new("git");
    command
        .arg("-C")
        .arg(repository)
        .args(["for-each-ref", "--format=%(refname) %(objectname)"]);
    let output = run_background_output(command)
        .await
        .context("Could not list refs before comparing the archive")?;
    if !output.status.success() {
        bail!("Could not inspect Git refs before comparing the archive.");
    }
    let refs = String::from_utf8_lossy(&output.stdout);
    Ok(refs
        .lines()
        .filter(|line| !line.starts_with("refs/gitarchive/"))
        .collect::<Vec<_>>()
        .join("\n"))
}

async fn fetch_existing_mirror(destination: &Path) -> Result<bool> {
    println!("INFO: Existing archive found; preserving current refs before fetch.");
    let refs_before = list_remote_refs(destination).await?;
    snapshot_refs(destination).await?;
    println!("INFO: Fetching remote refs without pruning.");

    let mut command = Command::new("git");
    command
        .env("GIT_TERMINAL_PROMPT", "0")
        .arg("-C")
        .arg(destination)
        .args([
            "-c", "remote.origin.mirror=false",
            "-c", "fetch.prune=false",
            "-c", "remote.origin.prune=false",
            "fetch", "--progress", "--force", "origin", "+refs/*:refs/*",
        ]);
    let status = run_background_status(command)
        .await
        .context("Could not start Git fetch")?;

    if !status.success() {
        bail!(
            "Git fetch failed (exit code: {:?}). The existing archive was kept.",
            status.code()
        );
    }
    let refs_after = list_remote_refs(destination).await?;
    let changed = refs_before != refs_after;
    println!(
        "INFO: Remote refs {}.",
        if changed { "changed" } else { "did not change" }
    );
    Ok(changed)
}

fn valid_git_host(host: &str) -> bool {
    !host.is_empty()
        && !host.starts_with('-')
        && host.chars().all(|character| {
            character.is_ascii_alphanumeric()
                || matches!(character, '.' | '-' | ':' | '[' | ']')
        })
}

fn valid_ssh_user(user: &str) -> bool {
    !user.is_empty()
        && user.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        })
}

fn validate_git_url(url: &str) -> Result<()> {
    if url.is_empty()
        || url.chars().any(|character| character.is_whitespace() || character.is_control())
        || url.contains('?')
        || url.contains('#')
    {
        bail!("Invalid Git clone URL: empty URL, whitespace, query strings and fragments are not allowed.");
    }

    // Only standard network transports are accepted. In particular, do not
    // allow Git's ext::, file://, local-path or arbitrary remote-helper forms.
    if let Some(rest) = url.strip_prefix("https://") {
        let Some((host, path)) = rest.split_once('/') else {
            bail!("HTTPS Git URL must include a server and repository path.");
        };
        if !valid_git_host(host) || path.is_empty() {
            bail!("Invalid HTTPS Git clone URL.");
        }
        return Ok(());
    }

    if let Some(rest) = url.strip_prefix("ssh://") {
        let Some((authority, path)) = rest.split_once('/') else {
            bail!("SSH Git URL must include a server and repository path.");
        };
        let host = match authority.rsplit_once('@') {
            Some((user, host)) if valid_ssh_user(user) && authority.matches('@').count() == 1 => host,
            Some(_) => bail!("Invalid SSH user information in Git URL."),
            None => authority,
        };
        if !valid_git_host(host) || path.is_empty() {
            bail!("Invalid SSH Git clone URL.");
        }
        return Ok(());
    }

    // Standard scp-like SSH form: git@host:group/project.git.
    if let Some((identity, path)) = url.split_once(':') {
        if let Some((user, host)) = identity.rsplit_once('@') {
            if valid_ssh_user(user) && valid_git_host(host) && !path.is_empty() {
                return Ok(());
            }
        }
    }

    bail!("Unsupported Git URL. Use an HTTPS or SSH clone URL.");
}

async fn snapshot_refs(repository: &Path) -> Result<()> {
    let mut command = Command::new("git");
    command
        .arg("-C")
        .arg(repository)
        .args(["for-each-ref", "--format=%(refname) %(objectname)"]);
    let output = run_background_output(command)
        .await
        .context("Could not list refs in the existing archive")?;
    if !output.status.success() {
        bail!("Could not inspect existing Git refs before update.");
    }

    let refs = String::from_utf8_lossy(&output.stdout);
    let mut saved = 0usize;

    for line in refs.lines() {
        let Some((ref_name, object_id)) = line.split_once(' ') else {
            continue;
        };
        if ref_name.starts_with("refs/gitarchive/") {
            continue;
        }
        let suffix = ref_name.strip_prefix("refs/").unwrap_or(ref_name);
        // The object ID makes this snapshot idempotent: unchanged refs do not
        // create another snapshot every ten-minute synchronization cycle.
        let snapshot_ref = format!("refs/gitarchive/snapshots/{object_id}/{suffix}");
        let mut command = Command::new("git");
        command
            .arg("-C")
            .arg(repository)
            .args(["update-ref", &snapshot_ref, object_id]);
        let update = run_background_output(command)
            .await
            .context("Could not preserve an old Git ref")?;
        if !update.status.success() {
            let error = String::from_utf8_lossy(&update.stderr);
            bail!("Failed to preserve {ref_name}: {}", error.trim());
        }
        saved += 1;
    }

    println!("INFO: Preserved {saved} previous ref(s).");
    Ok(())
}


#[cfg(test)]
mod tests {
    use super::{
        compression_code, fetch_existing_mirror, restore_mirror,
        validate_archive_entry_path, validate_git_url, valid_storage_format,
        CpuPriority, MemoryPriority, ProcessPriority,
    };
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::process::Command as StdCommand;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock must be after UNIX epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "gitarchive-test-{}-{nonce}",
            std::process::id()
        ))
    }

    fn git<I, S>(args: I) -> String
    where
        I: IntoIterator<Item = S>,
        S: AsRef<std::ffi::OsStr>,
    {
        let output = StdCommand::new("git")
            .args(args)
            .output()
            .expect("Git must be installed to run integration tests");
        assert!(
            output.status.success(),
            "Git command failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8_lossy(&output.stdout).trim().to_owned()
    }

    fn git_dir(repo: &Path, args: &[&str]) -> String {
        let mut command = vec!["--git-dir".to_owned(), repo.to_string_lossy().into_owned()];
        command.extend(args.iter().map(|value| (*value).to_owned()));
        git(command)
    }

    #[test]
    fn accepts_common_and_custom_https_ssh_git_urls() {
        assert!(validate_git_url("https://github.com/owner/repo.git").is_ok());
        assert!(validate_git_url("https://gitlab.com/group/subgroup/repo.git").is_ok());
        assert!(validate_git_url("https://git.example.net/team/project.git").is_ok());
        assert!(validate_git_url("ssh://git@git.example.net:2222/team/project.git").is_ok());
        assert!(validate_git_url("git@git.example.net:team/project.git").is_ok());
    }

    #[test]
    fn rejects_unsafe_or_unsupported_git_transports() {
        assert!(validate_git_url("http://git.example.net/team/project.git").is_err());
        assert!(validate_git_url("file:///C:/repo").is_err());
        assert!(validate_git_url("ext::command").is_err());
        assert!(validate_git_url("https://git.example.net/team/project.git?token=secret").is_err());
        assert!(validate_git_url("https://git.example.net/team/project.git\n").is_err());
        assert!(validate_git_url("ssh://user:password@git.example.net/team/project.git").is_err());
        assert!(validate_git_url("ssh://-oProxyCommand=bad@git.example.net/team/project.git").is_err());
    }

    #[test]
    fn resource_priority_levels_map_to_expected_windows_values() {
        let minimum = ProcessPriority {
            cpu: CpuPriority::Idle,
            memory: MemoryPriority::VeryLow,
        };
        let below_normal = ProcessPriority {
            cpu: CpuPriority::BelowNormal,
            memory: MemoryPriority::Low,
        };
        let normal = ProcessPriority {
            cpu: CpuPriority::Normal,
            memory: MemoryPriority::Normal,
        };

        assert_eq!(minimum.cpu_class(), 0x00000040);
        assert_eq!(below_normal.cpu_class(), 0x00004000);
        assert_eq!(normal.cpu_class(), 0x00000020);
        assert_eq!(minimum.memory_value(), 1);
        assert_eq!(below_normal.memory_value(), 2);
        assert_eq!(normal.memory_value(), 5);
    }

    #[test]
    fn accepts_only_supported_storage_and_compression_options() {
        assert_eq!(valid_storage_format("folder").unwrap(), "folder");
        assert_eq!(valid_storage_format("zip").unwrap(), "zip");
        assert_eq!(valid_storage_format("7z").unwrap(), "7z");
        assert!(valid_storage_format("rar").is_err());
        assert_eq!(compression_code("min").unwrap(), 1);
        assert_eq!(compression_code("medium").unwrap(), 5);
        assert_eq!(compression_code("max").unwrap(), 9);
        assert!(compression_code("extreme").is_err());
    }

    #[test]
    fn compressed_archive_paths_are_confined_to_the_mirror_root() {
        assert!(validate_archive_entry_path("mirror.git/HEAD").is_ok());
        assert!(validate_archive_entry_path("mirror.git/objects/ab/cdef").is_ok());
        assert!(validate_archive_entry_path("mirror.git/../outside").is_err());
        assert!(validate_archive_entry_path("../mirror.git/HEAD").is_err());
        assert!(validate_archive_entry_path("/mirror.git/HEAD").is_err());
        assert!(validate_archive_entry_path("C:/mirror.git/HEAD").is_err());
        assert!(validate_archive_entry_path("mirror.git/config:stream").is_err());
    }

    #[tokio::test]
    async fn restore_copies_current_branch_and_historical_snapshot_refs() {
        let root = temp_dir();
        let source = root.join("source.git");
        let work = root.join("work");
        let archive = root.join("archive.git");
        let restored = root.join("restored");
        fs::create_dir_all(&root).expect("create test root");

        git(["init", "--bare", source.to_str().unwrap()]);
        git([
            "--git-dir",
            source.to_str().unwrap(),
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
        ]);
        git(["init", "-b", "main", work.to_str().unwrap()]);
        git(["-C", work.to_str().unwrap(), "config", "user.name", "GitArchive Test"]);
        git(["-C", work.to_str().unwrap(), "config", "user.email", "gitarchive-test@example.invalid"]);
        fs::write(work.join("restore.txt"), "restore me\n").expect("write test commit");
        git(["-C", work.to_str().unwrap(), "add", "."]);
        git(["-C", work.to_str().unwrap(), "commit", "-m", "restore test"]);
        let head = git(["-C", work.to_str().unwrap(), "rev-parse", "HEAD"]);
        git(["-C", work.to_str().unwrap(), "remote", "add", "origin", source.to_str().unwrap()]);
        git(["-C", work.to_str().unwrap(), "push", "origin", "main"]);
        git(["clone", "--mirror", source.to_str().unwrap(), archive.to_str().unwrap()]);

        let snapshot = format!("refs/gitarchive/snapshots/{head}/heads/main");
        git_dir(&archive, &["update-ref", &snapshot, &head]);

        restore_mirror(&archive, &restored)
            .await
            .expect("restore should create a normal working copy");

        assert_eq!(git(["-C", restored.to_str().unwrap(), "rev-parse", "HEAD"]), head);
        assert_eq!(
            git(["-C", restored.to_str().unwrap(), "rev-parse", &snapshot]),
            head,
            "the restored working copy must contain archived snapshot refs"
        );

        let second_attempt = restore_mirror(&archive, &restored).await;
        assert!(
            second_attempt.is_err(),
            "restore must never overwrite an existing destination"
        );
        assert_eq!(
            git(["-C", restored.to_str().unwrap(), "rev-parse", "HEAD"]),
            head,
            "a rejected second restore must leave the existing working copy unchanged"
        );

        fs::remove_dir_all(&root).expect("clean up test repositories");
    }

    #[tokio::test]
    async fn force_push_and_deleted_branch_are_preserved_without_duplicate_snapshots() {
        let root = temp_dir();
        let source = root.join("source.git");
        let work = root.join("work");
        let archive = root.join("archive.git");
        fs::create_dir_all(&root).expect("create test root");

        git(["init", "--bare", source.to_str().unwrap()]);
        git(["init", "-b", "main", work.to_str().unwrap()]);
        git(["-C", work.to_str().unwrap(), "config", "user.name", "GitArchive Test"]);
        git(["-C", work.to_str().unwrap(), "config", "user.email", "gitarchive-test@example.invalid"]);
        fs::write(work.join("history.txt"), "first\n").expect("write first commit");
        git(["-C", work.to_str().unwrap(), "add", "."]);
        git(["-C", work.to_str().unwrap(), "commit", "-m", "first"]);
        let first = git(["-C", work.to_str().unwrap(), "rev-parse", "HEAD"]);
        git(["-C", work.to_str().unwrap(), "branch", "topic"]);
        git(["-C", work.to_str().unwrap(), "remote", "add", "origin", source.to_str().unwrap()]);
        git(["-C", work.to_str().unwrap(), "push", "origin", "main", "topic"]);

        fs::write(work.join("history.txt"), "second\n").expect("write second commit");
        git(["-C", work.to_str().unwrap(), "commit", "-am", "second"]);
        let second = git(["-C", work.to_str().unwrap(), "rev-parse", "HEAD"]);
        git(["-C", work.to_str().unwrap(), "push", "origin", "main"]);
        git(["clone", "--mirror", source.to_str().unwrap(), archive.to_str().unwrap()]);

        // Simulate user-wide prune settings in the archive's local Git config.
        git_dir(&archive, &["config", "fetch.prune", "true"]);
        git_dir(&archive, &["config", "remote.origin.prune", "true"]);

        fs::write(work.join("history.txt"), "third\n").expect("write force-pushed commit");
        git(["-C", work.to_str().unwrap(), "commit", "-am", "third"]);
        let third = git(["-C", work.to_str().unwrap(), "rev-parse", "HEAD"]);
        git(["-C", work.to_str().unwrap(), "push", "--force", "origin", "main"]);
        git(["-C", work.to_str().unwrap(), "push", "origin", ":topic"]);

        let changed = fetch_existing_mirror(&archive)
            .await
            .expect("mirror update should succeed");
        assert!(changed, "a force-pushed ref should trigger an archive repack");

        assert_eq!(git_dir(&archive, &["rev-parse", "refs/heads/main"]), third);
        assert_eq!(
            git_dir(
                &archive,
                &["rev-parse", &format!("refs/gitarchive/snapshots/{second}/heads/main")]
            ),
            second,
            "the old main tip must remain reachable after force-push"
        );
        assert_eq!(
            git_dir(&archive, &["rev-parse", "refs/heads/topic"]),
            first,
            "a branch deleted on the remote must remain in the local mirror"
        );
        assert_eq!(
            git_dir(
                &archive,
                &["rev-parse", &format!("refs/gitarchive/snapshots/{first}/heads/topic")]
            ),
            first,
            "the deleted branch tip must also be snapshotted"
        );

        let changed = fetch_existing_mirror(&archive)
            .await
            .expect("second mirror update should succeed");
        assert!(!changed, "an unchanged remote must not require a compressed archive repack");
        let snapshots_after_second = git_dir(
            &archive,
            &["for-each-ref", "--format=%(refname)", "refs/gitarchive/snapshots"],
        )
        .lines()
        .count();

        fetch_existing_mirror(&archive)
            .await
            .expect("third mirror update should succeed");
        let snapshots_after_third = git_dir(
            &archive,
            &["for-each-ref", "--format=%(refname)", "refs/gitarchive/snapshots"],
        )
        .lines()
        .count();

        assert_eq!(
            snapshots_after_second, snapshots_after_third,
            "unchanged refs must not create a new snapshot on every sync"
        );

        fs::remove_dir_all(&root).expect("clean up test repositories");
    }
}
