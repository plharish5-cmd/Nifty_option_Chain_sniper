# Kronos forecasting that keeps every sampled path. The stock KronosPredictor averages the paths
# into one line (kronos.py auto_regressive_inference, np.mean(preds, axis=1)), which throws away the
# spread we need for the up-probability and the range. This is the same inference loop, unchanged,
# minus that final average. Kronos: Shi et al., Tsinghua University, AAAI 2026, MIT licence.
import os
import sys

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # also inside the PyInstaller build
sys.path.insert(0, HERE)  # the Kronos "model" package (MIT, copied unchanged from the Kronos repo) sits next to this file
from model import Kronos, KronosTokenizer  # noqa: E402
from model.kronos import calc_time_stamps, sample_from_logits  # noqa: E402

COLS = ["open", "high", "low", "close", "volume", "amount"]


def load(threads=6, model_dir=None):
    torch.set_num_threads(threads)
    d = model_dir or os.path.join(HERE, "models")
    tok = KronosTokenizer.from_pretrained(os.path.join(d, "Kronos-Tokenizer-base")).eval()
    mdl = Kronos.from_pretrained(os.path.join(d, "Kronos-base")).eval()
    return tok, mdl


def _infer(tok, mdl, x, x_stamp, y_stamp, max_context, pred_len, clip, T, top_k, top_p, n):
    with torch.no_grad():
        x = torch.clip(x, -clip, clip)
        x = x.unsqueeze(1).repeat(1, n, 1, 1).reshape(-1, x.size(1), x.size(2))
        x_stamp = x_stamp.unsqueeze(1).repeat(1, n, 1, 1).reshape(-1, x_stamp.size(1), x_stamp.size(2))
        y_stamp = y_stamp.unsqueeze(1).repeat(1, n, 1, 1).reshape(-1, y_stamp.size(1), y_stamp.size(2))

        x_token = tok.encode(x, half=True)
        initial_seq_len = x.size(1)
        batch_size = x_token[0].size(0)
        total_seq_len = initial_seq_len + pred_len
        full_stamp = torch.cat([x_stamp, y_stamp], dim=1)

        generated_pre = x_token[0].new_empty(batch_size, pred_len)
        generated_post = x_token[1].new_empty(batch_size, pred_len)
        pre_buffer = x_token[0].new_zeros(batch_size, max_context)
        post_buffer = x_token[1].new_zeros(batch_size, max_context)
        buffer_len = min(initial_seq_len, max_context)
        if buffer_len > 0:
            start_idx = max(0, initial_seq_len - max_context)
            pre_buffer[:, :buffer_len] = x_token[0][:, start_idx:start_idx + buffer_len]
            post_buffer[:, :buffer_len] = x_token[1][:, start_idx:start_idx + buffer_len]

        for i in range(pred_len):
            current_seq_len = initial_seq_len + i
            window_len = min(current_seq_len, max_context)
            if current_seq_len <= max_context:
                input_tokens = [pre_buffer[:, :window_len], post_buffer[:, :window_len]]
            else:
                input_tokens = [pre_buffer, post_buffer]
            context_start = max(0, current_seq_len - max_context)
            current_stamp = full_stamp[:, context_start:current_seq_len, :].contiguous()

            s1_logits, context = mdl.decode_s1(input_tokens[0], input_tokens[1], current_stamp)
            sample_pre = sample_from_logits(s1_logits[:, -1, :], temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)
            s2_logits = mdl.decode_s2(context, sample_pre)
            sample_post = sample_from_logits(s2_logits[:, -1, :], temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)

            generated_pre[:, i] = sample_pre.squeeze(-1)
            generated_post[:, i] = sample_post.squeeze(-1)
            if current_seq_len < max_context:
                pre_buffer[:, current_seq_len] = sample_pre.squeeze(-1)
                post_buffer[:, current_seq_len] = sample_post.squeeze(-1)
            else:
                pre_buffer.copy_(torch.roll(pre_buffer, shifts=-1, dims=1))
                post_buffer.copy_(torch.roll(post_buffer, shifts=-1, dims=1))
                pre_buffer[:, -1] = sample_pre.squeeze(-1)
                post_buffer[:, -1] = sample_post.squeeze(-1)

        full_pre = torch.cat([x_token[0], generated_pre], dim=1)
        full_post = torch.cat([x_token[1], generated_post], dim=1)
        context_start = max(0, total_seq_len - max_context)
        z = tok.decode([full_pre[:, context_start:total_seq_len].contiguous(),
                        full_post[:, context_start:total_seq_len].contiguous()], half=True)
        return z.reshape(-1, n, z.size(1), z.size(2)).cpu().numpy()[0]  # (n, seq, 6)


def _rope(x, start, inv_freq):
    t = torch.arange(start, start + x.size(-2), device=x.device).type_as(inv_freq)
    emb = torch.cat([torch.einsum("i,j->ij", t, inv_freq)] * 2, dim=-1)
    x1, x2 = x.chunk(2, dim=-1)
    return x * emb.cos()[None, None] + torch.cat((-x2, x1), dim=-1) * emb.sin()[None, None]


