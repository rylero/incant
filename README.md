# incant

A little app I am playing around with that combines voice dicatation and automation using pipleines.

## Run
```powershell
uv sync
uv run ui.py
```

## Config (env vars)

| Var                  | Default          | Meaning                                         |
|----------------------|------------------|-------------------------------------------------|
| `STT_HOTKEY`         | `ctrl+alt+space` | Toggle hotkey                                    |
| `STT_MODEL`          | `large-v3`       | Whisper model (`large-v3`, `medium`, `small`...)|
| `STT_LANG`           | auto             | Force a language code, e.g. `en` (faster/safer) |
| `STT_TRAILING_SPACE` | `1`              | Append a space after typed text (`0` to disable)|
| `STT_TYPE_DELAY`     | `0`              | Seconds between keystrokes (raise if apps drop chars) |

Example — lock to English (skips language detection, slightly faster):

```powershell
$env:STT_LANG="en"; uv run stt
```

## Accuracy notes

- `large-v3` is the most accurate Whisper model. On your RTX 5070 Ti a few
  seconds of speech transcribes in well under a second after warmup.
- Setting `STT_LANG=en` avoids occasional language mis-detection on short clips.
- A good mic and speaking in complete phrases (not single words) both help —
  Whisper uses sentence context.

## Notes / troubleshooting

- **Nothing types in some apps:** apps run as Administrator (or some games)
  ignore synthetic keystrokes from a non-admin process. Run the terminal as
  Administrator.
- **`cublas64_12.dll not found`:** the bundled CUDA DLLs aren't on the path.
  `stt.py` handles this automatically; if you import it elsewhere, import `stt`
  (or run via `uv run stt`) so the DLL setup runs first.
- **Falls back to CPU:** if CUDA init fails it auto-uses CPU `int8` (slower but
  works). Watch the `[load]` lines to see which device was chosen.
