"""Research primitives only; callers must supply matched-period OI/price changes.

No feed, order, config mutation or protected-feature imports. A writing flag is
a short-buildup heuristic, not an observation of trade initiation or capital flow.
"""
import math


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def buildup(d_oi, d_ltp):
    oi, price = number(d_oi), number(d_ltp)
    if oi is None or price is None:
        return 'UNAVAILABLE'
    if oi == 0 or price == 0:
        return 'NEUTRAL'
    if oi > 0:
        return 'SHORT_BUILDUP_HEURISTIC' if price < 0 else 'LONG_BUILDUP'
    return 'SHORT_COVERING_HEURISTIC' if price > 0 else 'LONG_UNWINDING'


def regime(net, total, band):
    """Normalized directional context; never a probability."""
    if total <= 0:
        return {'score': None, 'regime': 'UNAVAILABLE'}
    score = net / total
    return {'score': score, 'regime': 'BULLISH' if score >= band else 'BEARISH' if score <= -band else 'NEUTRAL'}


def window(rows, spot, count):
    """ATM plus N observed strikes above/below; count is not a point distance."""
    strikes = sorted({number(r.get('strike')) for r in rows if number(r.get('strike')) is not None})
    spot = number(spot)
    if not strikes or spot is None or spot <= 0:
        return []
    atm = min(range(len(strikes)), key=lambda i: (abs(strikes[i]-spot), strikes[i]))
    chosen = set(strikes[max(0, atm-count):atm+count+1])
    return [dict(r) for r in rows if number(r.get('strike')) in chosen]


def aggregate(rows, spot, cfg):
    """Rows carry explicit d_oi/d_ltp and baseline_kind set by the acquisition layer.

    Missing required fields invalidate the aggregate rather than silently remove
    the opposing side. No price-confirm fallback changes the observation period.
    """
    selected = window(rows, spot, int(cfg['oi_window_strikes']))
    masses = {s: {'writing': 0.0, 'fresh': 0.0, 'positional': 0.0} for s in ('CE', 'PE')}
    invalid = []
    labels = []
    kinds = {r.get('baseline_kind') for r in selected}
    if len(kinds) != 1 or None in kinds:
        invalid.append('Matched OI/price baseline required')
    for r in selected:
        side = str(r.get('side') or '').upper()
        if side not in masses:
            invalid.append('Unknown contract side')
            continue
        oi, doi, price, dp, delta = [number(r.get(k)) for k in ('oi','d_oi','ltp','d_ltp','delta')]
        if any(v is None for v in (oi,doi,price,dp)) or oi < 0 or price <= 0 or (cfg['oi_use_delta_weight'] and (delta is None or abs(delta) > 1)):
            invalid.append(f"Invalid fields: {side} {r.get('strike')}")
            continue
        w = abs(delta) if cfg['oi_use_delta_weight'] else 1.0
        label = buildup(doi, dp)
        writing = doi > 0 and (dp < 0 or not cfg['oi_writing_requires_price_confirm'])
        labels.append(dict(strike=r['strike'], side=side, buildup=label, writing=writing))
        masses[side]['positional'] += oi*w
        if writing:
            masses[side]['fresh'] += doi*w
            masses[side]['writing'] += doi*w*(price if cfg['oi_use_premium_weight'] else 1)
    if invalid or not selected or set(r.get('side') for r in selected) != {'CE','PE'}:
        return dict(state='UNAVAILABLE', reasons=invalid or ['Both contract sides required'], masses=masses, labels=labels)
    result = dict(state='RESEARCH_ONLY', masses=masses, labels=labels,
                  baseline_kind=next(iter(kinds)), scope=sorted({r['strike'] for r in selected}))
    for key in ('writing','fresh','positional'):
        pe, ce = masses['PE'][key], masses['CE'][key]
        result[key] = dict(net=pe-ce, total=pe+ce, **regime(pe-ce, pe+ce, float(cfg['oi_regime_band'])))
    result['divergence'] = result['writing']['net'] * result['positional']['net'] < 0
    return result


def slope(now, then, elapsed_seconds, scope_now, scope_then, required_seconds):
    """No lookahead, interpolation or slope across a different contract scope."""
    if scope_now != scope_then or elapsed_seconds < required_seconds:
        return None
    a, b = number(now), number(then)
    return None if a is None or b is None or elapsed_seconds <= 0 else (a-b)/elapsed_seconds
