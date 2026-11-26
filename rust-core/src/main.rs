use anyhow::{bail, Context, Result};
use chrono::Utc;
use clap::{Parser, Subcommand};
use std::path::{Path, PathBuf};
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
    },
}

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("ERROR: {error:#}");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Sync { url, destination } => sync_mirror(&url, &destination).await,
    }
}

async fn sync_mirror(url: &str, destination: &Path) -> Result<()> {
    validate_github_url(url)?;
    check_git().await?;

    if let Some(parent) = destination.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .with_context(|| format!("Cannot create archive directory {}", parent.display()))?;
    }

    if destination.exists() {
        if !destination.join("HEAD").is_file() || !destination.join("objects").is_dir() {
            bail!(
                "Destination exists but is not a bare Git repository: {}",
                destination.display()
            );
        }

        println!("INFO: Existing archive found; preserving current refs before fetch.");
        snapshot_refs(destination).await?;
        println!("INFO: Fetching remote refs without pruning.");
        let output = Command::new("git")
            .arg("-C")
            .arg(destination)
            // --mirror sets remote.origin.mirror=true; that mode can prune deleted refs.
            // Override it and provide an explicit refspec so old local refs remain archived.
            .args(["-c", "remote.origin.mirror=false", "fetch", "--force", "origin", "+refs/*:refs/*"])
            .output()
            .await
            .context("Could not start Git fetch")?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            bail!("Git fetch failed. The existing archive was kept. {} {}", stdout, stderr);
        }
        print_output(&output.stdout);
        print_output(&output.stderr);
    } else {
        println!("INFO: Creating the first bare mirror.");
        let output = Command::new("git")
            .args(["clone", "--mirror", "--quiet"])
            .arg(url)
            .arg(destination)
            .output()
            .await
            .context("Could not start Git clone")?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            // The target is only removed when this process created it and clone failed.
            if destination.exists() {
                let _ = tokio::fs::remove_dir_all(destination).await;
            }
            bail!("Git clone failed: {} {}", stdout, stderr);
        }
        print_output(&output.stdout);
        print_output(&output.stderr);
    }

    println!("SUCCESS: Mirror is up to date.");
    Ok(())
}

async fn check_git() -> Result<()> {
    let output = Command::new("git")
        .arg("--version")
        .output()
        .await
        .context("Git for Windows was not found. Install Git for Windows and restart GitArchive.")?;
    if !output.status.success() {
        bail!("Git is installed but could not run. Check the Git for Windows installation.");
    }
    let version = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    println!("INFO: {version}");
    Ok(())
}

fn validate_github_url(url: &str) -> Result<()> {
    let valid_https = url.starts_with("https://github.com/");
    let valid_ssh = url.starts_with("git@github.com:");
    if !valid_https && !valid_ssh {
        bail!("Only GitHub repository URLs are supported in this version.");
    }
    Ok(())
}

async fn snapshot_refs(repository: &Path) -> Result<()> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repository)
        .args(["for-each-ref", "--format=%(refname) %(objectname)"])
        .output()
        .await
        .context("Could not list refs in the existing archive")?;
    if !output.status.success() {
        bail!("Could not inspect existing Git refs before update.");
    }

    let timestamp = Utc::now().format("%Y%m%dT%H%M%S%.6fZ").to_string();
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
        let snapshot_ref = format!("refs/gitarchive/snapshots/{timestamp}/{suffix}");
        let update = Command::new("git")
            .arg("-C")
            .arg(repository)
            .args(["update-ref", &snapshot_ref, object_id])
            .output()
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

fn print_output(bytes: &[u8]) {
    let text = String::from_utf8_lossy(bytes);
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        println!("GIT: {line}");
    }
}
