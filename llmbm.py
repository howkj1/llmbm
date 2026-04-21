#!/usr/bin/env python3
"""
llmbm.py — portable LLM benchmark runner for any Ollama endpoint.

Run with no arguments for interactive mode (auto-detects hardware, prompts for
model selection and settings).  Pass flags to pre-fill or skip prompts.

  python3 llmbm.py                    # full interactive TUI
  python3 llmbm.py --no-interactive   # all flags required

Share the generated model-benchmark-results-{machine-id}.md with the community;
run generate_benchmark_html.py --input-dir <dir> to add your machine to the HTML report.
"""

import argparse, subprocess, threading, time, re, os, sys, json, platform, shutil
import urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

# ── Argument parsing ──────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description=__doc__,
)
parser.add_argument('model',            nargs='?',  default=None)
parser.add_argument('--machine-id',     default=None,
                    help='Slug for filenames, e.g. rtx4090-ubuntu  (prompted if omitted)')
parser.add_argument('--machine-label',  default=None,
                    help='Short display label for HTML tabs')
parser.add_argument('--machine-desc',   default=None,
                    help='One-line hardware description for report headers')
parser.add_argument('--base-url',       default=None,
                    help='Ollama /v1 endpoint  (default: http://localhost:11434/v1)')
parser.add_argument('--pp',             default=None,
                    help='Prompt lengths, comma-separated  (default: 128,512,2048)')
parser.add_argument('--tg',             default=None,
                    help='Generation lengths, comma-separated  (default: 64,256)')
parser.add_argument('--runs',           type=int, default=None,
                    help='Runs per combo  (default: 3)')
parser.add_argument('--gpu-cool',       type=float, default=None,
                    help='GPU cooldown threshold °C  (default: disabled)')
parser.add_argument('--cpu-cool',       type=float, default=None,
                    help='CPU cooldown threshold °C  (default: disabled)')
parser.add_argument('--out-dir',        default=None,
                    help='Directory for per-combo result files  (default: ~/bench-results)')
parser.add_argument('--write-summary',  action='store_true',
                    help='Upsert results into model-benchmark-results-{id}.md')
parser.add_argument('--summary-dir',    default=None,
                    help='Directory for summary .md  (default: current directory)')
parser.add_argument('--gpu-index',      type=int, default=None,
                    help='Single GPU index (default: 0); use --gpu-indices for multi-GPU')
parser.add_argument('--gpu-indices',    default=None,
                    help='Comma-separated GPU indices for parallel runs, e.g. 0,1')
parser.add_argument('--endpoints',      default=None,
                    help='Comma-separated Ollama endpoints, one per GPU in --gpu-indices order')
parser.add_argument('--list-gpus',      action='store_true',
                    help='Print detected GPUs and exit')
parser.add_argument('--no-cache',       action='store_true')
parser.add_argument('--no-interactive', action='store_true',
                    help='Skip all prompts; require all flags explicitly')

args = parser.parse_args()

POLL_INTERVAL = 2
MAX_CHART_PTS = 40
COOL_POLL     = 10
COOL_TIMEOUT  = 600

# ── Hardware detection ────────────────────────────────────────────────────────

def _run(*cmd, **kw):
    try:
        return subprocess.check_output(list(cmd), text=True,
                                       stderr=subprocess.DEVNULL, **kw).strip()
    except Exception:
        return ''

def _find_hwmon_dir(name):
    hwmon = Path('/sys/class/hwmon')
    if not hwmon.exists():
        return None
    for p in hwmon.iterdir():
        nf = p / 'name'
        if nf.exists() and nf.read_text().strip() == name:
            return p
    return None

_VENDOR_ID = {'0x1002': 'amd', '0x8086': 'intel'}
_DRIVER_NAMES = {'amd': ['amdgpu'], 'intel': ['xe', 'i915']}

def _enumerate_drm_gpus():
    """Enumerate AMD/Intel GPUs via DRM sysfs.

    Links each card{N} to its hwmon dir (via matching PCI device symlink),
    busy_percent path, VRAM size, and GPU name from lspci.
    Returns list of dicts keyed by sequential index.
    """
    drm = Path('/sys/class/drm')
    if not drm.exists():
        return []

    # Build hwmon→device map once for fast lookup
    hwmon_map = {}  # resolved_device_path → hwmon_dir
    hwmon_base = Path('/sys/class/hwmon')
    if hwmon_base.exists():
        for hw in hwmon_base.iterdir():
            nf = hw / 'name'
            if not nf.exists():
                continue
            driver = nf.read_text().strip()
            if not any(driver in names for names in _DRIVER_NAMES.values()):
                continue
            dev_link = hw / 'device'
            try:
                hwmon_map[dev_link.resolve()] = hw
            except Exception:
                pass

    gpus = []
    card_idx = 0
    for card in sorted(drm.glob('card[0-9]*'), key=lambda p: int(p.name[4:])):
        device_dir = card / 'device'
        if not device_dir.exists():
            continue
        vendor_f = device_dir / 'vendor'
        try:
            vendor = _VENDOR_ID.get(vendor_f.read_text().strip().lower())
        except Exception:
            vendor = None
        if not vendor:
            continue

        # Resolve PCI device for hwmon matching and lspci name
        try:
            device_real = device_dir.resolve()
        except Exception:
            continue

        hwmon_dir = hwmon_map.get(device_real)

        busy_path = device_dir / 'gpu_busy_percent'
        if not busy_path.exists():
            busy_path = None

        vram_gb = 0
        try:
            vf = device_dir / 'mem_info_vram_total'
            if vf.exists():
                vram_gb = round(int(vf.read_text().strip()) / 1024**3)
        except Exception:
            pass

        # GPU name via lspci using PCI address (last component of resolved path)
        name = ''
        try:
            pci_addr = device_real.name  # e.g. 0000:03:00.0
            lspci_line = _run('lspci', '-s', pci_addr)
            if lspci_line:
                name = lspci_line.split(':', 2)[-1].strip()
        except Exception:
            pass
        name = name or f'{vendor.upper()} GPU'

        vram_str = f' {vram_gb}GB VRAM' if vram_gb else ''
        gpus.append({
            'index':     card_idx,
            'vendor':    vendor,
            'name':      name,
            'vram':      vram_gb,
            'hwmon_dir': hwmon_dir,
            'busy_path': busy_path,
            'display':   f'[{card_idx}] {name}{vram_str}',
        })
        card_idx += 1
    return gpus

