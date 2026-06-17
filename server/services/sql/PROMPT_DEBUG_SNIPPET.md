# SQL Prompt Debug Snippet

Temporary helper for dumping the rendered SQL prompt messages from `server/services/sql/prompt_service.py`.

Debug target prompt files:

- `tobe_sql_prompt.json`
- `tobe_sql_tuning_prompt.json`
- `bind_sql_prompt.json`
- `bind_tuned_sql_prompt.json`
- `bind_sql_final_retry_prompt.json`
- `test_sql_prompt.json`
- `test_sql_final_retry_prompt.json`

## Patch Point

Temporarily wrap `build_prompt_messages()` with the following debug block after `messages` is created:

```python
debug_prompt_files = {
    "tobe_sql_prompt.json",
    "tobe_sql_tuning_prompt.json",
    "bind_sql_prompt.json",
    "bind_tuned_sql_prompt.json",
    "bind_sql_final_retry_prompt.json",
    "test_sql_prompt.json",
    "test_sql_final_retry_prompt.json",
}
if filename in debug_prompt_files:
    debug_dir = Path(__file__).resolve().parent / "debug_prompts"
    debug_dir.mkdir(exist_ok=True)

    stem = Path(filename).stem
    md_path = debug_dir / f"{stem}_debug.md"
    json_path = debug_dir / f"{stem}_debug_payload.json"

    md_path.write_text(
        "\n\n".join(
            [
                f"# {filename}",
                "## system",
                messages[0]["content"],
                "## user",
                messages[1]["content"],
            ]
        ),
        encoding="utf-8",
    )

    json_path.write_text(
        json.dumps(
            {"filename": filename, "kwargs": kwargs, "payload": payload},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
```

## Generated Files

```text
server/services/sql/debug_prompts/
  tobe_sql_prompt_debug.md
  tobe_sql_tuning_prompt_debug.md
  bind_sql_prompt_debug.md
  bind_tuned_sql_prompt_debug.md
  bind_sql_final_retry_prompt_debug.md
  test_sql_prompt_debug.md
  test_sql_final_retry_prompt_debug.md
  tobe_sql_prompt_debug_payload.json
  tobe_sql_tuning_prompt_debug_payload.json
  bind_sql_prompt_debug_payload.json
  bind_tuned_sql_prompt_debug_payload.json
  bind_sql_final_retry_prompt_debug_payload.json
  test_sql_prompt_debug_payload.json
  test_sql_final_retry_prompt_debug_payload.json
```

Remove the temporary debug block after inspection so prompt payloads are not left on disk.
