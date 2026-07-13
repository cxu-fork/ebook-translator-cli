use std::{fs, path::Path};

use anyhow::Result;

pub fn replace(source: &Path, target: &Path) -> Result<()> {
    #[cfg(not(windows))]
    fs::rename(source, target)?;

    #[cfg(windows)]
    {
        if !target.exists() {
            fs::rename(source, target)?;
            return Ok(());
        }
        let parent = target.parent().unwrap_or_else(|| Path::new("."));
        let backup = tempfile::NamedTempFile::new_in(parent)?.into_temp_path();
        fs::remove_file(&backup)?;
        fs::rename(target, &backup)?;
        if let Err(error) = fs::rename(source, target) {
            let _ = fs::rename(&backup, target);
            return Err(error.into());
        }
        let _ = fs::remove_file(&backup);
    }
    Ok(())
}