def _slugify(text):
    s = text.lower()
    for noise in ('nvidia', 'geforce', 'amd', 'radeon rx', 'radeon', 'intel',
                  'core processor', 'processor', 'cpu', 'apple', '(r)', '(tm)',
                  'graphics', 'integrated', 'unified memory'):
        s = s.replace(noise, '')
    s = re.sub(r'\s+', '-', s.strip())
    s = re.sub(r'[^a-z0-9-]', '', s)
    s = re.sub(r'-{2,}', '-', s).strip('-')
    return s[:48]

def _mac_hardware():
    cpu = ram = model_name = ''
    try:
        sp = _run('system_profiler', 'SPHardwareDataType')
        for line in sp.splitlines():
            k, _, v = line.partition(':')
            k, v = k.strip(), v.strip()
            if k == 'Chip':
                cpu = v
            elif k == 'Model Name':
                model_name = v
            elif k == 'Memory' and not ram:
                ram = v
    except Exception:
        pass
    if not cpu:
        cpu = _run('sysctl', '-n', 'machdep.cpu.brand_string') or platform.processor()
    if not ram:
        b = _run('sysctl', '-n', 'hw.memsize')
        ram = f'{int(b) // (1024**3)} GB' if b.isdigit() else ''
    gpu = f'{cpu} (integrated)'  # Apple Silicon unified
    os_ver = platform.mac_ver()[0]
    label = f'{cpu}{f", {ram}" if ram else ""}'
    desc  = f'{model_name or cpu}{f", {ram} unified memory" if ram else ""}, macOS {os_ver}'
    suggested_id = _slugify(f'{cpu}-{re.sub(r"[^0-9]", "", ram)}gb')
    return cpu, gpu, ram, label, desc, suggested_id

def _linux_hardware():
    # CPU
    cpu = ''
    try:
        for line in open('/proc/cpuinfo'):
            if line.startswith('model name'):
                cpu = line.split(':', 1)[1].strip()
                break
    except Exception:
        pass
    cpu = cpu or platform.processor() or 'Unknown CPU'

    # RAM
    ram = ''
    try:
        for line in open('/proc/meminfo'):
            if line.startswith('MemTotal'):
                kb = int(line.split()[1])
                ram = f'{round(kb / 1024**2)} GB'
                break
    except Exception:
        pass

    # GPU — try nvidia-smi, then lspci + AMD sysfs
    gpu = ''
    nvsmi = _run('nvidia-smi', '--query-gpu=name,memory.total',
                 '--format=csv,noheader,nounits')
    if nvsmi:
        parts = nvsmi.splitlines()[0].split(',')
        name = parts[0].strip()
        try:
            vram = f'{int(parts[1].strip()) // 1024}GB VRAM'
        except Exception:
            vram = ''
        gpu = f'{name}{f" {vram}" if vram else ""}'
    if not gpu:
        lspci = _run('lspci')
        for line in lspci.splitlines():
            if re.search(r'vga|3d controller|display', line, re.I):
                gpu = line.split(':', 2)[-1].strip()
                break
    # Append AMD VRAM from sysfs if not already present
    if gpu and 'VRAM' not in gpu:
        for vf in sorted(Path('/sys/class/drm').glob('card*/device/mem_info_vram_total')):
            try:
                vram_gb = round(int(vf.read_text().strip()) / 1024**3)
                if vram_gb > 0:
                    gpu += f' {vram_gb}GB VRAM'
                break
            except Exception:
                pass
    gpu = gpu or 'Unknown GPU'

    short_gpu = re.sub(r'nvidia geforce |amd radeon |intel ', '', gpu, flags=re.I)
    short_cpu = re.sub(r'\d+-core.*|@ [\d.]+ ?ghz', '', cpu, flags=re.I).strip()
    label = f'{short_gpu}, {short_cpu}'
    desc  = f'{gpu}, {cpu}, {ram} RAM'
    suggested_id = _slugify(f'{short_gpu}-{re.sub(r"[^0-9a-z]", "-", short_cpu.lower())}')
    return cpu, gpu, ram, label, desc, suggested_id

def detect_hardware():
    if platform.system() == 'Darwin':
        return _mac_hardware()
    return _linux_hardware()

# ── Prerequisite checks ───────────────────────────────────────────────────────

def find_llama_benchy():
    candidates = [
        shutil.which('llama-benchy'),
        str(Path(sys.executable).parent / 'llama-benchy'),
        os.path.expanduser('~/.local/bin/llama-benchy'),
    ]
    uvx = shutil.which('uvx') or os.path.expanduser('~/.local/bin/uvx')
    for c in candidates:
        if c and Path(c).exists():
            return [c]
    if Path(uvx).exists() if uvx else False:
        return [uvx, 'llama-benchy']
    return None

def fetch_models(base_url):
    try:
        api = base_url.rstrip('/').removesuffix('/v1')
        data = json.loads(urllib.request.urlopen(f'{api}/api/tags', timeout=5).read())
        return [m['name'] for m in data.get('models', [])]
    except Exception:
        return None

