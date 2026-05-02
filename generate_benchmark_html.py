#!/usr/bin/env python3
"""
Generate benchmark.html from model-benchmark-results*.md files.

Usage:
  python3 generate_benchmark_html.py                       # scans current directory
  python3 generate_benchmark_html.py --input-dir ./results
  python3 generate_benchmark_html.py --input-dir ./results --output report.html

Drop any model-benchmark-results-{machine-id}.md file into --input-dir and it
will be auto-detected.  Machine labels are read from each file's H1 heading:
  # Your Hardware Label — Benchmark Summary
"""
import argparse, re, sys, math
from html import escape
from pathlib import Path

def _parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--input-dir', default='.',
                   help='Directory containing model-benchmark-results*.md (default: .)')
    p.add_argument('--output', default=None,
                   help='Output HTML path (default: {input-dir}/benchmark.html)')
    return p.parse_args()

_args    = _parse_args()
BASE_DIR = Path(_args.input_dir).expanduser().resolve()
DEST     = (Path(_args.output).expanduser().resolve()
            if _args.output else BASE_DIR / 'benchmark.html')

# ── Discover machine files ────────────────────────────────────────────────────

def label_from_file(path):
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            if line.startswith('# '):
                heading = line[2:].strip()
                m = re.match(r'^(.+?)\s+[—–-]{1,3}\s+.*(Summary|Benchmark)', heading)
                if m:
                    return m.group(1).strip()
                return heading
    except OSError:
        pass
    return None

def _mid_from_label(label):
    s = label.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s[:40] or 'machine'

def discover_machines():
    machines = []
    seen_ids = set()
    for p in sorted(BASE_DIR.glob('model-benchmark-results-*.md')):
        mid = p.stem.replace('model-benchmark-results-', '')
        label = label_from_file(p) or mid.upper()
        machines.append((mid, label, p))
        seen_ids.add(mid)
    bare = BASE_DIR / 'model-benchmark-results.md'
    if bare.exists():
        label = label_from_file(bare) or 'Machine'
        mid = _mid_from_label(label)
        if mid not in seen_ids:
            machines.insert(0, (mid, label, bare))
    return machines

# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower().strip()).strip('-')

def inline_fmt(text):
    text = escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'~~(.+?)~~', r'<del>\1</del>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    return text

NUMERIC_RE = re.compile(r'^\s*~?[\d,.<>±\s]+\s*(GB|ms|t\/s|°C|%|W|s)?\s*$')
NOWRAP_COLS = {'size', 'date', 'tested from', 'tested', 'duration (s)', 'gpu peak °c',
               'cpu peak °c', 'gpu peak', 'cpu peak', 'pp tok/s', 'ttft (ms)',
               'tg=64 @ pp=512', 'tg=256 @ pp=512', 'tg=64 @ pp=2048', 'tg=256 @ pp=2048',
               'avg tok/s', 'rank', 't/s', 'peak t/s', 'ttfr (ms)',
               'est_ppt (ms)', 'e2e_ttft (ms)'}

def render_table(rows):
    if len(rows) < 2:
        return ''
    headers = [c.strip() for c in rows[0].strip('|').split('|')]
    data_rows = [r for r in rows[2:] if r.strip() and not re.match(r'^\s*\|[-| :]+\|\s*$', r)]
    html = ['<div class="table-wrap"><table class="bench-table"><thead><tr>']
    for h in headers:
        html.append(f'<th>{inline_fmt(h)}</th>')
    html.append('</tr></thead><tbody>')
    for row in data_rows:
        cells = [c.strip() for c in row.strip('|').split('|')]
        html.append('<tr>')
        for i, cell in enumerate(cells):
            col_name = headers[i].lower().strip() if i < len(headers) else ''
            nowrap = col_name in NOWRAP_COLS or bool(NUMERIC_RE.match(cell))
            model_col = i == 0 and any(k in col_name for k in ('model', 'route', 'service'))
            cls = []
            if nowrap: cls.append('nowrap')
            if model_col: cls.append('model-name')
            attr = f' class="{" ".join(cls)}"' if cls else ''
            html.append(f'<td{attr}>{inline_fmt(cell)}</td>')
        html.append('</tr>')
    html.append('</tbody></table></div>')
    return '\n'.join(html)

# ── Domain / size helpers ─────────────────────────────────────────────────────

def parse_size_gb(size_str):
    if not size_str or size_str.strip() in ('?', '—', ''):
        return None
    m = re.search(r'([\d.]+)\s*GB', size_str, re.IGNORECASE)
    return float(m.group(1)) if m else None

_SIZE_BREAKS = [(5, 'XS'), (10, 'S'), (20, 'M'), (40, 'L')]

def size_class(gb):
    if gb is None: return '?'
    for limit, label in _SIZE_BREAKS:
        if gb < limit: return label
    return 'XL'

def domain_of(model_name):
    n = model_name.lower()
    if any(k in n for k in ('coder', 'codestral', 'code-')):
        return 'coding'
    if any(k in n for k in ('deepseek-r1', 'qwq', ':r1')):
        return 'reasoning'
    return 'general'

_DOMAIN_LABEL = {'coding': '💻 Coding', 'reasoning': '🧠 Reasoning', 'general': '💬 General'}

def _clean_model(name):
    return re.sub(r'\s*[⚠️✅⚠]\s*', '', name).strip()

# ── Leaderboard data extractors ───────────────────────────────────────────────

def extract_table_after(md_lines, heading_fragment):
    in_table = False
    rows = []
    for line in md_lines:
        if not in_table and heading_fragment.lower() in line.lower() and line.startswith('#'):
            in_table = True
            continue
        if in_table:
            if line.startswith('|'):
                rows.append(line)
            elif rows:
                break
    return rows

def parse_tg_table(rows):
    results = []
    if len(rows) < 3: return results
    for row in rows[2:]:
        cells = [c.strip() for c in row.strip('|').split('|')]
        if len(cells) < 4: continue
        model   = cells[0].strip()
        size    = cells[1].strip() if len(cells) > 1 else ''
        size_gb = parse_size_gb(size)
        try:
            tg64  = float(re.sub(r'[^\d.]', '', cells[2])) if cells[2].strip() else 0
            tg256 = float(re.sub(r'[^\d.]', '', cells[3])) if cells[3].strip() else 0
        except ValueError:
            continue
        if tg64 == 0 and tg256 == 0: continue
        results.append({'model': model, 'size': size, 'size_gb': size_gb,
                        'tg64': tg64, 'tg256': tg256,
                        'avg': round((tg64 + tg256) / 2, 1)})
    return sorted(results, key=lambda x: x['avg'], reverse=True)

def parse_pp_table(rows):
    results = []
    if len(rows) < 3: return results
    for row in rows[2:]:
        cells = [c.strip() for c in row.strip('|').split('|')]
        if len(cells) < 3: continue
        model = cells[0].strip()
        try:
            pp   = float(re.sub(r'[^\d.]', '', cells[1])) if cells[1].strip() else 0
            ttft = float(re.sub(r'[^\d.,]', '', cells[2]).replace(',','')) if cells[2].strip() else 0
        except ValueError:
            continue
        if pp == 0: continue
        results.append({'model': model, 'pp': pp, 'ttft': ttft})
    return results

def parse_thermal_table(rows):
    results = []
    if len(rows) < 3: return results
    for row in rows[2:]:
        cells = [c.strip() for c in row.strip('|').split('|')]
        if len(cells) < 3: continue
        model = cells[0].strip()
        try:
            gpu = float(re.sub(r'[^\d.]', '', cells[1])) if cells[1].strip() else 0
            cpu = float(re.sub(r'[^\d.]', '', cells[2])) if cells[2].strip() else 0
        except ValueError:
            continue
        if gpu == 0: continue
        results.append({'model': model, 'gpu': gpu, 'cpu': cpu})
    return results

def extract_machine_data(md_text):
    lines = md_text.splitlines()
    return {
        'tg':      parse_tg_table(extract_table_after(lines, 'Generation Throughput')),
        'pp':      parse_pp_table(extract_table_after(lines, 'Prompt Processing Throughput')),
        'thermal': parse_thermal_table(extract_table_after(lines, 'Thermal Profile')),
    }

# ── Chart helpers ─────────────────────────────────────────────────────────────

def medal(rank):
    return ['🥇','🥈','🥉'][rank] if rank < 3 else str(rank + 1)

