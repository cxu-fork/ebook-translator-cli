"""电子书格式转换 — 支持 KindleUnpack (MOBI/AZW3) 和 calibre ebook-convert。"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class ConverterError(Exception):
    pass


# ---------------------------------------------------------------------------
# KindleUnpack (MOBI/AZW3 -> EPUB, 纯 Python, 零外部依赖)
# ---------------------------------------------------------------------------
def _kindleunpack_to_epub(input_path: str, output_path: str) -> str:
    """使用 KindleUnpack 将 MOBI/AZW3 转为 EPUB。"""
    from .vendor.kindleunpack.kindleunpack import unpackBook

    tmp_dir = tempfile.mkdtemp(prefix="ku_")
    try:
        unpackBook(input_path, tmp_dir, epubver='2', use_hd=False)
        # KindleUnpack 输出到 tmp_dir/<书名>/ 目录下
        subdirs = [d for d in os.listdir(tmp_dir)
                   if os.path.isdir(os.path.join(tmp_dir, d))]
        # 找到生成的 EPUB 文件
        epub_file = None
        for root, _dirs, files in os.walk(tmp_dir):
            for f in files:
                if f.endswith('.epub'):
                    epub_file = os.path.join(root, f)
                    break
            if epub_file:
                break
        if epub_file is None:
            # KindleUnpack 可能输出的是展开的 EPUB 目录结构
            # 找 content.opf 所在目录，手动打包
            epub_file = _repack_epub_from_dir(tmp_dir)
        if epub_file is None:
            raise ConverterError("KindleUnpack 未能生成 EPUB 文件")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        shutil.copy2(epub_file, output_path)
        return output_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _repack_epub_from_dir(base_dir: str) -> str | None:
    """将 KindleUnpack 展开的目录重新打包为合规 EPUB。"""
    import zipfile

    opf_path = None
    for root, dirs, files in os.walk(base_dir):
        if 'content.opf' in files:
            opf_path = os.path.join(root, 'content.opf')
            break
    if opf_path is None:
        return None

    epub_root = None
    current = os.path.dirname(opf_path)
    while True:
        if (os.path.isdir(os.path.join(current, "META-INF"))
                or os.path.isfile(os.path.join(current, "mimetype"))):
            epub_root = current
            break
        if os.path.abspath(current) == os.path.abspath(base_dir):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    if epub_root is None:
        subdirs = [
            os.path.join(base_dir, d)
            for d in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, d))
        ]
        if len(subdirs) == 1 and os.path.commonpath([subdirs[0], opf_path]) == subdirs[0]:
            epub_root = subdirs[0]
        else:
            epub_root = base_dir

    opf_full_path = os.path.relpath(opf_path, epub_root).replace(os.sep, "/")
    epub_path = os.path.join(base_dir, "output.epub")

    # 生成 META-INF/container.xml
    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        f'<rootfiles><rootfile full-path="{opf_full_path}" '
        'media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>'
    )

    with zipfile.ZipFile(epub_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # mimetype: 必须第一个文件，不压缩
        zf.writestr("mimetype", "application/epub+zip",
                     compress_type=zipfile.ZIP_STORED)
        # META-INF/container.xml
        zf.writestr("META-INF/container.xml", container_xml)
        # 书的所有文件
        for root, dirs, files in os.walk(epub_root):
            for f in files:
                full = os.path.join(root, f)
                arcname = os.path.relpath(full, epub_root).replace(os.sep, "/")
                if arcname in {"mimetype", "META-INF/container.xml"}:
                    continue
                zf.write(full, arcname)
    return epub_path


# ---------------------------------------------------------------------------
# calibre ebook-convert (通用后备)
# ---------------------------------------------------------------------------
_INSTALL_HINT = """\
安装 calibre:
  macOS:      brew install --cask calibre
  Ubuntu:     sudo wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sudo sh /dev/stdin
  或在 config.json 中设置 ebook_convert_path 指向 ebook-convert 的路径"""


def find_ebook_convert(custom_path: str = "") -> str:
    if custom_path and os.path.isfile(custom_path):
        return custom_path
    found = shutil.which("ebook-convert")
    if found:
        return found
    candidates = [
        "/usr/bin/ebook-convert",
        "/usr/local/bin/ebook-convert",
        os.path.expanduser("~/.local/bin/ebook-convert"),
        "/Applications/calibre.app/Contents/MacOS/ebook-convert",
        r"C:\Program Files\Calibre2\ebook-convert.exe",
        r"C:\Program Files (x86)\Calibre2\ebook-convert.exe",
    ]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if root:
            candidates.append(os.path.join(root, "Calibre2", "ebook-convert.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise ConverterError(f"未找到 ebook-convert。\n\n{_INSTALL_HINT}")


def _ebook_convert(input_path: str, output_path: str, output_format: str,
                   ebook_convert_path: str = "") -> str:
    binary = find_ebook_convert(ebook_convert_path)
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    cmd = [binary, input_path, output_path]
    if output_format in ("epub", "mobi", "azw3"):
        cmd.extend(["--enable-heuristics"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise ConverterError(f"ebook-convert 超时 (300秒): {input_path}")
    if result.returncode != 0:
        raise ConverterError(
            f"ebook-convert 失败 (返回码 {result.returncode}):\n"
            f"{result.stderr[-2000:]}"
        )
    if not os.path.isfile(output_path):
        raise ConverterError(f"ebook-convert 未生成输出文件: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def convert(input_path: str, output_path: str, output_format: str,
            ebook_convert_path: str = "") -> str:
    """统一格式转换入口，自动选择最佳后端。"""
    input_ext = Path(input_path).suffix.lstrip(".").lower()

    # MOBI/AZW3 -> EPUB: 优先用 KindleUnpack (纯 Python, 无需 calibre)
    kindleunpack_error = None
    if input_ext in ("mobi", "azw3") and output_format == "epub":
        try:
            return _kindleunpack_to_epub(input_path, output_path)
        except Exception as e:
            kindleunpack_error = e

    # 其他情况: 用 ebook-convert
    try:
        return _ebook_convert(input_path, output_path, output_format, ebook_convert_path)
    except ConverterError as e:
        if kindleunpack_error is not None:
            raise ConverterError(
                "KindleUnpack 转换失败，ebook-convert 回退也失败。\n\n"
                f"KindleUnpack 错误: {kindleunpack_error}\n\n"
                f"ebook-convert 错误: {e}"
            ) from e
        raise


def convert_to_epub(input_path: str, ebook_convert_path: str = "") -> str:
    tmp_dir = os.path.join(os.path.dirname(input_path) or ".", ".et_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    stem = Path(input_path).stem
    tmp_epub = os.path.join(tmp_dir, f"{stem}.epub")
    return convert(input_path, tmp_epub, "epub", ebook_convert_path)