def check_sensors():
    """Return list of INFO strings about unavailable sensors (non-fatal)."""
    notes = []
    if platform.system() == 'Linux':
        has_nvidia = bool(shutil.which('nvidia-smi'))
        has_amdgpu = bool(_find_hwmon_dir('amdgpu'))
        has_intel  = bool(_find_hwmon_dir('xe') or _find_hwmon_dir('i915'))
        if not has_nvidia and not has_amdgpu and not has_intel:
            notes.append(
                'No GPU sensor found — temperature/power tracking disabled.\n'
                '    NVIDIA: install NVIDIA drivers (provides nvidia-smi).\n'
                '    AMD:    ensure the amdgpu kernel driver is loaded.\n'
                '    Intel:  ensure the xe (Arc) or i915 kernel driver is loaded.')
        elif has_intel and not shutil.which('intel_gpu_top'):
            notes.append(
                'intel_gpu_top not found — Intel GPU utilization tracking disabled.\n'
                '    Install: sudo apt install intel-gpu-tools  (or distro equivalent)')
    elif platform.system() == 'Darwin':
        try:
            subprocess.check_call(
                ['sudo', '-n', 'powermetrics', '-n', '1', '-i', '100',
                 '--samplers', 'cpu_power'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            user = os.environ.get('USER', '$USER')
            notes.append(
                'sudo -n powermetrics unavailable — CPU power tracking disabled.\n'
                '    To enable, run once:\n'
                f'      echo \'{user} ALL=(ALL) NOPASSWD: /usr/bin/powermetrics\' \\\n'
                '        | sudo tee /etc/sudoers.d/powermetrics-bench\n'
                '      sudo chmod 440 /etc/sudoers.d/powermetrics-bench')
    return notes

# ── GPU discovery ────────────────────────────────────────────────────────────

def discover_gpus():
    """Return list of (index, vendor, display_name, meta) for all detectable GPUs.

    meta is None for NVIDIA; a dict with hwmon_dir/busy_path/vram for AMD/Intel.
    NVIDIA GPUs are always listed first, then AMD/Intel in card order.
    """
    if platform.system() != 'Linux':
        return []
    gpus = []
    # NVIDIA — enumerate all via nvidia-smi
    out = _run('nvidia-smi', '--query-gpu=index,name,memory.total',
               '--format=csv,noheader,nounits')
    if out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            name = parts[1]
            try:
                vram = f'{int(parts[2]) // 1024}GB VRAM'
            except Exception:
                vram = ''
            display = f'[{idx}] {name}{f" {vram}" if vram else ""}'
            gpus.append((idx, 'nvidia', display, None))
    # AMD/Intel — enumerate via DRM sysfs
    nvidia_count = len(gpus)
    for g in _enumerate_drm_gpus():
        idx = nvidia_count + g['index']
        gpus.append((idx, g['vendor'], f'[{idx}] {g["name"]}' +
                     (f' {g["vram"]}GB VRAM' if g['vram'] else ''), g))
    return gpus

# ── TUI helpers ───────────────────────────────────────────────────────────────

def _hr(char='─', width=56):
    print(char * width)

def _section(title):
    print(f'\n  {title}')
    print('  ' + '─' * len(title))

def _ask(label, default=None, cast=None, validate=None):
    """Single-line prompt; returns cast(value) or default."""
    hint = f' [{default}]' if default is not None else ''
    while True:
        raw = input(f'    {label}{hint}: ').strip()
        val = raw if raw else (str(default) if default is not None else '')
        if not val:
            print('    (required)')
            continue
        if cast:
            try:
                val = cast(val)
            except Exception:
                print(f'    Invalid — expected {cast.__name__}')
                continue
        if validate and not validate(val):
            continue
        return val

def _confirm(label, default=True):
    hint = '[Y/n]' if default else '[y/N]'
    raw = input(f'    {label} {hint}: ').strip().lower()
    if not raw:
        return default
    return raw.startswith('y')

def _select_gpus(gpus, default_url):
    """Multi-GPU selection. Returns list of {gpu_index, gpu_vendor, gpu_meta, base_url}."""
    if not gpus:
        return [{'gpu_index': 0, 'gpu_vendor': 'unknown', 'gpu_meta': None, 'base_url': default_url}]

    _section('GPU selection  (multi-GPU runs benchmarks in parallel)')
    for i, (idx, vendor, name, _) in enumerate(gpus, 1):
        print(f'    [{i}] {name}')
    print()
    raw = input('    Select GPUs (numbers comma-separated, or "all") [1]: ').strip()
    if not raw or raw == '1':
        chosen = [gpus[0]]
    elif raw.lower() == 'all':
        chosen = gpus
    else:
        chosen = []
        for part in raw.split(','):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(gpus):
                chosen.append(gpus[int(part) - 1])
            else:
                print(f'    Skipping invalid selection: {part}')
        if not chosen:
            chosen = [gpus[0]]

    workers = []
    for i, (idx, vendor, name, meta) in enumerate(chosen):
        short = re.sub(r'^\[\d+\]\s*', '', name)
        default_ep = default_url
        if i > 0:
            # suggest incrementing port for additional GPUs
            try:
                base, port = default_url.rsplit(':', 1)
                path = ''
                if '/' in port:
                    port, path = port.split('/', 1)
                    path = '/' + path
                default_ep = f'{base}:{int(port) + i}{path}'
            except Exception:
                pass
        print(f'\n    GPU {idx} ({short})')
        url = _ask('    Ollama endpoint', default_ep)
        workers.append({'gpu_index': idx, 'gpu_vendor': vendor,
                        'gpu_meta': meta, 'base_url': url})
    return workers

def _select_gpu(gpus):
    """Return (index, vendor, meta) for the chosen GPU. Auto-selects if only one."""
    if not gpus:
        return 0, 'unknown', None
    if len(gpus) == 1:
        print(f'    GPU: {gpus[0][2]}')
        return gpus[0][0], gpus[0][1], gpus[0][3]
    _section('Available GPUs')
    for i, (idx, vendor, name, _) in enumerate(gpus, 1):
        print(f'    [{i}] {name}')
    while True:
        raw = input(f'    Select GPU [1]: ').strip()
        if not raw:
            return gpus[0][0], gpus[0][1], gpus[0][3]
        if raw.isdigit() and 1 <= int(raw) <= len(gpus):
            g = gpus[int(raw) - 1]
            return g[0], g[1], g[3]
        print('    Invalid selection')

def _select_models(available):
    _section('Available models')
    for i, m in enumerate(available, 1):
        print(f'    [{i:2}] {m}')
    print('    [ a] All models')
    print()
    while True:
        raw = input('    Select models (numbers, names, comma-separated, or "all"): ').strip()
        if not raw or raw.lower() in ('a', 'all'):
            return list(available)
        selected, ok = [], True
        for part in raw.split(','):
            part = part.strip()
            if part.isdigit():
                idx = int(part)
                if 1 <= idx <= len(available):
                    selected.append(available[idx - 1])
                else:
                    print(f'    No model #{idx}'); ok = False; break
            elif part in available:
                selected.append(part)
            else:
                hits = [m for m in available if part.lower() in m.lower()]
                if len(hits) == 1:
                    selected.append(hits[0])
                elif hits:
                    print(f'    Ambiguous "{part}" — matches: {hits}'); ok = False; break
                else:
                    print(f'    Not found: "{part}"'); ok = False; break
        if ok and selected:
            return list(dict.fromkeys(selected))  # dedupe, preserve order

def run_tui(hw_label, hw_desc, hw_id):
    """
    Interactive setup.  Returns a dict of all settings.
    Pre-fills from CLI args where provided.
    """
    _hr('═')
    print('  llmbm — LLM Benchmark Runner')
    _hr('═')

    cpu, gpu, ram, *_ = detect_hardware()
    _section('Detected hardware')
    print(f'    CPU / SoC : {cpu}')
    print(f'    GPU       : {gpu}')
    print(f'    RAM       : {ram}')
    print(f'    OS        : {platform.system()} {platform.release()} {platform.machine()}')

    _section('Machine identity  (used in filenames and HTML)')
    machine_id    = _ask('Machine ID', args.machine_id or hw_id)
    machine_label = _ask('Label (shown in HTML tabs)', args.machine_label or hw_label)
    machine_desc  = _ask('Description (report header)', args.machine_desc or hw_desc)

    _section('Primary Ollama endpoint')
    default_url = args.base_url or 'http://localhost:11434/v1'
    base_url = _ask('Base URL', default_url)

    print('\n  Checking endpoint…', end=' ', flush=True)
    available = fetch_models(base_url)
    if available is None:
        print('UNREACHABLE')
        print(f'\n  ERROR: Cannot reach {base_url}')
        print('  Is Ollama running?  Try: ollama serve')
        sys.exit(1)
    print(f'OK  ({len(available)} models)')

    gpus = discover_gpus()
    workers = _select_gpus(gpus, base_url)

    models = _select_models(available)

    _section('Benchmark settings')
    pp   = _ask('PP lengths  (prompt token counts)', args.pp   or '128,512,2048')
    tg   = _ask('TG lengths  (generation token counts)', args.tg or '64,256')
    runs = _ask('Runs per combo', args.runs or 3, cast=int)

    _section('Cooldown between tests  (leave blank to disable)')
    raw = input(f'    GPU cooldown threshold °C [{"disabled" if args.gpu_cool is None else args.gpu_cool}]: ').strip()
    gpu_cool = float(raw) if raw else args.gpu_cool
    raw = input(f'    CPU cooldown threshold °C [{"disabled" if args.cpu_cool is None else args.cpu_cool}]: ').strip()
    cpu_cool = float(raw) if raw else args.cpu_cool

    _section('Output')
    out_dir     = _ask('Results directory', args.out_dir or str(Path.home() / 'bench-results'))
    write_summ  = _confirm('Write summary .md for sharing?',
                           default=True if args.write_summary else True)
    summary_dir = _ask('Summary directory', args.summary_dir or str(Path.cwd()))

    # Distribute models round-robin across workers
    for i, model in enumerate(models):
        workers[i % len(workers)].setdefault('models', []).append(model)
    for w in workers:
        w.setdefault('models', [])
        # short label for output prefixing
        w['label'] = re.sub(r'^\[\d+\]\s*', '', next(
            (name for idx, _, name, _ in gpus if idx == w['gpu_index']), f'GPU {w["gpu_index"]}'))

    combos = len(pp.split(',')) * len(tg.split(','))
    total  = combos * runs * len(models)
    print()
    _hr()
    if len(workers) > 1:
        print(f'  Parallel mode: {len(workers)} GPUs')
        for w in workers:
            print(f'    {w["label"]} → {w["base_url"]}: {", ".join(w["models"]) or "(none)"}')
    else:
        print(f'  Ready: {len(models)} model(s) × {combos} combos × {runs} runs = {total} total runs')
        for m in models:
            print(f'    • {m}')
    _hr()
    if not _confirm('\n  Proceed?', default=True):
        print('  Aborted.')
        sys.exit(0)

    return dict(
        machine_id=machine_id, machine_label=machine_label, machine_desc=machine_desc,
        base_url=base_url, models=models, workers=workers,
        pp=list(map(int, pp.split(','))), tg=list(map(int, tg.split(','))),
        runs=runs, gpu_cool=gpu_cool, cpu_cool=cpu_cool,
        out_dir=Path(out_dir), write_summary=write_summ,
        summary_dir=Path(summary_dir),
        gpu_index=workers[0]['gpu_index'], gpu_vendor=workers[0]['gpu_vendor'],
        gpu_meta=workers[0]['gpu_meta'],
    )

def settings_from_args():
    """Non-interactive: validate required args and return settings dict."""
    missing = []
    if not args.machine_id:    missing.append('--machine-id')
    if not args.model:         missing.append('model (positional)')
    if not args.base_url:      missing.append('--base-url')
    if missing:
        print('ERROR: --no-interactive requires: ' + ', '.join(missing))
        sys.exit(1)

    available = fetch_models(args.base_url)
    if available is None:
        print(f'ERROR: Cannot reach {args.base_url} — is Ollama running?')
        sys.exit(1)
    if not any(args.model == m or args.model in m for m in available):
        print(f'WARNING: model "{args.model}" not found at endpoint.')
        print(f'  Available: {", ".join(available)}')

    gpus = discover_gpus()
    gpu_lookup = {idx: (v, name, m) for idx, v, name, m in gpus}

    # Build workers list
    base_url = args.base_url or 'http://localhost:11434/v1'
    if args.gpu_indices:
        indices   = [int(x.strip()) for x in args.gpu_indices.split(',')]
        endpoints = ([u.strip() for u in args.endpoints.split(',')]
                     if args.endpoints else [base_url] * len(indices))
        if len(endpoints) < len(indices):
            endpoints += [endpoints[-1]] * (len(indices) - len(endpoints))
        workers = []
        for i, idx in enumerate(indices):
            v, name, meta = gpu_lookup.get(idx, ('unknown', f'GPU {idx}', None))
            label = re.sub(r'^\[\d+\]\s*', '', name)
            workers.append({'gpu_index': idx, 'gpu_vendor': v, 'gpu_meta': meta,
                            'base_url': endpoints[i], 'label': label, 'models': []})
    else:
        idx = args.gpu_index or 0
        v, name, meta = gpu_lookup.get(idx, ('unknown', f'GPU {idx}', None))
        label = re.sub(r'^\[\d+\]\s*', '', name)
        workers = [{'gpu_index': idx, 'gpu_vendor': v, 'gpu_meta': meta,
                    'base_url': base_url, 'label': label, 'models': []}]

    # Distribute model(s) round-robin
    models = [args.model] if args.model else []
    for i, model in enumerate(models):
        workers[i % len(workers)]['models'].append(model)

    return dict(
        machine_id=args.machine_id,
        machine_label=args.machine_label or args.machine_id,
        machine_desc=args.machine_desc or f'{platform.system()} {platform.machine()}',
        base_url=base_url,
        models=models,
        workers=workers,
        pp=list(map(int, (args.pp or '128,512,2048').split(','))),
        tg=list(map(int, (args.tg or '64,256').split(','))),
        runs=args.runs or 3,
        gpu_cool=args.gpu_cool, cpu_cool=args.cpu_cool,
        out_dir=Path(args.out_dir or Path.home() / 'bench-results'),
        write_summary=args.write_summary,
        summary_dir=Path(args.summary_dir or Path.cwd()),
        gpu_index=workers[0]['gpu_index'], gpu_vendor=workers[0]['gpu_vendor'],
        gpu_meta=workers[0]['gpu_meta'],
    )

# ── Boot ──────────────────────────────────────────────────────────────────────

_, _, _, hw_label, hw_desc, hw_id = detect_hardware()

if args.list_gpus:
    gpus = discover_gpus()
    if gpus:
        print('Detected GPUs:')
        for idx, vendor, name, _ in gpus:
            print(f'  {name}  ({vendor})')
    else:
        print('No GPUs detected.')
    sys.exit(0)

bench_prefix = find_llama_benchy()
if not bench_prefix:
    print('ERROR: llama-benchy not found.')
    print('  Install:  pip install llama-benchy')
    print('  Or:       uvx llama-benchy  (if uvx is available)')
    sys.exit(1)

sensor_notes = check_sensors()
if sensor_notes:
    print()
    for note in sensor_notes:
        for i, line in enumerate(note.splitlines()):
            print(f'  {"NOTE:" if i == 0 else "      "} {line}')

if args.no_interactive:
    cfg = settings_from_args()
else:
    cfg = run_tui(hw_label, hw_desc, hw_id)

MACHINE_ID    = cfg['machine_id']
MACHINE_LABEL = cfg['machine_label']
MACHINE_DESC  = cfg['machine_desc']
BASE_URL      = cfg['base_url']
MODELS        = cfg['models']
PP            = cfg['pp']
TG            = cfg['tg']
RUNS          = cfg['runs']
GPU_COOL      = cfg['gpu_cool']
CPU_COOL      = cfg['cpu_cool']
OUT_DIR       = cfg['out_dir']
WRITE_SUMMARY = cfg['write_summary']
SUMMARY_DIR   = cfg['summary_dir']
LARGE         = args.no_cache
GPU_INDEX     = cfg['gpu_index']
GPU_VENDOR    = cfg['gpu_vendor']
_gpu_meta     = cfg.get('gpu_meta') or {}
GPU_HWMON_DIR = _gpu_meta.get('hwmon_dir')  # Path or None
GPU_BUSY_PATH = _gpu_meta.get('busy_path')  # Path or None

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Hardware readers ──────────────────────────────────────────────────────────

def _find_hwmon(name):
    d = _find_hwmon_dir(name)
    return (d / 'temp1_input') if d else None

_K10TEMP  = _find_hwmon('k10temp')   if platform.system() == 'Linux' else None
_CORETEMP = _find_hwmon('coretemp')  if platform.system() == 'Linux' else None
_mac_power_ok = None

def _read_hwmon_gpu(hwmon_dir=None, driver_names=None):
    """Read temp and power from a hwmon dir. Falls back to driver name search."""
    hw = hwmon_dir
    if hw is None and driver_names:
        for name in driver_names:
            hw = _find_hwmon_dir(name)
            if hw:
                break
    temp_c = power_w = None
    if hw:
        try:
            t = hw / 'temp1_input'
            if t.exists():
                temp_c = round(int(t.read_text().strip()) / 1000, 1)
        except Exception:
            pass
        for pf in ('power1_average', 'power1_input'):
            try:
                pv = hw / pf
                if pv.exists():
                    power_w = round(int(pv.read_text().strip()) / 1_000_000, 1)
                    break
            except Exception:
                pass
    return temp_c, power_w

def _read_amdgpu(hwmon_dir=None, busy_path=None):
    """AMD GPU: hwmon temp/power + drm busy_percent for utilization."""
    temp_c, power_w = _read_hwmon_gpu(hwmon_dir, ['amdgpu'])
    util_pct = None
    if busy_path:
        try:
            util_pct = int(Path(busy_path).read_text().strip())
        except Exception:
            pass
    else:
        for card in sorted(Path('/sys/class/drm').glob('card*/device/gpu_busy_percent')):
            try:
                util_pct = int(card.read_text().strip())
                break
            except Exception:
                pass
    return temp_c, util_pct, power_w

def _read_intelgpu(hwmon_dir=None):
    """Intel GPU: xe (Arc) or i915 (integrated) hwmon temp/power.
    Utilization via intel_gpu_top if installed."""
    temp_c, power_w = _read_hwmon_gpu(hwmon_dir, ['xe', 'i915'])
    util_pct = None
    if shutil.which('intel_gpu_top'):
        try:
            out = subprocess.check_output(
                ['intel_gpu_top', '-J', '-s', '250'],
                text=True, stderr=subprocess.DEVNULL, timeout=2)
            for line in out.splitlines():
                m = re.search(r'"Render/3D".*?"busy":\s*([\d.]+)', line)
                if m:
                    util_pct = round(float(m.group(1)))
                    break
        except Exception:
            pass
    return temp_c, util_pct, power_w

def read_gpu():
    if platform.system() != 'Linux':
        return None, None, None
    if GPU_VENDOR == 'nvidia':
        try:
            out = subprocess.check_output(
                ['nvidia-smi', '-i', str(GPU_INDEX),
                 '--query-gpu=temperature.gpu,utilization.gpu,power.draw',
                 '--format=csv,noheader,nounits'],
                text=True, stderr=subprocess.DEVNULL).strip()
            parts = [x.strip() for x in out.split(',')]
            return int(parts[0]), int(parts[1]), float(parts[2])
        except Exception:
            pass
        return None, None, None
    if GPU_VENDOR == 'amd':
        temp_c, util_pct, power_w = _read_amdgpu(GPU_HWMON_DIR, GPU_BUSY_PATH)
        return temp_c, util_pct, power_w
    if GPU_VENDOR == 'intel':
        temp_c, util_pct, power_w = _read_intelgpu(GPU_HWMON_DIR)
        return temp_c, util_pct, power_w
    # Unknown vendor — try all
    try:
        out = subprocess.check_output(
            ['nvidia-smi', '-i', str(GPU_INDEX),
             '--query-gpu=temperature.gpu,utilization.gpu,power.draw',
             '--format=csv,noheader,nounits'],
            text=True, stderr=subprocess.DEVNULL).strip()
        parts = [x.strip() for x in out.split(',')]
        return int(parts[0]), int(parts[1]), float(parts[2])
    except Exception:
        pass
    temp_c, util_pct, power_w = _read_amdgpu()
    if temp_c is not None or util_pct is not None:
        return temp_c, util_pct, power_w
    temp_c, util_pct, power_w = _read_intelgpu()
    if temp_c is not None or util_pct is not None:
        return temp_c, util_pct, power_w
    return None, None, None

def _make_gpu_reader(vendor, idx, hwmon_dir, busy_path):
    """Return a read_gpu function bound to a specific GPU."""
    def _reader():
        if platform.system() != 'Linux':
            return None, None, None
        if vendor == 'nvidia':
            try:
                out = subprocess.check_output(
                    ['nvidia-smi', '-i', str(idx),
                     '--query-gpu=temperature.gpu,utilization.gpu,power.draw',
                     '--format=csv,noheader,nounits'],
                    text=True, stderr=subprocess.DEVNULL).strip()
                parts = [x.strip() for x in out.split(',')]
                return int(parts[0]), int(parts[1]), float(parts[2])
            except Exception:
                pass
            return None, None, None
        if vendor == 'amd':
            return _read_amdgpu(hwmon_dir, busy_path)
        if vendor == 'intel':
            return _read_intelgpu(hwmon_dir)
        return None, None, None
    return _reader

def read_cpu():
    if platform.system() == 'Darwin':
        return _read_cpu_mac()
    for sensor in (_K10TEMP, _CORETEMP):
        if sensor and sensor.exists():
            try:
                return round(int(sensor.read_text().strip()) / 1000, 1)
            except Exception:
                pass
    return None

def _read_cpu_mac():
    global _mac_power_ok
    if _mac_power_ok is False:
        return None
    try:
        out = subprocess.check_output(
            ['sudo', '-n', 'powermetrics', '-n', '1', '-i', '200', '--samplers', 'cpu_power'],
            text=True, stderr=subprocess.DEVNULL, timeout=6)
        m = re.search(r'CPU Power:\s*([\d.]+)\s*mW', out)
        if m:
            _mac_power_ok = True
            return round(float(m.group(1)) / 1000, 2)
    except Exception:
        pass
    if _mac_power_ok is None:
        _mac_power_ok = False
    return None

CPU_COL = 'CPU W' if platform.system() == 'Darwin' else 'CPU °C'

# ── Temp poller ───────────────────────────────────────────────────────────────

class TempPoller:
    def __init__(self, read_gpu_fn=None):
        self.readings = []
        self._stop = threading.Event()
        self._t = self._t0 = None
        self._read_gpu = read_gpu_fn or read_gpu

    def start(self):
        self._stop.clear(); self._t0 = time.time()
        self._t = threading.Thread(target=self._run, daemon=True); self._t.start()

    def stop(self):
        self._stop.set()
        if self._t: self._t.join(timeout=POLL_INTERVAL + 1)

    def _run(self):
        while not self._stop.is_set():
            elapsed = round(time.time() - self._t0)
            self.readings.append((elapsed, *self._read_gpu(), read_cpu()))
            self._stop.wait(POLL_INTERVAL)

# ── Mermaid helpers ───────────────────────────────────────────────────────────

def _thin(lst, n):
    if len(lst) <= n: return lst
    step = len(lst) / n
    return [lst[int(i * step)] for i in range(n)]

def mermaid_temps(pp, tg, readings):
    if not readings: return '_No thermal data collected._'
    has_gpu = any(r[1] is not None for r in readings)
    has_cpu = any(r[4] is not None for r in readings)
    if not has_gpu and not has_cpu: return '_No thermal data collected._'
    t = _thin(readings, MAX_CHART_PTS)
    times  = [r[0] for r in t]; gpu_c  = [r[1] or 0 for r in t]
    cpu_v  = [r[4] or 0 for r in t]; gpu_pct = [r[2] or 0 for r in t]
    gpu_w  = [r[3] or 0.0 for r in t]
    all_v  = [v for v in gpu_c + cpu_v if v]
    y_min  = max(0, min(all_v) - 5) if all_v else 0
    y_max  = (max(all_v) + 5) if all_v else 100
    w_max  = max(max(gpu_w) + 10, 30)
    fl = lambda lst: '[' + ', '.join(str(v) for v in lst) + ']'
    y_unit = 'W' if platform.system() == 'Darwin' else '°C'
    out = [f'```mermaid\nxychart-beta\n  title "Thermal — pp={pp} tg={tg}"\n'
           f'  x-axis "Time (s)" {fl(times)}\n  y-axis "{y_unit}" {y_min} --> {y_max}']
    if has_gpu: out.append(f'  line "GPU °C" {fl(gpu_c)}')
    if has_cpu: out.append(f'  line "{CPU_COL}" {fl(cpu_v)}')
    out.append('```')
    if has_gpu:
        out.append(
            f'\n```mermaid\nxychart-beta\n  title "GPU Util — pp={pp} tg={tg}"\n'
            f'  x-axis "Time (s)" {fl(times)}\n  y-axis "%" 0 --> 100\n'
            f'  line "GPU util %" {fl(gpu_pct)}\n```\n'
            f'\n```mermaid\nxychart-beta\n  title "GPU Power — pp={pp} tg={tg}"\n'
            f'  x-axis "Time (s)" {fl(times)}\n  y-axis "Watts" 0 --> {int(w_max)}\n'
            f'  line "Power (W)" {fl([round(v) for v in gpu_w])}\n```')
    return '\n'.join(out)

# ── Cooldown gate ─────────────────────────────────────────────────────────────

def wait_for_cooldown(read_gpu_fn=None, prefix=''):
    if GPU_COOL is None and CPU_COOL is None: return
    _rg = read_gpu_fn or read_gpu
    t0 = time.time()
    while True:
        gpu_c, _, _ = _rg(); cpu_c = read_cpu()
        gpu_ok = GPU_COOL is None or gpu_c is None or gpu_c <= GPU_COOL
        cpu_ok = CPU_COOL is None or cpu_c is None or cpu_c <= CPU_COOL
        elapsed = int(time.time() - t0)
        if gpu_ok and cpu_ok:
            if elapsed > 0:
                print(f'{prefix}Cooldown done after {elapsed}s (GPU {gpu_c}, {CPU_COL} {cpu_c})',
                      flush=True)
            return
        if elapsed >= COOL_TIMEOUT:
            print(f'{prefix}WARNING: cooldown timeout. Proceeding.', flush=True); return
        print(f'{prefix}Cooling… GPU {gpu_c} {CPU_COL} {cpu_c} [{elapsed}s]', flush=True)
        time.sleep(COOL_POLL)

# ── Summary file helpers ──────────────────────────────────────────────────────

_summary_lock = threading.Lock()

def _ff(s):
    m = re.match(r'\s*([\d,]+\.?\d*)', s.strip())
    return float(m.group(1).replace(',', '')) if m else None

def parse_bench_metrics(bench_md):
    r = {'tg': None, 'pp': None, 'ttft': None}
    for line in bench_md.splitlines():
        if not line.startswith('|'): continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 3: continue
        test = cells[1] if len(cells) > 1 else ''
        tps  = _ff(cells[2]) if len(cells) > 2 else None
        ttfr = _ff(cells[4]) if len(cells) > 4 else None
        if test.startswith('tg'):   r['tg'] = tps
        elif test.startswith('pp'): r['pp'] = tps; r['ttft'] = ttfr
    return r

def _fmt(v, hi=100):
    if v is None: return '—'
    return f'~{v:,.0f}' if v >= hi else str(round(v, 1))

def _upsert_table(lines, frag, model_name, new_row):
    result, i = [], 0
    while i < len(lines):
        line = lines[i]
        if frag.lower() in line.lower() and re.match(r'^#+\s', line):
            result.append(line); i += 1
            while i < len(lines) and not lines[i].startswith('|'):
                result.append(lines[i]); i += 1
            tbl = []
            while i < len(lines) and lines[i].startswith('|'):
                tbl.append(lines[i]); i += 1
            if len(tbl) >= 2:
                header, sep, data = tbl[0], tbl[1], tbl[2:]
                found, new_data = False, []
                for row in data:
                    if row.strip('|').split('|')[0].strip() == model_name:
                        new_data.append(new_row); found = True
                    else:
                        new_data.append(row)
                if not found: new_data.append(new_row)
                result.extend([header, sep] + new_data)
            else:
                result.extend(tbl)
        else:
            result.append(line); i += 1
    return result

def _upsert_section(lines, model, section):
    marker = f'# Benchmark Report: {model}'
    start = next((i for i, l in enumerate(lines) if l.strip() == marker), None)
    if start is None:
        while lines and lines[-1].strip() in ('', '---'): lines.pop()
        return lines + ['', '---', ''] + section
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith('# Benchmark Report:')), len(lines))
    return lines[:start] + section + lines[end:]

