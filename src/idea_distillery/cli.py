from __future__ import annotations

import argparse
import os
import platform
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Union

import sounddevice as sd
from openai import OpenAI


DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-transcribe"
DEFAULT_SUMMARY_MODEL = "gpt-5.5"
DEFAULT_SEGMENT_SECONDS = 60
MAX_TRANSCRIBE_BYTES = 25 * 1024 * 1024
DEFAULT_DATA_DIR = Path.home() / ".idea" / "distillery"
GREEN = "\033[32m"
DIM = "\033[2m"
RESET = "\033[0m"


@dataclass(frozen=True)
class Session:
    root: Path
    started_at: str

    @property
    def chunks_pattern(self) -> Path:
        return self.root / "audio_%03d.m4a"

    @property
    def transcript_path(self) -> Path:
        return self.root / "transcript.md"

    @property
    def vision_path(self) -> Path:
        return self.root / "vision.md"


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    argv = default_to_record_args(argv)
    args = parser.parse_args(argv)
    try:
        if args.command == "devices":
            return list_devices()
        if args.command == "record":
            return record(args)
        if args.command == "process":
            return process_existing(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except UserFacingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idea-distillery",
        description="Record project idea calls and distill them into Markdown vision docs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="List recordable audio input devices.")
    devices.set_defaults(command="devices")

    record_parser = subparsers.add_parser("record", help="Record a call until Enter, then distill it.")
    add_common_processing_args(record_parser)
    record_parser.add_argument(
        "--audio-device",
        default=None,
        help="Audio input device index/name. Run `idea-distillery devices` first. Default: system default input",
    )
    record_parser.add_argument(
        "--backend",
        choices=["sounddevice", "ffmpeg"],
        default="sounddevice",
        help="Recording backend. Default: sounddevice",
    )
    record_parser.add_argument(
        "--segment-seconds",
        type=int,
        default=DEFAULT_SEGMENT_SECONDS,
        help=f"Audio chunk duration before transcription. Default: {DEFAULT_SEGMENT_SECONDS}",
    )
    record_parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Record audio only and skip transcription/summarization.",
    )
    record_parser.set_defaults(command="record")

    process_parser = subparsers.add_parser("process", help="Distill an existing audio file.")
    process_parser.add_argument("audio", type=Path, help="Audio file to transcribe and distill.")
    add_common_processing_args(process_parser)
    process_parser.set_defaults(command="process")

    return parser


def default_to_record_args(argv: Optional[list[str]]) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return ["record"]
    if args[0] in {"-h", "--help"}:
        return args
    commands = {"devices", "record", "process"}
    if args[0] not in commands and args[0].startswith("-"):
        return ["record", *args]
    return args


def add_common_processing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Target project directory included in the copied Codex prompt. Default: current directory",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Hidden Idea Distillery storage directory. Default: {DEFAULT_DATA_DIR}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional extra Markdown output path, relative to project-dir unless absolute. Default: none",
    )
    parser.add_argument(
        "--transcribe-model",
        default=DEFAULT_TRANSCRIBE_MODEL,
        help=f"OpenAI transcription model. Default: {DEFAULT_TRANSCRIBE_MODEL}",
    )
    parser.add_argument(
        "--summary-model",
        default=DEFAULT_SUMMARY_MODEL,
        help=f"OpenAI summary model. Default: {DEFAULT_SUMMARY_MODEL}",
    )


def list_devices() -> int:
    print("Audio input devices:")
    try:
        devices = sd.query_devices()
    except sd.PortAudioError as exc:
        raise UserFacingError(f"could not list audio devices: {exc}") from exc
    for index, device in enumerate(devices):
        if int(device["max_input_channels"]) > 0:
            marker = "*" if index == sd.default.device[0] else " "
            print(f"{marker} {index}: {device['name']} ({int(device['max_input_channels'])} in)")
    return 0


