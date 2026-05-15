# Idea Distillery

Idea Distillery is a command-line recorder for project calls. Run it from any project folder, talk through an idea, stop it with Enter, and it writes a hidden distilled Markdown plan.

## What it captures

The recorder captures the selected computer audio input device. On macOS, that usually means:

- Built-in microphone for your own voice.
- A loopback or aggregate device, such as BlackHole or Loopback, if you also want the other side of a call.

Normal speaker output is not exposed as a microphone input by macOS, so call audio needs to be routed into an input device before Idea Distillery can record it.

## Install

Requirements:

- Python 3.9+
- `OPENAI_API_KEY`
- Optional: `ffmpeg`, if you want to use the ffmpeg recording backend directly

One-command local setup after cloning:

```bash
./scripts/install.sh
```

The installer creates `.venv`, installs the CLI in editable mode, links `idea-distillery` and `ida-distillery` into `~/.local/bin`, creates `~/.idea/distillery`, optionally saves `OPENAI_API_KEY` to `~/.idea/distillery/.env`, and runs the test suite.

Manual install:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install .
```

From this repo during development:

```bash
python3 -m pip install -e .
```

## Use

List available macOS audio input devices:

```bash
idea-distillery devices
```

Record from the default first input device. Press Enter to stop:

```bash
idea-distillery
```

This is equivalent to:

```bash
idea-distillery record
```

The shorter alias is also installed as:

```bash
ida-distillery
```

Record from a specific device:

```bash
idea-distillery record --audio-device 2
```

Use ffmpeg explicitly:

```bash
idea-distillery record --backend ffmpeg --audio-device 2
```

Process an existing audio file:

```bash
idea-distillery process ./call.m4a
```

Record only, without OpenAI processing:

```bash
idea-distillery --no-ai
```

By default, recording is split into 60-second chunks. Each completed chunk is transcribed in the background into hidden scratch storage. When you press Enter, the final partial chunk is transcribed, appended, and the complete transcript is summarized into the full project plan.

The final plan is written as one Markdown file under:

```bash
~/.idea/distillery/ideas/<random-id>.md
```

After the idea file is written, Idea Distillery copies a Codex handoff prompt to your clipboard with the directory where you launched the command and the hidden plan file path already included.

If you explicitly want an extra copy in a project, pass `--output`:

```bash
idea-distillery --output docs/vision.md
```

## Output

During a run, Idea Distillery uses temporary hidden scratch storage under:

```bash
~/.idea/distillery/scratch/
```

After a successful AI-enabled run, the scratch folder for that run is removed. The only durable default artifact is the randomized idea file under `~/.idea/distillery/ideas/`.

The Markdown document is optimized for handing to an LLM or coding agent. It preserves project ethos, decisions, constraints, disagreements, non-goals, vocabulary, and open questions from the call.
