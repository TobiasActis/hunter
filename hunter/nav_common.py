"""Barra de navegacion comun a las dos vistas del dashboard: Memes (/) y Criptos estables (/criptos)."""

NAV_CSS = """
  .topnav { display:flex; align-items:center; gap:8px; margin:-24px -24px 20px -24px; padding:10px 24px; background:#161b22; border-bottom:1px solid #30363d; position:sticky; top:0; z-index:60; flex-wrap:wrap; }
  .topnav .brand { font-weight:700; letter-spacing:2px; margin-right:14px; color:#c9d1d9; font-size:14px; }
  .topnav a.navlink { color:#8b949e; text-decoration:none; padding:6px 14px; border-radius:8px; font-size:13px; border:1px solid transparent; }
  .topnav a.navlink small { color:inherit; opacity:.75; margin-left:6px; font-size:11px; }
  .topnav a.navlink:hover { color:#c9d1d9; background:#21262d; }
  .topnav a.navlink.active { color:#0d1117; background:#58a6ff; font-weight:600; }
  .topnav .navpaper { margin-left:auto; font-size:11px; padding:2px 10px; border-radius:10px; background:#1f6feb33; color:#79c0ff; }
"""


def nav_html(active: str) -> str:
    """active: 'memes' | 'criptos'."""
    def link(key, href, label, small):
        cls = "navlink active" if key == active else "navlink"
        return f'<a class="{cls}" href="{href}">{label}<small>{small}</small></a>'
    return ('<div class="topnav"><span class="brand">HUNTER</span>'
            + link("memes", "/", "&#127919; Memes", "Robinhood &middot; Solana")
            + link("criptos", "/criptos", "&#128200; Criptos estables", "BTC &middot; ETH &middot; SOL")
            + '<span class="navpaper">PAPEL &middot; nada se opera de verdad</span></div>')