def record(args: argparse.Namespace) -> int:
    load_env_files(args.project_dir)
    session = create_session(args.data_dir)
    print(f"Session: {session.root}")
    print(f"{GREEN}Status: active{RESET}. Recording now. Press Enter to stop.")

    client = None
    transcriber = None
    if not args.no_ai:
        require_openai_key()
        client = OpenAI()
        if args.backend == "sounddevice":
            transcriber = LiveTranscriber(client, args.transcribe_model, session.transcript_path)

    if args.backend == "ffmpeg":
        require_ffmpeg()
        proc = start_ffmpeg_recording(
            output_pattern=session.chunks_pattern,
            audio_device=args.audio_device,
            segment_seconds=args.segment_seconds,
        )
        spinner = Spinner()
        stop = EnterStopper()
        stop.start()
        try:
            while proc.poll() is None:
                if stop.stop_requested:
                    stop_process(proc)
                    break
                elapsed = format_duration(stop.elapsed_seconds)
                print(f"\r{GREEN}active{RESET} {spinner.next()} elapsed {elapsed} ", end="", flush=True)
                time.sleep(1)
        except KeyboardInterrupt:
            stop_process(proc)
        finally:
            print("\rStatus: stopped.        ")

        if proc.returncode not in (0, None, -signal.SIGTERM):
            raise UserFacingError(f"ffmpeg exited with status {proc.returncode}")
    else:
        record_with_sounddevice(
            session=session,
            audio_device=args.audio_device,
            segment_seconds=args.segment_seconds,
            transcriber=transcriber,
        )

    chunks = sorted(session.root.glob("audio_*.m4a")) + sorted(session.root.glob("audio_*.wav"))
    if not chunks:
        raise UserFacingError("no audio chunks were recorded")

    print(f"Recorded {len(chunks)} audio chunk(s).")
    if args.no_ai:
        print(f"Audio saved in {session.root}")
        return 0

    output = resolve_optional_output_path(args.project_dir, args.output)
    assert client is not None
    if transcriber is not None:
        transcript = transcriber.finish()
        idea_path = run_summary(
            client=client,
            transcript=transcript,
            project_dir=args.project_dir,
            data_dir=args.data_dir,
            output_path=output,
            summary_model=args.summary_model,
        )
    else:
        idea_path = run_distillation(
            audio_files=chunks,
            transcript_path=session.transcript_path,
            project_dir=args.project_dir,
            data_dir=args.data_dir,
            output_path=output,
            transcribe_model=args.transcribe_model,
            summary_model=args.summary_model,
            client=client,
        )
    cleanup_session(session)
    print(f"Kept idea plan {idea_path}")
    return 0


def process_existing(args: argparse.Namespace) -> int:
    load_env_files(args.project_dir)
    if not args.audio.exists():
        raise UserFacingError(f"audio file does not exist: {args.audio}")
    session = create_session(args.data_dir)
    copied_audio = session.root / args.audio.name
    shutil.copy2(args.audio, copied_audio)
    output = resolve_optional_output_path(args.project_dir, args.output)
    idea_path = run_distillation(
        audio_files=[copied_audio],
        transcript_path=session.transcript_path,
        project_dir=args.project_dir,
        data_dir=args.data_dir,
        output_path=output,
        transcribe_model=args.transcribe_model,
        summary_model=args.summary_model,
    )
    cleanup_session(session)
    print(f"Kept idea plan {idea_path}")
    return 0


def record_with_sounddevice(
    session: Session,
    audio_device: Optional[str],
    segment_seconds: int,
    transcriber: Optional["LiveTranscriber"],
) -> None:
    if segment_seconds < 10:
        raise UserFacingError("--segment-seconds must be at least 10")

    device = parse_sounddevice_device(audio_device)
    samplerate = 16000
    channels = 1
    blocksize = 1024
    frames_per_chunk = samplerate * segment_seconds
    chunk_index = 0
    frames_written = 0
    spinner = Spinner()
    stopper = EnterStopper()
    current = open_wave_chunk(session.root, chunk_index, samplerate, channels)
    current_path = wave_chunk_path(session.root, chunk_index)
    started_at = time.monotonic()
    stopper.start()

    try:
        with sd.RawInputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            device=device,
            channels=channels,
            dtype="int16",
        ) as stream:
            last_status = 0.0
            while True:
                if stopper.stop_requested:
                    break
                data, overflowed = stream.read(blocksize)
                current.writeframes(data)
                frames_written += blocksize
                now = time.monotonic()
                if now - last_status >= 1:
                    overflow = " overflow" if overflowed else ""
                    words = transcriber.word_count if transcriber else 0
                    chunks_done = transcriber.completed_chunks if transcriber else chunk_index
                    elapsed = format_duration(now - started_at)
                    print(
                        f"\r{GREEN}active{RESET} {spinner.next()} "
                        f"elapsed {elapsed} | chunks {chunks_done} | words in context {words}{overflow} ",
                        end="",
                        flush=True,
                    )
                    last_status = now
                if frames_written >= frames_per_chunk:
                    current.close()
                    if transcriber:
                        transcriber.submit(current_path)
                    chunk_index += 1
                    frames_written = 0
                    current = open_wave_chunk(session.root, chunk_index, samplerate, channels)
                    current_path = wave_chunk_path(session.root, chunk_index)
    except KeyboardInterrupt:
        pass
    except sd.PortAudioError as exc:
        raise UserFacingError(f"audio recording failed: {exc}") from exc
    finally:
        current.close()
        if current_path.exists() and current_path.stat().st_size > 44 and transcriber:
            transcriber.submit(current_path)
        print("\rStatus: stopped.        ")