def make_bar_chart(title, items, unit, lower_is_better=False, color_override=None):
    if not items: return ''
    items_s = sorted(items, key=lambda x: x[1], reverse=not lower_is_better)
    max_val = max(v for _, v in items_s) or 1
    n = len(items_s)
    rows = []
    for i, (label, val) in enumerate(items_s):
        pct   = round(val / max_val * 100, 1)
        clean = re.sub(r'[⚠️✅⚠]', '', label).strip()
        warn  = ' ⚠' if '⚠' in label else ''
        if color_override:
            color = color_override
        else:
            hue   = int(120 - 120 * i / max(n - 1, 1))
            color = f'hsl({hue},55%,42%)'
        fmt = f'{val:,.0f}' if val >= 100 else str(round(val, 1))
        rows.append(
            f'<div class="cbar-row">'
            f'<span class="cbar-label" title="{escape(clean)}">{escape(clean)}{warn}</span>'
            f'<div class="cbar-track">'
            f'<div class="cbar-fill" style="width:{pct}%;background:{color}">'
            f'<span class="cbar-val">{fmt} {unit}</span>'
            f'</div></div></div>'
        )
    return (f'<div class="cbar-wrap"><div class="cbar-title">{escape(title)}</div>'
            + ''.join(rows) + '</div>')

MACHINE_COLORS = ['hsl(210,65%,45%)', 'hsl(30,70%,45%)', 'hsl(270,55%,50%)',
                  'hsl(150,55%,38%)', 'hsl(0,60%,45%)']

def make_grouped_bar_chart(title, by_model, machine_labels, colors,
                           unit='tok/s', lower_is_better=False):
    """Grouped bar chart: models as groups, one colored sub-bar per machine."""
    if not by_model: return ''
    all_vals = [v for mv in by_model.values() for v in mv.values()
                if v is not None and v > 0]
    if not all_vals: return ''
    max_val = max(all_vals) or 1

    sorted_models = sorted(
        by_model.keys(),
        key=lambda m: (max((v for v in by_model[m].values() if v), default=0)),
        reverse=not lower_is_better
    )

    groups = []
    for model in sorted_models:
        vals  = by_model[model]
        clean = _clean_model(model)
        sub   = []
        for i, mlabel in enumerate(machine_labels):
            v = vals.get(mlabel)
            if v is None or v == 0: continue
            pct   = round(v / max_val * 100, 1)
            color = colors[i % len(colors)]
            fmt   = f'{v:,.0f}' if v >= 100 else str(round(v, 1))
            sub.append(
                f'<div class="gbar-row">'
                f'<span class="gbar-mlabel" title="{escape(mlabel)}">{escape(mlabel)}</span>'
                f'<div class="gbar-track">'
                f'<div class="gbar-fill" style="width:{pct}%;background:{color}">'
                f'<span class="gbar-val">{fmt} {unit}</span>'
                f'</div></div></div>'
            )
        if sub:
            groups.append(
                f'<div class="gbar-group">'
                f'<div class="gbar-group-label" title="{escape(clean)}">{escape(clean)}</div>'
                f'<div class="gbar-bars">{"".join(sub)}</div>'
                f'</div>'
            )
    if not groups: return ''
    return (f'<div class="gbar-wrap"><div class="gbar-title">{escape(title)}</div>'
            + ''.join(groups) + '</div>')

# ── Scatter plot (size vs TG speed) ──────────────────────────────────────────

