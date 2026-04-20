# llmbm — LLM Benchmark Suite

Benchmark local LLM models via [Ollama](https://ollama.com) and generate a shareable HTML report with per-machine leaderboards and cross-machine comparison.

## How it works

1. **`llmbm.py`** — runs benchmarks, auto-detects your hardware, prompts for model selection, and writes a results `.md` file
2. **`generate_benchmark_html.py`** — scans for `model-benchmark-results-*.md` files and renders `benchmark.html`

Each contributor submits one `.md` file. All files in the same folder are auto-detected and rendered as separate tabs with a comparison view.

---

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com) running locally (or reachable over LAN)
- [`llama-benchy`](https://github.com/Mozilla-Ocho/llama-benchy): `pip install llama-benchy`
- **macOS** — CPU power tracking requires passwordless `sudo powermetrics`:
  ```
  echo "$USER ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" \
    | sudo tee /etc/sudoers.d/powermetrics-bench
  sudo chmod 440 /etc/sudoers.d/powermetrics-bench
  ```
- **Linux** — GPU tracking requires NVIDIA drivers (`nvidia-smi`)

---

## Running benchmarks

```bash
python3 llmbm.py
```

The TUI will:
- Detect your CPU, GPU, and RAM
- Query your Ollama endpoint for installed models
- Let you select which models to benchmark and configure settings
- Write `model-benchmark-results-{machine-id}.md` when done

For scripted/CI use:

```bash
python3 llmbm.py --no-interactive \
  --machine-id rtx4090-linux \
  --machine-label "RTX 4090, Ryzen 9 7950X" \
  --base-url http://localhost:11434/v1 \
  modelname:latest \
  --write-summary
```

---

## Generating the HTML report

```bash
# from the directory containing your .md result files:
python3 generate_benchmark_html.py

# or point it at a folder:
python3 generate_benchmark_html.py --input-dir ./results --output benchmark.html
```

---

## Contributing your results

1. Fork this repo
2. Run `llmbm.py` on your machine
3. Copy your `model-benchmark-results-{machine-id}.md` into the repo root
4. Open a pull request

Your machine label (shown in the HTML tab) comes from the H1 in your `.md` file — `llmbm.py` sets this to your detected hardware specs automatically.

**One file per machine.** Don't commit `benchmark.html` — it's generated.

---

## Results files included

| File | Machine |
|---|---|
| `model-benchmark-results.md` | AMD Ryzen 7 + RTX 2080 Super |
| `model-benchmark-results-m4.md` | Apple M4 Mac Mini, 16GB |
