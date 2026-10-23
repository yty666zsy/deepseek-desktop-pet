# DeepSeek Desktop Pet 🐋

A Windows desktop AI companion built with **PySide6 (Qt)** and the **DeepSeek API**. It chats, remembers, reminds you to take breaks, and can run small tools for you.

**[中文 README（完整新手教程）](README.md)**

![DeepSeek Desktop Pet](screenshots/readme/01-hero.png)

> Screenshots are rendered from the real app by [`tools/verify_readme.py`](tools/verify_readme.py).
> Independent third-party project, not affiliated with DeepSeek. Character art is AI-generated.

## Quick start

**Portable build (recommended, no Python needed)**

1. Download `DeepSeekPet_v2.x_portable.zip` from [Releases](https://github.com/yty666zsy/deepseek-desktop-pet/releases/latest).
2. Extract the **whole folder** (do not run the exe from inside the zip).
3. Double-click `DeepSeekPet.exe`.
4. Right-click the pet → **Settings → Model → Change API Key**, paste your key.

**From source (Python 3.10+)**

```powershell
python -m pip install -r requirements.txt
python desktop_pet.py
```

## API Key

- Create a key at [platform.deepseek.com](https://platform.deepseek.com).
- The key is stored only in the local `config.json`; usage is billed by DeepSeek.
- No key? The app still launches, but AI replies are disabled.
- To use another endpoint or model: **Settings → Model → Model Manager**.

## Features

- Dual characters, affection/level progression, long-term memory
- Proactive care, reminders, 15 mini-games
- Tool calling: weather, reminders, apps, files, clipboard
- Optional: Live2D, TTS, music, photo wall, ledger, file conversion, video download

## Docs

The [Chinese README](README.md) contains the full beginner guide, settings reference, FAQ, privacy notes, and developer instructions.

[MIT](LICENSE)