def build_scatter_svg(machines_data):
    points = []
    for i, (mid, mlabel, data) in enumerate(machines_data):
        color = MACHINE_COLORS[i % len(MACHINE_COLORS)]
        for r in data['tg']:
            gb = r.get('size_gb')
            if not gb or r['avg'] <= 0: continue
            points.append({
                'model': _clean_model(r['model']), 'machine': mlabel,
                'x': gb, 'y': r['avg'], 'color': color
            })
    if not points:
        return ''

    W, H   = 680, 340
    PL, PR, PT, PB = 58, 24, 18, 52

    raw_max_x = max(p['x'] for p in points)
    raw_max_y = max(p['y'] for p in points)
    max_x = math.ceil(raw_max_x / 5) * 5 or 10
    max_y = (math.ceil(raw_max_y / 20) * 20) or 20

    iw = W - PL - PR
    ih = H - PT - PB

    def sx(v): return PL + iw * v / max_x
    def sy(v): return PT + ih * (1 - v / max_y)

    lines = [
        f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'xmlns="http://www.w3.org/2000/svg" class="scatter-svg">'
    ]

    # Grid
    x_step = max(1, max_x // 6)
    y_step = max(10, max_y // 5)
    for xv in range(0, max_x + 1, x_step):
        x = sx(xv)
        lines.append(f'<line x1="{x:.1f}" y1="{PT}" x2="{x:.1f}" y2="{PT+ih}" '
                     f'stroke="var(--border)" stroke-width="1" stroke-dasharray="3,3"/>')
        lines.append(f'<text x="{x:.1f}" y="{PT+ih+16}" text-anchor="middle" '
                     f'font-size="10" fill="var(--text)" opacity="0.7">{xv}GB</text>')
    for yv in range(0, max_y + 1, y_step):
        y = sy(yv)
        lines.append(f'<line x1="{PL}" y1="{y:.1f}" x2="{PL+iw}" y2="{y:.1f}" '
                     f'stroke="var(--border)" stroke-width="1" stroke-dasharray="3,3"/>')
        lines.append(f'<text x="{PL-6}" y="{y:.1f}" text-anchor="end" dy="0.35em" '
                     f'font-size="10" fill="var(--text)" opacity="0.7">{yv}</text>')

    # Axes
    lines.append(f'<line x1="{PL}" y1="{PT}" x2="{PL}" y2="{PT+ih}" '
                 f'stroke="var(--text)" stroke-width="1.5" opacity="0.5"/>')
    lines.append(f'<line x1="{PL}" y1="{PT+ih}" x2="{PL+iw}" y2="{PT+ih}" '
                 f'stroke="var(--text)" stroke-width="1.5" opacity="0.5"/>')

    # Axis labels
    cx = PL + iw / 2
    lines.append(f'<text x="{cx:.1f}" y="{H-4}" text-anchor="middle" '
                 f'font-size="11" fill="var(--text)" opacity="0.65">Model Size (GB)</text>')
    cy = PT + ih / 2
    lines.append(f'<text x="11" y="{cy:.1f}" text-anchor="middle" '
                 f'transform="rotate(-90,11,{cy:.1f})" '
                 f'font-size="11" fill="var(--text)" opacity="0.65">TG tok/s (avg)</text>')

    # Points
    for p in points:
        x, y = sx(p['x']), sy(p['y'])
        tip  = escape(f'{p["model"]} @ {p["machine"]}: {p["x"]}GB → {p["y"]} tok/s')
        lines.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{p["color"]}" '
            f'opacity="0.82" stroke="var(--bg)" stroke-width="1.5">'
            f'<title>{tip}</title></circle>'
        )

    lines.append('</svg>')
    return '\n'.join(lines)

# ── All-models supergrid ──────────────────────────────────────────────────────

def build_supergrid(machines_data, all_models_info, grid):
    machine_list = [(mid, mlabel) for mid, mlabel, _ in machines_data]
    sorted_models = sorted(
        all_models_info.keys(),
        key=lambda m: all_models_info[m]['best_tg'], reverse=True
    )

    h  = '<h3 id="supergrid">All Models — Full Grid</h3>\n'
    h += '<p><strong>—</strong> = not tested on this machine. Bold = fastest for that model.</p>\n'
    h += '<div class="table-wrap"><table class="bench-table supergrid-table"><thead>'
    h += '<tr><th rowspan="2">Model</th><th rowspan="2">Size</th>'
    h += '<th rowspan="2" class="nowrap">Class</th>'
    for mid, mlabel in machine_list:
        h += f'<th colspan="3" class="sg-machine-header">{escape(mlabel)}</th>'
    h += '</tr><tr>'
    for _ in machine_list:
        h += ('<th class="nowrap sg-metric">TG tok/s</th>'
              '<th class="nowrap sg-metric">PP tok/s</th>'
              '<th class="nowrap sg-metric">TTFT ms</th>')
    h += '</tr></thead><tbody>'

    for model in sorted_models:
        info = all_models_info[model]
        dom  = info['domain']
        sc   = info['sc']
        # Find best per metric across machines for bolding
        m_cells = {mid: grid.get(model, {}).get(mid) for mid, _ in machine_list}
        tg_vals  = {mid: c['tg']   for mid, c in m_cells.items() if c and c.get('tg')}
        pp_vals  = {mid: c['pp']   for mid, c in m_cells.items() if c and c.get('pp')}
        ttft_vals= {mid: c['ttft'] for mid, c in m_cells.items() if c and c.get('ttft')}
        best_tg_mid   = max(tg_vals,   key=tg_vals.get)   if tg_vals   else None
        best_pp_mid   = max(pp_vals,   key=pp_vals.get)   if pp_vals   else None
        best_ttft_mid = min(ttft_vals, key=ttft_vals.get) if ttft_vals else None

        h += f'<tr data-domain="{dom}" data-sc="{sc}">'
        h += f'<td class="model-name">{escape(model)}</td>'
        h += f'<td class="nowrap">{escape(info["size"])}</td>'
        sc_cls = sc.lower() if sc != '?' else 'unknown'
        h += f'<td class="nowrap sc-chip sc-{sc_cls}">{sc}</td>'
        for mid, mlabel in machine_list:
            cell = m_cells.get(mid)
            if not cell:
                h += '<td class="nowrap sg-dash">—</td><td class="nowrap sg-dash">—</td><td class="nowrap sg-dash">—</td>'
                continue
            tg   = cell.get('tg')
            pp   = cell.get('pp')
            ttft = cell.get('ttft')
            tg_s   = (f'<strong>{tg}</strong>'   if mid == best_tg_mid   else str(tg))   if tg   else '—'
            pp_s   = (f'<strong>{pp:,.0f}</strong>' if mid == best_pp_mid else f'{pp:,.0f}') if pp   else '—'
            ttft_s = (f'<strong>{ttft:,.0f}</strong>' if mid == best_ttft_mid else f'{ttft:,.0f}') if ttft else '—'
            h += f'<td class="nowrap">{tg_s}</td><td class="nowrap">{pp_s}</td><td class="nowrap">{ttft_s}</td>'
        h += '</tr>'

    h += '</tbody></table></div>'
    return h

# ── Per-model accordion (comparison tab) ─────────────────────────────────────

def build_model_accordion(machines_data, shared_models, all_models_info, grid):
    if not shared_models:
        return ''
    machine_list = [(mid, mlabel) for mid, mlabel, _ in machines_data]

    h  = '<h3 id="model-detail">Per-Model Detail</h3>\n'
    h += '<p>Click a model to see machine-by-machine breakdown.</p>\n'

    for model in shared_models:
        info = all_models_info.get(model, {})
        sc   = info.get('sc', '?')
        dom  = info.get('domain', 'general')
        best = info.get('best_tg', 0)
        dom_label = _DOMAIN_LABEL.get(dom, dom)

        h += '<details class="cmp-accordion-row">\n'
        h += '<summary class="cmp-acc-summary">'
        h += f'<span class="model-name">{escape(model)}</span>'
        h += f'<span class="sc-chip sc-{sc.lower()}">{sc}</span>'
        h += f'<span class="dom-chip dom-{dom}">{dom_label}</span>'
        h += f'<span class="acc-best-tg">Best TG: {best} tok/s</span>'
        h += '</summary>\n'
        h += '<div class="cmp-acc-body">'
        h += '<table class="bench-table"><thead><tr>'
        h += '<th>Machine</th><th class="nowrap">TG tok/s</th>'
        h += '<th class="nowrap">PP tok/s</th><th class="nowrap">TTFT ms</th>'
        h += '</tr></thead><tbody>'
        for mid, mlabel in machine_list:
            cell = grid.get(model, {}).get(mid)
            if not cell: continue
            tg   = str(cell['tg'])   if cell.get('tg')   else '—'
            pp   = f"{cell['pp']:,.0f}" if cell.get('pp')   else '—'
            ttft = f"{cell['ttft']:,.0f}" if cell.get('ttft') else '—'
            h += (f'<tr><td>{escape(mlabel)}</td>'
                  f'<td class="nowrap">{tg}</td>'
                  f'<td class="nowrap">{pp}</td>'
                  f'<td class="nowrap">{ttft}</td></tr>')
        h += '</tbody></table>'
        h += '</div></details>\n'

    return h

# ── Per-machine leaderboard ───────────────────────────────────────────────────

def build_leaderboard(md_lines, mid=''):
    tg_rows      = extract_table_after(md_lines, 'Generation Throughput')
    pp_rows      = extract_table_after(md_lines, 'Prompt Processing Throughput')
    thermal_rows = extract_table_after(md_lines, 'Thermal Profile')

    tg_data      = parse_tg_table(tg_rows)
    pp_data      = sorted(parse_pp_table(pp_rows), key=lambda x: x['pp'], reverse=True)
    ttft_data    = sorted(parse_pp_table(pp_rows), key=lambda x: x['ttft'])
    thermal_data = parse_thermal_table(thermal_rows)

    eff_data = sorted(
        [{'model': r['model'], 'size': r['size'], 'size_gb': r['size_gb'],
          'avg': r['avg'], 'eff': round(r['avg'] / r['size_gb'], 2)}
         for r in tg_data if r['size_gb'] and r['size_gb'] > 0],
        key=lambda x: x['eff'], reverse=True
    )

    p = (mid + '-') if mid else ''

    def lb_section(anchor, title, headers, rows_fn, data, chart_html='', extra=''):
        tid = f'{p}{anchor}-tbl'
        h  = f'<section id="{anchor}" class="leaderboard-block">\n<h3>{title}</h3>\n'
        if extra: h += extra
        h += '<div class="lb-row">'
        h += (f'<div class="table-wrap"><table id="{tid}" '
              f'class="bench-table leaderboard-table sortable"><thead><tr>')
        h += ''.join(f'<th>{c}</th>' for c in headers)
        h += '</tr></thead><tbody>\n'
        for i, row in enumerate(data):
            result = rows_fn(i, row)
            if isinstance(result, tuple):
                attrs, cells = result
                h += f'<tr {attrs}>{cells}</tr>\n'
            else:
                h += f'<tr>{result}</tr>\n'
        h += '</tbody></table></div>'
        if chart_html:
            h += f'<div class="lb-chart">{chart_html}</div>'
        h += '</div></section>\n'
        return h

    def tg_row(i, r):
        sc   = size_class(r['size_gb'])
        dom  = domain_of(r['model'])
        warn = ' ⚠' if '⚠' in r['model'] else ''
        return (
            f'data-sc="{sc}" data-domain="{dom}"',
            f'<td class="nowrap" data-val="{i}">{medal(i)}</td>'
            f'<td class="model-name">{escape(r["model"])}{warn}</td>'
            f'<td class="nowrap" data-val="{r["size_gb"] or 0}">{escape(r["size"])}</td>'
            f'<td class="nowrap"><span class="sc-chip sc-{sc.lower() if sc != "?" else "unknown"}">{sc}</span></td>'
            f'<td class="nowrap" data-val="{r["tg64"]}">{r["tg64"]}</td>'
            f'<td class="nowrap" data-val="{r["tg256"]}">{r["tg256"]}</td>'
            f'<td class="nowrap" data-val="{r["avg"]}"><strong>{r["avg"]}</strong></td>'
        )

    def pp_row(i, r):
        dom  = domain_of(r['model'])
        warn = ' ⚠' if '⚠' in r['model'] else ''
        return (
            f'data-domain="{dom}"',
            f'<td class="nowrap" data-val="{i}">{medal(i)}</td>'
            f'<td class="model-name">{escape(r["model"])}{warn}</td>'
            f'<td class="nowrap" data-val="{r["pp"]}"><strong>{r["pp"]:,.0f}</strong></td>'
            f'<td class="nowrap" data-val="{r["ttft"]}">{r["ttft"]:,.0f}</td>'
        )

    def ttft_row(i, r):
        dom  = domain_of(r['model'])
        warn = ' ⚠' if '⚠' in r['model'] else ''
        return (
            f'data-domain="{dom}"',
            f'<td class="nowrap" data-val="{i}">{medal(i)}</td>'
            f'<td class="model-name">{escape(r["model"])}{warn}</td>'
            f'<td class="nowrap" data-val="{r["ttft"]}"><strong>{r["ttft"]:,.0f}</strong></td>'
            f'<td class="nowrap" data-val="{r["pp"]}">{r["pp"]:,.0f}</td>'
        )

    def eff_row(i, r):
        sc  = size_class(r['size_gb'])
        dom = domain_of(r['model'])
        return (
            f'data-sc="{sc}" data-domain="{dom}"',
            f'<td class="nowrap" data-val="{i}">{medal(i)}</td>'
            f'<td class="model-name">{escape(r["model"])}</td>'
            f'<td class="nowrap" data-val="{r["size_gb"]}">{escape(r["size"])}</td>'
            f'<td class="nowrap" data-val="{r["avg"]}">{r["avg"]}</td>'
            f'<td class="nowrap" data-val="{r["eff"]}"><strong>{r["eff"]}</strong></td>'
        )

    tg_chart   = make_bar_chart('TG Speed (avg tok/s)',
                                [(r['model'], r['avg']) for r in tg_data], 'tok/s')
    pp_chart   = make_bar_chart('PP Speed (tok/s @ pp=512)',
                                [(r['model'], r['pp']) for r in pp_data], 'tok/s')
    ttft_chart = make_bar_chart('TTFT (ms, lower = faster)',
                                [(r['model'], r['ttft']) for r in ttft_data],
                                'ms', lower_is_better=True)
    eff_chart  = make_bar_chart('Efficiency (tok/s per GB)',
                                [(r['model'], r['eff']) for r in eff_data], 'tok/s/GB')

    # Domain filter pills (applied to all leaderboard tables in pane via JS)
    all_domains = sorted(set(domain_of(r['model']) for r in tg_data))
    if len(all_domains) > 1:
        dom_btns = '<button class="lb-domain-btn active" data-domain="all">All</button>'
        for d in all_domains:
            dom_btns += (f'<button class="lb-domain-btn" data-domain="{d}">'
                         f'{_DOMAIN_LABEL.get(d, d)}</button>')
        dom_filter = f'<div class="filter-pills lb-domain-filter">{dom_btns}</div>'
    else:
        dom_filter = ''

    # Size-class filter pills for TG section
    present_sc = [sc for sc in ['XS', 'S', 'M', 'L', 'XL', '?']
                  if any(size_class(r['size_gb']) == sc for r in tg_data)]
    if len(present_sc) > 1:
        sc_btns = f'<button class="sc-filter-btn active" data-sc="all" data-mid="{mid}">All</button>'
        for sc in present_sc:
            sc_btns += f'<button class="sc-filter-btn" data-sc="{sc}" data-mid="{mid}">{sc}</button>'
        sc_filter = f'<div class="filter-pills sc-filter">{sc_btns}</div>'
    else:
        sc_filter = ''

    out  = f'<section id="leaderboard">\n<h2>Leaderboard</h2>\n'
    out += dom_filter
    out += lb_section('lb-tg', '⚡ TG Speed (tok/s)',
                      ['Rank','Model','Size','Class','tg=64','tg=256','Avg'],
                      tg_row, tg_data, tg_chart, extra=sc_filter)
    out += lb_section('lb-pp', '🚀 PP Speed (tok/s @ pp=512)',
                      ['Rank','Model','PP tok/s','TTFT (ms)'],
                      pp_row, pp_data, pp_chart)
    out += lb_section('lb-ttft', '⏱ TTFT — fastest first (ms)',
                      ['Rank','Model','TTFT (ms)','PP tok/s'],
                      ttft_row, ttft_data, ttft_chart)
    if eff_data:
        out += lb_section('lb-eff', '📐 Efficiency (tok/s per GB)',
                          ['Rank','Model','Size','TG avg','tok/s per GB'],
                          eff_row, eff_data, eff_chart)
    out += '</section>\n'

    cross_charts = {
        'generation throughput': make_bar_chart(
            'TG Speed comparison (avg tok/s @ pp=512)',
            [(r['model'], r['avg']) for r in tg_data], 'tok/s'),
        'prompt processing throughput': make_bar_chart(
            'PP Speed comparison (tok/s @ pp=512)',
            [(r['model'], r['pp']) for r in pp_data], 'tok/s'),
        'thermal profile': (
            make_bar_chart('GPU Peak Temperature',
                [(r['model'], r['gpu']) for r in thermal_data], '°C', lower_is_better=True)
            + make_bar_chart('CPU Peak Temperature',
                [(r['model'], r['cpu']) for r in thermal_data], '°C', lower_is_better=True)
        ),
    }
    return out, cross_charts

# ── Cross-machine comparison ──────────────────────────────────────────────────

def build_comparison(machines_data):
    if len(machines_data) < 2:
        return ''

    tg_by_model   = {}
    pp_by_model   = {}
    ttft_by_model = {}
    all_models_info = {}
    grid = {}

    for i, (mid, mlabel, data) in enumerate(machines_data):
        for r in data['tg']:
            key = _clean_model(r['model'])
            tg_by_model.setdefault(key, {})[mlabel] = r['avg']
            if key not in all_models_info:
                all_models_info[key] = {
                    'size': r['size'], 'size_gb': r['size_gb'],
                    'sc': size_class(r['size_gb']),
                    'domain': domain_of(key), 'best_tg': 0,
                }
            all_models_info[key]['best_tg'] = max(
                all_models_info[key]['best_tg'], r['avg'])
            grid.setdefault(key, {}).setdefault(mid, {})['tg'] = r['avg']
            # preserve size on grid entry
            grid[key][mid]['size']    = r['size']
            grid[key][mid]['size_gb'] = r['size_gb']

        for r in data['pp']:
            key = _clean_model(r['model'])
            if r['pp'] > 0:
                pp_by_model.setdefault(key, {})[mlabel] = r['pp']
            if r['ttft'] > 0:
                ttft_by_model.setdefault(key, {})[mlabel] = r['ttft']
            grid.setdefault(key, {}).setdefault(mid, {})
            grid[key][mid]['pp']   = r['pp']   if r['pp']   > 0 else None
            grid[key][mid]['ttft'] = r['ttft'] if r['ttft'] > 0 else None

    machine_labels = [mlabel for _, mlabel, _ in machines_data]
    colors         = MACHINE_COLORS

    shared_tg   = {m: v for m, v in tg_by_model.items()   if len(v) >= 2}
    shared_pp   = {m: v for m, v in pp_by_model.items()   if len(v) >= 2}
    shared_ttft = {m: v for m, v in ttft_by_model.items() if len(v) >= 2}
    shared_models = sorted(
        set(shared_tg) | set(shared_pp),
        key=lambda m: all_models_info.get(m, {}).get('best_tg', 0), reverse=True
    )

    # Machine color legend
    legend = '<div class="machine-legend">'
    for i, (mid, mlabel, _) in enumerate(machines_data):
        color = colors[i % len(colors)]
        legend += (f'<span class="legend-dot" style="background:{color}"></span>'
                   f'<span class="legend-label">{escape(mlabel)}</span>')
    legend += '</div>'

    out  = '<section id="comparison">\n'
    out += '<h2>⚖ Cross-Machine Comparison</h2>\n'
    out += legend

    # 1. All-models supergrid
    out += build_supergrid(machines_data, all_models_info, grid)

    # 2. Grouped bar charts (models on 2+ machines)
    if shared_tg or shared_pp or shared_ttft:
        out += '<h3 id="cross-charts">Cross-Machine Charts</h3>\n'
        out += '<p><strong>Bold</strong> = fastest for that model across machines.</p>\n'
        charts_html = ''
        if shared_tg:
            charts_html += make_grouped_bar_chart(
                'TG Speed (avg tok/s)', shared_tg, machine_labels, colors, 'tok/s')
        if shared_pp:
            charts_html += make_grouped_bar_chart(
                'PP Speed (tok/s @ pp=512)', shared_pp, machine_labels, colors, 'tok/s')
        if shared_ttft:
            charts_html += make_grouped_bar_chart(
                'TTFT (ms — lower is faster)', shared_ttft, machine_labels, colors,
                'ms', lower_is_better=True)
        out += f'<div class="gbar-grid">{charts_html}</div>'

    # 3. Scatter plot
    scatter = build_scatter_svg(machines_data)
    if scatter:
        out += '<h3 id="scatter">Size vs Speed</h3>\n'
        out += '<p>Model size (GB) vs. avg TG speed. Hover a dot for details. Only models with known size shown.</p>\n'
        out += scatter

    # 4. Per-model accordion
    acc = build_model_accordion(machines_data, shared_models, all_models_info, grid)
    if acc:
        out += acc

    if not shared_tg and not shared_pp:
        out += '<p><em>No models tested on multiple machines yet.</em></p>\n'

    out += '</section>\n'
    return out

# ── Markdown parser ───────────────────────────────────────────────────────────

def parse_markdown(md_text, cross_charts=None):
    lines = md_text.splitlines()
    out   = []
    model_sections = []

    in_front_matter   = False
    front_matter_done = False
    in_mermaid        = False
    mermaid_buf       = []
    in_code           = False
    code_buf          = []
    code_lang         = ''
    in_list           = False
    table_buf         = []
    in_model_section  = False
    first_model       = True
    pending_chart     = None

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    def flush_table():
        nonlocal table_buf, pending_chart
        if table_buf:
            out.append(render_table(table_buf))
            table_buf = []
            if pending_chart:
                out.append(pending_chart)
                pending_chart = None

    for line in lines:
        raw = line

        if not front_matter_done:
            if line.strip() == '---':
                if not in_front_matter:
                    in_front_matter = True
                    continue
                else:
                    in_front_matter = False
                    front_matter_done = True
                    continue
            if in_front_matter:
                continue

        if line.strip().startswith('```mermaid'):
            flush_list(); flush_table()
            in_mermaid = True
            mermaid_buf = []
            continue
        if in_mermaid:
            if line.strip() == '```':
                src = escape('\n'.join(mermaid_buf))
                out.append(f'<div class="mermaid" data-src="{src}">{src}</div>')
                in_mermaid = False
                mermaid_buf = []
            else:
                mermaid_buf.append(line)
            continue

        if line.strip().startswith('```') and not in_code:
            flush_list(); flush_table()
            code_lang = line.strip()[3:].strip()
            in_code = True
            code_buf = []
            continue
        if in_code:
            if line.strip() == '```':
                lang_attr = f' class="language-{escape(code_lang)}"' if code_lang else ''
                out.append(f'<pre><code{lang_attr}>{escape(chr(10).join(code_buf))}</code></pre>')
                in_code = False
                code_buf = []
            else:
                code_buf.append(line)
            continue

        stripped = line.strip()
        if stripped.startswith('<details') or stripped.startswith('</details') or \
           stripped.startswith('<summary') or stripped.startswith('</summary'):
            flush_list(); flush_table()
            out.append(raw)
            continue

        if stripped.startswith('|'):
            flush_list()
            table_buf.append(raw)
            continue
        else:
            flush_table()

        if stripped.startswith('> ⚠') or stripped.startswith('> ⚠️'):
            flush_list()
            text = re.sub(r'^>\s*⚠️?\s*', '', stripped)
            out.append(f'<div class="warning-note"><span class="warn-icon">⚠️</span>'
                       f'<span class="warn-text">{inline_fmt(text)}</span></div>')
            continue
        if stripped.startswith('>'):
            flush_list()
            text = re.sub(r'^>\s*', '', stripped)
            out.append(f'<blockquote>{inline_fmt(text)}</blockquote>')
            continue

        h_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if h_match:
            flush_list(); flush_table()
            level = len(h_match.group(1))
            title = h_match.group(2).strip()
            sid   = slugify(title)

            if cross_charts and not in_model_section:
                for key, chart_html in cross_charts.items():
                    if key in title.lower():
                        pending_chart = chart_html
                        break

            if level == 1 and title.startswith('Benchmark Report:'):
                if in_model_section:
                    out.append('</details></section>')
                model_name = title.replace('Benchmark Report:', '').strip()
                model_id   = 'model-' + slugify(model_name)
                dom        = domain_of(model_name)
                model_sections.append({'id': model_id, 'title': model_name, 'domain': dom})
                open_attr = ' open' if first_model else ''
                first_model = False
                in_model_section = True
                out.append(f'<section id="{model_id}" class="model-section" data-domain="{dom}">')
                out.append(f'<details{open_attr}>'
                           f'<summary class="model-header"><h2>{escape(title)}</h2></summary>')
                continue

            if level == 1:
                if in_model_section:
                    out.append('</details></section>')
                    in_model_section = False
                out.append(f'<section id="{sid}"><h1>{inline_fmt(title)}</h1>')
                continue

            tag = f'h{min(level + 1, 6)}'
            out.append(f'<{tag} id="{sid}">{inline_fmt(title)}</{tag}>')
            continue

        li_match = re.match(r'^[-*]\s+(.+)$', stripped)
        if li_match:
            if not in_list:
                out.append('<ul>')
                in_list = True
            out.append(f'<li>{inline_fmt(li_match.group(1))}</li>')
            continue

        if not stripped:
            flush_list()
            out.append('')
            continue

        if re.match(r'^[-*]{3,}$', stripped):
            flush_list()
            out.append('<hr>')
            continue

        flush_list()
        out.append(f'<p>{inline_fmt(stripped)}</p>')

    flush_list(); flush_table()
    if in_model_section:
        out.append('</details></section>')

    return '\n'.join(out), model_sections

# ── Domain filter pills for model sections ────────────────────────────────────

def build_model_domain_pills(mid, model_sections):
    domains = sorted(set(m['domain'] for m in model_sections))
    if len(domains) <= 1:
        return ''
    h = '<div class="filter-pills model-domain-filter">'
    h += f'<button class="model-domain-btn active" data-mid="{mid}" data-domain="all">All Models</button>'
    for d in domains:
        h += (f'<button class="model-domain-btn" data-mid="{mid}" data-domain="{d}">'
              f'{_DOMAIN_LABEL.get(d, d)}</button>')
    h += '</div>'
    return h

# ── Navigation ────────────────────────────────────────────────────────────────

def build_nav(machines_with_sections, has_comparison):
    parts = []
    parts.append('''
<div class="theme-wrap">
  <button id="theme-btn">☀️ Light ▾</button>
  <div id="theme-menu">
    <button onclick="applyTheme('light')">☀️ Light</button>
    <button onclick="applyTheme('dark')">🌙 Dark</button>
    <button onclick="applyTheme('solar')">🌤 Solar</button>
    <button onclick="applyTheme('twilight')">🌆 Twilight</button>
  </div>
</div>''')

    if len(machines_with_sections) > 1:
        parts.append('<div class="nav-section">Machines</div>')
        for mid, mlabel, _ in machines_with_sections:
            parts.append(
                f'<a class="nav-link nav-machine" href="#" '
                f'data-tab="{mid}">{escape(mlabel)}</a>'
            )
        if has_comparison:
            parts.append('<a class="nav-link nav-machine" href="#" data-tab="compare">⚖ Compare</a>')

    parts.append('<div class="nav-section">Pages</div>')
    parts.append('<a class="nav-link nav-tablink" data-tab-link href="#leaderboard" data-section="leaderboard">Leaderboard</a>')
    parts.append('<div class="nav-section">Tables</div>')
    parts.append('<a class="nav-link nav-sub nav-tablink" data-tab-link href="#lb-tg" data-section="lb-tg">⚡ TG Speed</a>')
    parts.append('<a class="nav-link nav-sub nav-tablink" data-tab-link href="#lb-pp" data-section="lb-pp">🚀 PP Speed</a>')
    parts.append('<a class="nav-link nav-sub nav-tablink" data-tab-link href="#lb-ttft" data-section="lb-ttft">⏱ TTFT</a>')
    parts.append('<a class="nav-link nav-sub nav-tablink" data-tab-link href="#lb-eff" data-section="lb-eff">📐 Efficiency</a>')
    if has_comparison:
        parts.append('<a class="nav-link nav-sub" href="#" data-tab="compare" data-section="comparison">⚖ Cross-Machine</a>')
        parts.append('<a class="nav-link nav-sub nav-cmp" href="#" data-tab="compare" data-section="supergrid"> ↳ All-Models Grid</a>')
        parts.append('<a class="nav-link nav-sub nav-cmp" href="#" data-tab="compare" data-section="scatter"> ↳ Size vs Speed</a>')

    for mid, mlabel, model_sections in machines_with_sections:
        parts.append(f'<div class="nav-section nav-models-{mid}">Models — {escape(mlabel)}</div>')
        for m in model_sections:
            label = escape(m['title'])
            warn  = ' <span class="warn-badge">⚠</span>' if '⚠' in m['title'] else ''
            dom   = m.get('domain', 'general')
            parts.append(
                f'<a class="nav-link nav-sub nav-models-{mid} nav-model-{dom}" '
                f'href="#{m["id"]}" data-section="{m["id"]}">{label}{warn}</a>'
            )

    return '\n'.join(parts)

# ── HTML template ─────────────────────────────────────────────────────────────

CSS = '''
*, *::before, *::after { box-sizing: border-box; }

[data-theme="light"]   { --bg:#ffffff; --text:#1a1a1a; --accent:#2980b9; --sidebar:#f5f7fa;
                         --border:#d0d7de; --th-bg:#eef2f7; --row-alt:#f8fafc;
                         --warn-bg:#fff8e1; --warn-border:#f39c12; --code-bg:#f4f4f4;
                         --hr:#e0e0e0; --summary-bg:#eef2f7; --link:#2980b9;
                         --tab-active:#2980b9; --tab-active-text:#fff; }
[data-theme="dark"]    { --bg:#1e1e2e; --text:#cdd6f4; --accent:#89b4fa; --sidebar:#181825;
                         --border:#45475a; --th-bg:#313244; --row-alt:#252535;
                         --warn-bg:#2a2000; --warn-border:#f9e04b; --code-bg:#313244;
                         --hr:#45475a; --summary-bg:#313244; --link:#89b4fa;
                         --tab-active:#89b4fa; --tab-active-text:#1e1e2e; }
[data-theme="solar"]   { --bg:#fdf6e3; --text:#657b83; --accent:#268bd2; --sidebar:#eee8d5;
                         --border:#ccc4b0; --th-bg:#e8e0cc; --row-alt:#f5f0e4;
                         --warn-bg:#f7f3e0; --warn-border:#cb4b16; --code-bg:#eee8d5;
                         --hr:#ccc4b0; --summary-bg:#e8e0cc; --link:#268bd2;
                         --tab-active:#268bd2; --tab-active-text:#fff; }
[data-theme="twilight"] { --bg:#2d2a3e; --text:#d4bfff; --accent:#c084fc; --sidebar:#231f35;
                          --border:#4a4560; --th-bg:#3a3550; --row-alt:#322f45;
                          --warn-bg:#2a1f3d; --warn-border:#a855f7; --code-bg:#3a3550;
                          --hr:#4a4560; --summary-bg:#3a3550; --link:#c084fc;
                          --tab-active:#c084fc; --tab-active-text:#2d2a3e; }

html, body { margin:0; padding:0; background:var(--bg); color:var(--text);
             font-family:system-ui,-apple-system,sans-serif; font-size:15px;
             line-height:1.6; }
a { color:var(--link); }
body { display:flex; min-height:100vh; }

/* ── Sidebar ── */
#sidebar { width:230px; min-width:230px; height:100vh; position:sticky; top:0;
           overflow-y:auto; background:var(--sidebar); border-right:1px solid var(--border);
           padding-bottom:2rem; }
.nav-section { padding:10px 16px 2px; font-size:0.7em; font-weight:700;
               text-transform:uppercase; letter-spacing:.06em; opacity:.55; }
.nav-link { display:block; padding:4px 16px; color:var(--text); text-decoration:none;
            font-size:0.85em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.nav-link:hover { color:var(--accent); background:rgba(128,128,128,.1); }
.nav-link.active { color:var(--accent); font-weight:600;
                   border-left:3px solid var(--accent); padding-left:13px; }
.nav-sub { padding-left:28px; }
.nav-sub.active { padding-left:25px; }
.nav-machine { font-weight:600; font-size:0.82em; }
.nav-machine.active { color:var(--tab-active); }
.nav-cmp { font-size:0.78em; opacity:.8; }
.warn-badge { font-size:.8em; }

/* ── Theme switcher ── */
.theme-wrap { padding:12px 12px 8px; position:relative; }
#theme-btn { width:100%; padding:6px 10px; background:var(--th-bg);
             border:1px solid var(--border); color:var(--text); cursor:pointer;
             border-radius:5px; font-size:0.85em; text-align:left; }
#theme-menu { display:none; position:absolute; left:12px; right:12px; top:100%;
              background:var(--sidebar); border:1px solid var(--border);
              border-radius:5px; z-index:200; box-shadow:0 4px 12px rgba(0,0,0,.2); }
#theme-menu.open { display:block; }
#theme-menu button { display:block; width:100%; padding:7px 14px; background:none;
                     border:none; color:var(--text); cursor:pointer; text-align:left;
                     font-size:0.85em; }
#theme-menu button:hover { background:rgba(128,128,128,.15); }

/* ── Machine tab bar ── */
.machine-tab-bar { display:flex; gap:6px; flex-wrap:wrap;
                   padding:14px 0 10px; border-bottom:1px solid var(--border);
                   margin-bottom:1.5rem; }
.machine-tab-bar button { padding:7px 18px; border:1px solid var(--border);
                           background:var(--th-bg); color:var(--text); cursor:pointer;
                           border-radius:20px; font-size:0.85em; font-weight:500;
                           transition:background .15s, color .15s; }
.machine-tab-bar button:hover { border-color:var(--accent); color:var(--accent); }
.machine-tab-bar button.active { background:var(--tab-active); color:var(--tab-active-text);
                                  border-color:var(--tab-active); font-weight:700; }

/* ── Main content ── */
#content { flex:1; padding:2rem 2.5rem; max-width:1300px; overflow-x:hidden; }
#content section { margin-bottom:2.5rem; }
.tab-pane { display:none; }
.tab-pane.active { display:block; }
h1 { font-size:1.6em; border-bottom:2px solid var(--border); padding-bottom:.4em; }
h2 { font-size:1.3em; border-bottom:1px solid var(--border); padding-bottom:.3em; }
h3 { font-size:1.1em; }
h4 { font-size:1em; opacity:.85; }
hr { border:none; border-top:1px solid var(--hr); margin:1.5rem 0; }
code { background:var(--code-bg); padding:1px 5px; border-radius:3px;
       font-family:monospace; font-size:.88em; }
pre { background:var(--code-bg); padding:1rem; border-radius:6px; overflow-x:auto; }
pre code { background:none; padding:0; }
blockquote { border-left:4px solid var(--border); margin:1em 0; padding:.5em 1em;
             opacity:.8; }

/* ── Tables ── */
.table-wrap { overflow-x:auto; margin:1rem 0; }
.bench-table { border-collapse:collapse; width:100%; font-size:.88em; }
.bench-table th, .bench-table td { border:1px solid var(--border); padding:6px 12px;
                                    text-align:left; vertical-align:top; }
.bench-table th { background:var(--th-bg); font-weight:600; white-space:nowrap; }
.bench-table tbody tr:nth-child(even) td { background:var(--row-alt); }
.bench-table tbody tr:hover td { filter:brightness(.97); }
td.nowrap, th.nowrap { white-space:nowrap; }
td.model-name { font-family:monospace; font-size:.85em; white-space:nowrap; }
.leaderboard-table td:first-child { text-align:center; font-size:1.1em; }

/* ── Sortable tables ── */
.bench-table.sortable th { cursor:pointer; user-select:none; }
.bench-table.sortable th:hover { color:var(--accent); }
.sort-arrow { margin-left:4px; font-size:.75em; opacity:.8; }

/* ── Filter pills ── */
.filter-pills { display:flex; gap:5px; flex-wrap:wrap; margin:.6rem 0; }
.filter-pills button { padding:3px 11px; border:1px solid var(--border);
                       background:var(--th-bg); color:var(--text); cursor:pointer;
                       border-radius:12px; font-size:.78em; white-space:nowrap;
                       transition:background .12s, color .12s; }
.filter-pills button:hover { border-color:var(--accent); color:var(--accent); }
.filter-pills button.active { background:var(--tab-active); color:var(--tab-active-text);
                               border-color:var(--tab-active); font-weight:600; }

/* ── Size-class chips ── */
.sc-chip { display:inline-block; padding:1px 6px; border-radius:9px; font-size:.72em;
           font-weight:700; border:1px solid var(--border); white-space:nowrap; }
.sc-xs { background:rgba(39,174,96,.15); }
.sc-s  { background:rgba(52,152,219,.15); }
.sc-m  { background:rgba(243,156,18,.15); }
.sc-l  { background:rgba(231,76,60,.15); }
.sc-xl { background:rgba(142,68,173,.15); }
.sc-unknown { opacity:.5; }

/* ── Domain chips ── */
.dom-chip { display:inline-block; padding:1px 7px; border-radius:9px; font-size:.72em;
            border:1px solid var(--border); white-space:nowrap; }
.dom-coding    { background:rgba(52,152,219,.14); }
.dom-reasoning { background:rgba(155,89,182,.14); }
.dom-general   { background:rgba(39,174,96,.12); }

/* ── Mermaid charts ── */
.mermaid { margin:1rem 0; }
.mermaid svg { width:100%; max-width:860px; height:auto; }

/* ── Warning notes ── */
.warning-note { display:flex; gap:.6em; align-items:flex-start;
                background:var(--warn-bg); border-left:4px solid var(--warn-border);
                padding:.7em 1em; margin:.8em 0; border-radius:0 5px 5px 0;
                line-height:1.6; font-size:.91em; }
.warn-icon { flex-shrink:0; font-size:1.1em; }

/* ── Model sections ── */
.model-section { margin-bottom:1.5rem; border:1px solid var(--border);
                 border-radius:6px; overflow:hidden; }
.model-section details > summary { list-style:none; cursor:pointer;
                                    padding:.7em 1em; background:var(--summary-bg); }
.model-section details > summary::-webkit-details-marker { display:none; }
.model-section details > summary h2 { display:inline; margin:0; font-size:1.1em;
                                       border:none; padding:0; }
.model-section details > summary::before { content:"▶ "; font-size:.8em; opacity:.6; }
.model-section details[open] > summary::before { content:"▼ "; }
.model-section details > *:not(summary) { padding:0 1.2rem; }

/* ── Leaderboard layout ── */
.leaderboard-block { margin-bottom:1.5rem; }
.lb-row { display:flex; gap:2rem; align-items:flex-start; flex-wrap:wrap; }
.lb-row .table-wrap { flex:0 0 auto; }
.lb-chart { flex:1; min-width:260px; }

/* ── Bar charts ── */
.cbar-wrap { margin:.5rem 0 1.5rem; }
.cbar-title { font-size:.8em; font-weight:600; opacity:.65; margin-bottom:.5em;
              text-transform:uppercase; letter-spacing:.04em; }
.cbar-row { display:flex; align-items:center; margin:3px 0; gap:8px; font-size:.8em; }
.cbar-label { width:200px; text-align:right; font-family:monospace; flex-shrink:0;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; opacity:.85; }
.cbar-track { flex:1; background:var(--row-alt); border-radius:3px; height:20px;
              border:1px solid var(--border); overflow:hidden; }
.cbar-fill { height:100%; border-radius:2px; display:flex; align-items:center;
             padding:0 5px; min-width:4px; }
.cbar-val { color:#fff; font-size:.75em; font-weight:600; white-space:nowrap;
            text-shadow:0 1px 2px rgba(0,0,0,.45); }

/* ── Grouped bar charts ── */
.gbar-grid { display:flex; gap:2rem; flex-wrap:wrap; align-items:flex-start; }
.gbar-wrap { flex:1; min-width:260px; margin:.5rem 0 1.5rem; }
.gbar-title { font-size:.8em; font-weight:600; opacity:.65; margin-bottom:.8em;
              text-transform:uppercase; letter-spacing:.04em; }
.gbar-group { margin-bottom:.9rem; }
.gbar-group-label { font-size:.8em; font-family:monospace; font-weight:600; opacity:.9;
                    margin-bottom:3px; white-space:nowrap; overflow:hidden;
                    text-overflow:ellipsis; padding-left:2px; }
.gbar-bars { padding-left:8px; border-left:2px solid var(--border); }
.gbar-row { display:flex; align-items:center; margin:2px 0; gap:6px; font-size:.77em; }
.gbar-mlabel { width:130px; text-align:right; flex-shrink:0; white-space:nowrap;
               overflow:hidden; text-overflow:ellipsis; opacity:.7; }
.gbar-track { flex:1; background:var(--row-alt); border-radius:3px; height:17px;
              border:1px solid var(--border); overflow:hidden; }
.gbar-fill { height:100%; border-radius:2px; display:flex; align-items:center;
             padding:0 5px; min-width:3px; }
.gbar-val { color:#fff; font-size:.73em; font-weight:600; white-space:nowrap;
            text-shadow:0 1px 2px rgba(0,0,0,.5); }

/* ── Supergrid ── */
.supergrid-table th { text-align:center; }
.sg-machine-header { background:var(--th-bg); font-weight:700; }
.sg-metric { font-weight:500; font-size:.82em; opacity:.8; }
.sg-dash { opacity:.3; text-align:center; }

/* ── Scatter plot ── */
.scatter-svg { display:block; max-width:100%; height:auto; overflow:visible; margin:1rem 0; }

/* ── Machine legend ── */
.machine-legend { display:flex; gap:1.2rem; flex-wrap:wrap; margin:1rem 0; font-size:.85em; }
.legend-dot { display:inline-block; width:12px; height:12px; border-radius:50%;
              margin-right:4px; vertical-align:middle; }
.legend-label { vertical-align:middle; }

/* ── Comparison accordion ── */
.cmp-accordion-row { border:1px solid var(--border); border-radius:5px;
                     margin-bottom:.5rem; overflow:hidden; }
.cmp-acc-summary { list-style:none; cursor:pointer; padding:.55em 1em;
                   background:var(--summary-bg); display:flex; align-items:center;
                   gap:.6em; flex-wrap:wrap; }
.cmp-acc-summary::-webkit-details-marker { display:none; }
.cmp-acc-summary::before { content:"▶ "; font-size:.78em; opacity:.55; flex-shrink:0; }
details[open] .cmp-acc-summary::before { content:"▼ "; }
.acc-best-tg { font-size:.78em; opacity:.65; margin-left:auto; }
.cmp-acc-body { padding:.7rem 1rem; }

/* ── Expand controls ── */
.expand-controls { margin:1rem 0; display:flex; gap:.5rem; }
.expand-controls button { padding:5px 14px; border:1px solid var(--border);
                           background:var(--th-bg); color:var(--text);
                           cursor:pointer; border-radius:5px; font-size:.85em; }
.expand-controls button:hover { border-color:var(--accent); color:var(--accent); }
'''

JS_TEMPLATE = '''
const THEMES = ['light','dark','solar','twilight'];
const ICONS  = {light:'☀️ Light',dark:'🌙 Dark',solar:'🌤 Solar',twilight:'🌆 Twilight'};
let currentTheme = localStorage.getItem('bench-theme') || 'twilight';

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('bench-theme', t);
  currentTheme = t;
  document.getElementById('theme-btn').textContent = ICONS[t] + ' ▾';
  document.getElementById('theme-menu').classList.remove('open');
  rerenderMermaid(t);
}

document.getElementById('theme-btn').addEventListener('click', e => {
  e.stopPropagation();
  document.getElementById('theme-menu').classList.toggle('open');
});
document.addEventListener('click', () =>
  document.getElementById('theme-menu').classList.remove('open'));

function rerenderMermaid(theme) {
  const isDark = ['dark','twilight'].includes(theme);
  mermaid.initialize({
    startOnLoad: false,
    theme: isDark ? 'dark' : 'base',
    themeVariables: { xyChart: { plotColorPalette: isDark
      ? '#f38ba8,#89b4fa,#a6e3a1,#fab387,#cba6f7'
      : '#e05252,#4a90d9,#27ae60,#e67e22,#8e44ad' } }
  });
  document.querySelectorAll('.mermaid').forEach(el => {
    el.removeAttribute('data-processed');
    el.innerHTML = el.getAttribute('data-src') || el.innerHTML;
  });
  mermaid.run();
}

document.querySelectorAll('.model-section details').forEach(det => {
  det.addEventListener('toggle', () => {
    if (!det.open) return;
    const nodes = [...det.querySelectorAll('.mermaid')].filter(
      el => !el.getAttribute('data-processed'));
    if (nodes.length) {
      nodes.forEach(el => { el.innerHTML = el.getAttribute('data-src') || el.innerHTML; });
      mermaid.run({ nodes });
    }
  });
});

document.querySelectorAll('.expand-ctrl').forEach(btn => {
  btn.addEventListener('click', () => {
    const open = btn.dataset.action === 'expand';
    const tab = document.getElementById('tab-' + btn.dataset.machine);
    if (tab) tab.querySelectorAll('.model-section details').forEach(d => d.open = open);
  });
});

// ── Machine tabs ──────────────────────────────────────────────────────────────
const MACHINE_IDS = __MACHINE_IDS__;
const HAS_MULTI   = MACHINE_IDS.length > 1;
let   activeTab   = localStorage.getItem('bench-tab') || MACHINE_IDS[0];
if (!MACHINE_IDS.includes(activeTab) && activeTab !== 'compare') activeTab = MACHINE_IDS[0];

function switchTab(tabId) {
  activeTab = tabId;
  localStorage.setItem('bench-tab', tabId);
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  const pane = document.getElementById('tab-' + tabId);
  if (pane) pane.classList.add('active');
  document.querySelectorAll('.machine-tab-bar button').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabId);
  });
  document.querySelectorAll('.nav-machine').forEach(a => {
    a.classList.toggle('active', a.dataset.tab === tabId);
  });
  MACHINE_IDS.forEach(mid => {
    document.querySelectorAll('.nav-models-' + mid).forEach(el => {
      el.style.display = (tabId === mid || !HAS_MULTI) ? '' : 'none';
    });
  });
  document.querySelectorAll('#tab-' + tabId + ' .model-section details[open] .mermaid').forEach(el => {
    if (!el.getAttribute('data-processed')) {
      el.innerHTML = el.getAttribute('data-src') || el.innerHTML;
    }
  });
  mermaid.run();
}

if (HAS_MULTI) {
  document.querySelectorAll('.machine-tab-bar button').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  document.querySelectorAll('.nav-machine').forEach(a => {
    a.addEventListener('click', e => { e.preventDefault(); switchTab(a.dataset.tab); });
  });
  switchTab(activeTab);
} else {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.add('active'));
}

// ── Sortable leaderboard tables ───────────────────────────────────────────────
document.querySelectorAll('.bench-table.sortable').forEach(table => {
  const ths = [...table.querySelectorAll('thead th')];
  ths.forEach((th, col) => {
    th.addEventListener('click', () => {
      const tbody = table.querySelector('tbody');
      if (!tbody) return;
      const asc = th.dataset.sortDir !== 'asc';
      ths.forEach(t => {
        delete t.dataset.sortDir;
        t.querySelectorAll('.sort-arrow').forEach(a => a.remove());
      });
      th.dataset.sortDir = asc ? 'asc' : 'desc';
      th.insertAdjacentHTML('beforeend',
        `<span class="sort-arrow">${asc ? '↑' : '↓'}</span>`);
      const rows = [...tbody.querySelectorAll('tr')];
      rows.sort((a, b) => {
        const ac = a.children[col], bc = b.children[col];
        const av = (ac?.dataset.val ?? ac?.textContent ?? '').replace(/,/g,'');
        const bv = (bc?.dataset.val ?? bc?.textContent ?? '').replace(/,/g,'');
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
});

// ── Domain filter for leaderboard tables (per tab pane) ───────────────────────
document.querySelectorAll('.lb-domain-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const pane = btn.closest('.tab-pane');
    const domain = btn.dataset.domain;
    if (pane) {
      pane.querySelectorAll('.lb-domain-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      pane.querySelectorAll('.leaderboard-table tbody tr').forEach(row => {
        row.style.display =
          (domain === 'all' || row.dataset.domain === domain) ? '' : 'none';
      });
      // also filter nav model links in sidebar
      const mid = pane.id.replace('tab-', '');
      document.querySelectorAll('.nav-models-' + mid + ' a, .nav-model-general, .nav-model-coding, .nav-model-reasoning').forEach(a => {
        if (!a.classList.contains('nav-models-' + mid)) return;
        a.style.display = (domain === 'all' || a.classList.contains('nav-model-' + domain)) ? '' : 'none';
      });
    }
  });
});

// ── Size-class filter for TG leaderboard ─────────────────────────────────────
document.querySelectorAll('.sc-filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const pane   = btn.closest('.tab-pane');
    const sc     = btn.dataset.sc;
    const mid    = btn.dataset.mid;
    const filter = btn.closest('.sc-filter');
    if (filter) {
      filter.querySelectorAll('.sc-filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    }
    if (pane) {
      const tgTable = pane.querySelector('#' + mid + '-lb-tg-tbl');
      if (tgTable) {
        tgTable.querySelectorAll('tbody tr').forEach(row => {
          row.style.display = (sc === 'all' || row.dataset.sc === sc) ? '' : 'none';
        });
      }
    }
  });
});

// ── Domain filter for model sections ─────────────────────────────────────────
document.querySelectorAll('.model-domain-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const mid    = btn.dataset.mid;
    const domain = btn.dataset.domain;
    const filter = btn.closest('.model-domain-filter');
    if (filter) {
      filter.querySelectorAll('.model-domain-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    }
    const pane = document.getElementById('tab-' + mid);
    if (pane) {
      pane.querySelectorAll('.model-section').forEach(sec => {
        sec.style.display =
          (domain === 'all' || sec.dataset.domain === domain) ? '' : 'none';
      });
    }
  });
});

// ── Scrollspy ─────────────────────────────────────────────────────────────────
const navLinks = document.querySelectorAll('.nav-link[data-section]');
const io = new IntersectionObserver(entries => {
  entries.forEach(e => {
    const lnk = document.querySelector(`.nav-link[data-section="${e.target.id}"]`);
    if (lnk) lnk.classList.toggle('active', e.isIntersecting);
  });
}, { threshold: 0.05, rootMargin: '0px 0px -60% 0px' });
document.querySelectorAll('section[id], .leaderboard-block[id]').forEach(s => io.observe(s));

// ── Init ──────────────────────────────────────────────────────────────────────
applyTheme(currentTheme);
'''

def build_html(tab_bar_html, tab_panes_html, comparison_html, nav_html, machine_ids):
    machine_ids_js = '[' + ','.join(f'"{m}"' for m in machine_ids) + ']'
    js = JS_TEMPLATE.replace('__MACHINE_IDS__', machine_ids_js)

    return f'''<!DOCTYPE html>
<html data-theme="twilight">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LLM Benchmark Results</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <style>{CSS}</style>
</head>
<body>
<nav id="sidebar">
{nav_html}
</nav>
<main id="content">
{tab_bar_html}
{tab_panes_html}
{comparison_html}
</main>
<script>{js}</script>
</body>
</html>'''

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    machines = discover_machines()
    if not machines:
        print(f'No model-benchmark-results*.md files found in {BASE_DIR}', file=sys.stderr)
        sys.exit(1)

    print(f'Found {len(machines)} machine file(s): {[m[0] for m in machines]}')

    machines_data = []
    machines_html = []

    for mid, mlabel, path in machines:
        md_text  = path.read_text(encoding='utf-8')
        md_lines = md_text.splitlines()
        leaderboard_html, cross_charts = build_leaderboard(md_lines, mid)
        content_html, model_sections   = parse_markdown(md_text, cross_charts)
        data = extract_machine_data(md_text)
        machines_data.append((mid, mlabel, data))
        machines_html.append((mid, mlabel, leaderboard_html, content_html, model_sections))

    comparison_html = build_comparison(machines_data) if len(machines_data) > 1 else ''

    machine_ids = [mid for mid, *_ in machines]

    if len(machine_ids) > 1:
        btn_html = ''
        for i, (mid, mlabel, *_) in enumerate(machines_html):
            active = ' active' if i == 0 else ''
            btn_html += f'<button class="tab-btn{active}" data-tab="{mid}">{escape(mlabel)}</button>\n'
        if comparison_html:
            btn_html += '<button class="tab-btn" data-tab="compare">⚖ Compare</button>\n'
        tab_bar_html = f'<div class="machine-tab-bar">\n{btn_html}</div>'
    else:
        tab_bar_html = ''

    panes = []
    for i, (mid, mlabel, leaderboard_html, content_html, model_sections) in enumerate(machines_html):
        active    = ' active' if i == 0 else ''
        dom_pills = build_model_domain_pills(mid, model_sections)
        expand    = (f'<div class="expand-controls">'
                     f'<button class="expand-ctrl" data-machine="{mid}" data-action="expand">Expand All</button>'
                     f'<button class="expand-ctrl" data-machine="{mid}" data-action="collapse">Collapse All</button>'
                     f'</div>')
        pane = (f'<div id="tab-{mid}" class="tab-pane{active}">\n'
                f'{leaderboard_html}\n'
                f'{dom_pills}\n'
                f'{expand}\n'
                f'{content_html}\n'
                f'</div>')
        panes.append(pane)

    if comparison_html:
        panes.append(f'<div id="tab-compare" class="tab-pane">\n{comparison_html}\n</div>')

    tab_panes_html = '\n'.join(panes)

    mws      = [(mid, mlabel, ms) for mid, mlabel, _, _, ms in machines_html]
    nav_html = build_nav(mws, bool(comparison_html))

    html = build_html(tab_bar_html, tab_panes_html, '', nav_html, machine_ids)

    DEST.write_text(html, encoding='utf-8')
    size_kb = DEST.stat().st_size // 1024
    print(f'Done: {DEST} ({html.count(chr(10))} lines, {size_kb}KB)')

if __name__ == '__main__':
    main()
