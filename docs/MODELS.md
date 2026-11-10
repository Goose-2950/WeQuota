# 语音转写模型清单

WeQuota 的语音转写跑在本地 **faster-whisper**（CTranslate2 量化）上，模型文件不进仓库，
只在这里和 `wequota/transcribe.py` 的 `MODEL_MANIFEST` 里登记**模型名 + 锁定版本 + 文件指纹**。

## 为什么不入库

- 单个 tiny 模型 75MB，进仓库会让 clone 又慢又重，Git 也管不好二进制大文件；
- 模型可以从 HuggingFace（国内走 `hf-mirror.com` 镜像）**按 revision 原样重新下载**，
  只要锁定提交号，新设备拿到的就是字节完全一致的一份。

## 当前登记的模型

| 规格 | 仓库（repo_id） | 锁定版本（revision） | 体积 |
|------|-----------------|----------------------|------|
| tiny（默认） | `Systran/faster-whisper-tiny` | `d90ca5fe260221311c53c58e660288d3deb8d356` | 约 75MB |

关键文件指纹（sha256 / 字节）：

| 文件 | 大小 | sha256 |
|------|------|--------|
| `config.json` | 2249 | `a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8` |
| `model.bin` | 75538270 | `dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1` |
| `tokenizer.json` | 2203239 | `fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab` |
| `vocabulary.txt` | 459861 | `34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913` |

同一份信息以代码形式存在 `wequota/transcribe.py::MODEL_MANIFEST`，下载时用它锁定 revision、
下载完逐文件校验 sha256，两边不一致会直接报错而不是悄悄用错版本。

### 本机预置（2026-11-02）

本机已把 tiny 四个文件直接下载到 `assets/models/faster-whisper-tiny/`，并通过上面的
sha256 校验；`config.json` 已设 `whisper.auto_download=false`，启动时不会再次联网下载。
模型目录仍被 `.gitignore` 忽略（大文件不入库），打包时由 `installer.build_release`
按 `assets/models/` 一并带入产物。

## 换设备 / 首次部署时怎么拿模型

```bash
wequota --download-model          # 按上表锁定版本下载并校验，下载完退出
```

- 默认落到 `assets/models/faster-whisper-tiny/`（可用 `config.json → whisper.model_dir` 改）；
- 走 `hf-mirror.com` 镜像（代码里设置了 `HF_ENDPOINT` 与 `HF_HUB_DISABLE_XET`）；
- 下载后自动校验四个文件的 sha256，不符会打印「下载内容与锁定版本不一致」；
- 平时不手动跑也行：模型缺失时首次启动会后台拉取，期间语音按时长折算，不阻塞启动。

## 想换模型规格 / 换版本

1. 改 `config.json` 的 `whisper.model_size`（如 `base`）。**注意**：清单里只登记了 tiny，
   其他规格不会锁 revision、也不校验指纹（会拉仓库最新提交，可能随时间变化）。
2. 想锁别的版本：填 `whisper.model_revision`（HF 提交号），留空即用清单里的默认版本。
3. 发布包内置：`python -m installer.build_release` 会把 `assets/models/` 打进产物，
   这样目标机器不用联网下载（spec 里 `models_dir.exists()` 才内置）。

## 补充约定

- 微信语音单条最长 60 秒，因此**不设「超长语音跳过转写」的门槛**，有模型就一律转写；
  转写失败才降级为「时长 × 25」。
- 转写结果按「路径 + 大小 + 修改时间」缓存进 SQLite，同一条语音只跑一次模型。