def _block(layer, x, cache, start):
    # One TransformerBlock (module.py) with a key/value cache. RoPE positions are absolute window
    # positions, so cached keys stay valid as long as the window never slides.
    a = layer.self_attn
    b, t, _ = x.shape
    h = layer.norm1(x)
    q, k, v = (p(h).view(b, t, a.n_heads, a.head_dim).transpose(1, 2) for p in (a.q_proj, a.k_proj, a.v_proj))
    q, k = _rope(q, start, a.rotary.inv_freq), _rope(k, start, a.rotary.inv_freq)
    if cache[0] is not None:
        k, v = torch.cat([cache[0], k], dim=2), torch.cat([cache[1], v], dim=2)
    cache[0], cache[1] = k, v
    o = F.scaled_dot_product_attention(q, k, v, is_causal=t > 1)
    x = x + a.out_proj(o.transpose(1, 2).contiguous().view(b, t, a.d_model))
    return x + layer.ffn(layer.norm2(x))


def _infer_fast(tok, mdl, x, x_stamp, y_stamp, max_context, pred_len, clip, T, top_k, top_p, n):
    """Same maths as _infer (verified token-for-token by verify_fast.py), but each step feeds only
    the newest candle instead of re-running the whole window, and the shared history is computed
    once for all paths. Requires len(history) + pred_len <= max_context (no window slide)."""
    L0 = x.size(1)
    assert L0 + pred_len <= max_context, "history + forecast must fit in the context window"
    dep = mdl.dep_layer.cross_attn
    nh, hd = dep.n_heads, dep.head_dim

    def dep_kv(H):
        # The dependency layer's cross-attention applies RoPE for a length-1 query, i.e. position 0
        # (cos=1, sin=0) to every key - an identity - so its keys/values are cacheable too.
        bb, L, _ = H.shape
        return (dep.k_proj(H).view(bb, L, nh, hd).transpose(1, 2), dep.v_proj(H).view(bb, L, nh, hd).transpose(1, 2))

    with torch.no_grad():
        x = torch.clip(x, -clip, clip)
        x_token = tok.encode(x, half=True)
        full_stamp = torch.cat([x_stamp, y_stamp], dim=1)

        caches = [[None, None] for _ in mdl.transformer]
        h = mdl.embedding([x_token[0], x_token[1]]) + mdl.time_emb(full_stamp[:, :L0])
        for layer, c in zip(mdl.transformer, caches):
            h = _block(layer, h, c, 0)
        H = mdl.norm(h)
        DK, DV = dep_kv(H)
        for c in caches:
            c[0], c[1] = c[0].expand(n, -1, -1, -1).contiguous(), c[1].expand(n, -1, -1, -1).contiguous()
        DK, DV = DK.expand(n, -1, -1, -1).contiguous(), DV.expand(n, -1, -1, -1).contiguous()
        last = H[:, -1:, :].expand(n, -1, -1)

        pre, post = [], []
        for i in range(pred_len):
            s1 = sample_from_logits(mdl.head(last)[:, -1, :], temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)
            q = dep.q_proj(mdl.embedding.emb_s1(s1)).view(n, 1, nh, hd).transpose(1, 2)
            o = F.scaled_dot_product_attention(q, DK, DV)
            x2 = mdl.dep_layer.norm(last + dep.out_proj(o.transpose(1, 2).reshape(n, 1, -1)))
            s2 = sample_from_logits(mdl.head.cond_forward(x2)[:, -1, :], temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)
            pre.append(s1)
            post.append(s2)
            if i == pred_len - 1:
                break
            pos = L0 + i
            h = mdl.embedding([s1, s2]) + mdl.time_emb(full_stamp[:, pos:pos + 1].expand(n, -1, -1))
            for layer, c in zip(mdl.transformer, caches):
                h = _block(layer, h, c, pos)
            last = mdl.norm(h)
            k2, v2 = dep_kv(last)
            DK, DV = torch.cat([DK, k2], dim=2), torch.cat([DV, v2], dim=2)

        full_pre = torch.cat([x_token[0].expand(n, -1), torch.cat(pre, dim=1)], dim=1)
        full_post = torch.cat([x_token[1].expand(n, -1), torch.cat(post, dim=1)], dim=1)
        z = tok.decode([full_pre, full_post], half=True)
        return z.cpu().numpy()  # (n, seq, 6)


def sample_paths(tok, mdl, df, x_ts, y_ts, n_paths=50, T=1.0, top_k=0, top_p=0.9, max_context=512, clip=5, seed=None, fast=True):
    """df: past candles (open, high, low, close[, volume]) oldest first; x_ts/y_ts: pandas Series of
    past/future timestamps. Returns (n_paths, len(y_ts), 6) in price units, columns = COLS."""
    if seed is not None:
        torch.manual_seed(seed)
    df = df.copy()
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if "amount" not in df.columns:
        df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
    x = df[COLS].values.astype(np.float32)
    mean, std = x.mean(axis=0), x.std(axis=0)
    xn = np.clip((x - mean) / (std + 1e-5), -clip, clip)
    xs = calc_time_stamps(pd.Series(x_ts).reset_index(drop=True)).values.astype(np.float32)
    ys = calc_time_stamps(pd.Series(y_ts).reset_index(drop=True)).values.astype(np.float32)
    pred_len = len(ys)
    z = (_infer_fast if fast else _infer)(tok, mdl, torch.from_numpy(xn[None]), torch.from_numpy(xs[None]),
                                          torch.from_numpy(ys[None]), max_context, pred_len, clip, T, top_k, top_p, n_paths)
    return z[:, -pred_len:, :] * (std + 1e-5) + mean
