"""App CSS (bubbles, badges, call screen), using libadwaita's named colours so
it follows light/dark mode."""
from __future__ import annotations
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk  # noqa: E402

CSS = """
.bubble { border-radius: 16px; padding: 8px 12px; }
.bubble.incoming { background: alpha(@window_fg_color, 0.08); }
.bubble.outgoing { background: @accent_bg_color; color: @accent_fg_color; }
.bubble.outgoing .dim-label, .bubble.outgoing .accent { color: alpha(@accent_fg_color, 0.8); }
.quote { border-left: 3px solid alpha(currentColor, 0.6); padding: 2px 8px;
         background: alpha(currentColor, 0.08); border-radius: 6px; }
.reactions { background: @card_bg_color; border-radius: 12px; padding: 0 6px;
             margin-top: -6px; box-shadow: 0 1px 2px alpha(black, 0.2); }
.attachment-image { border-radius: 10px; }
.reply-bar { background: alpha(@accent_bg_color, 0.15); border-radius: 8px; padding: 4px 8px; }
.unread-badge { background: @accent_bg_color; color: @accent_fg_color; border-radius: 10px;
                padding: 0 7px; font-size: smaller; font-weight: bold; min-width: 12px; }
.call-page { background: @window_bg_color; }
.call-button { min-width: 64px; min-height: 64px; -gtk-icon-size: 28px; }
.call-button.success { background: @success_bg_color; color: @success_fg_color; }
"""


def install():
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS.encode())
    Gtk.StyleContext.add_provider_for_display(display, provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