def _summary_path():
    return SUMMARY_DIR / f'model-benchmark-results-{MACHINE_ID}.md'

def _skeleton(date_str):
    return (
        '---\nlayout: default\ncustom_css: benchmark\n---\n\n'
        f'# {MACHINE_LABEL} — Benchmark Summary\n\n'
        f'**Machine:** {MACHINE_DESC}  \n'
        f'**Tool:** llama-benchy | **Date:** {date_str}  \n'
        '**Method:** runs per test, latency mode: generation\n\n---\n\n'
        '## Cross-Model Comparison\n\n'
        '### Generation Throughput (TG tok/s)\n\n'
        '| Model | Size | tg=64 @ pp=512 | tg=256 @ pp=512 | tg=64 @ pp=2048 | tg=256 @ pp=2048 | Tested from | Date |\n'
        '|:------|-----:|---:|---:|---:|---:|:------|:------|\n\n'
        '### Prompt Processing Throughput (PP tok/s @ pp=512)\n\n'
        '| Model | PP tok/s | TTFT (ms) | Tested from | Date |\n'
        '|:------|---:|---:|:------|:------|\n\n'
        '### Thermal Profile (peak across all tests)\n\n'
        f'| Model | GPU peak °C | {CPU_COL} peak | Tested from | Date | Notes |\n'
        '|:------|---:|---:|:------|:------|:------|\n\n'
        '---\n\n*Per-model reports follow below.*\n\n---\n'
    )

