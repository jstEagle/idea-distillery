import os
from pathlib import Path

from idea_distillery.cli import (
    build_codex_handoff_prompt,
    count_words,
    create_idea_file_path,
    default_to_record_args,
    format_duration,
    load_env_file,
    normalize_markdown,
    resolve_output_path,
    run_summary,
)


def test_normalize_markdown_adds_title_when_missing() -> None:
    assert normalize_markdown("## Project Essence\nUseful context.") == (
        "# Vision\n\n## Project Essence\nUseful context.\n"
    )


def test_normalize_markdown_preserves_existing_title() -> None:
    assert normalize_markdown("# Vision\n\n## Project Essence\nUseful context.") == (
        "# Vision\n\n## Project Essence\nUseful context.\n"
    )


def test_resolve_output_path_uses_project_dir_for_relative_paths(tmp_path: Path) -> None:
    assert resolve_output_path(tmp_path, Path("docs/vision.md")) == tmp_path / "docs/vision.md"


def test_default_to_record_args_for_bare_command() -> None:
    assert default_to_record_args([]) == ["record"]


def test_default_to_record_args_for_bare_record_options() -> None:
    assert default_to_record_args(["--no-ai", "--audio-device", "2"]) == [
        "record",
        "--no-ai",
        "--audio-device",
        "2",
    ]


def test_default_to_record_args_keeps_global_help() -> None:
    assert default_to_record_args(["--help"]) == ["--help"]


def test_default_to_record_args_keeps_explicit_commands() -> None:
    assert default_to_record_args(["devices"]) == ["devices"]


def test_format_duration() -> None:
    assert format_duration(65) == "01:05"
    assert format_duration(3661) == "1:01:01"


def test_count_words_ignores_empty_whitespace() -> None:
    assert count_words("  build the thing\n\nwith taste ") == 5


def test_create_idea_file_path_uses_hidden_ideas_dir(tmp_path: Path) -> None:
    path = create_idea_file_path(tmp_path)

    assert path.parent == tmp_path / "ideas"
    assert path.suffix == ".md"
    assert len(path.stem) == 12


def test_build_codex_handoff_prompt_points_at_project_and_plan(tmp_path: Path) -> None:
    plan = tmp_path / ".idea" / "distillery" / "ideas" / "abc123.md"
    prompt = build_codex_handoff_prompt(tmp_path, plan)

    assert "Hey, look at this plan!" in prompt
    assert f"Project directory: {tmp_path.resolve()}" in prompt
    assert f"Plan file: {plan.resolve()}" in prompt


def test_load_env_file_does_not_overwrite_existing_value(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=from-file\nOTHER_KEY='quoted'\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "existing")

    load_env_file(env)

    assert os.environ["OPENAI_API_KEY"] == "existing"
    assert os.environ["OTHER_KEY"] == "quoted"


def test_run_summary_writes_idea_file_and_copies_prompt(tmp_path: Path, monkeypatch) -> None:
    copied_prompts: list[str] = []

    class Responses:
        def create(self, **kwargs):
            return type("Response", (), {"output_text": "# Vision\n\n## Project Essence\nTest plan."})()

    class Client:
        responses = Responses()

    def fake_copy(prompt: str) -> bool:
        copied_prompts.append(prompt)
        return True

    monkeypatch.setattr("idea_distillery.cli.copy_to_clipboard", fake_copy)

    run_summary(
        client=Client(),
        transcript="# Transcript\n\nWe should build it.",
        project_dir=tmp_path,
        data_dir=tmp_path / ".idea" / "distillery",
        output_path=None,
        summary_model="gpt-5.5",
    )

    idea_files = list((tmp_path / ".idea" / "distillery" / "ideas").glob("*.md"))
    assert len(idea_files) == 1
    assert idea_files[0].read_text(encoding="utf-8").startswith("# Vision")
    assert not (tmp_path / "docs" / "vision.md").exists()
    assert str(idea_files[0].resolve()) in copied_prompts[0]