def parse_sounddevice_device(audio_device: Optional[str]) -> Optional[Union[int, str]]:
    if audio_device is None:
        return None
    try:
        return int(audio_device)
    except ValueError:
        return audio_device


def wave_chunk_path(root: Path, index: int) -> Path:
    return root / f"audio_{index:03d}.wav"


def open_wave_chunk(root: Path, index: int, samplerate: int, channels: int) -> wave.Wave_write:
    path = wave_chunk_path(root, index)
    wav = wave.open(str(path), "wb")
    wav.setnchannels(channels)
    wav.setsampwidth(2)
    wav.setframerate(samplerate)
    return wav


def start_ffmpeg_recording(
    output_pattern: Path,
    audio_device: Optional[str],
    segment_seconds: int,
) -> subprocess.Popen[str]:
    if segment_seconds < 30:
        raise UserFacingError("--segment-seconds must be at least 30")

    if platform.system() == "Darwin":
        input_spec = f":{audio_device or 0}"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "avfoundation",
            "-i",
            input_spec,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            str(output_pattern),
        ]
    else:
        raise UserFacingError("recording is currently implemented for macOS ffmpeg/avfoundation only")

    return subprocess.Popen(cmd, text=True)


def stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)


class EnterStopper:
    def __init__(self) -> None:
        self.stop_requested = False
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._wait_for_enter, daemon=True)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def start(self) -> None:
        self._thread.start()

    def _wait_for_enter(self) -> None:
        try:
            sys.stdin.readline()
        except OSError:
            return
        self.stop_requested = True


class LiveTranscriber:
    def __init__(self, client: OpenAI, model: str, transcript_path: Path) -> None:
        self.client = client
        self.model = model
        self.transcript_path = transcript_path
        self.word_count = 0
        self.completed_chunks = 0
        self._queue: "queue.Queue[Optional[Path]]" = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._lock = threading.Lock()
        self._errors: list[Exception] = []
        self.transcript_path.write_text("# Transcript\n\n", encoding="utf-8")
        self._worker.start()

    def submit(self, audio_path: Path) -> None:
        self._queue.put(audio_path)

    def finish(self) -> str:
        print("Finalizing transcript...")
        self._queue.put(None)
        self._worker.join()
        if self._errors:
            raise UserFacingError(f"transcription failed: {self._errors[0]}")
        return self.transcript_path.read_text(encoding="utf-8")

    def _worker_loop(self) -> None:
        while True:
            audio_path = self._queue.get()
            if audio_path is None:
                return
            self._transcribe_and_append(audio_path)

    def _transcribe_and_append(self, audio_path: Path) -> None:
        try:
            text = transcribe_file(self.client, audio_path, self.model)
            words = count_words(text)
            with self._lock:
                chunk_number = self.completed_chunks + 1
                section = f"## Chunk {chunk_number}: {audio_path.name}\n\n{text.strip()}\n\n"
                with self.transcript_path.open("a", encoding="utf-8") as transcript:
                    transcript.write(section)
                self.word_count += words
                self.completed_chunks += 1
        except Exception as exc:
            with self._lock:
                self._errors.append(exc)


def run_distillation(
    *,
    audio_files: Iterable[Path],
    transcript_path: Path,
    project_dir: Path,
    data_dir: Path,
    output_path: Optional[Path],
    transcribe_model: str,
    summary_model: str,
    client: Optional[OpenAI] = None,
) -> Path:
    require_openai_key()
    client = client or OpenAI()

    print("Transcribing audio...")
    transcript = transcribe_files(client, audio_files, transcribe_model)
    transcript_path.write_text(transcript, encoding="utf-8")

    return run_summary(
        client=client,
        transcript=transcript,
        project_dir=project_dir,
        data_dir=data_dir,
        output_path=output_path,
        summary_model=summary_model,
    )


def run_summary(
    *,
    client: OpenAI,
    transcript: str,
    project_dir: Path,
    data_dir: Path,
    output_path: Optional[Path],
    summary_model: str,
) -> Path:
    project_dir = project_dir.resolve()
    print(f"{GREEN}Summarizing transcript into the full project plan...{RESET}")
    vision = build_vision_document(client, transcript, summary_model)
    idea_path = create_idea_file_path(data_dir)
    idea_path.write_text(vision, encoding="utf-8")
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(vision, encoding="utf-8")
    prompt = build_codex_handoff_prompt(project_dir, idea_path)
    copied = copy_to_clipboard(prompt)
    print(f"Wrote idea file {idea_path}")
    if output_path is not None:
        print(f"Wrote {output_path}")
    if copied:
        print("Copied Codex handoff prompt to clipboard.")
    else:
        print("Could not copy to clipboard. Handoff prompt:")
        print(prompt)
    return idea_path


