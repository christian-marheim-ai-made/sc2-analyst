# sc2-analyst

A StarCraft II replay goes in; a battle report comes out. The replay is read once, front to back. Each
aspect of the game (fights, build, economy, bases, camera) is kept in a repository of its own. The
repositories are combined into one view of the game and cut into numbered facts. Only then does a
local model write about it, and every claim it makes is checked against the facts it cites.
Everything runs on your own machine.

## Needs

- Windows. The model runner (llm-bootstrap, in `vendor/`) uses Windows job objects.
- Python 3.12.
- An NVIDIA card with 24 GB. The thinking model needs about 20 GB free; one model is loaded at a time.
- StarCraft II, only for maps beyond the 38 included (they are imported from its cache).

## Install

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Add llama-server and the models

Neither is in this repository. Put them in two folders here.

**`llama/`.** Download llama.cpp release b10724 for Windows with CUDA 12.4: both files from
<https://github.com/ggml-org/llama.cpp/releases/tag/b10724>.

| file | size | sha256 |
|---|---|---|
| `llama-b10724-bin-win-cuda-12.4-x64.zip` | 251.6 MB | `cf93a5f3e05a6eb474fc591224bab23c55a2de64ac0c3fd7efb9d364385cf4f0` |
| `cudart-llama-bin-win-cuda-12.4-x64.zip` | 391.4 MB | `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6` |

Unzip both into `llama/`, so that `llama/llama-server.exe` exists.

**`models/`.** Put these two GGUF files here. Search Hugging Face for the exact file names.

| file | size | role |
|---|---|---|
| `Qwen3.6-27B-Q4_K_M.gguf` | 16.8 GB | thinks: the analysis, the plot |
| `TheDrummer_Cydonia-24B-v4.1-Q5_K_M.gguf` | 16.8 GB | only rewords the plot into the battle report |

To keep them somewhere else, copy `config/runner.toml` to `config/runner.local.toml` and set
`llama_server` and `models_dir` there.

## Try a demo

`demo/replays/` holds 22 real games, with every name, profile id, clan tag and chat message removed.
You are `Player_1`, your allies `Ally_2` and `Ally_3`, the enemies `Enemy_1`, `Enemy_2`, `Enemy_3`.

```
.venv\Scripts\python tools\analyse_replay.py demo\replays\Sandstorm_CE_2026-10-17_1008.SC2Replay
```

The page and the battle report are written to `data-home/assessments/<map>_<start>/`: `page.html`,
`battle_report.md`, and every pass's answer. A 3v3 takes about 8 minutes on the card.

**Without llama-server or the models**, add `--mock`. It runs every step with llm-bootstrap's
stand-in, which writes made-up words. They fail the checks, so you get the page but no battle report
text: that is the checks working.

## Your own replays

```
.venv\Scripts\python tools\analyse_replay.py "<path>\<game>.SC2Replay" --player <your handle>
```

The intake writes an anonymised copy first: every other player's name is replaced. Your original
is read once and never changed. The data goes to `data-home/`. To use another place, copy
`config/analyst.toml` to `config/analyst.local.toml` and set `data_home`, or set `SC2A_DATA_HOME`.

## The workbench

```
.venv\Scripts\python tools\workbench.py --model mock
```

A local page (<http://127.0.0.1:8798>) to look into a game second by second, see where each number
comes from, and run single questions on the model.

## Tests

```
.venv\Scripts\python tests\run.py
```

Tests that need data you do not have (your own library, reference files) skip instead of failing.

---

Copyright © 2026 the repository owner. All rights reserved. No licence is granted.
