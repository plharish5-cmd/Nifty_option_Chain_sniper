"""
ui_utils.py — Shared Streamlit UI Utilities.

Provides a bulletproof render_html() / _render_html() function that prevents
CommonMark code-block parsing artifacts (where 4-space indented strings or blank lines
cause Streamlit to render HTML as raw <pre><code> blocks).
"""

from __future__ import annotations

import textwrap
import streamlit as st


def render_html(html_str: str, *args, **kwargs) -> None:
    """
    Renders raw HTML cleanly in Streamlit without CommonMark indentation or blank line code-block issues.
    Uses st.html when available (Streamlit >= 1.33), otherwise falls back to
    dedented st.markdown(..., unsafe_allow_html=True).
    """
    if not html_str:
        return

    # Strip empty lines that cause CommonMark to split HTML blocks into code blocks
    lines = [line for line in html_str.splitlines() if line.strip()]
    clean = "\n".join(lines)

    if hasattr(st, "html"):
        st.html(clean)
    else:
        st.markdown(textwrap.dedent(clean).strip(), unsafe_allow_html=True)


# Alias for backward compatibility
_render_html = render_html
