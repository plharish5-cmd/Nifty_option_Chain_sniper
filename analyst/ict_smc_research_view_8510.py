"""Presentation of frozen research results only; no strategy or broker calls."""
import html

def render(report):
    esc=lambda v:html.escape(str(v))
    def fmt(v):return '—' if v is None else f'{v:.3g}'
    intro='''<!doctype html><meta charset="utf-8"><title>ICT SMC Desk Signals — provisional research</title>
<style>body{background:#0b1220;color:#e2e8f0;font:15px system-ui;margin:28px;line-height:1.5}h1{color:#7dd3fc}h2{margin-top:32px}.coverage,.warning{color:#fbbf24}.table{overflow-x:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}td,th{border:1px solid #334155;padding:9px;text-align:right}th:first-child,td:first-child{text-align:left}small{color:#94a3b8}</style>
<h1>ICT SMC Desk Signals — research scenarios</h1><p class="warning">NO GO FOR EXECUTION. Estimated premium exits are sensitivity scenarios, not observed option exits or broker fills.</p>'''
    intro+=f"<p>{esc(report['coverage'])}</p><p>Pre-registered family: {report['hypotheses']} hypotheses; evaluated: {len(report['summary'])} paired contrasts. Bonferroni alpha: {report['bonferroni_alpha']:.8f}. Repeated filters and overlapping alerts are not independent trades.</p>"
    intro+='<p>'+esc(' '.join(report['limitations']))+'</p>'
    intro+='<p>Current instrument-master lot: '+esc(report['lot_from_current_master'])+'. '+esc(report['historical_lot_caveat'])+'</p>'
    intro+='<p>Observed entry LTP plus a constant-delta estimated exit (.59 or .67). Both are scenario sensitivities; neither is a reconstructed historical execution. No signal is promoted to the algo.</p>'
    parts=[intro]
    for group in sorted({x['group'] for x in report['summary']}):
        parts.append('<h2>'+esc(group)+'</h2><p class="coverage">'+esc(report['coverage'])+'</p><div class="table"><table><tr><th>Horizon / delta</th><th>n / priced</th><th>Target / stop / ambiguous</th><th>Full-horizon MFE / MAE (spot pts)</th><th>Signal / opposite net ₹ (estimated)</th><th>Best day gross share</th><th>Paired sessions / t / p</th><th>Critical |t|</th></tr>')
        for row in report['summary']:
            if row['group']!=group:continue
            a=row['signal'];b=row['opposite'];c=row['direction_control_session_blocks']
            values=[str(row['horizon'])+' / '+str(row['delta']),f"{a['n']} / {a['priced']}",f"{a['target']} / {a['stop']} / {a['ambiguous']}",fmt(a['mean_forward_mfe'])+' / '+fmt(a['mean_forward_mae']),fmt(a['net_per_trade'])+' / '+fmt(b['net_per_trade']),fmt(a['best_day_gross_share_pct'])+'%',str(c['n_sessions'])+' / '+fmt(c['t_stat'])+' / '+fmt(c['two_sided_p']),fmt(c['critical_t'])]
            parts.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>')
        parts.append('</table></div>')
    parts.append('<h2>Scope still unavailable</h2><p>Active Corridor and Pullback Radar: context, no independent entry rule. Writer Stack-up: NOT BUILT, exact source rule not identified. Matrix diverging bars: display port only, recorded-time provenance not certified. These have no invented backtest outcomes.</p>')
    return ''.join(parts)
