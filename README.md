# AutoClipper Zero

**Website:** https://sifat-jaman-13.github.io/autoclipper-zero/ — AI Twitch clip maker and highlight generator.

### A local AI highlight studio for Twitch

AutoClipper Zero turns Twitch live streams and past broadcasts into captioned, upload-ready vertical clips. It runs locally on Windows, keeps its working data on your machine, and never publishes anything automatically.

![Platform](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

- Watches a Twitch channel, processes a single VOD, or records a live stream.
- Splits long broadcasts into manageable chunks and resumes after interruptions.
- Transcribes speech with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), using CUDA when available and CPU fallback otherwise.
- Scores candidate moments with local Ollama, with a built-in heuristic fallback when Ollama is unavailable.
- Renders full-screen 9:16 MP4 clips at 1080×1920 with animated karaoke-style captions.
- Writes a matching text file containing a suggested title, description, hashtags, transcript, and source timestamp.

## Processing pipeline

```text
Twitch stream / VOD
        ↓
Streamlink + FFmpeg capture
        ↓
Audio extraction + faster-whisper transcription
        ↓
Ollama scoring or local heuristic ranking
        ↓
FFmpeg vertical crop + animated captions
        ↓
MP4 clip + upload notes (.txt)
```

## Quick start on Windows

Open PowerShell in the project folder and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Setup.ps1
```

Then start the app from a new PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Run-AutoclipperZero.ps1
```

The setup script creates `.venv`, installs the Python dependencies, checks FFmpeg, and optionally installs NVIDIA CUDA libraries for GPU transcription. If FFmpeg was installed by setup, open a new PowerShell window before launching the app.

## Requirements

- Windows 10 or later
- Python 3.10 or newer
- FFmpeg with `libass` support (the Gyan Essentials build works well)
- An internet connection for Twitch downloads
- Optional: an NVIDIA GPU and CUDA runtime for faster transcription
- Optional: [Ollama](https://ollama.com/) running locally with the configured model

The default configuration uses `qwen3.5:4b` at `http://127.0.0.1:11434`. If Ollama is not running, AutoClipper Zero continues with its built-in scorer.

## Modes

| Mode | Use it for |
| --- | --- |
| Past broadcasts | Scan a channel's latest VODs, processing them in chunks and remembering progress. |
| Single broadcast | Process one VOD link such as `https://www.twitch.tv/videos/123456789`. |
| Live stream | Record and process each finished segment while a channel is live. |

## Output folders

Generated files are intentionally ignored by Git:

- `AutoclipperZeroData\ready_to_upload\` — finished MP4 clips and matching TXT notes
- `AutoclipperZeroData\recordings\` — temporary live segments
- `AutoclipperZeroData\state.json` — resume state and clip history

Delete an entry from `state.json` if you want to reprocess a broadcast. Review every generated clip before publishing.

## Configuration

The app writes its settings to `config.json`. Common settings include:

| Setting | Purpose | Default |
| --- | --- | --- |
| `channel_url` | Twitch channel or VOD URL | `https://www.twitch.tv/zerkaa` |
| `source_mode` | `vods`, `single_vod`, or `live` | `vods` |
| `segment_minutes` | Length of each processing chunk | `5` |
| `clip_seconds` | Target clip duration | `42` |
| `clips_per_segment` | Candidates selected from each chunk | `2` |
| `max_clips_per_vod` | Per-VOD cap; `0` means unlimited | `12` |
| `whisper_model` | `small`, `medium`, `large-v3-turbo`, or `large-v3` | `medium` |
| `whisper_device` | `auto`, `cpu`, or `cuda` | `cuda` |

For a first run on CPU, choose `small` and `cpu`. For a CUDA-enabled machine, `medium` or `large-v3-turbo` generally gives stronger transcripts.

## Troubleshooting

- **FFmpeg not found:** rerun `Setup.ps1`, then open a new PowerShell window.
- **CUDA errors:** switch `whisper_device` to `cpu`; the app will use an INT8 CPU model.
- **Ollama unavailable:** this is safe; the built-in scorer is used automatically.
- **A VOD is skipped:** subscriber-only, deleted, or otherwise inaccessible broadcasts cannot be downloaded.
- **No good highlights:** the detector uses speech, audio energy, and language-model scoring. It does not understand silent visual events or Twitch chat.

## Responsible use

Only record, edit, and redistribute content you have permission to use. Respect Twitch's terms, the broadcaster's rules, music and game-content rights, and any applicable platform policies. AutoClipper Zero creates drafts for review; it does not decide what you are legally allowed to publish.

## Credits

Created by **Sifat Jaman** with development and documentation assistance from **ChatGPT**.

## License

AutoClipper Zero is released under the [MIT License](LICENSE).

## Project structure

```text
autoclipperzero.py       Main Tkinter application and processing engine
config.json              Persisted user configuration
Setup.ps1                Windows dependency and environment setup
Run-AutoclipperZero.ps1  Launcher for the virtual environment
requirements.txt         Python dependencies
```