def transcribe_files(client: OpenAI, audio_files: Iterable[Path], model: str) -> str:
    sections: list[str] = []
    for index, path in enumerate(audio_files, start=1):
        text = transcribe_file(client, path, model)
        sections.append(f"## Segment {index}: {path.name}\n\n{text.strip()}\n")
    return "\n".join(sections).strip() + "\n"


def transcribe_file(client: OpenAI, path: Path, model: str) -> str:
    size = path.stat().st_size
    if size > MAX_TRANSCRIBE_BYTES:
        raise UserFacingError(
            f"{path.name} is {size / 1024 / 1024:.1f} MB, above the 25 MB transcription limit. "
            "Record with a smaller --segment-seconds value."
        )
    with path.open("rb") as audio:
        result = client.audio.transcriptions.create(
            model=model,
            file=audio,
            response_format="text",
        )
    return str(result).strip()


def build_vision_document(client: OpenAI, transcript: str, model: str) -> str:
    response = client.responses.create(
        model=model,
        reasoning={"effort": "medium"},
        instructions=VISION_INSTRUCTIONS,
        input=f"Distill this project call transcript into the requested Markdown vision document:\n\n{transcript}",
    )
    text = getattr(response, "output_text", None)
    if not text:
        raise UserFacingError("OpenAI returned an empty summary")
    return normalize_markdown(text)


def normalize_markdown(markdown: str) -> str:
    markdown = markdown.strip()
    if not markdown.startswith("# "):
        markdown = "# Vision\n\n" + markdown
    return markdown + "\n"


def count_words(text: str) -> int:
    return len([word for word in text.split() if word.strip()])


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def create_session(data_dir: Path) -> Session:
    data_dir = data_dir.expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_root = data_dir / "scratch" / timestamp
    session_root.mkdir(parents=True, exist_ok=False)
    return Session(root=session_root, started_at=timestamp)


def cleanup_session(session: Session) -> None:
    shutil.rmtree(session.root, ignore_errors=True)


def create_idea_file_path(data_dir: Path) -> Path:
    ideas_dir = data_dir.expanduser().resolve() / "ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    while True:
        idea_id = secrets.token_hex(6)
        path = ideas_dir / f"{idea_id}.md"
        if not path.exists():
            return path


def build_codex_handoff_prompt(project_dir: Path, idea_path: Path) -> str:
    return (
        "Hey, look at this plan! Get this plan into this project from this directory.\n\n"
        f"Project directory: {project_dir.resolve()}\n"
        f"Plan file: {idea_path.resolve()}\n\n"
        "Read the plan, pull the relevant docs/context into the project first, then proceed from that context."
    )


def copy_to_clipboard(text: str) -> bool:
    if shutil.which("pbcopy") is None:
        return False
    proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, check=False)
    return proc.returncode == 0


def load_env_files(project_dir: Path) -> None:
    candidates = [
        DEFAULT_DATA_DIR / ".env",
        Path.home() / "Documents" / "Programming" / ".env",
        project_dir.resolve() / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            load_env_file(env_path)


def load_env_file(env_path: Path) -> None:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_output_path(project_dir: Path, output: Path) -> Path:
    if output.is_absolute():
        return output
    return project_dir.resolve() / output


def resolve_optional_output_path(project_dir: Path, output: Optional[Path]) -> Optional[Path]:
    if output is None:
        return None
    return resolve_output_path(project_dir, output)


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise UserFacingError("ffmpeg is required. Install it with `brew install ffmpeg`.")


def require_openai_key() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise UserFacingError("OPENAI_API_KEY is required for transcription and summarization")


class Spinner:
    def __init__(self) -> None:
        self._frames = iter_cycle("|/-\\")

    def next(self) -> str:
        return next(self._frames)


def iter_cycle(items: str):
    while True:
        for item in items:
            yield item


class UserFacingError(Exception):
    pass


VISION_INSTRUCTIONS = """You turn raw project-call transcripts into concise but high-context Markdown vision documents for future LLM coding agents.

Write only Markdown. Preserve the user's intent, voice, vocabulary, boundaries, taste, and reasons. Compress aggressively, but keep the ideas that would help an implementation agent avoid building the wrong thing.

Use this structure:

# Vision
## Project Essence
## Why This Matters
## Target User and Context
## Core Product Shape
## Workflows
## Design and Interaction Character
## Technical Direction
## Explicit Non-Goals
## Decisions Made
## Debates, Pushback, and Tensions
## Open Questions
## Implementation Prompts

The "Implementation Prompts" section should contain concrete prompt-ready bullets that a coding agent could use to build the project in phases.
"""


if __name__ == "__main__":
    raise SystemExit(main())
