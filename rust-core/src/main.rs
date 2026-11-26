use anyhow::{bail, Context, Result};
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
    /// Restore a normal working copy from a local GitArchive mirror.
    Restore {
        #[arg(long)]
        archive: PathBuf,
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
        Commands::Restore { archive, destination } => {
            restore_mirror(&archive, &destination).await
        }
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

        fetch_existing_mirror(destination).await?;
    } else {
        println!("INFO: Creating the first bare mirror.");
        // --progress forces Git to emit progress through the QProcess pipe,
        // so the desktop app can show live transfer activity.
        let status = Command::new("git")
            .args(["clone", "--mirror", "--progress"])
            .arg(url)
            .arg(destination)
            .status()
            .await
            .context("Could not start Git clone")?;
        if !status.success() {
            // The target is only removed when this process created it and clone failed.
            if destination.exists() {
                let _ = tokio::fs::remove_dir_all(destination).await;
            }
            bail!("Git clone failed (exit code: {:?}).", status.code());
        }
    }

    println!("SUCCESS: Mirror is up to date.");
    Ok(())
}

async fn restore_mirror(archive: &Path, destination: &Path) -> Result<()> {
    if !archive.join("HEAD").is_file() || !archive.join("objects").is_dir() {
        bail!("Source is not a valid bare GitArchive mirror: {}", archive.display());
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
    let status = Command::new("git")
        .args(["clone", "--progress"])
        .arg(archive)
        .arg(destination)
        .status()
        .await
        .context("Could not start Git restore clone")?;
    if !status.success() {
        // The destination did not exist before this operation, so only this
        // newly-created partial clone may be removed after a failed clone.
        if destination.exists() {
            let _ = tokio::fs::remove_dir_all(destination).await;
        }
        bail!("Git restore clone failed (exit code: {:?}).", status.code());
    }

    println!("INFO: Restoring preserved historical refs.");
    let status = Command::new("git")
        .arg("-C")
        .arg(destination)
        .args(["fetch", "--no-tags", "--force", "--progress"])
        .arg(archive)
        .arg("+refs/gitarchive/snapshots/*:refs/gitarchive/snapshots/*")
        .status()
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

async fn fetch_existing_mirror(destination: &Path) -> Result<()> {
    println!("INFO: Existing archive found; preserving current refs before fetch.");
    snapshot_refs(destination).await?;
    println!("INFO: Fetching remote refs without pruning.");

    // --mirror sets remote.origin.mirror=true. Override that behavior and
    // disable pruning explicitly, including when the user's Git config enables it.
    // The explicit refspec updates current refs but leaves deleted remote refs local.
    let status = Command::new("git")
        .arg("-C")
        .arg(destination)
        .args([
            "-c", "remote.origin.mirror=false",
            "-c", "fetch.prune=false",
            "-c", "remote.origin.prune=false",
            "fetch", "--progress", "--force", "origin", "+refs/*:refs/*",
        ])
        .status()
        .await
        .context("Could not start Git fetch")?;

    if !status.success() {
        bail!(
            "Git fetch failed (exit code: {:?}). The existing archive was kept.",
            status.code()
        );
    }
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


#[cfg(test)]
mod tests {
    use super::{fetch_existing_mirror, restore_mirror};
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

        fetch_existing_mirror(&archive)
            .await
            .expect("mirror update should succeed");

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

        fetch_existing_mirror(&archive)
            .await
            .expect("second mirror update should succeed");
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
