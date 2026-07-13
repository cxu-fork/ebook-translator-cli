use std::{
    env,
    ffi::{c_char, c_int},
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::Duration,
};

use anyhow::{Context, Result, anyhow, bail};
use tempfile::{Builder, NamedTempFile, TempDir};
use wait_timeout::ChildExt;

use crate::fsutil::replace;

unsafe extern "C" {
    fn et_mobi_to_epub(
        input: *const c_char,
        output: *const c_char,
        error: *mut c_char,
        error_size: usize,
    ) -> c_int;
}

const INSTALL_HINT: &str = "安装 calibre:\n  macOS:      brew install --cask calibre\n  Ubuntu:     sudo wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sudo sh /dev/stdin\n  或在 config.json 中设置 ebook_convert_path 指向 ebook-convert 的路径";

pub struct ConvertedEpub {
    pub path: PathBuf,
    _owner: Option<TempDir>,
}

impl ConvertedEpub {
    pub fn borrowed(path: &Path) -> Self {
        Self {
            path: path.to_owned(),
            _owner: None,
        }
    }
}

pub fn convert(input: &Path, output: &Path, output_format: &str, custom: &Path) -> Result<()> {
    let input_format = input
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_lowercase();
    let mut native_error = None;
    if matches!(input_format.as_str(), "mobi" | "azw3") && output_format == "epub" {
        match mobi_to_epub_file(input, output) {
            Ok(()) => return Ok(()),
            Err(error) => native_error = Some(error),
        }
    }
    if let Err(error) = ebook_convert(input, output, output_format, custom) {
        if let Some(native_error) = native_error {
            bail!(
                "内置 MOBI/AZW3 转换失败，ebook-convert 回退也失败。\n\n内置转换错误: {native_error:#}\n\nebook-convert 错误: {error:#}"
            );
        }
        return Err(error);
    }
    Ok(())
}

pub fn convert_to_epub(input: &Path, custom: &Path) -> Result<ConvertedEpub> {
    if input
        .extension()
        .and_then(|x| x.to_str())
        .is_some_and(|x| x.eq_ignore_ascii_case("epub"))
    {
        return Ok(ConvertedEpub::borrowed(input));
    }
    let owner = tempfile::Builder::new().prefix("et_epub_").tempdir()?;
    let stem = input.file_stem().and_then(|x| x.to_str()).unwrap_or("book");
    let path = owner.path().join(format!("{stem}.epub"));
    convert(input, &path, "epub", custom)?;
    Ok(ConvertedEpub {
        path,
        _owner: Some(owner),
    })
}

fn mobi_to_epub_file(input: &Path, output: &Path) -> Result<()> {
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    let parent = output.parent().unwrap_or_else(|| Path::new("."));
    let temp = NamedTempFile::new_in(parent)?;
    let input = std::ffi::CString::new(input.as_os_str().as_encoded_bytes())?;
    let generated_path = temp.path().to_owned();
    drop(temp);
    let generated = std::ffi::CString::new(generated_path.as_os_str().as_encoded_bytes())?;
    let mut error = vec![0i8; 1024];
    // SAFETY: all pointers remain valid for the duration of the call; the error
    // buffer is writable and its exact length is supplied.
    let status = unsafe {
        et_mobi_to_epub(
            input.as_ptr(),
            generated.as_ptr(),
            error.as_mut_ptr(),
            error.len(),
        )
    };
    if status != 0 {
        let message = unsafe { std::ffi::CStr::from_ptr(error.as_ptr()) }.to_string_lossy();
        bail!("{message}");
    }
    replace(&generated_path, output)?;
    Ok(())
}

pub fn ebook_convert(
    input: &Path,
    output: &Path,
    output_format: &str,
    custom: &Path,
) -> Result<()> {
    let binary = find_ebook_convert(custom)?;
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    let parent = output.parent().unwrap_or_else(|| Path::new("."));
    let temp_dir = Builder::new().prefix(".et-").tempdir_in(parent)?;
    let suffix = output
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or(output_format);
    let temp_output = temp_dir.path().join(format!("output.{suffix}"));
    let mut command = Command::new(&binary);
    command.arg(input).arg(&temp_output);
    if matches!(output_format, "epub" | "mobi" | "azw3") {
        command.arg("--enable-heuristics");
    }
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .with_context(|| format!("无法运行 ebook-convert: {}", binary.display()))?;
    let stdout = child.stdout.take().map(|mut stream| {
        thread::spawn(move || {
            let mut data = Vec::new();
            let _ = stream.read_to_end(&mut data);
            data
        })
    });
    let stderr = child.stderr.take().map(|mut stream| {
        thread::spawn(move || {
            let mut data = Vec::new();
            let _ = stream.read_to_end(&mut data);
            data
        })
    });
    let status = match child.wait_timeout(Duration::from_secs(300))? {
        Some(status) => status,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            bail!("ebook-convert 超时 (300秒): {}", input.display());
        }
    };
    let _stdout = stdout.and_then(|x| x.join().ok()).unwrap_or_default();
    let stderr = stderr.and_then(|x| x.join().ok()).unwrap_or_default();
    if !status.success() {
        let stderr = String::from_utf8_lossy(&stderr);
        let tail = stderr
            .chars()
            .rev()
            .take(2000)
            .collect::<String>()
            .chars()
            .rev()
            .collect::<String>();
        bail!(
            "ebook-convert 失败 (返回码 {}):\n{tail}",
            status.code().unwrap_or(-1)
        );
    }
    if !temp_output.is_file() {
        bail!("ebook-convert 未生成输出文件: {}", output.display());
    }
    replace(&temp_output, output)?;
    Ok(())
}

pub fn find_ebook_convert(custom: &Path) -> Result<PathBuf> {
    if !custom.as_os_str().is_empty() {
        if custom.is_file() {
            return Ok(custom.to_owned());
        }
        if custom.components().count() == 1
            && let Some(found) = find_on_path(custom)
        {
            return Ok(found);
        }
        bail!("配置的 ebook-convert 路径无效: {}", custom.display());
    }
    if let Some(found) = find_on_path(Path::new("ebook-convert")) {
        return Ok(found);
    }
    let mut candidates = vec![
        PathBuf::from("/usr/bin/ebook-convert"),
        PathBuf::from("/usr/local/bin/ebook-convert"),
        dirs::home_dir()
            .unwrap_or_default()
            .join(".local/bin/ebook-convert"),
        PathBuf::from("/Applications/calibre.app/Contents/MacOS/ebook-convert"),
        PathBuf::from(r"C:\Program Files\Calibre2\ebook-convert.exe"),
        PathBuf::from(r"C:\Program Files (x86)\Calibre2\ebook-convert.exe"),
    ];
    for name in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"] {
        if let Some(root) = env::var_os(name) {
            candidates.push(PathBuf::from(root).join("Calibre2/ebook-convert.exe"));
        }
    }
    candidates
        .into_iter()
        .find(|x| x.is_file())
        .ok_or_else(|| anyhow!("未找到 ebook-convert。\n\n{INSTALL_HINT}"))
}

fn find_on_path(name: &Path) -> Option<PathBuf> {
    let path = env::var_os("PATH")?;
    env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_custom_path_never_falls_back() {
        let dir = tempfile::tempdir().unwrap();
        assert!(
            find_ebook_convert(&dir.path().join("missing"))
                .unwrap_err()
                .to_string()
                .contains("配置的")
        );
    }
}
