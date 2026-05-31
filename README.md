# ebook-translator

无头命令行批量电子书翻译工具。专为 VPS / 服务器设计，资源占用极低。

## 安装

```bash
cd ebook-translator-cli
pip install -e .
```

依赖仅 `httpx` + `lxml` + `tqdm`，纯 Python，无需编译。

## 什么时候需要 calibre？

工具内置了 [KindleUnpack](https://github.com/kevinhendricks/KindleUnpack)（纯 Python，零外部依赖），MOBI 和 AZW3 输入**不需要 calibre**。只有以下情况才需要：

| 操作 | 需要 calibre |
|------|:---:|
| MOBI / AZW3 输入 -> EPUB | **不需要** (内置 KindleUnpack) |
| PDF / DOCX / RTF 输入 | 需要 |
| 输出为 MOBI / AZW3 | 需要 |
| 输入输出都是 EPUB | **不需要** |

### 安装 calibre（仅在需要时）

**macOS:**
```bash
brew install --cask calibre
```

**Ubuntu / Debian / VPS:**
```bash
sudo wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sudo sh /dev/stdin
```

工具会自动查找 `ebook-convert`，无需额外配置。如路径不在默认位置，在 `config.json` 中设置：
```json
{ "ebook_convert_path": "/path/to/ebook-convert" }
```

## 快速开始

```bash
# 1. 配置
cp config.example.json config.json
# 填入 API 密钥

# 2. 翻译目录中的所有书籍
ebook-translator /path/to/books /path/to/output -c config.json

# 3. MOBI 输入也直接支持（无需 calibre）
ebook-translator /path/to/book.mobi /path/to/output -c config.json

# 4. 预览
ebook-translator /path/to/books /path/to/output --dry-run
```

## 用法

```
ebook-translator 输入 输出 [选项]

位置参数:
  输入                    输入目录或单个电子书文件
  输出                    输出目录

选项:
  --output-format, -o     输出格式 (epub, mobi, azw3)，默认: epub
  --config, -c            配置文件路径
  --engine, -e            翻译引擎 (openai, claude, deepseek)
  --source-lang, -s       源语言
  --target-lang, -t       目标语言
  --concurrency           并发数
  --force, -f             覆盖已存在的输出
  --no-cache              禁用缓存（不支持断点续翻）
  --log-file              日志文件
  --dry-run               预览模式
  --version, -V           版本号
```

## 配置

```json
{
    "engine": "openai",
    "source_lang": "English",
    "target_lang": "Chinese",
    "engines": {
        "openai": {
            "api_key": "sk-你的密钥",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "concurrency": 3,
            "request_interval": 1.0
        }
    }
}
```

完整配置见 `config.example.json`。支持 `openai`（兼容所有 OpenAI 格式）、`claude`、`deepseek`。

## 支持格式

| 输入 | 后端 | 说明 |
|------|------|------|
| epub | 内置 | 直接处理 |
| mobi, azw3 | 内置 KindleUnpack | 纯 Python，无需 calibre |
| pdf, docx, rtf, fb2, txt, html | calibre | 需安装 |

| 输出 | 后端 | 说明 |
|------|------|------|
| epub | 内置 | 默认格式 |
| mobi, azw3 | calibre | 需安装 |

## 断点续翻

进度缓存在 `~/.cache/ebook-translator/books/`。中断后重跑同一命令自动续翻。

## 术语表

```
source term
target term

another source
another target
```

`config.json` 中设置：`"glossary_path": "/path/to/glossary.txt"`

## VPS 部署

```bash
# 安装
pip install httpx lxml tqdm
# 如需非 EPUB 输入: wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sudo sh /dev/stdin

# 配置
cp config.example.json config.json

# 运行
nohup ebook-translator /books /output -o epub -c config.json > translate.log 2>&1 &

# 断点续翻：中断后直接重跑同一命令
```