def write_summary(model, all_results, run_date):
    path = _summary_path()
    date_str = run_date.strftime('%Y-%m-%d')
    combos = {(r['pp'], r['tg']): parse_bench_metrics(r['bench_md']) for r in all_results}

    def tg_val(pp, tg):
        m = combos.get((pp, tg)); return _fmt(m['tg'] if m else None)

    pp512 = [combos[(p, t)] for (p, t) in combos if p == 512]
    pp_tps = round(sum(m['pp']   for m in pp512 if m['pp'])   / max(len(pp512), 1)) if pp512 else None
    ttft   = round(sum(m['ttft'] for m in pp512 if m['ttft']) / max(len(pp512), 1)) if pp512 else None

    gpu_peak = max((r['gpu_peak'] for r in all_results if r['gpu_peak']), default=0)
    cpu_peak = max((r['cpu_peak'] for r in all_results if r['cpu_peak']), default=0)

    tg_row = (f'| {model} | ? | {tg_val(512,64)} | {tg_val(512,256)} | '
              f'{tg_val(2048,64)} | {tg_val(2048,256)} | {MACHINE_LABEL} | {date_str} |')
    pp_row = (f'| {model} | {_fmt(pp_tps)} | {_fmt(ttft, hi=1)} | {MACHINE_LABEL} | {date_str} |')
    th_row = (f'| {model} | {gpu_peak} | {cpu_peak} | {MACHINE_LABEL} | {date_str} | |')

    section  = [f'# Benchmark Report: {model}', '']
    section += [f'**Date:** {run_date.strftime("%Y-%m-%d %H:%M")}  ',
                f'**Machine:** {MACHINE_DESC}  ',
                f'**Endpoint:** {BASE_URL}  ',
                f'**Run platform:** {platform.system()} {platform.release()} {platform.machine()}  ',
                f'**Runs per test:** {RUNS}  ', '', '---', '', '## Summary', '']
    section += [f'| pp | tg | Duration (s) | GPU peak °C | {CPU_COL} peak |',
                '|---:|---:|---:|---:|---:|']
    for r in all_results:
        section.append(f'| {r["pp"]} | {r["tg"]} | {r["duration"]} | {r["gpu_peak"]} | {r["cpu_peak"]} |')
    section += ['', '---', '']
    for r in all_results:
        pp, tg = r['pp'], r['tg']
        section += [f'## pp={pp} tg={tg}', '', *r['bench_md'].strip().splitlines(), '',
                    '### Thermal Data', '', *mermaid_temps(pp, tg, r['temps']).splitlines(), '',
                    '<details><summary>Raw readings</summary>', '',
                    f'| Time (s) | GPU °C | GPU util % | GPU W | {CPU_COL} |',
                    '|---:|---:|---:|---:|---:|']
        for rd in r['temps']:
            section.append(f'| {rd[0]} | {rd[1]} | {rd[2]} | {rd[3]} | {rd[4]} |')
        section += ['', '</details>', '', '---', '']

    with _summary_lock:
        text  = path.read_text(encoding='utf-8') if path.exists() else _skeleton(date_str)
        lines = text.splitlines()
        lines = _upsert_table(lines, 'Generation Throughput',        model, tg_row)
        lines = _upsert_table(lines, 'Prompt Processing Throughput', model, pp_row)
        lines = _upsert_table(lines, 'Thermal Profile',              model, th_row)
        lines = _upsert_section(lines, model, section)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'  Summary updated: {path}', flush=True)

