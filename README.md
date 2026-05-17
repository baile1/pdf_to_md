# MinerU PDF to Markdown Converter

基于 [MinerU 精准解析 API](https://mineru.net/apiManage/docs) 的 PDF 转 Markdown 工具。

**核心特性：**

- **自动拆分大文件** — PDF 超过 200MB 或 200 页时，自动拆分为多个分块分别转换，最后合并成一个完整的 Markdown 文件
- **批量转换** — 支持传入整个目录，自动处理所有 PDF，跳过已转换的文件（按文件名去重）
- **增量续传** — 输出目录里已有的 `.md` 文件不会重新转换，中断后重新运行即可断点续传

---

## 安装

```bash
pip install requests PyPDF2
```

## 配置 Token

在项目根目录的 `config.json` 中填入你的 MinerU API Token：

```json
{
    "token": "eyJ0eXBlIjoi..."
}
```

Token 获取地址：[https://mineru.net/apiManage/docs](https://mineru.net/apiManage/docs)

也可以通过环境变量或命令行参数传入（优先级：`--token` > 环境变量 `MINERU_TOKEN` > `config.json`）。

---

## 使用方式

### 1. 批量转换整个目录（推荐）

```bash
python mineru_convert.py ./input -o ./output
```

- 扫描 `./input` 目录下所有 `.pdf` 文件
- 转换结果输出到 `./output` 目录，文件名与原 PDF 同名（`.md` 后缀）
- 如果 `./output` 中已存在同名 `.md` 文件，自动跳过不重复转换

**示例目录结构：**

```
input/
├── paper1.pdf          # 将被转换 -> output/paper1.md
├── paper2.pdf          # 将被转换 -> output/paper2.md
└── already_done.pdf    # 如果 output/already_done.md 已存在则跳过

output/
└── already_done.md     # 已存在，对应的 PDF 会被跳过
```

运行效果：

```
Found 3 PDF(s)
  Skipping 1 already converted: already_done.pdf
  Converting 2 file(s)

[1/2] paper1.pdf
  45.2MB, 120 pages — splitting...
  ...
  -> output/paper1.md

[2/2] paper2.pdf
  12.3MB, 30 pages — splitting...
  ...
  -> output/paper2.md

All done.
```

### 2. 转换单个文件

```bash
# 自动命名输出文件（与输入同名 .md）
python mineru_convert.py document.pdf

# 指定输出文件名
python mineru_convert.py document.pdf -o result.md
```

---

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `input` | PDF 文件路径或包含 PDF 的目录 | 必填 |
| `-o, --output` | 输出路径：单文件模式为文件路径，目录模式为目录路径 | 单文件：`{input}.md`，目录：与 input 相同 |
| `--token` | API Token（优先级最高） | 从 config.json 读取 |
| `--language` | 文档语言 | `ch`（中英双语） |
| `--model` | 模型版本：`pipeline` 或 `vlm` | `pipeline` |
| `--ocr` | 启用 OCR 识别 | 关闭 |
| `--no-formula` | 禁用公式识别 | 开启 |
| `--no-table` | 禁用表格识别 | 开启 |
| `--extra-formats` | 额外输出格式：`docx` `html` `latex` | 无 |
| `--poll-interval` | 轮询间隔（秒） | `5` |
| `--timeout` | 最大等待时间（秒） | `3600` |

### 语言选项

| 值 | 适用场景 |
|----|----------|
| `ch` | 中文 + 英文 + 繁体中文（默认） |
| `en` | 纯英文 |
| `japan` | 日文为主 |
| `korean` | 韩文 + 英文 |
| `latin` | 法语、德语、西班牙语等 30+ 语言 |
| `arabic` | 阿拉伯语、波斯语等 |
| `cyrillic` | 俄语、乌克兰语等 |
| `devanagari` | 印地语、梵语等 |

---

## 使用示例

```bash
# 英文论文批量转换
python mineru_convert.py ./papers -o ./papers_md --language en

# 扫描件 + OCR
python mineru_convert.py scanned.pdf --ocr --language ch

# 使用 VLM 模型，同时输出 docx
python mineru_convert.py document.pdf --model vlm --extra-formats docx

# 纯英文，10 秒轮询一次
python mineru_convert.py english_paper.pdf --language en --poll-interval 10
```

---

## API 限制说明

| 限制项 | 值 |
|--------|-----|
| 单文件最大体积 | 200 MB |
| 单文件最大页数 | 200 页 |
| 单次批量文件数 | 50 个 |
| 日配额 | 1000 页（高优先级），超出后优先级降低 |

**超过 200MB 或 200 页的 PDF 会自动拆分**，拆分会同时考虑体积和页数两个维度：

1. 根据平均每页体积估算每个分块的页数上限
2. 同时满足 ≤200MB 和 ≤200 页
3. 如果某个分块仍超出体积限制，递归减半重试

分块转换完成后，所有 Markdown 按顺序合并（分块之间用 `---` 分隔）。

---

## 依赖

- Python 3.8+
- [requests](https://pypi.org/project/requests/) — HTTP 请求
- [PyPDF2](https://pypi.org/project/PyPDF2/) — PDF 拆分
