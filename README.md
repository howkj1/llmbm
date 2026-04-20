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
- **Linux (NVIDIA)** — GPU tracking requires NVIDIA drivers (`nvidia-smi`). Multi-GPU systems fully supported — use `--gpu-index` or the TUI selector
- **Linux (AMD)** — GPU tracking uses the standard `amdgpu` kernel driver (no ROCm needed); reads temperature, power, and utilization from sysfs. Multi-GPU supported
- **Linux (Intel)** — GPU tracking uses the `xe` (Arc discrete) or `i915` (integrated) kernel driver; both ship with the Linux kernel. Multi-GPU supported. Utilization tracking is optional: `sudo apt install intel-gpu-tools`

---

## Running benchmarks

```bash
python3 llmbm.py
```

The TUI will:
- Detect your CPU, GPU(s), and RAM
- Show a GPU selector if multiple GPUs are detected
- Query your Ollama endpoint for installed models
- Let you select which models to benchmark and configure settings
- Write `model-benchmark-results-{machine-id}.md` when done

To list detected GPUs without running the full TUI:

```bash
python3 llmbm.py --list-gpus
```

### Manual / scripted runs (skip TUI)

Benchmark a single model with default pp/tg settings and write the summary file:

```bash
python3 llmbm.py --no-interactive \
  --machine-id rtx2080-ryzen7 \
  --machine-label "RTX 2080 Super, Ryzen 7 5800X, 64GB" \
  --machine-desc "RTX 2080 Super 8GB VRAM, AMD Ryzen 7 5800X, 64GB DDR4, Ubuntu 22.04" \
  --base-url http://localhost:11434/v1 \
  qwen3:14b \
  --write-summary
```

Benchmark multiple prompt/generation lengths with 5 runs each, cooldown, and explicit GPU selection:

```bash
python3 llmbm.py --no-interactive \
  --machine-id rtx2080-ryzen7 \
  --machine-label "RTX 2080 Super, Ryzen 7 5800X, 64GB" \
  --base-url http://localhost:11434/v1 \
  --pp 128,512,2048 \
  --tg 64,256 \
  --runs 5 \
  --gpu-index 0 \
  --gpu-cool 65 \
  --out-dir ~/bench-results \
  qwen3:14b \
  --write-summary
```

Benchmark against a remote Ollama instance on the local network:

```bash
python3 llmbm.py --no-interactive \
  --machine-id m4-mac-mini \
  --machine-label "Apple M4 Mac Mini, 16GB" \
  --machine-desc "Apple M4 Mac Mini, 16GB unified memory, macOS 15" \
  --base-url http://192.168.1.50:11434/v1 \
  --pp 512,2048 \
  --tg 64,256 \
  --runs 3 \
  --out-dir ~/bench-results \
  gemma4:e4b \
  --write-summary
```

The `--out-dir` flag controls where raw per-combo result files land. The summary file (`model-benchmark-results-{machine-id}.md`) always goes in the current directory unless `--summary-dir` is set.

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