# ── Benchmark runner ─────────────────────────────────────────────────────────

def run_gpu_worker(worker):
    """Run all assigned models on a single GPU, sequentially."""
    meta = worker.get('gpu_meta') or {}
    read_gpu_fn = _make_gpu_reader(
        worker['gpu_vendor'], worker['gpu_index'],
        meta.get('hwmon_dir'), meta.get('busy_path'))
    label = worker.get('label', f'GPU {worker["gpu_index"]}')
    for model in worker['models']:
        run_model(model,
                  base_url=worker['base_url'],
                  read_gpu_fn=read_gpu_fn,
                  gpu_label=label)

def run_model(model, base_url=None, read_gpu_fn=None, gpu_label=''):
    _base_url  = base_url or BASE_URL
    _read_gpu  = read_gpu_fn or read_gpu
    pfx        = f'[{gpu_label}] ' if gpu_label else '  '
    safe       = model.replace(':', '_').replace('/', '_')
    combos     = [(pp, tg) for pp in PP for tg in TG]
    results    = []
    first      = True

    print(f'\n{"═"*56}', flush=True)
    print(f'{pfx}Model: {model}  |  {len(combos)} combos × {RUNS} runs', flush=True)
    print(f'{"═"*56}', flush=True)
    wait_for_cooldown(_read_gpu, pfx)

    for pp, tg in combos:
        print(f'\n{pfx}pp={pp} tg={tg}', flush=True)
        result_file = OUT_DIR / f'{safe}_{MACHINE_ID}_pp{pp}_tg{tg}.md'
        cmd = bench_prefix + [
            '--base-url', _base_url, '--model', model,
            '--pp', str(pp), '--tg', str(tg), '--runs', str(RUNS),
            '--latency-mode', 'generation', '--format', 'md',
            '--save-result', str(result_file),
        ]
        if not first: cmd += ['--no-warmup', '--skip-coherence']
        if LARGE:     cmd += ['--no-cache']

        poller = TempPoller(read_gpu_fn=_read_gpu)
        poller.start()
        t0   = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        dur  = round(time.time() - t0)
        poller.stop()

        bench_md  = result_file.read_text() if result_file.exists() else proc.stdout
        gpu_peak  = max((r[1] for r in poller.readings if r[1]), default=0)
        cpu_peak  = max((r[4] for r in poller.readings if r[4]), default=0)
        print(f'{pfx}Done {dur}s | GPU {gpu_peak} | {CPU_COL} {cpu_peak}', flush=True)

        results.append({'pp': pp, 'tg': tg, 'bench_md': bench_md.strip(),
                        'temps': poller.readings, 'duration': dur,
                        'gpu_peak': gpu_peak, 'cpu_peak': cpu_peak})
        first = False
        wait_for_cooldown(_read_gpu, pfx)

    # per-model detail report
    report = OUT_DIR / f'{safe}_{MACHINE_ID}_report.md'
    with open(report, 'w') as f:
        f.write(f'# Benchmark Report: {model}\n\n')
        f.write(f'**Date:** {time.strftime("%Y-%m-%d %H:%M")}  \n')
        f.write(f'**Machine:** {MACHINE_DESC}  \n')
        f.write(f'**Endpoint:** {_base_url}  \n')
        f.write(f'**Run platform:** {platform.system()} {platform.release()} {platform.machine()}  \n')
        f.write(f'**Runs per test:** {RUNS}  \n\n---\n\n')
        f.write(f'## Summary\n\n| pp | tg | Duration (s) | GPU peak °C | {CPU_COL} peak |\n')
        f.write('|---:|---:|---:|---:|---:|\n')
        for r in results:
            f.write(f'| {r["pp"]} | {r["tg"]} | {r["duration"]} | {r["gpu_peak"]} | {r["cpu_peak"]} |\n')
        f.write('\n---\n\n')
        for r in results:
            pp, tg = r['pp'], r['tg']
            f.write(f'## pp={pp} tg={tg}\n\n{r["bench_md"]}\n\n')
            f.write(f'### Thermal Data\n\n{mermaid_temps(pp, tg, r["temps"])}\n\n')
            f.write(f'<details><summary>Raw readings</summary>\n\n')
            f.write(f'| Time (s) | GPU °C | GPU util % | GPU W | {CPU_COL} |\n')
            f.write('|---:|---:|---:|---:|---:|\n')
            for rd in r['temps']:
                f.write(f'| {rd[0]} | {rd[1]} | {rd[2]} | {rd[3]} | {rd[4]} |\n')
            f.write('\n</details>\n\n---\n\n')
    print(f'  Report: {report}', flush=True)

    if WRITE_SUMMARY:
        write_summary(model, results, datetime.now())
    else:
        print(f'  Tip: re-run with --write-summary to update {_summary_path()}', flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────

WORKERS = cfg.get('workers', [{'gpu_index': GPU_INDEX, 'gpu_vendor': GPU_VENDOR,
                                'gpu_meta': _gpu_meta, 'base_url': BASE_URL,
                                'label': '', 'models': MODELS}])

if len(WORKERS) > 1:
    print(f'\nStarting parallel benchmark across {len(WORKERS)} GPUs…', flush=True)
    threads = [threading.Thread(target=run_gpu_worker, args=(w,), daemon=True)
               for w in WORKERS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print('\nAll GPU workers done.', flush=True)
else:
    run_gpu_worker(WORKERS[0])

print(f'\n{"═"*56}')
print(f'  All done.  Results in {OUT_DIR}')
if WRITE_SUMMARY:
    print(f'  Summary:  {_summary_path()}')
print(f'{"═"*56}')
