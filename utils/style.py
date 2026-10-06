"""
Look and feel: palette, progress bar, branding. No discord import, so alerts
(which may go out through the plain webhook) can share it as dict embeds.

Colours are sampled from the bot's avatar (assets/avatar.png):
  ORANGE  the CS2 helmet patch      open sign-up, still filling
  GREEN   camo green                full
  RED     the comb                  game day / starting and still short
  GREY    the vest                  cancelled or over
"""

ORANGE = 0xE48215
GREEN = 0x5E8C31
RED = 0xC0392B
GREY = 0x6D6A64

BRAND = "CS2 10-Man"
STARTING_SOON_HOURS = 3  # a short roster this close to start turns red

BAR_FULL = "▰"   # ▰
BAR_EMPTY = "▱"  # ▱


def progress_bar(count: int, slots: int, width: int = 10) -> str:
    filled = 0 if slots <= 0 else min(width, round(width * count / slots))
    if count and not filled:
        filled = 1  # one player should show as progress
    return f"`{BAR_FULL * filled}{BAR_EMPTY * (width - filled)}`"


def safe_link(url: str) -> str:
    """A validated URL made safe inside a [text](url) masked link."""
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
