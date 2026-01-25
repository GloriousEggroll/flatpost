#!/usr/bin/python3

import gi
import sys
gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Flatpak", "1.0")
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gio, Gdk, GLib, GdkPixbuf
import flatpost.fp_turbo as fp_turbo
from flatpost.fp_turbo import AppStreamComponentKind as AppKind
import json
import threading
import subprocess
from pathlib import Path
from html.parser import HTMLParser
import requests
import os
import pwd
import atexit
from datetime import datetime, timedelta, timezone
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)

APP_ID = "com.flatpost.flatpostapp"
ICON_NAME = APP_ID

settings = Gtk.Settings.get_default()
settings.set_property("gtk-theme-name", "adw-gtk3-dark")  # Replace with the exact theme name if different
settings.set_property("gtk-application-prefer-dark-theme", True)

# ---- metadata TTL + stamp helpers ----
METADATA_TTL_DAYS = 7

def _metadata_cache_dir():
    d = os.path.join(GLib.get_user_cache_dir(), "flatpak-turbo")
    os.makedirs(d, exist_ok=True)
    return d

def _metadata_stamp_path(system_mode: bool):
    mode = "system" if system_mode else "user"
    return os.path.join(_metadata_cache_dir(), f"metadata_refresh_{mode}.stamp")

def mark_metadata_refreshed(system_mode: bool):
    path = _metadata_stamp_path(system_mode)
    with open(path, "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).isoformat())

def should_refresh_metadata(system_mode: bool, ttl_days: int = METADATA_TTL_DAYS) -> bool:
    path = _metadata_stamp_path(system_mode)
    if not os.path.exists(path):
        return True

    # Prefer reading the timestamp; fallback to file mtime
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
        last = datetime.fromisoformat(s)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        last = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)

    return (datetime.now(timezone.utc) - last) > timedelta(days=ttl_days)

def retrieve_metadata_compat(searcher, system_mode: bool, need_refresh: bool):
    """
    Works with both older and newer fp_turbo/AppstreamSearcher APIs.
    - Newer: retrieve_metadata(..., refresh=bool)
    - Older: retrieve_metadata(...) (no refresh kw)
    """
    # 1) Try the newer API first
    try:
        return searcher.retrieve_metadata(system_mode, refresh=need_refresh), need_refresh
    except TypeError as e:
        msg = str(e)
        if "unexpected keyword argument" not in msg or "refresh" not in msg:
            raise  # some other TypeError, don't hide it

    # 2) Older API: no refresh kw.
    # If you wanted a "force refresh", attempt to call a refresh method if the object has one.
    did_refresh = False
    if need_refresh:
        for meth_name in ("refresh_metadata", "refresh_remote", "refresh"):
            meth = getattr(searcher, meth_name, None)
            if callable(meth):
                try:
                    meth(system_mode)
                except TypeError:
                    # some variants might be meth() with no args
                    meth()
                did_refresh = True
                break

    # 3) Finally retrieve using the old signature
    return searcher.retrieve_metadata(system_mode), did_refresh


# ---- metadata cache (fast startup) ----

def _metadata_cache_path(system_mode: bool):
    mode = "system" if system_mode else "user"
    d = os.path.join(GLib.get_user_cache_dir(), "flatpost")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"metadata_cache_{mode}.json")

class CachedApp:
    """
    Minimal proxy for UI rendering from cached JSON.
    Implements get_details() and .id for your status checks.
    """
    is_cached = True

    def __init__(self, details: dict):
        self._details = details or {}
        self.id = self._details.get("id")  # used by your installed/updates checks

    def get_details(self):
        return self._details

class MetadataCache:
    SCHEMA_VERSION = 1

    def __init__(self, system_mode: bool):
        self.system_mode = system_mode
        self.path = _metadata_cache_path(system_mode)

    def _serialize_app(self, app):
        # Take only primitives from get_details()
        d = app.get_details() or {}

        # IMPORTANT: screenshots in fp_turbo are often objects -> not JSON-safe.
        # We omit them from cache (details view will resolve real app later).
        screenshots = []
        # If screenshots are already strings/urls, keep them; otherwise drop.
        for s in (d.get("screenshots") or []):
            if isinstance(s, (str, int, float, bool)) or s is None:
                screenshots.append(s)

        return {
            "id": d.get("id"),
            "name": d.get("name"),
            "summary": d.get("summary"),
            "description": d.get("description"),
            "developer": d.get("developer"),
            "version": d.get("version"),
            "kind": d.get("kind"),
            "categories": d.get("categories") or [],
            "repo": d.get("repo"),
            "icon_filename": d.get("icon_filename"),
            "icon_path_128": d.get("icon_path_128"),
            "urls": d.get("urls") or {},
            "screenshots": screenshots,
        }

    def save(self, category_results, collection_results, installed_results, updates_results, all_apps):
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "category_results": [self._serialize_app(a) for a in (category_results or [])],
            "collection_results": [self._serialize_app(a) for a in (collection_results or [])],
            "installed_results": [self._serialize_app(a) for a in (installed_results or [])],
            "updates_results": [self._serialize_app(a) for a in (updates_results or [])],
            "all_apps": [self._serialize_app(a) for a in (all_apps or [])],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def load(self):
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if payload.get("schema_version") != self.SCHEMA_VERSION:
                return None

            def mk(list_of_details):
                return [CachedApp(d) for d in (list_of_details or [])]

            return (
                mk(payload.get("category_results")),
                mk(payload.get("collection_results")),
                mk(payload.get("installed_results")),
                mk(payload.get("updates_results")),
                mk(payload.get("all_apps")),
            )
        except Exception:
            return None


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, system_mode=False, system_only_mode=False):
        app_title = "Flatpost (user mode)"
        if system_only_mode:
            app_title = "Flatpost (system-only mode)"
        elif system_mode:
            app_title = "Flatpost (system mode)"
        super().__init__(title=app_title)
        self.system_mode = system_mode
        self.system_only_mode = system_only_mode
        self.system_switch = Gtk.Switch()
        # Create system mode label
        self.system_label = Gtk.Label(label="System Mode")
        if self.system_mode:
            self.system_switch.set_active(True)
        if self.system_only_mode:
            self.system_switch.set_active(True)
            self.system_switch.set_sensitive(False)
        # Step 1: Verify file exists and is accessible
        icon_path = "/usr/share/icons/hicolor/64x64/apps/com.flatpost.flatpostapp.svg"
        if not os.path.exists(icon_path):
            print("ERROR: Icon file not found!")
            return

        # Step 2: Test loading individual pixbufs
        try:
            # Try loading smallest size first
            self.pixbuf16 = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 16, 16, True)

            # Now load full set of sizes
            self.pixbuf24 = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 24, 24, True)
            self.pixbuf32 = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 32, 32, True)
            self.pixbuf48 = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 48, 48, True)
            self.pixbuf64 = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 64, 64, True)

            Gtk.Window.set_default_icon(self.pixbuf64)

            # Set the icon list
            self.set_icon_list([self.pixbuf16, self.pixbuf24, self.pixbuf32, self.pixbuf48, self.pixbuf64])

        except Exception as e:
            print(f"ERROR loading icon: {str(e)}")


        # Store search results as an instance variable
        self.all_apps = []
        self.current_component_type = None
        self.category_results = []  # Initialize empty list
        self.subcategory_buttons = {}
        self.collection_results = []  # Initialize empty list
        self.installed_results = []  # Initialize empty list
        self.updates_results = []  # Initialize empty list
        self.current_page = None  # Track current page
        self.current_group = None  # Track current group (system/collections/categories)
        self.current_category = None          # last clicked category/subcategory key
        self.current_category_group = None    # last clicked group
        self.app_rows = {}  # app_id -> dict(row widgets + app ref)
        self.enable_row_updates = True

        # Set window size
        self.set_default_size(1280, 720)

        # Enable drag and drop
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self.on_drag_data_received)

        # Define category groups and their titles
        self.category_groups = {
            'system': {
                'installed': 'Installed',
                'updates': 'Updates',
                'repositories': 'Repositories'
            },
            'collections': {
                'trending': 'Trending',
                'popular': 'Popular',
                'recently-added': 'New',
                'recently-updated': 'Updated'
            },
            'categories': {
                'office': 'Productivity',
                'graphics': 'Graphics & Photography',
                'audiovideo': 'Audio & Video',
                'education': 'Education',
                'network': 'Networking',
                'game': 'Games',
                'development': 'Developer Tools',
                'science': 'Science',
                'system': 'System',
                'utility': 'Utilities'
            }
        }

        # Define subcategories
        self.subcategory_groups = {
            'audiovideo': {
                'audiovideoediting': 'Audio & Video Editing',
                'discburning': 'Disc Burning',
                'midi': 'Midi',
                'mixer': 'Mixer',
                'player': 'Player',
                'recorder': 'Recorder',
                'sequencer': 'Sequencer',
                'tuner': 'Tuner',
                'tv': 'TV'
            },
            'development': {
                'building': 'Building',
                'database': 'Database',
                'debugger': 'Debugger',
                'guidesigner': 'GUI Designer',
                'ide': 'IDE',
                'profiling': 'Profiling',
                'revisioncontrol': 'Revision Control',
                'translation': 'Translation',
                'webdevelopment': 'Web Development'
            },
            'game': {
                'actiongame': 'Action Games',
                'adventuregame': 'Adventure Games',
                'arcadegame': 'Arcade Games',
                'blocksgame': 'Blocks Games',
                'boardgame': 'Board Games',
                'cardgame': 'Card Games',
                'emulator': 'Emulators',
                'kidsgame': 'Kids\' Games',
                'logicgame': 'Logic Games',
                'roleplaying': 'Role Playing',
                'shooter': 'Shooter',
                'simulation': 'Simulation',
                'sportsgame': 'Sports Games',
                'strategygame': 'Strategy Games'
            },
            'graphics': {
                '2dgraphics': '2D Graphics',
                '3dgraphics': '3D Graphics',
                'ocr': 'OCR',
                'photography': 'Photography',
                'publishing': 'Publishing',
                'rastergraphics': 'Raster Graphics',
                'scanning': 'Scanning',
                'vectorgraphics': 'Vector Graphics',
                'viewer': 'Viewer'
            },
            'network': {
                'chat': 'Chat',
                'email': 'Email',
                'feed': 'Feed',
                'filetransfer': 'File Transfer',
                'hamradio': 'Ham Radio',
                'instantmessaging': 'Instant Messaging',
                'ircclient': 'IRC Client',
                'monitor': 'Monitor',
                'news': 'News',
                'p2p': 'P2P',
                'remoteaccess': 'Remote Access',
                'telephony': 'Telephony',
                'videoconference': 'Video Conference',
                'webbrowser': 'Web Browser',
                'webdevelopment': 'Web Development'
            },
            'office': {
                'calendar': 'Calendar',
                'chart': 'Chart',
                'contactmanagement': 'Contact Management',
                'database': 'Database',
                'dictionary': 'Dictionary',
                'email': 'Email',
                'finance': 'Finance',
                'presentation': 'Presentation',
                'projectmanagement': 'Project Management',
                'publishing': 'Publishing',
                'spreadsheet': 'Spreadsheet',
                'viewer': 'Viewer',
                'wordprocessor': 'Word Processor'
            },
            'system': {
                'emulator': 'Emulators',
                'filemanager': 'File Manager',
                'filesystem': 'Filesystem',
                'filetools': 'File Tools',
                'monitor': 'Monitor',
                'security': 'Security',
                'terminalemulator': 'Terminal Emulator'
            },
            'utility': {
                'accessibility': 'Accessibility',
                'archiving': 'Archiving',
                'calculator': 'Calculator',
                'clock': 'Clock',
                'compression': 'Compression',
                'filetools': 'File Tools',
                'telephonytools': 'Telephony Tools',
                'texteditor': 'Text Editor',
                'texttools': 'Text Tools'
            }
        }

        # Initialize mouse cursor test
        # display = Gdk.Display.get_default()
        # cursor = Gdk.Cursor.new_from_name(display, "pointer");

        # Add CSS provider for custom styling
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data("""
            .large-title {
                font-weight: 800
            }

            .top-bar {
                margin: 0px;
                padding: 0px;
                border: 0px;
                background-color: @sidebar_backdrop_color;
                border-bottom: 1px solid mix(currentColor,@window_bg_color,0.86);
            }

            dialog button label {
                padding: 8px 0;
            }

            button.titlebutton {
                min-height: 24px;
                min-width: 24px;
            }

            /* revealer and tool_box are hidden components inside GtkSearchBar
               This gets rid of the stupid grey line the tool_box causes.
            */
            #search_hidden_revealer,
            #search_hidden_tool_box {
                background: transparent;
                border: none;
                box-shadow: none;
                background-image: none;
                border-image: none;
                padding: 0px;
                margin: 0px;
            }

            .category-panel {
                padding: 0 6px;
                margin: 12px;
                border-radius: 4px;
                background-color: @sidebar_backdrop_color;
                border: 1px solid mix(currentColor,@window_bg_color,0.86);
            }

            .category-group-header {
                margin: 0;
                font-weight: bold;
            }

            .category-button {
                border: 0px;
                padding: 12px 8px;
                margin: 0;
                background: none;
                transition: margin-left 0.2s cubic-bezier(0.040, 0.455, 0.215, 0.995), padding 0.2s cubic-bezier(0.040, 0.455, 0.215, 0.995);
            }

            .category-button.active {
                padding: 12px 14px;
                margin-left: 4px;
                background-color: mix(currentColor,@window_bg_color,0.9);
                border-radius: 4px;
                font-weight: bold;
            }

            .pan-button {
                border: 0px;
                padding: 6px;
                margin: 0;
                background: none;
                box-shadow: none;
            }

            .no-scroll-bars scrollbar {
                min-width: 0px;
                opacity: 0;
                margin-top: -20px;
            }

            .subcategory-group-header {
                margin: 2px;
                background-color: @sidebar_backdrop_color;
                border-radius: 4px;
            }

            .subcategory-button {
                border: 0px;
                padding: 10px;
                margin: 0;
                background: none;
                transition: all 0.2s cubic-bezier(0.040, 0.455, 0.215, 0.995), font-weight 0s;
            }

            .subcategory-button.active {
                font-weight: bold;
                background-color: @window_bg_color;
                padding: 10px 24px;
                border-radius: 4px;
            }

            .subcategories-scroll {
                border: none;
                background-color: transparent;
                min-height: 32px;
            }

            .repo-item {
                padding: 6px;
                margin: 2px;
                border-bottom: 1px solid #eee;
            }
            .repo-delete-button {
                border: none;
                padding: 6px;
                margin-left: 6px;
            }
            .repo-list-header {
                font-size: 18px;
                padding: 5px;
            }

            .app-panel {
                margin: 12px;
            }

            .app-panel-header {
                padding: 16px 8px;
            }

            .app-list {
                border: 0px;
                background: none;
            }

            .app-list-item {
                padding: 24px 32px;
                margin: 5px 0;
                border-radius: 16px;
                background-color: alpha(@card_bg_color,0.6);
                border: 1px solid mix(currentColor,@window_bg_color,0.9);
                transition: background-color 0.2s ease-out;
            }

            .app-list-item.hover-event {
                background-color: alpha(@card_bg_color,0.2);
                box-shadow: 2px 2px 6px rgba(0,0,0,0.05);
            }

            .app-list-header {
                font-size: 18px;
                font-weight: bold;
            }
            .app-list-developer, .app-list-misc {
                font-size: 13px;
            }
            .app-list-summary {
                padding-top: 6px;
                padding-bottom: 2px;
            }
            .app-page-header {
                font-size: 24px;
                font-weight: bold;
                padding: 12px;
            }

            .app-repo-label {
                font-size: 0.8em;
                color: @search_bg_color
            }

            .app-type-label {
                font-size: 0.8em;
            }

            .app-panel-nothing {
                transition: opacity 2s ease;
            }

            .updates_available_bar {
                background-color: @accent_bg_color;
                padding: 4px;
                border-radius: 4px;
            }

            .updates_available_bar_label {
                color: @accent_fg_color;
            }

            .details-window {
                border: 0px;
                margin: 0px;
                background: none;
            }

            .details-content {
                margin: 0px 32px;
            }

            .details-gallery {
                background-color: @shade_color;
                padding: 16px 0px;
            }

            details-gallery-screenshot {
                border-radius: 4px;
            }

            .details-gallery-arrow {
                transition: all 0.2s cubic-bezier(0.040, 0.455, 0.215, 0.995);
                padding: 16px;
                border-radius: 50%;
            }

            .details-gallery-arrow.hover-event {
                background-color: mix(@window_bg_color,currentColor,0.1);
                color: @accent_bg_color;
            }

            .details-gallery-bullet {
                color: @accent_fg_color;
                font-size: 6px;
                padding: 6px;
                min-width: 13px;
                min-height: 12.5px;
                border-radius: 50%;
                transition: all 0.2s cubic-bezier(0.040, 0.455, 0.215, 0.995);
                opacity: 0.3;
            }

            .details-gallery-bullet.hover-event {
                background-color: mix(@window_bg_color,currentColor,0.1);
                opacity: 1;
            }

            .details-gallery-bullet.active {
                color: @accent_bg_color;
                opacity: 1;
                font-size: 9px;
            }

            .details-textview text {
                background-color: @window_bg_color;
                border-width: 0;
                border-radius: 4px;
            }

            .url-list {
                background-color: @card_bg_color;
                border-radius: 4px;
                border: 1px solid mix(currentColor,@window_bg_color,0.86);
            }

            .url-list-item {
                padding: 10px 16px;
                transition: all 0.2s cubic-bezier(0.040, 0.455, 0.215, 0.995), background-color 0.2s ease-out;
            }

            .url-list-item image {
                color: mix(currentColor,@window_bg_color,0.3);
            }

            .url-list-item.hover-event {
                background-color: @sidebar_backdrop_color;
            }

            .url-list-item-title {
                font-size: 14px;
                font-weight: 500;
            }

            .url-list-item-url {
                font-size: 13px;
            }

            .url {
                color: @accent_bg_color;
            }

            .url.hover-event {
                text-decoration-line: underline;
            }
            .permissions-window {
                border: 0px;
                margin: 0px;
                padding: 20px;
                background: none;
            }
            .permissions-header-label {
                font-weight: bold;
                font-size: 24px;
            }
            .permissions-row {
                padding: 6px;
            }
            .permissions-item-label {
                font-size: 14px;
            }
            .permissions-item-summary {
                font-size: 12px;
                color: #8c8c8c;
            }
            .permissions-global-indicator {
                background: none;
            }
            .permissions-spacing-box {
                background: none;
                padding: 5px;
            }
            .permissions-path-vbox {
                padding: 6px;
            }
            .permissions-path {
                padding: 6px;
            }
            .permissions-path-text text {
                color: @search_fg_color;
            }

            .permissions-path-text textview {
                border-radius: 4px;
                padding: 8px;
                background-color: @search_bg_color;
                border: 1px solid @search_border_color;
                margin: 8px;
            }

            .permissions-path-text border {
                background-color: @search_border_color;
                border-radius: 4px;
            }

            .permissions-path-scroll {
                padding: 6px;
            }
            .permissions-bus-box {
                padding-left: 8px;
                background: none;
            }
            combobox,
            combobox box,
            combobox button {
                font-size: 12px;
                padding-top: 0px;
                padding-bottom: 0px;
                margin: 0px;
                min-height: 0px;
            }
            button {
                padding-top: 0px;
                padding-bottom: 0px;
                margin: 0px;
                min-height: 0px;
            }
            .app-action-button {
                border-radius: 4px;
                padding: 8px;
                transition: all 0.2s ease;
            }

            .app-action-button.icon_label {
                padding: 8px 14px;
            }

            .app-action-button:hover {
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            .app-url-label {
                color: @accent_bg_color;
                text-decoration: underline;
            }

            .app-url-label:hover {
                text-decoration: none;
            }
        """)

        # Add CSS provider to the default screen
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 600
        )

        # Metadata fetch state
        self.metadata_loading = False
        self.metadata_loaded = False
        self.metadata_error = None
        self._metadata_fetch_seq = 0

        # Cache helper
        self._metadata_cache = MetadataCache(self.system_mode)

        # 1) Load cached metadata immediately
        cached = self._metadata_cache.load()
        if cached:
            (self.category_results,
            self.collection_results,
            self.installed_results,
            self.updates_results,
            self.all_apps) = cached
            self.metadata_loaded = True
            self.metadata_loading = False
            self.metadata_error = None

        # Create main layout
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.add(self.main_box)

        # Create_header_bar
        self.create_header_bar()

        # Create panels
        self.create_panels()

        # Select Trending by default
        self.select_default_category()

        self.refresh_local()

        # 3) Refresh in background (non-blocking)
        self.start_metadata_refresh(show_error_dialog=False, force=False)


    def _refresh_visible_page(self):
        # Prefer your internal state if it exists
        if self.current_page and self.current_group:
            self.refresh_current_page(preserve_scroll=True)
            return

        # Fallback: if you have a sidebar listbox, use its selection
        row = self.category_listbox.get_selected_row() if hasattr(self, "category_listbox") else None
        if row and hasattr(row, "category") and hasattr(row, "group"):
            self.current_page = row.category
            self.current_group = row.group
            self.refresh_current_page(preserve_scroll=True)
            return

        # Last-resort: refresh the default you say is visible
        self.current_group = "system"
        self.current_page = "installed"
        self.show_category_apps("installed")

    def _collections_keys(self):
        return set(self.category_groups.get('collections', {}).keys())

    def _show_loading_view(self, message="Fetching Metadata… Please wait"):
        """Replace right panel content with a centered spinner + message."""
        self._clear_views_container()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        center.set_hexpand(True)
        center.set_vexpand(True)

        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.start()

        title = Gtk.Label(label=message)
        title.get_style_context().add_class("title-2")
        title.set_halign(Gtk.Align.CENTER)

        subtitle = Gtk.Label(label="This may take a moment.")
        subtitle.get_style_context().add_class("dim-label")
        subtitle.set_halign(Gtk.Align.CENTER)

        center.pack_start(spinner, False, False, 0)
        center.pack_start(title, False, False, 0)
        center.pack_start(subtitle, False, False, 0)

        outer.pack_start(center, True, True, 0)
        self.right_container.pack_start(outer, True, True, 0)
        self.right_container.show_all()

    def _show_loading_error_view(self, message):
        """Show an in-page error (instead of a startup blocking dialog)."""
        self._clear_views_container()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        center.set_hexpand(True)
        center.set_vexpand(True)

        icon = Gtk.Image.new_from_gicon(
            Gio.Icon.new_for_string("dialog-error-symbolic"),
            Gtk.IconSize.DIALOG
        )
        icon.set_pixel_size(64)

        title = Gtk.Label(label="Metadata fetch failed")
        title.get_style_context().add_class("title-2")
        title.set_halign(Gtk.Align.CENTER)

        body = Gtk.Label(label=message)
        body.get_style_context().add_class("dim-label")
        body.set_halign(Gtk.Align.CENTER)
        body.set_line_wrap(True)
        body.set_max_width_chars(60)

        retry = Gtk.Button(label="Retry")
        retry.get_style_context().add_class("suggested-action")
        retry.connect("clicked", lambda *_: self.start_metadata_refresh(show_error_dialog=False))

        center.pack_start(icon, False, False, 0)
        center.pack_start(title, False, False, 0)
        center.pack_start(body, False, False, 0)
        center.pack_start(retry, False, False, 0)

        outer.pack_start(center, True, True, 0)
        self.right_container.pack_start(outer, True, True, 0)
        self.right_container.show_all()

    def start_metadata_refresh(self, show_error_dialog=False, force=False):

        if getattr(self, "metadata_loading", False):
            return

        need_refresh = force or should_refresh_metadata(self.system_mode)
        if not need_refresh and self.metadata_loaded and getattr(self, "all_apps", None):
            # Nothing to do; we already have displayable data
            return

        # If you have metadata already loaded for this session and it isn't stale, don't do anything.
        if getattr(self, "metadata_loaded", False) and not need_refresh:
            return

        self.metadata_loading = True
        self.metadata_error = None

        self._metadata_fetch_seq = getattr(self, "_metadata_fetch_seq", 0) + 1
        seq = self._metadata_fetch_seq

        searcher = fp_turbo.get_reposearcher(self.system_mode)
        collections_keys = self._collections_keys()

        def worker():
            try:
                (results, did_refresh) = retrieve_metadata_compat(searcher, self.system_mode, need_refresh)
                category_results, collection_results, installed_results, updates_results, all_apps = results

                def apply_results():
                    if seq != getattr(self, "_metadata_fetch_seq", seq):
                        return False
                    self.category_results = category_results
                    self.collection_results = collection_results
                    self.installed_results = installed_results
                    self.updates_results = updates_results
                    self.all_apps = all_apps

                    # Save cache (best effort)
                    try:
                        self._metadata_cache.save(
                            self.category_results,
                            self.collection_results,
                            self.installed_results,
                            self.updates_results,
                            self.all_apps
                        )
                    except Exception as e:
                        logger.warning("Failed to save metadata cache: %s", e)

                    self.metadata_loading = False
                    self.metadata_loaded = True
                    self.metadata_error = None

                    # Only stamp if we actually refreshed
                    if did_refresh:
                        mark_metadata_refreshed(self.system_mode)

                    # Always re-render the currently visible page after metadata changes.
                    self._refresh_visible_page()
                    return False

                GLib.idle_add(apply_results)

            except Exception as e:
                err_text = str(e)

                def apply_error():
                    if seq != getattr(self, "_metadata_fetch_seq", seq):
                        return False

                    self.metadata_loading = False
                    # Keep cached data usable if we have it
                    if getattr(self, "all_apps", None):
                        self.metadata_loaded = True
                    else:
                        self.metadata_loaded = False
                    self.metadata_error = err_text

                    if self.current_group == "collections" and self.current_page in collections_keys:
                        self._show_loading_error_view(err_text)

                    if show_error_dialog:
                        dlg = Gtk.MessageDialog(
                            transient_for=self,
                            modal=True,
                            destroy_with_parent=True,
                            message_type=Gtk.MessageType.ERROR,
                            buttons=Gtk.ButtonsType.OK,
                            text="Error retrieving metadata",
                            secondary_text=err_text
                        )
                        dlg.run()
                        dlg.destroy()

                    return False

                GLib.idle_add(apply_error)

        threading.Thread(target=worker, daemon=True).start()



    def on_drag_data_received(self, widget, context, x, y, data, info, time):
        """Handle drag and drop events"""
        # Check if data is a URI list
        if isinstance(data, int):
            return
        uri = data.get_uris()[0]
        file_path = Gio.File.new_for_uri(uri).get_path()
        if file_path and file_path.endswith('.flatpakref'):
            self.handle_flatpakref_file(file_path)
        if file_path and file_path.endswith('.flatpakrepo'):
            self.handle_flatpakrepo_file(file_path)
        context.finish(True, False, time)

    def handle_flatpakref_file(self, file_path):
        """Handle .flatpakref file installation"""
        self.on_install_clicked(None, file_path)

    def handle_flatpakrepo_file(self, file_path):
        """Handle .flatpakrepo file installation"""
        self.on_add_repo_button_clicked(None, file_path)

    def create_header_bar(self):
        # Create horizontal bar
        self.top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.top_bar.get_style_context().add_class("top-bar")
        self.top_bar.set_hexpand(True)
        self.top_bar.set_vexpand(False)
        self.top_bar.set_spacing(6)
        self.top_bar.set_margin_top(0)
        self.top_bar.set_margin_bottom(0)
        self.top_bar.set_margin_start(0)
        self.top_bar.set_margin_end(0)

        # Add search bar
        self.searchbar = Gtk.SearchBar()
        self.searchbar.set_show_close_button(False)
        self.searchbar.set_hexpand(False)
        self.searchbar.set_vexpand(False)
        self.searchbar.set_margin_top(0)
        self.searchbar.set_margin_bottom(0)
        self.searchbar.set_margin_start(0)
        self.searchbar.set_margin_end(0)
        revealer = self.searchbar.get_children()[0]
        revealer.set_name("search_hidden_revealer")
        revealer.set_margin_top(0)
        revealer.set_margin_bottom(0)
        revealer.set_margin_start(0)
        revealer.set_margin_end(0)
        tool_box = revealer.get_children()[0]
        tool_box.set_name("search_hidden_tool_box")
        tool_box.set_margin_top(0)
        tool_box.set_margin_bottom(0)
        tool_box.set_margin_start(0)
        tool_box.set_margin_end(0)

        # Create search entry with icon
        searchentry = Gtk.SearchEntry()
        searchentry.set_placeholder_text("Search applications...")
        searchentry.set_icon_from_gicon(Gtk.EntryIconPosition.PRIMARY,
                                    Gio.Icon.new_for_string('system-search-symbolic'))

        # Connect search entry signals
        searchentry.connect("search-changed", self.on_search_changed)
        searchentry.connect("activate", self.on_search_activate)

        # Connect search entry to search bar
        self.searchbar.connect_entry(searchentry)
        self.searchbar.add(searchentry)
        self.searchbar.set_search_mode(True)

        self.top_bar.pack_start(self.searchbar, False, False, 0)

        self.component_type_combo_label = Gtk.Label(label="Search Type:")
        # Create component type dropdown
        self.component_type_combo = Gtk.ComboBoxText()
        self.component_type_combo.props.valign = Gtk.Align.CENTER
        self.component_type_combo.set_hexpand(False)
        self.component_type_combo.set_vexpand(False)
        self.component_type_combo.set_wrap_width(1)
        self.component_type_combo.set_size_request(150, 32)  # Set width in pixels
        self.component_type_combo.connect("changed", self.on_component_type_changed)

        # Add "ALL" option first
        self.component_type_combo.append_text("ALL")

        # Add all component types
        for kind in AppKind:
            if kind != AppKind.UNKNOWN:
                self.component_type_combo.append_text(kind.name)

        # Select "ALL" by default
        self.component_type_combo.set_active(0)

        # Add dropdown to header bar
        self.top_bar.pack_start(self.component_type_combo_label, False, False, 0)
        self.top_bar.pack_start(self.component_type_combo, False, False, 0)

        # Add about button
        about_button = Gtk.Button()
        about_button.set_size_request(26, 26)  # 40x40 pixels
        about_button.get_style_context().add_class("app-action-button")
        about_button.set_tooltip_text("About")
        about_button_icon = Gio.Icon.new_for_string('help-about-symbolic')
        about_button.set_image(Gtk.Image.new_from_gicon(about_button_icon, Gtk.IconSize.BUTTON))
        about_button.connect("clicked", self.on_about_clicked)


        # Add global overrides button
        global_overrides_button = Gtk.Button()
        global_overrides_button.set_size_request(26, 26)  # 40x40 pixels
        global_overrides_button.get_style_context().add_class("app-action-button")
        global_overrides_button.set_tooltip_text("Global Setting Overrides")
        global_overrides_button_icon = Gio.Icon.new_for_string('applications-system-symbolic')
        global_overrides_button.set_image(Gtk.Image.new_from_gicon(global_overrides_button_icon, Gtk.IconSize.BUTTON))
        global_overrides_button.connect("clicked", self.global_on_options_clicked)

        # Add refresh metadata button
        refresh_metadata_button = Gtk.Button()
        refresh_metadata_button.set_size_request(26, 26)  # 40x40 pixels
        refresh_metadata_button.get_style_context().add_class("app-action-button")
        refresh_metadata_button.set_tooltip_text("Refresh metadata")
        refresh_metadata_button_icon = Gio.Icon.new_for_string('view-refresh-symbolic')
        refresh_metadata_button.set_image(Gtk.Image.new_from_gicon(refresh_metadata_button_icon, Gtk.IconSize.BUTTON))
        refresh_metadata_button.connect("clicked", self.on_refresh_metadata_button_clicked)

        parent_system_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        parent_system_box.set_vexpand(True)
        parent_system_box.set_valign(Gtk.Align.CENTER)
        # Create system mode switch box
        system_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        system_box.set_hexpand(False)
        system_box.set_vexpand(False)
        system_box.set_margin_top(0)
        system_box.set_margin_bottom(0)
        system_box.set_margin_start(0)
        system_box.set_margin_end(6)
        system_box.set_halign(Gtk.Align.CENTER)

        self.system_switch.props.valign = Gtk.Align.CENTER
        self.system_switch.connect("notify::active", self.on_system_mode_toggled)
        self.system_switch.set_hexpand(False)
        self.system_switch.set_vexpand(False)

        # Pack switch and label
        if not self.system_only_mode:
            system_box.pack_end(self.system_switch, False, False, 0)
            system_box.pack_end(self.system_label, False, False, 0)
        system_box.pack_end(about_button, False, False, 0)
        system_box.pack_end(global_overrides_button, False, False, 0)
        system_box.pack_end(refresh_metadata_button, False, False, 0)
        parent_system_box.pack_end(system_box, False, False, 0)
        # Add system controls to header
        self.top_bar.pack_end(parent_system_box, False, False, 0)

        # Add the top bar to the main box
        self.main_box.pack_start(self.top_bar, False, True, 0)

    def enter_hover_event(self, content, position=None, dots=None): # Use this function with "enter-notify-event" signals to handle hover state
        if content.get_style_context().has_class("details-gallery-arrow") == True and content.get_style_context().has_class("dim-label") == True:
            return
        elif content.get_style_context().has_class("details-gallery-bullet") == True:
            for i, dot in enumerate(dots):
            # Get the bullet label from the event box
                bullet_selected = dot.get_children()[0]
                if i == position:
                    bullet_selected.get_style_context().add_class("hover-event")
                    return
        # if content.get_style_context().has_class("url") == True:
            # Gdk.Window.set_cursor(content, cursor)
            # I would like to create a way to change cursor for URLs, but idk how to use GDK without recoding everything
        content.get_style_context().add_class("hover-event")

    def leave_hover_event(self, content, position=None, dots=None): # Use this function with "leave-notify-event" signals to handle hover state
        # if content.get_style_context().has_class("url") == True:
            # print("Leave: class detected")
        if content.get_style_context().has_class("details-gallery-bullet") == True:
            for i, dot in enumerate(dots):
            # Get the bullet label from the event box
                bullet_selected = dot.get_children()[0]
                if i == position:
                    bullet_selected.get_style_context().remove_class("hover-event")
                    return
        content.get_style_context().remove_class("hover-event")

    def on_about_clicked(self, button):
        """Show the about dialog with version and license information."""
        # Create the dialog
        about_dialog = Gtk.Dialog(
            title="About Flatpost",
            parent=self,
            modal=True,
            destroy_with_parent=True
        )

        # Set size
        about_dialog.set_default_size(400, 200)

        # Create content area
        content_area = about_dialog.get_content_area()

        # Create main box for content
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_border_width(12)

        # Icon
        image = Gtk.Image.new_from_pixbuf(self.pixbuf64)

        # Version label
        name_label = Gtk.Label(label="Flatpost")
        name_label.get_style_context().add_class("permissions-header-label")
        version_label = Gtk.Label(label="Version 1.1.1")
        copyright_label = Gtk.Label(label=f"Copyright © 2025-{datetime.now().year} Thomas Crider")
        program_label = Gtk.Label(label="This program comes with absolutely no warranty.")

        license_label = Gtk.Label(label="License:")
        license_url = Gtk.Label(label="BSD 2-Clause License")
        license_url.set_use_underline(True)
        license_url.get_style_context().add_class("url")
        license_event_box = Gtk.EventBox()
        license_event_box.add(license_url)
        license_event_box.connect("button-release-event",
                        lambda w, e: Gio.AppInfo.launch_default_for_uri("https://github.com/GloriousEggroll/flatpost/blob/main/LICENSE"))
        license_event_box.connect("enter-notify-event", lambda w, e: self.enter_hover_event(license_url));
        license_event_box.connect("leave-notify-event", lambda w, e: self.leave_hover_event(license_url));

        issue_label = Gtk.Label(label="Report an Issue:")
        issue_url = Gtk.Label(label="https://github.com/GloriousEggroll/flatpost/issue")
        issue_url.set_use_underline(True)
        issue_url.set_use_markup(True)
        issue_url.get_style_context().add_class("url")
        issue_event_box = Gtk.EventBox()
        issue_event_box.add(issue_url)
        issue_event_box.connect("button-release-event",
                        lambda w, e: Gio.AppInfo.launch_default_for_uri("https://github.com/GloriousEggroll/flatpost/issues"))
        issue_event_box.connect("enter-notify-event", lambda w, e: self.enter_hover_event(issue_url)); # These connect signals handles hover
        issue_event_box.connect("leave-notify-event", lambda w, e: self.leave_hover_event(issue_url));

        # Add all widgets
        content_area.add(main_box)
        main_box.pack_start(name_label, False, False, 0)
        main_box.pack_start(image, False, False, 0)
        main_box.pack_start(version_label, False, False, 0)
        main_box.pack_start(copyright_label, False, False, 0)
        main_box.pack_start(program_label, False, False, 0)
        main_box.pack_start(license_label, False, False, 0)
        main_box.pack_start(license_event_box, False, False, 0)
        main_box.pack_start(issue_label, False, False, 0)
        main_box.pack_start(issue_event_box, False, False, 0)

        # Show the dialog
        about_dialog.show_all()
        about_dialog.run()
        about_dialog.destroy()

    def on_refresh_metadata_button_clicked(self, button):
        if self.current_group == "collections" and self.current_page in self._collections_keys():
            self._show_loading_view("Fetching Metadata… Please wait")
        self.start_metadata_refresh(show_error_dialog=True, force=True)


    def on_component_type_changed(self, combo):
        """Handle component type filter changes"""
        selected_type = combo.get_active_text()
        if selected_type:
            if selected_type == "ALL":
                self.current_component_type = None
            else:
                self.current_component_type = selected_type
        else:
            self.current_component_type = None
        self.refresh_current_page()

    def relaunch_as_user(self):
        uid_str = (
            os.environ.get("ORIG_USER")
            or os.environ.get("SUDO_UID")
            or os.environ.get("PKEXEC_UID")
            or str(os.getuid())
        )
        uid = int(uid_str)

        pw_record = pwd.getpwuid(uid)
        username = pw_record.pw_name
        user_home = pw_record.pw_dir
        gid = pw_record.pw_gid

        os.setgid(gid)
        os.setuid(uid)

        os.environ["HOME"] = user_home
        os.environ["LOGNAME"] = username
        os.environ["USER"] = username
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

        script_path = Path(__file__).resolve()
        os.execvp(sys.executable, [sys.executable, str(script_path)])

    def on_system_mode_toggled(self, switch, gparam):
        """Handle system mode toggle switch state changes"""
        desired_state = switch.get_active()

        if desired_state:
            # Get current script path
            current_script = sys.argv[0]

            # Re-execute as root with system mode enabled
            try:
                # Construct command to re-execute with system mode enabled
                script_path = Path(__file__).resolve()
                subprocess.run(["xhost", "si:localuser:root"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
                )
                os.execvp(
                    "pkexec",
                    [
                        "pkexec",
                        "--disable-internal-agent",
                        "env",
                        f"DISPLAY={os.environ['DISPLAY']}",
                        f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
                        f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '').lower()}",
                        f"ORIG_USER={os.getuid()!s}",
                        f"PKEXEC_UID={os.getuid()!s}",
                        "G_MESSAGES_DEBUG=none",
                        sys.executable,
                        str(script_path),
                        '--system-mode',
                    ]
                )

            except subprocess.CalledProcessError:
                # Authentication failed, reset switch and show error
                switch.set_active(False)
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Authentication failed",
                    secondary_text="Could not enable system mode"
                )
                dialog.connect("response", lambda d, r: d.destroy())
                dialog.show()
        else:
            try:
                # Construct command to re-execute with system mode enabled
                self.relaunch_as_user()
                sys.exit(0)

            except subprocess.CalledProcessError:
                # Authentication failed, reset switch and show error
                switch.set_active(True)
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Authentication failed",
                    secondary_text="Could not enable user mode"
                )
                dialog.connect("response", lambda d, r: d.destroy())
                dialog.show()


    def populate_repo_dropdown(self):
        # Get list of repositories
        repos = fp_turbo.repolist(self.system_mode)

        # Clear existing items
        self.repo_dropdown.remove_all()

        # Add repository names
        for repo in repos:
            self.repo_dropdown.append_text(repo.get_name())

        # Connect selection changed signal
        self.repo_dropdown.connect("changed", self.on_repo_selected)

    def on_repo_selected(self, dropdown):
        active_index = dropdown.get_active()
        if active_index != -1:
            self.selected_repo = dropdown.get_model()[active_index][0]
            print(f"Selected repository: {self.selected_repo}")

    def refresh_data(self):
        # Create dialog and progress bar
        dialog = Gtk.Dialog(
            title="Fetching metadata, please wait...",
            parent=self,
            modal=True,
            destroy_with_parent=True
        )
        dialog.set_size_request(400, 100)

        progress_bar = Gtk.ProgressBar()
        progress_bar.set_text("Initializing...")
        progress_bar.set_show_text(True)
        progress_bar.set_margin_start(32)
        progress_bar.set_margin_end(32)
        dialog.vbox.pack_start(progress_bar, True, True, 32) # Padding necessary
        dialog.vbox.set_valign(Gtk.Align.FILL) # Make it use all window space

        # Show the dialog
        dialog.show_all()

        searcher = fp_turbo.get_reposearcher(self.system_mode)

        # Define thread target function
        def retrieve_metadata():
            try:
                category_results, collection_results, installed_results, updates_results, all_apps = searcher.retrieve_metadata(self.system_mode)
                self.category_results = category_results
                self.collection_results = collection_results
                self.installed_results = installed_results
                self.updates_results = updates_results
                self.all_apps = all_apps
            except Exception as e:
                dialog = Gtk.MessageDialog(
                    transient_for=None,  # Changed from self
                    modal=True,
                    destroy_with_parent=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text=f"Error retrieving metadata: {str(e)}"
                )
                dialog.run()
                dialog.destroy()
        # Start the refresh thread
        refresh_thread = threading.Thread(target=retrieve_metadata)
        refresh_thread.start()
        def update_progress():
            if refresh_thread.is_alive():
                progress_bar.set_text("Fetching...")
                progress_bar.set_fraction(searcher.refresh_progress / 100.0)
                return True
            progress_bar.set_fraction(1.0)
            dialog.destroy()
            return False

        # Start the progress update timer
        GLib.timeout_add(250, update_progress)
        dialog.run()
        if not refresh_thread.is_alive() and dialog.is_active():
            dialog.destroy()

    def refresh_local(self):
        try:
            searcher = fp_turbo.get_reposearcher(self.system_mode)
            installed_results, updates_results = searcher.refresh_local(self.system_mode)
            self.installed_results = installed_results
            self.updates_results = updates_results
        except Exception as e:
            message_type = Gtk.MessageType.ERROR
            dialog = Gtk.MessageDialog(
                transient_for=None,  # Changed from self
                modal=True,
                destroy_with_parent=True,
                message_type=message_type,
                buttons=Gtk.ButtonsType.OK,
                text=f"Error refreshing local data: {str(e)}"
            )
            dialog.run()
            dialog.destroy()


    def create_panels(self):
        # Check if panels already exist
        if hasattr(self, 'left_panel') and self.left_panel.get_parent():
            self.main_box.remove(self.left_panel)

        if hasattr(self, 'right_panel') and self.right_panel.get_parent():
            self.main_box.remove(self.right_panel)

        # Create left panel with grouped categories
        self.left_panel = self.create_grouped_category_panel("Categories", self.category_groups)

        # Create right panel
        self.right_panel = self.create_applications_panel("Applications")

        # Create panels container
        self.panels_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.panels_box.set_hexpand(True)

        # Pack the panels with proper expansion
        self.panels_box.pack_start(self.left_panel, False, False, 0)  # Left panel doesn't expand
        self.panels_box.pack_end(self.right_panel, True, True, 0)    # Right panel expands both ways

        # Add panels container to main box
        self.main_box.pack_start(self.panels_box, True, True, 0)

    def create_grouped_category_panel(self, title, groups):

        # Create container for categories
        panel_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel_container.set_spacing(6)
        panel_container.set_size_request(300, -1)  # Set fixed width
        panel_container.set_hexpand(False)
        panel_container.set_vexpand(True)
        panel_container.set_halign(Gtk.Align.FILL)  # Fill horizontally
        panel_container.set_valign(Gtk.Align.FILL)  # Align to top
        panel_container.get_style_context().add_class("category-panel")

        # Create scrollable area
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)   # Expand vertically
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Create container for categories
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        container.set_spacing(2)
        container.set_border_width(6)
        container.set_halign(Gtk.Align.FILL)  # Fill horizontally
        container.set_valign(Gtk.Align.START)  # Align to top
        container.set_hexpand(True)
        container.set_vexpand(False)   # Expand vertically

        # Dictionary to store category widgets
        self.category_widgets = {}

        first_category_group = False
        # Add group headers and categories
        for group_name, categories in groups.items():
            # Create a box for the header
            header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            header_box.get_style_context().add_class("category-group-header")
            header_box.set_hexpand(True)  # Make the box expand horizontally

            # Create the label
            group_header = Gtk.Label(label=group_name.capitalize())

            category_group_icon = "" # May be better as an array ?
            if "system" in group_name:
                category_group_icon = 'applications-system-symbolic'
            elif "collections" in group_name:
                category_group_icon = 'system-file-manager-symbolic'
            elif "categories" in group_name:
                category_group_icon = 'application-x-addon-symbolic'
            else:
                category_group_icon = 'user-bookmarks-symbolic'

            group_header_icon = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string(category_group_icon), 2)
            group_header_icon.set_size_request(-1, 32)
            group_header_icon.set_valign(Gtk.Align.CENTER)
            group_header.get_style_context().add_class("title-3")
            group_header.set_halign(Gtk.Align.START)

            # Add the label to the box
            header_box.pack_start(group_header_icon, False, False, 7)
            header_box.pack_start(group_header, False, False, 0)

            # Add the box to the container

            if first_category_group == False:
                first_category_group = True
            else:
                container.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 8)
            container.pack_start(header_box, False, False, 4)

            # Store widgets for this group
            self.category_widgets[group_name] = []

            # Add categories in the group
            for category, display_title in categories.items():
                # Create a clickable box for each category
                category_box = Gtk.EventBox()
                category_box.set_hexpand(True)
                category_box.set_halign(Gtk.Align.FILL)
                category_box.get_style_context().add_class("category-box")

                # Create label for the category
                category_label = Gtk.Label(label=display_title)
                category_label.set_halign(Gtk.Align.START)
                category_label.set_hexpand(True)
                category_label.get_style_context().add_class("category-button")

                # Add label to the box
                category_box.add(category_label)

                # Connect click event
                category_box.connect("button-release-event",
                                lambda widget, event, cat=category, grp=group_name:
                                self.on_category_clicked(cat, grp))

                # Store widget in group
                self.category_widgets[group_name].append(category_box)
                container.pack_start(category_box, False, False, 0)

        # Add container to scrolled window
        scrolled_window.add(container)

        # Pack the scrolled window directly into main box
        panel_container.pack_start(scrolled_window, True, True, 0)

        return panel_container

    def on_search_changed(self, searchentry):
        """Handle search text changes"""
        pass  # Don't perform search on every keystroke


    def on_search_activate(self, searchentry):
        """Handle Enter key press in search"""
        self.update_category_header("Search Results")
        search_term = searchentry.get_text().lower()
        if not search_term:
            # Reset to showing all categories when search is empty
            if self.current_page and self.current_group:
                self.show_category_apps(self.current_page)
            return

        # Combine all searchable fields
        searchable_items = []
        for app in self.all_apps:
            details = app.get_details()
            searchable_items.append({
                'app': app,
                'id': details['id'].lower(),
                'text': f"{details['name']} {details['description']} {details['categories']}".lower(),
                'name': details['name'].lower()
            })

        # Filter and rank results
        filtered_apps = self.rank_search_results(search_term, searchable_items)

        # Show search results
        self.show_search_results(filtered_apps)

    def rank_search_results(self, search_term, searchable_items):
        """Rank search results based on match type and component type filter"""
        exact_id_matches = []
        exact_name_matches = []
        partial_matches = []
        other_matches = []

        # Get current component type filter
        component_type_filter = self.current_component_type
        if component_type_filter is None:
            component_type_filter = None  # Allow all types

        # Process each item
        for item in searchable_items:
            # Check if component type matches filter
            if component_type_filter and item['app'].get_details()['kind'] != component_type_filter:
                continue

            # Check exact ID match
            if item['id'] == search_term:
                exact_id_matches.append(item['app'])
                continue

            # Check exact name match
            if item['name'] == search_term:
                exact_name_matches.append(item['app'])
                continue

            # Check for partial matches longer than 5 characters
            if len(search_term) > 5:
                if search_term in item['id'] or search_term in item['name']:
                    partial_matches.append(item['app'])
                    continue

            # Check for other matches
            if search_term in item['text']:
                other_matches.append(item['app'])

        # Combine results in order of priority
        return exact_id_matches + exact_name_matches + partial_matches + other_matches

    def show_search_results(self, apps):
        """Display search results in the right panel"""
        self.display_apps(apps)

    def on_category_clicked(self, category, group, *args):
        # Remove active state and reset labels for all widgets
        for group_name in self.category_widgets:
            for widget in self.category_widgets[group_name]:
                label = widget.get_children()[0]
                label.set_use_markup(False)

                # Loop through known original titles to find a match
                for grp in self.category_groups:
                    for key, val in self.category_groups[grp].items():
                        # Escape val for comparison with possible markup in label
                        safe_val = GLib.markup_escape_text(val)
                        if safe_val in label.get_text() or val in label.get_text():
                            if safe_val == "Updates" and len(self.updates_results) != 0:
                                markup_updates = f"{safe_val} ({len(self.updates_results)})"
                                label.set_markup(markup_updates)
                            else:
                                label.set_label(val)
                            label.get_style_context().remove_class("active")
                            break

        # Add active state and markup icon
        display_title = self.category_groups[group][category]
        for widget in self.category_widgets[group]:
            label = widget.get_children()[0]
            if display_title in label.get_text():
                safe_title = GLib.markup_escape_text(display_title)
                markup_selected = f" <span foreground='#3584e4'><b>❯</b></span>"
                if "Updates" in display_title and len(self.updates_results) != 0:
                    markup_updates = f" ({len(self.updates_results)})"
                    label.set_markup(safe_title+markup_updates+markup_selected)
                else:
                    label.set_markup(safe_title+markup_selected)
                label.get_style_context().add_class("active")
                break

        if self.updates_results == []:
            self.updates_available_bar.set_visible(False)

        self.current_category = category
        self.current_category_group = group
        self.current_page = category
        self.current_group = group
        self.update_category_header(category)
        self.update_subcategories_bar(category)
        self.update_updates_available_bar(category)
        self.show_category_apps(category)


    def refresh_current_page(self, preserve_scroll=True):
        """Refresh the currently displayed page (optionally preserving scroll)."""
        saved_scroll = 0.0
        if preserve_scroll and hasattr(self, "category_scrolled_window") and self.category_scrolled_window:
            vadj = self.category_scrolled_window.get_vadjustment()
            saved_scroll = vadj.get_value()

        # Close details window if open
        if hasattr(self, "details_window") and self.details_window:
            try:
                self.details_window.destroy()
            except Exception:
                pass
            self.details_window = None

        # Re-render the current page WITHOUT re-triggering category click logic
        if self.current_page and self.current_group:
            self.show_category_apps(self.current_page, preserve_scroll=preserve_scroll)

        if preserve_scroll:
            def _restore():
                vadj2 = self.category_scrolled_window.get_vadjustment()
                upper = vadj2.get_upper() - vadj2.get_page_size()
                vadj2.set_value(min(saved_scroll, max(0, upper)))
                return False

            GLib.idle_add(_restore)


    def update_category_header(self, category):
        """Update the category header text based on the selected category."""
        display_title = ""
        if category in self.category_groups['system']:
            display_title = self.category_groups['system'][category]
        elif category in self.category_groups['collections']:
            display_title = self.category_groups['collections'][category]
        elif category in self.category_groups['categories']:
            display_title = self.category_groups['categories'][category]
        else:            # Find the parent category and get the title
            for parent_category, subcategories in self.subcategory_groups.items():
                if category in subcategories:
                    parent_title = self.category_groups['categories'].get(parent_category, parent_category)
                    subcat_title = subcategories[category]
                    display_title = f"{parent_title} » {subcat_title}"
                    break
            if display_title == "":
                # Fallback if category isn't found
                display_title = category
        self.category_header.set_label(display_title)

    def create_applications_panel(self, title):
        # Create right panel
        self.right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.right_panel.set_hexpand(True)  # Add this line
        self.right_panel.set_vexpand(True)  # Add this line
        self.right_panel.get_style_context().add_class("app-panel")

        # Add category header
        self.category_header = Gtk.Label(label="")
        self.category_header.get_style_context().add_class("title-1")
        self.category_header.get_style_context().add_class("app-panel-header")
        self.category_header.set_hexpand(True)
        self.category_header.set_halign(Gtk.Align.START)
        self.right_panel.pack_start(self.category_header, False, False, 0)

        # Create subcategories bar (initially hidden)
        self.subcategories_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.subcategories_bar.set_hexpand(True)
        self.subcategories_bar.set_spacing(6)
        self.subcategories_bar.set_border_width(6)
        self.subcategories_bar.set_visible(False)
        self.subcategories_bar.set_halign(Gtk.Align.FILL)  # Ensure full width
        self.right_panel.pack_start(self.subcategories_bar, False, False, 0)

        # Create subcategories bar (initially hidden)
        self.updates_available_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.updates_available_bar.set_hexpand(True)
        self.updates_available_bar.set_spacing(6)
        self.updates_available_bar.set_border_width(6)
        self.updates_available_bar.set_visible(False)
        self.updates_available_bar.set_halign(Gtk.Align.FILL)  # Ensure full width
        self.right_panel.pack_start(self.updates_available_bar, False, False, 0)
        # self.right_panel.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        # Create scrollable area
        self.category_scrolled_window = Gtk.ScrolledWindow()
        self.category_scrolled_window.set_hexpand(True)
        self.category_scrolled_window.set_vexpand(True)

        # Create container for applications
        self.right_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.right_container.set_spacing(6)
        self.right_container.set_border_width(6)
        self.right_container.set_hexpand(True)  # Add this line
        self.right_container.set_vexpand(True)  # Add this line
        self.right_container.get_style_context().add_class("app-list")
        self.category_scrolled_window.add(self.right_container)
        self.right_panel.pack_start(self.category_scrolled_window, True, True, 0)
        return self.right_panel

    def create_subcategory_container(self):
        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        container.set_spacing(6)
        container.set_border_width(6)
        container.set_hexpand(True)
        container.set_halign(Gtk.Align.CENTER)
        container.set_homogeneous(False)
        return container

    def create_scroll_buttons(self):
        pan_start = Gtk.Button()
        pan_start_icon = Gio.Icon.new_for_string('pan-start-symbolic')
        pan_start.set_image(Gtk.Image.new_from_gicon(pan_start_icon, Gtk.IconSize.BUTTON))
        pan_start.get_style_context().add_class("pan-button")
        pan_start.connect("clicked", self.on_pan_start)

        pan_end = Gtk.Button()
        pan_end_icon = Gio.Icon.new_for_string('pan-end-symbolic')
        pan_end.set_image(Gtk.Image.new_from_gicon(pan_end_icon, Gtk.IconSize.BUTTON))
        pan_end.get_style_context().add_class("pan-button")
        pan_end.connect("clicked", self.on_pan_end)

        return pan_start, pan_end

    def build_subcategory_bar(self, category):
        container = self.create_subcategory_container()

        for subcategory, title in self.subcategory_groups[category].items():
            subcategory_box = Gtk.EventBox()
            subcategory_box.set_hexpand(False)
            subcategory_box.set_halign(Gtk.Align.START)
            subcategory_box.set_margin_top(2)
            subcategory_box.set_margin_bottom(2)

            label = Gtk.Label(label=title)
            label.set_halign(Gtk.Align.START)
            label.set_hexpand(False)
            label.get_style_context().add_class("subcategory-button")

            if subcategory == category:
                label.get_style_context().add_class("selected")

            subcategory_box.add(label)
            subcategory_box.connect(
                "button-release-event",
                lambda widget, event, subcat=subcategory: self.on_subcategory_clicked(subcat)
            )

            self.subcategory_buttons[subcategory] = label
            container.pack_start(subcategory_box, False, False, 0)

        return container

    def build_subcategory_context_view(self, category, parent_category):
        container = self.create_subcategory_container()

        parent_box = Gtk.EventBox()
        parent_box.set_hexpand(False)
        parent_box.set_halign(Gtk.Align.START)
        parent_box.set_margin_top(2)
        parent_box.set_margin_bottom(2)

        parent_label = Gtk.Label(label=self.category_groups['categories'][parent_category])
        parent_label.set_halign(Gtk.Align.START)
        parent_label.set_hexpand(False)
        parent_label.get_style_context().add_class("subcategory-button")

        parent_box.add(parent_label)
        parent_box.connect(
            "button-release-event",
            lambda widget, event, cat=parent_category, grp='categories':
            self.on_category_clicked(cat, grp)
        )
        container.pack_start(parent_box, False, False, 0)

        subcategory_box = Gtk.EventBox()
        subcategory_box.set_hexpand(False)
        subcategory_box.set_halign(Gtk.Align.START)
        subcategory_box.set_margin_top(2)
        subcategory_box.set_margin_bottom(2)

        subcategory_label = Gtk.Label(label=self.subcategory_groups[parent_category][category])
        subcategory_label.set_halign(Gtk.Align.START)
        subcategory_label.set_hexpand(False)
        subcategory_label.get_style_context().add_class("subcategory-button")
        subcategory_box.add(subcategory_label)
        subcategory_box.connect(
            "button-release-event",
            lambda widget, event, subcat=category:
            self.on_subcategory_clicked(subcat)
        )
        container.pack_start(subcategory_box, False, False, 0)

        return container

    def scroll_to_widget(self, widget):
        """Scrolls the scrolled window to ensure the widget is fully visible."""
        adjustment = self.scrolled_window.get_hadjustment()

        # Container is the Gtk.Box inside the scrolled window
        container = self.scrolled_window.get_child()
        if not container:
            return False

        # Translate widget's position relative to the container
        widget_coords = widget.translate_coordinates(container, 0, 0)
        if not widget_coords:
            return False

        widget_x, _ = widget_coords
        widget_width = widget.get_allocated_width()
        view_start = adjustment.get_value()
        view_end = view_start + adjustment.get_page_size()

        # Scroll only if the widget is outside the visible area
        if widget_x < view_start:
            adjustment.set_value(widget_x)
        elif (widget_x + widget_width) > view_end:
            adjustment.set_value(widget_x + widget_width - adjustment.get_page_size())

        return False

    def update_subcategories_bar(self, category):
        for child in self.subcategories_bar.get_children():
            child.destroy()
        self.subcategory_buttons.clear()

        if not hasattr(self, 'scrolled_window'):
            self.scrolled_window = Gtk.ScrolledWindow()

        for child in self.scrolled_window.get_children():
            child.destroy()

        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(False)
        self.scrolled_window.set_size_request(-1, 40)
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.scrolled_window.set_min_content_width(0)
        self.scrolled_window.set_max_content_width(-1)
        self.scrolled_window.set_overlay_scrolling(False)
        self.scrolled_window.get_style_context().add_class("no-scroll-bars")

        pan_start, pan_end = self.create_scroll_buttons()
        self.subcategories_bar.get_style_context().add_class("subcategory-group-header")
        self.subcategories_bar.set_visible(True)

        if category in self.subcategory_groups:
            container = self.build_subcategory_bar(category)
        else:
            parent_category = self.get_parent_category(category)
            if parent_category:
                container = self.build_subcategory_context_view(category, parent_category)
            else:
                self.subcategories_bar.set_visible(False)
                return

        self.scrolled_window.add(container)
        self.subcategories_bar.pack_start(pan_start, False, False, 0)
        self.subcategories_bar.pack_start(self.scrolled_window, True, True, 0)
        self.subcategories_bar.pack_start(pan_end, False, False, 0)
        self.subcategories_bar.queue_resize()
        self.subcategories_bar.show_all()

    def update_updates_available_bar(self, category):
        for child in self.updates_available_bar.get_children():
            child.destroy()

        if category == "updates":
            if self.updates_results != [] :
                self.updates_available_bar.get_style_context().add_class("updates_available_bar")
                self.updates_available_bar.set_visible(True)
                self.updates_available_bar.set_valign(Gtk.Align.CENTER)

                buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                buttons_box.set_spacing(8)
                buttons_box.set_halign(Gtk.Align.END)
                buttons_box.set_valign(Gtk.Align.CENTER)

                update_all_button = Gtk.Button()
                update_all_button.set_label("  "+"Update all")
                update_all_button.set_size_request(26, 26)  # 40x40 pixels
                update_all_button.get_style_context().add_class("app-action-button")
                update_all_icon = Gio.Icon.new_for_string('software-update-available-symbolic')
                update_all_button.set_image(Gtk.Image.new_from_gicon(update_all_icon, Gtk.IconSize.BUTTON))
                update_all_button.connect("clicked", self.on_update_all_button_clicked)
                update_all_button.set_always_show_image(True)
                buttons_box.pack_end(update_all_button, False, False, 0)

                # Create left label
                left_label = Gtk.Label(label="New updates are available")
                left_label.set_halign(Gtk.Align.CENTER)
                left_label.get_style_context().add_class("updates_available_bar_label")
                self.updates_available_bar.pack_end(buttons_box, False, False, 0)
                self.updates_available_bar.pack_start(left_label, False, False, 8)

                self.updates_available_bar.show_all()
        else:
            self.updates_available_bar.set_visible(False)

    def on_update_all_button_clicked(self, button=None):
        # Create a message dialog
        dialog = Gtk.MessageDialog(
            transient_for=self,  # Parent window
            modal=True,                # Make it modal
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Download and install all available Flatpak updates?",
            title="Confirm"
        )

        # Show the dialog and get the response
        response = dialog.run()

        # Handle the response
        if response == Gtk.ResponseType.OK:
            # Perform Removal
            def perform_update():
                # Show waiting dialog
                GLib.idle_add(self.show_waiting_dialog, "Updating packages...")

                success, message = fp_turbo.update_all_flatpaks(self.updates_results, self.system_mode)

                # Update UI on main thread
                GLib.idle_add(lambda: self.on_task_complete(success, message))

            # Start spinner and begin installation
            thread = threading.Thread(target=perform_update)
            thread.daemon = True  # Allow program to exit even if thread is still running
            thread.start()
        dialog.destroy()

    def get_parent_category(self, subcategory):
        for parent, subcats in self.subcategory_groups.items():
            if subcategory in subcats:
                return parent
        return None

    def on_pan_start(self, button):
        # Get the scrolled window's adjustment
        adjustment = self.scrolled_window.get_hadjustment()
        # Scroll to the left by a page
        adjustment.set_value(adjustment.get_value() - adjustment.get_page_size())

    def on_pan_end(self, button):
        # Get the scrolled window's adjustment
        adjustment = self.scrolled_window.get_hadjustment()
        # Scroll to the right by a page
        adjustment.set_value(adjustment.get_value() + adjustment.get_page_size())

    def highlight_selected_subcategory(self, selected_subcat):
        for subcat, widget in self.subcategory_buttons.items():
            if subcat == selected_subcat:
                widget.get_style_context().add_class("active")
            else:
                widget.get_style_context().remove_class("active")

        # Scroll to make sure the selected subcategory is visible
        selected_widget = self.subcategory_buttons.get(selected_subcat)
        if selected_widget:
            adj = self.scrolled_window.get_hadjustment()
            alloc = selected_widget.get_allocation()
            new_value = alloc.x + alloc.width / 2 - adj.get_page_size() / 2
            adj.set_value(max(0, new_value))

    def on_subcategory_clicked(self, subcategory):
        """Handle subcategory button clicks."""
        # Remove 'selected' from all subcategory buttons
        for label in self.subcategory_buttons.values():
            label.get_style_context().remove_class("selected")

        # Add 'selected' to the clicked one
        if subcategory in self.subcategory_buttons:
            self.subcategory_buttons[subcategory].get_style_context().add_class("selected")

        # Update current state
        self.current_category = subcategory
        self.current_category_group = 'subcategories'
        self.current_page = subcategory
        self.current_group = 'subcategories'
        self.update_category_header(subcategory)
        self.highlight_selected_subcategory(subcategory)
        self.show_category_apps(subcategory)
        if subcategory in self.subcategory_buttons:
            selected_widget = self.subcategory_buttons[subcategory]
            GLib.idle_add(self.scroll_to_widget, selected_widget)

    # Create and connect buttons
    def create_button(self, callback, app, label=None, condition=None):
        button = Gtk.Button(label=label) if label else Gtk.Button()
        button.get_style_context().add_class("app-button")

        if condition is not None and not condition(app):
            return None

        button.connect("clicked", callback, app)
        return button

    def clear_container(self, container):
        """Clear all widgets from a container"""
        for child in container.get_children():
            child.destroy()

    def get_app_priority(self, kind):
        """Convert AppKind to numeric priority for sorting"""
        priorities = {
            "DESKTOP_APP": 0,
            "ADDON": 1,
            "RUNTIME": 2
        }
        return priorities.get(kind, 3)

    def show_category_apps(self, category, preserve_scroll=False):
        if self.current_group == "system" and category == "installed":
            # Installed/Updates are local; don't block on metadata.
            if category in ("installed", "updates"):
                apps = []
                if not preserve_scroll:
                    vadjustment = self.category_scrolled_window.get_vadjustment()
                    vadjustment.set_value(vadjustment.get_lower())

                if category == "installed":
                    apps.extend(list(self.installed_results or []))
                else:
                    apps.extend(list(self.updates_results or []))

                if apps:
                    apps.sort(key=lambda app: self.get_app_priority(app.get_details().get('kind')))
                self.display_apps(apps)
                return

            # Everything else needs metadata
            if self.metadata_loading or not self.metadata_loaded:
                self._show_loading_view("Fetching Metadata… Please wait")
                return

        collections_keys = self._collections_keys()  # <-- add this

        if self.current_group == "collections" and category in collections_keys:
            if getattr(self, "metadata_loading", False):
                self._show_loading_view("Fetching Metadata… Please wait")
                return
            if not getattr(self, "metadata_loaded", False):
                err = getattr(self, "metadata_error", None)
                if err:
                    self._show_loading_error_view(err)
                else:
                    self._show_loading_view("Fetching Metadata… Please wait")
                    self.start_metadata_refresh(show_error_dialog=False, force=True)
                return

        if self.metadata_loading or not self.metadata_loaded:
            self._show_loading_view("Fetching Metadata… Please wait")
            return

        # Initialize apps list
        apps = []
        if not preserve_scroll:
            vadjustment = self.category_scrolled_window.get_vadjustment()
            vadjustment.set_value(vadjustment.get_lower())

        # Load system data
        if category == "installed":
            apps.extend([app for app in self.installed_results])
        if category == "updates":
            apps.extend([app for app in self.updates_results])

        if (category == "installed") or (category == "updates"):
            # Sort apps by component type priority
            if apps:
                apps.sort(key=lambda app: self.get_app_priority(app.get_details()['kind']))

        # Define paths
        app_data_dir = Path.home() / ".local" / "share" / "flatpost"
        system_data_dir = Path("/usr/share/flatpost")

        # Ensure local directory exists
        app_data_dir.mkdir(parents=True, exist_ok=True)

        # Define file paths
        json_path = app_data_dir / "collections_data.json"

        # Load collections data
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                collections_data = json.load(f)

                # Find the specific category in collections data
                category_entry = next((
                    entry for entry in collections_data
                    if entry['category'] == category
                ), None)

                if category_entry:
                    # Get all app IDs in this category
                    app_ids_in_category = [
                        hit['app_id'] for hit in category_entry['data']['hits']
                    ]

                    # Filter apps based on presence in category
                    apps.extend([
                        app for app in self.collection_results
                        if app.get_details()['id'] in app_ids_in_category
                    ])
                else:
                    # Fallback to previous behavior if category isn't in collections
                    apps.extend([
                        app for app in self.collection_results
                        if category in app.get_details()['categories']
                    ])

        except (IOError, json.JSONDecodeError) as e:
            apps.extend([
                app for app in self.collection_results
                if category in app.get_details()['categories']
            ])


        if category == "repositories":
            # Clear existing content
            for child in self.right_container.get_children():
                child.destroy()

            # Create header bar
            header_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            header_bar.set_hexpand(True)
            header_bar.set_spacing(6)
            header_bar.set_border_width(6)

            # Create left label
            left_label = Gtk.Label(label="On/Off")
            left_label.get_style_context().add_class("repo-list-header")
            left_label.set_halign(Gtk.Align.START)  # Align left
            header_bar.pack_start(left_label, True, True, 0)

            # Center container to fix "URL" label alignment
            center_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            center_container.set_halign(Gtk.Align.START)  # Align left

            # Create center label
            center_label = Gtk.Label(label="URL")
            center_label.get_style_context().add_class("repo-list-header")
            center_label.set_halign(Gtk.Align.START)  # Align center

            center_container.pack_start(center_label, True, True, 0)
            header_bar.pack_start(center_container, True, True, 0)

            # Create right label
            right_label = Gtk.Label(label="+/-")
            right_label.get_style_context().add_class("repo-list-header")
            right_label.set_halign(Gtk.Align.END)   # Align right
            header_bar.pack_end(right_label, False, False, 0)

            # Add header bar to container
            self.right_container.pack_start(header_bar, False, False, 0)

            # Get list of repositories
            repos = fp_turbo.repolist(self.system_mode)

            # Create a scrolled window for repositories
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_hexpand(True)
            scrolled_window.set_vexpand(True)

            # Create container for repositories
            repo_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            repo_container.set_spacing(6)
            repo_container.set_border_width(6)

            # Add repository button
            add_repo_button = Gtk.Button()
            add_repo_button.set_size_request(26, 26)  # 40x40 pixels
            add_repo_button.get_style_context().add_class("app-action-button")
            add_icon = Gio.Icon.new_for_string('list-add-symbolic')
            add_repo_button.set_image(Gtk.Image.new_from_gicon(add_icon, Gtk.IconSize.BUTTON))
            add_repo_button.connect("clicked", self.on_add_repo_button_clicked)

            add_flathub_repo_button = Gtk.Button(label="Add Flathub Repo")
            add_flathub_repo_button.connect("clicked", self.on_add_flathub_repo_button_clicked)

            add_flathub_beta_repo_button = Gtk.Button(label="Add Flathub Beta Repo")
            add_flathub_beta_repo_button.connect("clicked", self.on_add_flathub_beta_repo_button_clicked)

            # Check for existing Flathub repositories and disable buttons accordingly
            flathub_url = "https://dl.flathub.org/repo/"
            flathub_beta_url = "https://dl.flathub.org/beta-repo/"

            existing_urls = [repo.get_url().rstrip('/') for repo in repos]
            add_flathub_repo_button.set_sensitive(flathub_url.rstrip('/') not in existing_urls)
            add_flathub_beta_repo_button.set_sensitive(flathub_beta_url.rstrip('/') not in existing_urls)

            # Add repositories to container
            for repo in repos:
                repo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                repo_box.set_spacing(6)
                repo_box.set_hexpand(True)

                # Create checkbox
                if self.system_mode:
                    checkbox = Gtk.CheckButton(label=f"{repo.get_name()} (System)")
                else:
                    checkbox = Gtk.CheckButton(label=f"{repo.get_name()} (User)")
                checkbox.set_active(not repo.get_disabled())
                if not repo.get_disabled():
                    checkbox.get_style_context().remove_class("dim-label")
                else:
                    checkbox.get_style_context().add_class("dim-label")
                checkbox.connect("toggled", self.on_repo_toggled, repo)
                checkbox_url_label = Gtk.Label(label=repo.get_url())
                checkbox_url_label.set_halign(Gtk.Align.START)
                checkbox_url_label.set_hexpand(True)
                checkbox_url_label.get_style_context().add_class("dim-label")

                # Create delete button
                delete_button = Gtk.Button()
                delete_button.set_size_request(26, 26)  # 40x40 pixels
                delete_button.get_style_context().add_class("app-action-button")
                delete_icon = Gio.Icon.new_for_string('list-remove-symbolic')
                delete_button.set_image(Gtk.Image.new_from_gicon(delete_icon, Gtk.IconSize.BUTTON))
                delete_button.get_style_context().add_class("destructive-action")
                delete_button.connect("clicked", self.on_repo_delete, repo)

                # Add widgets to box
                repo_box.pack_start(checkbox, False, False, 0)
                repo_box.pack_start(checkbox_url_label, False, False, 0)
                repo_box.pack_end(delete_button, False, False, 0)

                # Add box to container
                repo_container.pack_start(repo_box, False, False, 0)

            repo_container.pack_start(add_repo_button, False, False, 0)
            repo_container.pack_start(add_flathub_repo_button, False, False, 0)
            repo_container.pack_start(add_flathub_beta_repo_button, False, False, 0)

            # Add container to scrolled window
            scrolled_window.add(repo_container)
            self.right_container.pack_start(scrolled_window, True, True, 0)

            self.right_container.show_all()
            return

        # Apply component type filter if set
        component_type_filter = self.current_component_type
        if component_type_filter:
            apps = [app for app in apps if app.get_details()['kind'] == component_type_filter]

        self.display_apps(apps)

    def create_scaled_icon(self, icon, size=64, is_themed=False):
        if is_themed:
            # For themed icons, create a pixbuf directly using the icon theme
            icon_theme = Gtk.IconTheme.get_default()
            pb = icon_theme.load_icon(icon.get_names()[0], size, Gtk.IconLookupFlags.FORCE_SIZE)
        else:
            # For file-based icons
            pb = GdkPixbuf.Pixbuf.new_from_file(icon)

        # Scale to 64x64 using high-quality interpolation
        scaled_pb = pb.scale_simple(
            size, size,  # New dimensions
            GdkPixbuf.InterpType.BILINEAR  # High-quality scaling
        )

        # Create the image widget from the scaled pixbuf
        return Gtk.Image.new_from_pixbuf(scaled_pb)

    def display_apps(self, apps):
        """Display applications in the right container."""
        self._clear_views_container()
        apps_by_id = self._group_apps_by_id(apps)
        for app_id, app_data in apps_by_id.items():
            self._create_and_add_app_row(app_data)
        if apps_by_id == {}:
            self._create_nothing_to_show()

    def _create_nothing_to_show(self):
        """Create and add a row for a single application."""

        container = self._create_app_container()
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, hexpand=True, vexpand=True)

        nothing_icon = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string("preferences-desktop-screensaver-symbolic"), 6)
        nothing_icon.set_pixel_size(128)

        nothing_title = Gtk.Label(label="It's empty here...", halign=Gtk.Align.CENTER)
        nothing_title.get_style_context().add_class('title-2')

        nothing_label = Gtk.Label(label="We couldn't find any applications corresponding to your demand.")
        nothing_label.set_halign(Gtk.Align.CENTER)
        nothing_label.get_style_context().add_class('dim-label')

        content_box.pack_start(nothing_icon, False, False, 16)
        content_box.pack_start(nothing_title, False, False, 0)
        content_box.pack_start(nothing_label, False, False, 0)
        content_box.set_opacity(0.5)
        content_box.set_margin_bottom(64)
        content_box.get_style_context().add_class('app-panel-nothing')

        event_box = Gtk.EventBox()

        event_box.add(content_box)
        container.add(event_box)

        self.right_container.pack_start(container, False, False, 0)
        # self.right_container.pack_start(Gtk.Separator(), False, False, 0)
        self.right_container.show_all()

    def _clear_views_container(self):
        """Clear all children from the right container."""
        for child in self.right_container.get_children():
            child.destroy()

    def _group_apps_by_id(self, apps):
        """Group applications by their IDs and collect repositories."""
        apps_dict = {}
        for app in apps:
            details = app.get_details()
            app_id = details['id']

            if app_id not in apps_dict:
                apps_dict[app_id] = {'app': app, 'repos': set()}

            apps_dict[app_id]['repos'].add(details.get('repo', 'unknown'))
        return apps_dict

    def _create_and_add_app_row(self, app_data):
        """Create and add a row for a single application."""
        app = app_data['app']
        details = app.get_details()

        status = self._get_app_status(app)
        container = self._create_app_container()
        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._setup_icon(content_box, details)
        self._setup_text_layout(content_box, details, app_data['repos'])
        row_state = {}
        self._setup_buttons(content_box, status, app, row_state=row_state)
        app_id = details.get("id")
        if app_id:
            row_state["app"] = app
            row_state["content_box"] = content_box
            row_state["row_container"] = container  # outer row container, useful for removing row
            self.app_rows[app_id] = row_state
        content_box.get_style_context().add_class('app-list-item')

        event_box = Gtk.EventBox()
        event_box.connect("button-release-event",
                        lambda w, e: self.click_event(app, content_box))
        event_box.connect("enter-notify-event", lambda w, e: self.enter_hover_event(content_box)); # These connect signals handles hover
        event_box.connect("leave-notify-event", lambda w, e: self.leave_hover_event(content_box));

        event_box.add(content_box)
        container.add(event_box)

        self.right_container.pack_start(container, False, False, 0)
        # self.right_container.pack_start(Gtk.Separator(), False, False, 0)
        self.right_container.show_all()

    def click_event(self, app=None, content=None):
        if content.get_style_context().has_class("hover-event") == True:
            if content.get_style_context().has_class("app-list-item") == True:
                self.on_details_clicked(Gtk.Button, app)

    def update_row_status(self, app_id: str):
        row = self.app_rows.get(app_id)
        if not row:
            return

        app = row.get("app")
        if not app:
            return

        status = self._get_app_status(app)

        # If we're on list pages like Installed/Updates, the row may need to disappear.
        if self.current_group == "system" and self.current_page == "installed" and not status["is_installed"]:
            try:
                row["row_container"].destroy()
            except Exception:
                pass
            self.app_rows.pop(app_id, None)
            return

        if self.current_group == "system" and self.current_page == "updates" and not status["is_updatable"]:
            try:
                row["row_container"].destroy()
            except Exception:
                pass
            self.app_rows.pop(app_id, None)
            return

        # Rebuild buttons in-place (simplest + robust)
        old_box = row.get("buttons_box")
        if old_box:
            try:
                old_box.destroy()
            except Exception:
                pass

        # Recreate buttons with current status
        self._setup_buttons(row["content_box"], status, app, row_state=row)

        # Make sure GTK relayout happens
        row["content_box"].show_all()

    def _get_app_status(self, app):
        """Determine installation and update status of an application."""
        details = app.get_details()
        app_id = details.get('id')
        return {
            'is_installed': bool(app_id) and any(pkg.id == app_id for pkg in self.installed_results),
            'is_updatable': bool(app_id) and any(pkg.id == app_id for pkg in self.updates_results),
            'has_donation_url': bool(app.get_details().get('urls', {}).get('donation'))
        }

    def _create_app_container(self):
        """Create the horizontal container for an application row."""
        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        return container

    def _setup_icon(self, container, details):
        """Set up the icon box and icon for the application."""
        icon_box = Gtk.Box()
        icon_box.set_size_request(88, -1)

        icon_widget = self.create_scaled_icon(
            Gio.Icon.new_for_string('package-x-generic-symbolic'),
            is_themed=True
        )

        icon_filename = details.get('icon_filename')
        icon_path_128 = details.get('icon_path_128')

        if icon_filename and icon_path_128:
            icon_path = Path(f"{icon_path_128}/{icon_filename}")
            if icon_path.exists():
                icon_widget = self.create_scaled_icon(str(icon_path), is_themed=False)

        icon_widget.set_size_request(64, 64)
        icon_box.pack_start(icon_widget, False, True, 0)
        container.pack_start(icon_box, False, False, 0)

    def _setup_text_layout(self, container, details, repos):
        """Set up the text layout including title, kind, repositories, and description."""
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_box.set_spacing(4)
        right_box.set_hexpand(True)

        name = details.get('name') or details.get('id') or "Unknown"
        developer = details.get('developer') or ""
        summary = details.get('summary') or ""
        kind = details.get('kind') or "UNKNOWN"

        # Title
        title_label = Gtk.Label(label=name)
        title_label.get_style_context().add_class("app-list-header")
        title_label.set_halign(Gtk.Align.START)
        title_label.set_hexpand(True)
        right_box.pack_start(title_label, False, False, 0)

        #Developer
        developer_label = Gtk.Label(label=f"{developer}")
        developer_label.set_halign(Gtk.Align.START)
        developer_label.set_hexpand(True)
        developer_label.set_line_wrap(True)
        developer_label.set_line_wrap_mode(Gtk.WrapMode.WORD)
        developer_label.get_style_context().add_class("dim-label")
        developer_label.get_style_context().add_class("app-list-developer")
        right_box.pack_start(developer_label, False, False, 0)

        # Description
        desc_label = Gtk.Label(label=summary)
        desc_label.set_halign(Gtk.Align.START)
        desc_label.set_yalign(1)
        desc_label.set_hexpand(True)
        desc_label.set_line_wrap(True)
        desc_label.set_line_wrap_mode(Gtk.WrapMode.WORD)
        desc_label.get_style_context().add_class("app-list-summary")
        right_box.pack_start(desc_label, False, False, 0)

        # Kind label (disabled)
        kind_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        kind_box.set_spacing(4)
        kind_box.set_halign(Gtk.Align.START)
        kind_box.set_valign(Gtk.Align.START)
        kind_box.get_style_context().add_class("dim-label")
        kind_box.get_style_context().add_class("app-list-misc")

        kind_label = Gtk.Label(label=f"Type: {details['kind']}")
        # kind_box.pack_end(kind_label, False, False, 0)
        # right_box.pack_start(kind_box, False, False, 0)

        # Kind & Repositories Label (in one line)
        repo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        repo_box.set_spacing(4)
        repo_box.set_halign(Gtk.Align.START)
        repo_box.set_valign(Gtk.Align.START)
        repo_box.get_style_context().add_class("dim-label")
        repo_box.get_style_context().add_class("app-list-misc")

        repo_list_label = Gtk.Label(label=f"Type: {kind} • Sources:")
        repo_box.pack_start(repo_list_label, False, False, 0)

        for repo in sorted(repos):
            repo_label = Gtk.Label(label=f"{repo}")
            repo_label.set_halign(Gtk.Align.START)
            repo_box.pack_end(repo_label, False, False, 0)

        right_box.pack_start(repo_box, False, False, 0)

        container.pack_start(right_box, True, True, 0)

    def _setup_buttons(self, container, status, app, panel=None, row_state=None):
        """Set up action buttons for the application."""
        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        buttons_box.set_spacing(8)
        buttons_box.set_halign(Gtk.Align.END)
        buttons_box.set_valign(Gtk.Align.CENTER)  # Center vertically
        if row_state is not None:
            row_state["buttons_box"] = buttons_box
            row_state["buttons"] = {}
        update_button = False

        # Add install/remove buttons separately
        if status['is_updatable'] and self.current_page != "installed":
            btn = self._add_action_button(
                buttons_box,
                True,
                app,
                self.on_update_clicked,
                'software-update-available-symbolic',
                None,
                "Update"
            )
            if row_state is not None:
                row_state["buttons"]["update"] = btn
            update_button = True

        elif status['is_installed']:
            btn = self._add_action_button(
                buttons_box,
                True,
                app,
                self.on_remove_clicked,
                "list-remove-symbolic",
                None,
                "Uninstall"
            )
            if row_state is not None:
                row_state["buttons"]["uninstall"] = btn
        else:
            btn = self._add_action_button(
                buttons_box,
                True,
                app,
                self.on_install_clicked,
                "list-add-symbolic",
                None,
                "Install",
                True
            )
            if row_state is not None:
                row_state["buttons"]["install"] = btn

        if status['is_installed']:
            btn = self._add_action_button(
                buttons_box,
                True,
                app,
                self.on_app_options_clicked,
                "applications-system-symbolic",
                "Manage permissions"
            )
            if row_state is not None:
                row_state["buttons"]["gear"] = btn
        # if panel != "info":
        #     self._add_action_button(
        #         buttons_box,
        #         True,
        #         app,
        #         self.on_details_clicked,
        #         'help-about-symbolic',
        #         "Show details"
        #     )

        if status['has_donation_url']:
            if panel == "info":
                btn = self._add_action_button(
                    buttons_box,
                    True,
                    app,
                    self.on_donate_clicked,
                    'emote-love-symbolic',
                    None,
                    "Donate"
                )
                if row_state is not None:
                    row_state["buttons"]["donate"] = btn
            else:
                btn = self._add_action_button(
                    buttons_box,
                    True,
                    app,
                    self.on_donate_clicked,
                    'emote-love-symbolic',
                    "Donate"
                )
                if row_state is not None:
                    row_state["buttons"]["donate"] = btn

        if status['is_updatable'] and update_button != True:
            btn = self._add_action_button(
                buttons_box,
                True,
                app,
                self.on_update_clicked,
                'software-update-available-symbolic',
                "Update"
            )
            if row_state is not None:
                row_state["buttons"]["update"] = btn


        container.pack_end(buttons_box, False, False, 0)

    def _add_action_button(self, parent, visible, app, callback, icon_name, tooltip=None, label_name=None, accent=False):
        """Helper method to add a consistent action button."""
        if not visible:
            return

        button = self.create_button(callback, app)
        if button:
            button.set_size_request(26, 26)
            button.get_style_context().add_class("app-action-button")

            icon = Gio.Icon.new_for_string(icon_name)
            button.set_image(Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON))
            parent.pack_end(button, False, False, 0)
            button.set_always_show_image(True)

            if tooltip:
                button.set_tooltip_text(tooltip)
            if label_name:
                button.set_label("  " + label_name)
                button.get_style_context().add_class("icon_label")
            if accent is True:
                button.get_style_context().add_class("suggested-action")

            return button

        return None

    def show_install_progress_dialog(self, title="Installing…", subtitle="Please wait while task is running"):
        if getattr(self, "waiting_dialog", None):
            try:
                self.waiting_dialog.destroy()
            except Exception:
                pass

        dialog = Gtk.Dialog(
            title=title,
            transient_for=self,
            modal=True,
            destroy_with_parent=True
        )
        dialog.set_default_size(420, -1)

        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        status_label = Gtk.Label(label=subtitle)
        status_label.set_xalign(0)

        pbar = Gtk.ProgressBar()
        pbar.set_show_text(True)
        pbar.set_fraction(0.0)
        pbar.set_text("0%")

        box.add(status_label)
        box.add(pbar)
        box.show_all()

        self.waiting_dialog = dialog
        self.install_status_label = status_label
        self.install_progressbar = pbar

        dialog.show()


    def update_install_progress(self, pct: int, status: str = ""):
        if not getattr(self, "install_progress_bar", None):
            return False

        frac = max(0.0, min(1.0, pct / 100.0))
        self.install_progress_bar.set_fraction(frac)
        self.install_progress_bar.set_text(f"{pct}%")

        if getattr(self, "install_progress_label", None):
            self.install_progress_label.set_text(status or "Installing…")

        return False  # important for idle_add: don't repeat


    def show_waiting_dialog(self, message="Please wait while task is running..."):
        """Show a modal dialog with a spinner"""
        self.waiting_dialog = Gtk.Dialog(
            title="Running Task...",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )

        # Create spinner
        self.spinner = Gtk.Spinner()
        self.spinner.start()

        # Add content
        box = self.waiting_dialog.get_content_area()
        box.set_spacing(12)
        box.set_border_width(12)

        # Add label and spinner
        box.pack_start(Gtk.Label(label=message), False, False, 0)
        box.pack_start(self.spinner, False, False, 0)

        # Show dialog
        self.waiting_dialog.show_all()

    def on_install_clicked(self, button=None, app=None):
        """Handle the Install button click with installation options"""
        if not app:
            self._show_error("Error: No app specified")
            return

        title, label = self._get_dialog_details(app, button)
        dialog = self._create_dialog(title)
        content_area = self._setup_dialog_content(dialog, label)

        if button and app:
            self._handle_repository_selection(content_area, app)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            self._perform_installation(dialog, app, button)

        dialog.destroy()

    def _get_dialog_details(self, app, button):
        """Extract dialog details based on input"""
        if button:
            details = app.get_details()
            return f"Install {details['name']}?", f"Install: {details['id']}"
        return f"Install {app}?", f"Install: {app}"

    def _create_dialog(self, title):
        """Create and configure the dialog"""
        dialog = Gtk.Dialog(
            title=title,
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        # Add buttons using the new method
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Install", Gtk.ResponseType.OK)
        return dialog

    def _setup_dialog_content(self, dialog, label):
        """Setup dialog content area"""
        content_area = dialog.get_content_area()
        content_area.set_spacing(12)
        content_area.set_border_width(12)
        content_area.pack_start(Gtk.Label(label=label), False, False, 0)

        installation_type = "User" if not self.system_mode else "System"
        content_area.pack_start(
            Gtk.Label(label=f"Installation Type: {installation_type}"),
            False, False, 0
        )
        return content_area

    def _handle_repository_selection(self, content_area, app):
        self.repo_combo = Gtk.ComboBoxText()

        searcher = fp_turbo.get_reposearcher(self.system_mode)
        repos = fp_turbo.repolist(self.system_mode)
        app_id = app.get_details()['id']

        available = [
            repo for repo in repos
            if (not repo.get_disabled()) and searcher.search_flatpak(app_id, repo.get_name())
        ]

        if len(available) >= 2:
            for repo in available:
                self.repo_combo.append_text(repo.get_name())
            self.repo_combo.set_active(0)
            content_area.pack_start(self.repo_combo, False, False, 0)
        elif len(available) == 1:
            # Keep a "selected repo" without showing the widget
            self.repo_combo.append_text(available[0].get_name())
            self.repo_combo.set_active(0)
        else:
            # No repos found: you can choose to show a label instead
            lbl = Gtk.Label(label="No enabled repositories contain this app.")
            lbl.get_style_context().add_class("dim-label")
            content_area.pack_start(lbl, False, False, 0)

    def show_install_progress_dialog(self, title="Please wait while task is running"):
        self.waiting_dialog = Gtk.Dialog(
            title=title,
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )

        content = self.waiting_dialog.get_content_area()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)
        content.add(box)  # GTK3

        self.install_progress_label = Gtk.Label(label="Preparing…")
        self.install_progress_label.set_xalign(0.0)

        self.install_progress_bar = Gtk.ProgressBar()
        self.install_progress_bar.set_show_text(True)
        self.install_progress_bar.set_fraction(0.0)
        self.install_progress_bar.set_text("0%")

        box.pack_start(self.install_progress_label, False, False, 0)
        box.pack_start(self.install_progress_bar, False, False, 0)

        self.waiting_dialog.show_all()

    def _perform_installation(self, dialog, app, button):
        selected_repo = self.repo_combo.get_active_text() if button else None

        def progress_cb(pct, status):
            GLib.idle_add(self.update_install_progress, pct, status)

        def installation_thread():
            GLib.idle_add(self.show_install_progress_dialog)

            if button:
                success, message = fp_turbo.install_flatpak(app, selected_repo, self.system_mode, progress_cb=progress_cb)
                aid = app.get_details().get("id")
            else:
                success, message = fp_turbo.install_flatpakref(app, self.system_mode, progress_cb=progress_cb)
                aid = str(app)

            GLib.idle_add(lambda: self.on_task_complete(success, message, aid))

        thread = threading.Thread(target=installation_thread, daemon=True)
        thread.start()


    def on_task_complete(self, success, message, app_id=None):
        """Handle task completion"""

        # Only show a popup if something failed (or if you want to show warnings)
        if not success and message:
            finished_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=message
            )
            finished_dialog.run()
            finished_dialog.destroy()

        # Always do the "OK actions" (refresh/update UI) regardless of success
        self.refresh_local()

        # Option B: update just the affected row (no full refresh) when possible
        if self.enable_row_updates and app_id and app_id in self.app_rows:
            if self.current_group == "system" and self.current_page in ("installed", "updates"):
                self.refresh_current_page(preserve_scroll=True)
            else:
                self.update_row_status(app_id)
                self.update_updates_available_bar(self.current_page)
        else:
            self.refresh_current_page(preserve_scroll=True)

        # Close the waiting/progress dialog
        if hasattr(self, "waiting_dialog") and self.waiting_dialog:
            try:
                self.waiting_dialog.destroy()
            except Exception:
                pass
            self.waiting_dialog = None


    def on_remove_clicked(self, button, app):
        """Handle the Remove button click with removal options"""
        details = app.get_details()

        # Create dialog
        dialog = Gtk.Dialog(
            title=f"Remove {details['name']}?",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        # Add buttons using the new method
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Remove", Gtk.ResponseType.OK)

        # Create content area
        content_area = dialog.get_content_area()
        content_area.set_spacing(12)
        content_area.set_border_width(12)

        content_area.pack_start(Gtk.Label(label=f"Remove: {details['id']}?"), False, False, 0)

        # Show dialog
        dialog.show_all()

        # Run dialog
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            # Perform Removal
            def perform_removal():
                # Show waiting dialog
                GLib.idle_add(self.show_waiting_dialog, "Removing package...")

                success, message = fp_turbo.remove_flatpak(app, self.system_mode)

                # Update UI on main thread
                aid = app.get_details().get("id")
                GLib.idle_add(lambda: self.on_task_complete(success, message, aid))
            # Start spinner and begin installation
            thread = threading.Thread(target=perform_removal)
            thread.daemon = True  # Allow program to exit even if thread is still running
            thread.start()

        dialog.destroy()

    def _add_bus_section(self, app_id, app, listbox, section_title, perm_type):
        """Helper method to add System Bus or Session Bus section"""
        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        # Get permissions
        global_success, global_perms = fp_turbo.global_list_other_perm_values(perm_type, True, self.system_mode)
        if not global_success:
            global_perms = {"paths": []}
        success, perms = fp_turbo.list_other_perm_values(app_id, perm_type, self.system_mode)
        if not success:
            perms = {"paths": []}

        # Add Talks section
        talks_row = Gtk.ListBoxRow(selectable=False)
        talks_row.get_style_context().add_class("permissions-row")
        talks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        talks_box.get_style_context().add_class("permissions-bus-box")
        talks_row.add(talks_box)

        talks_header = Gtk.Label(label="Talks", xalign=0)
        talks_header.get_style_context().add_class("permissions-item-label")
        talks_box.pack_start(talks_header, False, False, 0)

        # Add talk paths
        for path in global_perms["paths"]:
            if path != "" and "talk" in path:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, perm_type)

                # Configure button based on permission type
                btn.set_sensitive(False)
                btn.get_style_context().add_class("destructive-action")

                btn_box.pack_end(btn, False, False, 0)
                indicator_label = Gtk.Label(label="*", xalign=0)
                btn_box.pack_end(indicator_label, False, True, 0)

                hbox.pack_end(btn_box, False, False, 0)
                talks_box.add(row)

        for path in perms["paths"]:
            if path != "" and "talk" in path and path not in global_perms["paths"]:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, perm_type)

                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)
                talks_box.add(row)

        listbox.add(talks_row)

        # Add Owns section
        owns_row = Gtk.ListBoxRow(selectable=False)
        owns_row.get_style_context().add_class("permissions-row")
        owns_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        owns_box.get_style_context().add_class("permissions-bus-box")
        owns_row.add(owns_box)

        owns_header = Gtk.Label(label="Owns", xalign=0)
        owns_header.get_style_context().add_class("permissions-item-label")
        owns_box.pack_start(owns_header, False, False, 0)

        # Add own paths
        for path in global_perms["paths"]:
            if path != "" and "own" in path:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, perm_type)

                # Configure button based on permission type
                btn.set_sensitive(False)
                btn.get_style_context().add_class("destructive-action")

                btn_box.pack_end(btn, False, False, 0)
                indicator_label = Gtk.Label(label="*", xalign=0)
                btn_box.pack_end(indicator_label, False, True, 0)

                hbox.pack_end(btn_box, False, False, 0)
                owns_box.add(row)

        for path in perms["paths"]:
            if path != "" and "own" in path and path not in global_perms["paths"]:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, perm_type)

                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)

                owns_box.add(row)

        owns_row.show_all()
        listbox.add(owns_row)

        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        # Add add button
        add_path_row = Gtk.ListBoxRow(selectable=False)
        add_path_row.get_style_context().add_class("permissions-row")
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        add_path_row.add(hbox)

        btn = Gtk.Button()
        btn.set_size_request(26, 26)  # 40x40 pixels
        btn.get_style_context().add_class("app-action-button")
        add_rm_icon = "list-add-symbolic"
        use_icon = Gio.Icon.new_for_string(add_rm_icon)
        btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
        btn.connect("clicked", self._on_add_path, app_id, app, perm_type)
        hbox.pack_end(btn, False, True, 0)

        listbox.add(add_path_row)

    def _add_path_section(self, app_id, app, listbox, section_title, perm_type):
        """Helper method to add sections with paths (Persistent, Environment)"""
        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        # Get permissions
        if perm_type == "persistent":
            success, perms = fp_turbo.list_other_perm_toggles(app_id, perm_type, self.system_mode)
        else:
            success, perms = fp_turbo.list_other_perm_values(app_id, perm_type, self.system_mode)
        if not success:
            perms = {"paths": []}

        if perm_type == "persistent":
            global_success, global_perms = fp_turbo.global_list_other_perm_toggles(perm_type, True, self.system_mode)
        else:
            global_success, global_perms = fp_turbo.global_list_other_perm_values(perm_type, True, self.system_mode)
        if not global_success:
            global_perms = {"paths": []}


        # First, create rows for global paths
        for path in global_perms["paths"]:
            if path != "":
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, perm_type)

                # Configure button based on permission type
                btn.set_sensitive(False)
                btn.get_style_context().add_class("destructive-action")

                btn_box.pack_end(btn, False, False, 0)
                indicator_label = Gtk.Label(label="*", xalign=0)
                btn_box.pack_end(indicator_label, False, True, 0)

                hbox.pack_end(btn_box, False, False, 0)
                listbox.add(row)

        # Then create rows for application-specific paths
        for path in perms["paths"]:
            if path != "" and path not in global_perms["paths"]:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, perm_type)

                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)
                listbox.add(row)

        # Add add button
        row = Gtk.ListBoxRow(selectable=False)
        row.get_style_context().add_class("permissions-row")
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        row.add(hbox)

        btn = Gtk.Button()
        btn.set_size_request(26, 26)  # 40x40 pixels
        btn.get_style_context().add_class("app-action-button")
        add_rm_icon = "list-add-symbolic"
        use_icon = Gio.Icon.new_for_string(add_rm_icon)
        btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
        btn.connect("clicked", self._on_add_path, app_id, app, perm_type)
        hbox.pack_end(btn, False, True, 0)

        listbox.add(row)

    def _add_filesystem_section(self, app_id, app, listbox, section_title):
        """Helper method to add the Filesystems section"""
        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        # Get filesystem permissions
        global_success, global_perms = fp_turbo.global_list_file_perms(True, self.system_mode)
        if not global_success:
            global_perms = {"paths": [], "special_paths": []}
        success, perms = fp_turbo.list_file_perms(app_id, self.system_mode)
        if not success:
            perms = {"paths": [], "special_paths": []}

        # Add special paths as toggles
        special_paths = [
            ("All user files", "home", "Access to all user files"),
            ("All system files", "host", "Access to all system files"),
            ("All system libraries, executables and static data", "host-os", "Access to system libraries and executables"),
            ("All system configurations", "host-etc", "Access to system configurations")
        ]

        for display_text, option, description in special_paths:
            row = Gtk.ListBoxRow(selectable=False)
            row.get_style_context().add_class("permissions-row")
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
            row.add(hbox)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            hbox.pack_start(vbox, True, True, 0)

            label = Gtk.Label(label=display_text, xalign=0)
            label.get_style_context().add_class("permissions-item-label")
            desc = Gtk.Label(label=description, xalign=0)
            desc.get_style_context().add_class("permissions-item-summary")
            vbox.pack_start(label, True, True, 0)
            vbox.pack_start(desc, True, True, 0)

            switch = Gtk.Switch()
            switch.props.valign = Gtk.Align.CENTER

            # Add indicator label before switch
            switch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

            in_perms = option in perms["special_paths"]
            in_global_perms = option in global_perms["special_paths"]

            switch.set_active(in_global_perms or in_perms)
            # Set sensitivity based on your requirements
            if in_global_perms:
                switch.set_sensitive(False)  # Global permissions take precedence
                indicator = Gtk.Label(label="*", xalign=1.0)
                indicator.get_style_context().add_class("global-indicator")
                switch_box.pack_start(indicator, False, True, 0)

            elif in_perms:
                switch.set_sensitive(True)   # Local permissions enabled and sensitive

            switch_box.pack_start(switch, False, True, 0)
            switch.connect("state-set", self._on_switch_toggled, app_id, "filesystems", option)
            hbox.pack_end(switch_box, False, True, 0)

            listbox.add(row)


        # First, create rows for global paths
        for path in global_perms["paths"]:
            if path != "":
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, "filesystems")

                # Configure button based on permission type
                btn.set_sensitive(False)
                btn.get_style_context().add_class("destructive-action")

                btn_box.pack_end(btn, False, False, 0)
                indicator_label = Gtk.Label(label="*", xalign=0)
                btn_box.pack_end(indicator_label, False, True, 0)

                hbox.pack_end(btn_box, False, False, 0)
                listbox.add(row)

        # Then create rows for application-specific paths
        for path in perms["paths"]:
            if path != "" and path not in global_perms["paths"]:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._on_remove_path, app_id, app, path, "filesystems")

                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)
                listbox.add(row)

        # Add add button
        row = Gtk.ListBoxRow(selectable=False)
        row.get_style_context().add_class("permissions-row")
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        row.add(hbox)

        btn = Gtk.Button()
        btn.set_size_request(26, 26)  # 40x40 pixels
        btn.get_style_context().add_class("app-action-button")
        add_rm_icon = "list-add-symbolic"
        use_icon = Gio.Icon.new_for_string(add_rm_icon)
        btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
        btn.connect("clicked", self._on_add_path, app_id, app, "filesystems")
        hbox.pack_end(btn, False, True, 0)

        listbox.add(row)


    def on_app_options_clicked(self, button, app):
        """Handle the app options click"""
        details = app.get_details()
        app_id = details['id']

        # Create window (as before)
        self.options_window = Gtk.Window(title=f"{details['name']} Settings")
        self.options_window.set_default_size(600, 800)

        # Set subtitle
        header_bar = Gtk.HeaderBar(title=f"{details['name']} Settings",
                                subtitle="List of resources selectively granted to the application")
        header_bar.set_show_close_button(True)
        self.options_window.set_titlebar(header_bar)

        # Create main container with padding
        box_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box_outer.set_border_width(20)
        self.options_window.add(box_outer)

        # Create scrolled window for content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Create list box for options
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.get_style_context().add_class("permissions-window")

        indicator = Gtk.Label(label="* = global override", xalign=1.0)
        indicator.get_style_context().add_class("permissions-global-indicator")
        # Add other sections with correct permission types
        self._add_section(app_id, listbox, "Shared", "shared", [
            ("Network", "network", "Can communicate over network"),
            ("Inter-process communications", "ipc", "Can communicate with other applications")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._add_section(app_id, listbox, "Sockets", "sockets", [
            ("X11 windowing system", "x11", "Can access X11 display server"),
            ("Wayland windowing system", "wayland", "Can access Wayland display server"),
            ("Fallback to X11 windowing system", "fallback-x11", "Can fallback to X11 if Wayland unavailable"),
            ("PulseAudio sound server", "pulseaudio", "Can access PulseAudio sound system"),
            ("D-Bus session bus", "session-bus", "Can communicate with session D-Bus"),
            ("D-Bus system bus", "system-bus", "Can communicate with system D-Bus"),
            ("Secure Shell agent", "ssh-auth", "Can access SSH authentication agent"),
            ("Smart cards", "pcsc", "Can access smart card readers"),
            ("Printing system", "cups", "Can access printing subsystem"),
            ("GPG-Agent directories", "gpg-agent", "Can access GPG keyring"),
            ("Inherit Wayland socket", "inherit-wayland-socket", "Can inherit existing Wayland socket")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._add_section(app_id, listbox, "Devices", "devices", [
            ("GPU Acceleration", "dri", "Can use hardware graphics acceleration"),
            ("Input devices", "input", "Can access input devices"),
            ("Virtualization", "kvm", "Can access virtualization services"),
            ("Shared memory", "shm", "Can use shared memory"),
            ("All devices (e.g. webcam)", "all", "Can access all device files")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._add_section(app_id, listbox, "Features", "features", [
            ("Development syscalls", "devel", "Can perform development operations"),
            ("Programs from other architectures", "multiarch", "Can execute programs from other architectures"),
            ("Bluetooth", "bluetooth", "Can access Bluetooth hardware"),
            ("Controller Area Network bus", "canbus", "Can access CAN bus"),
            ("Application Shared Memory", "per-app-dev-shm", "Can use shared memory for IPC")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        # Add Filesystems section
        self._add_filesystem_section(app_id, app, listbox, "Filesystems")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._add_path_section(app_id, app, listbox, "Persistent", "persistent")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._add_path_section(app_id, app, listbox, "Environment", "environment")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._add_bus_section(app_id, app, listbox, "System Bus", "system_bus")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._add_bus_section(app_id, app, listbox, "Session Bus", "session_bus")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        # Add Portals section
        self._add_section(app_id, listbox, "Portals", section_options=[
            ("Background", "background", "Can run in the background"),
            ("Notifications", "notifications", "Can send notifications"),
            ("Microphone", "microphone", "Can listen to your microphone"),
            ("Speakers", "speakers", "Can play sounds to your speakers"),
            ("Camera", "camera", "Can record videos with your camera"),
            ("Location", "location", "Can access your location")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        # Add widgets to container
        box_outer.pack_start(indicator, False, False, 0)
        box_outer.pack_start(scrolled, True, True, 0)
        scrolled.add(listbox)

        # Connect destroy signal
        self.options_window.connect("destroy", lambda w: w.destroy())

        # Show window
        self.options_window.show_all()

    def _add_section(self, app_id, listbox, section_title, perm_type=None, section_options=None):
        """Helper method to add a section with multiple options"""

        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        # Handle portal permissions specially
        perms = {}
        global_perms = {}
        if section_title == "Portals":
            success, perms = fp_turbo.portal_get_app_permissions(app_id)
            if not success:
                perms = {}
        elif section_title in ["Persistent", "Environment", "System Bus", "Session Bus"]:
            global_success, global_perms = fp_turbo.global_list_other_perm_toggles(perm_type, True, self.system_mode)
            if not global_success:
                global_perms = {"paths": []}
            success, perms = fp_turbo.list_other_perm_toggles(app_id, perm_type, self.system_mode)
            if not success:
                perms = {"paths": []}
        else:
            global_success, global_perms = fp_turbo.global_list_other_perm_toggles(perm_type, True, self.system_mode)
            if not global_success:
                global_perms = {"paths": []}
            success, perms = fp_turbo.list_other_perm_toggles(app_id, perm_type, self.system_mode)
            if not success:
                perms = {"paths": []}
        if section_options:
            # Add options
            for display_text, option, description in section_options:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)

                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                hbox.pack_start(vbox, True, True, 0)

                label = Gtk.Label(label=display_text, xalign=0)
                label.get_style_context().add_class("permissions-item-label")
                desc = Gtk.Label(label=description, xalign=0)
                desc.get_style_context().add_class("permissions-item-summary")
                vbox.pack_start(label, True, True, 0)
                vbox.pack_start(desc, True, True, 0)

                switch = Gtk.Switch()
                switch.props.valign = Gtk.Align.CENTER

                # Add indicator label before switch
                switch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Handle portal permissions differently
                if section_title == "Portals":
                    if option in perms:
                        switch.set_active(perms[option] == 'yes')
                        switch.set_sensitive(True)
                    else:
                        switch.set_sensitive(False)
                else:
                    # First check if option exists in either perms or global_perms
                    in_perms = option.lower() in [p.lower() for p in perms["paths"]]
                    in_global_perms = option.lower() in [p.lower() for p in global_perms["paths"]]

                    # Set active state based on precedence rules
                    switch.set_active(in_global_perms or in_perms)

                    # Set sensitivity based on your requirements
                    if in_global_perms:
                        switch.set_sensitive(False)  # Global permissions take precedence
                        indicator = Gtk.Label(label="*", xalign=0)
                        indicator.get_style_context().add_class("global-indicator")
                        switch_box.pack_start(indicator, False, True, 0)

                    elif in_perms:
                        switch.set_sensitive(True)   # Local permissions enabled and sensitive

                switch_box.pack_start(switch, False, True, 0)

                switch.connect("state-set", self._on_switch_toggled, app_id, perm_type, option)
                hbox.pack_end(switch_box, False, True, 0)

                listbox.add(row)

    def _on_switch_toggled(self, switch, state, app_id, perm_type, option):
        """Handle switch toggle events"""
        if perm_type is None:  # Portal section
            success, message = fp_turbo.portal_set_app_permissions(
                option.lower(),
                app_id,
                "yes" if state else "no"
            )
        else:
            success, message = fp_turbo.toggle_other_perms(
                app_id,
                perm_type,
                option.lower(),
                state,
                self.system_mode
            )

        if not success:
            switch.set_active(not state)
            print(f"Error: {message}")

    def _on_remove_path(self, button, app_id, app, path, perm_type=None):
        """Handle remove path button click"""
        if perm_type:
            if perm_type == "persistent":
                success, message = fp_turbo.remove_file_permissions(
                    app_id,
                    path,
                    "persistent",
                    self.system_mode
                )
            elif perm_type == "filesystems":
                success, message = fp_turbo.remove_file_permissions(
                    app_id,
                    path,
                    "filesystems",
                    self.system_mode
                )
            else:
                success, message = fp_turbo.remove_permission_value(
                    app_id,
                    perm_type,
                    path,
                    self.system_mode
                )
        else:
            success, message = fp_turbo.remove_file_permissions(
                app_id,
                path,
                "filesystems",
                self.system_mode
            )
        if success:
            # Refresh the current window
            self.options_window.destroy()
            self.on_app_options_clicked(None, app)

    def _on_add_path(self, button, app_id, app, perm_type=None):
        """Handle add path button click"""
        dialog = Gtk.Dialog(
            title="Add Filesystem Path",
            parent=self.options_window,
            modal=True,
            destroy_with_parent=True,
        )

        # Add buttons separately
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Enter filesystem path")
        dialog.vbox.pack_start(entry, True, True, 0)
        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            path = entry.get_text()
            if perm_type:
                if perm_type == "persistent":
                    success, message = fp_turbo.add_file_permissions(
                        app_id,
                        path,
                        "persistent",
                        self.system_mode
                    )
                elif perm_type == "filesystems":
                    success, message = fp_turbo.add_file_permissions(
                        app_id,
                        path,
                        "filesystems",
                        self.system_mode
                    )
                else:
                    success, message = fp_turbo.add_permission_value(
                        app_id,
                        perm_type,
                        path,
                        self.system_mode
                    )
            else:
                success, message = fp_turbo.add_file_permissions(
                    app_id,
                    path,
                    "filesystems",
                    self.system_mode
                )
            if success:
                # Refresh the current window
                self.options_window.destroy()
                self.on_app_options_clicked(None, app)
                message_type = Gtk.MessageType.INFO
            else:
                message_type = Gtk.MessageType.ERROR
            if message:
                error_dialog = Gtk.MessageDialog(
                    transient_for=None,  # Changed from self
                    modal=True,
                    destroy_with_parent=True,
                    message_type=message_type,
                    buttons=Gtk.ButtonsType.OK,
                    text=message
                )
                error_dialog.run()
                error_dialog.destroy()
        dialog.destroy()

    def _add_option(self, parent_box, label_text, description):
        """Helper method to add an individual option"""
        row = Gtk.ListBoxRow(selectable=False)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        row.add(hbox)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        hbox.pack_start(vbox, True, True, 0)

        label = Gtk.Label(label=label_text, xalign=0)
        desc = Gtk.Label(label=description, xalign=0)
        vbox.pack_start(label, True, True, 0)
        vbox.pack_start(desc, True, True, 0)

        switch = Gtk.Switch()
        switch.props.valign = Gtk.Align.CENTER
        hbox.pack_end(switch, False, True, 0)

        parent_box.add(row)
        return row, switch

    def _global_add_bus_section(self, listbox, section_title, perm_type):
        """Helper method to add System Bus or Session Bus section"""
        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        # Get permissions
        success, perms = fp_turbo.global_list_other_perm_values(perm_type, True, self.system_mode)
        if not success:
            perms = {"paths": []}

        # Add Talks section
        talks_row = Gtk.ListBoxRow(selectable=False)
        talks_row.get_style_context().add_class("permissions-row")
        talks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        talks_box.get_style_context().add_class("permissions-bus-box")
        talks_row.add(talks_box)

        talks_header = Gtk.Label(label="Talks", xalign=0)
        talks_header.get_style_context().add_class("permissions-item-label")
        talks_box.pack_start(talks_header, False, False, 0)

        # Add talk paths
        for path in perms["paths"]:
            if "talk" in path:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._global_on_remove_path, path, perm_type)
                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)

                talks_box.add(row)

        listbox.add(talks_row)

        # Add Owns section
        owns_row = Gtk.ListBoxRow(selectable=False)
        owns_row.get_style_context().add_class("permissions-row")
        owns_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        owns_box.get_style_context().add_class("permissions-bus-box")
        owns_row.add(owns_box)

        owns_header = Gtk.Label(label="Owns", xalign=0)
        owns_header.get_style_context().add_class("permissions-item-label")
        owns_box.pack_start(owns_header, False, False, 0)

        # Add own paths
        for path in perms["paths"]:
            if "own" in path:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._global_on_remove_path, path, perm_type)
                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)

                owns_box.add(row)

        owns_row.show_all()
        listbox.add(owns_row)

        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        # Add add button
        add_path_row = Gtk.ListBoxRow(selectable=False)
        add_path_row.get_style_context().add_class("permissions-row")
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        add_path_row.add(hbox)

        btn = Gtk.Button()
        btn.set_size_request(26, 26)  # 40x40 pixels
        btn.get_style_context().add_class("app-action-button")
        add_rm_icon = "list-add-symbolic"
        use_icon = Gio.Icon.new_for_string(add_rm_icon)
        btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
        btn.connect("clicked", self._global_on_add_path, perm_type)
        hbox.pack_end(btn, False, True, 0)

        listbox.add(add_path_row)

    def _global_add_path_section(self, listbox, section_title, perm_type):
        """Helper method to add sections with paths (Persistent, Environment)"""
        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        # Get permissions
        if perm_type == "persistent":
            success, perms = fp_turbo.global_list_other_perm_toggles(perm_type, True, self.system_mode)
        else:
            success, perms = fp_turbo.global_list_other_perm_values(perm_type, True, self.system_mode)
        if not success:
            perms = {"paths": []}

        # Add normal paths with remove buttons
        for path in perms["paths"]:
            if path != "":
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._global_on_remove_path, path, perm_type)
                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)

                listbox.add(row)

        # Add add button
        row = Gtk.ListBoxRow(selectable=False)
        row.get_style_context().add_class("permissions-row")
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        row.add(hbox)

        btn = Gtk.Button()
        btn.set_size_request(26, 26)  # 40x40 pixels
        btn.get_style_context().add_class("app-action-button")
        add_rm_icon = "list-add-symbolic"
        use_icon = Gio.Icon.new_for_string(add_rm_icon)
        btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
        btn.connect("clicked", self._global_on_add_path, perm_type)
        hbox.pack_end(btn, False, True, 0)

        listbox.add(row)

    def _global_add_filesystem_section(self, listbox, section_title):
        """Helper method to add the Filesystems section"""
        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        # Get filesystem permissions
        success, perms = fp_turbo.global_list_file_perms(True, self.system_mode)
        if not success:
            perms = {"paths": [], "special_paths": []}

        # Add special paths as toggles
        special_paths = [
            ("All user files", "home", "Access to all user files"),
            ("All system files", "host", "Access to all system files"),
            ("All system libraries, executables and static data", "host-os", "Access to system libraries and executables"),
            ("All system configurations", "host-etc", "Access to system configurations")
        ]

        for display_text, option, description in special_paths:
            row = Gtk.ListBoxRow(selectable=False)
            row.get_style_context().add_class("permissions-row")
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
            row.add(hbox)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            hbox.pack_start(vbox, True, True, 0)

            label = Gtk.Label(label=display_text, xalign=0)
            label.get_style_context().add_class("permissions-item-label")
            desc = Gtk.Label(label=description, xalign=0)
            desc.get_style_context().add_class("permissions-item-summary")
            vbox.pack_start(label, True, True, 0)
            vbox.pack_start(desc, True, True, 0)

            switch = Gtk.Switch()
            switch.props.valign = Gtk.Align.CENTER
            switch.set_active(option in perms["special_paths"])
            switch.set_sensitive(True)
            switch.connect("state-set", self._global_on_switch_toggled, "filesystems", option)
            hbox.pack_end(switch, False, True, 0)

            listbox.add(row)

        # Add normal paths with remove buttons
        for path in perms["paths"]:
            if path != "":
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                #vbox.get_style_context().add_class("permissions-path-vbox")
                vbox.set_size_request(400, 30)
                hbox.pack_start(vbox, False, True, 0)

                text_view = Gtk.TextView()
                text_view.set_size_request(400, 20)
                text_view.get_style_context().add_class("permissions-path-text")
                text_view.set_editable(False)
                text_view.set_cursor_visible(False)
                #text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                # Enable horizontal scrolling
                scrolled_window = Gtk.ScrolledWindow()
                #scrolled_window.get_style_context().add_class("permissions-path-scroll")
                scrolled_window.set_hexpand(False)
                scrolled_window.set_vexpand(False)
                scrolled_window.set_size_request(400, 30)
                scrolled_window.set_policy(
                    Gtk.PolicyType.AUTOMATIC,  # Enable horizontal scrollbar
                    Gtk.PolicyType.NEVER       # Disable vertical scrollbar
                )

                # Add TextView to ScrolledWindow
                scrolled_window.add(text_view)

                # Add the text
                buffer = text_view.get_buffer()
                buffer.set_text(path)

                vbox.pack_start(scrolled_window, False, True, 0)

                btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                # Create remove button
                btn = Gtk.Button()
                btn.set_size_request(26, 26)  # 40x40 pixels
                btn.get_style_context().add_class("app-action-button")
                add_rm_icon = "list-remove-symbolic"
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                btn.connect("clicked", self._global_on_remove_path, path, "filesystems")
                btn_box.pack_end(btn, False, False, 0)

                hbox.pack_end(btn_box, False, False, 0)

                listbox.add(row)

        # Add add button
        row = Gtk.ListBoxRow(selectable=False)
        row.get_style_context().add_class("permissions-row")
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        row.add(hbox)

        btn = Gtk.Button()
        btn.set_size_request(26, 26)  # 40x40 pixels
        btn.get_style_context().add_class("app-action-button")
        add_rm_icon = "list-add-symbolic"
        use_icon = Gio.Icon.new_for_string(add_rm_icon)
        btn.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
        btn.connect("clicked", self._global_on_add_path, "filesystems")
        hbox.pack_end(btn, False, True, 0)

        listbox.add(row)


    def global_on_options_clicked(self, button):
        """Handle the app options click"""

        # Create window (as before)
        self.global_options_window = Gtk.Window(title="Global Setting Overrides")
        self.global_options_window.set_default_size(600, 800)

        # Set subtitle
        header_bar = Gtk.HeaderBar(title="Global Setting Overrides",
                                subtitle="Override list of resources selectively granted to applications")
        header_bar.set_show_close_button(True)
        self.global_options_window.set_titlebar(header_bar)

        # Create main container with padding
        box_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box_outer.set_border_width(20)
        self.global_options_window.add(box_outer)

        # Create scrolled window for content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Create list box for options
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.get_style_context().add_class("permissions-window")

        indicator = Gtk.Label(label="* = global override", xalign=1.0)
        indicator.get_style_context().add_class("permissions-global-indicator")

        # No portals section. Portals are only handled on per-user basis.

        # Add other sections with correct permission types
        self._global_add_section(listbox, "Shared", "shared", [
            ("Network", "network", "Can communicate over network"),
            ("Inter-process communications", "ipc", "Can communicate with other applications")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._global_add_section(listbox, "Sockets", "sockets", [
            ("X11 windowing system", "x11", "Can access X11 display server"),
            ("Wayland windowing system", "wayland", "Can access Wayland display server"),
            ("Fallback to X11 windowing system", "fallback-x11", "Can fallback to X11 if Wayland unavailable"),
            ("PulseAudio sound server", "pulseaudio", "Can access PulseAudio sound system"),
            ("D-Bus session bus", "session-bus", "Can communicate with session D-Bus"),
            ("D-Bus system bus", "system-bus", "Can communicate with system D-Bus"),
            ("Secure Shell agent", "ssh-auth", "Can access SSH authentication agent"),
            ("Smart cards", "pcsc", "Can access smart card readers"),
            ("Printing system", "cups", "Can access printing subsystem"),
            ("GPG-Agent directories", "gpg-agent", "Can access GPG keyring"),
            ("Inherit Wayland socket", "inherit-wayland-socket", "Can inherit existing Wayland socket")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._global_add_section(listbox, "Devices", "devices", [
            ("GPU Acceleration", "dri", "Can use hardware graphics acceleration"),
            ("Input devices", "input", "Can access input devices"),
            ("Virtualization", "kvm", "Can access virtualization services"),
            ("Shared memory", "shm", "Can use shared memory"),
            ("All devices (e.g. webcam)", "all", "Can access all device files")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._global_add_section(listbox, "Features", "features", [
            ("Development syscalls", "devel", "Can perform development operations"),
            ("Programs from other architectures", "multiarch", "Can execute programs from other architectures"),
            ("Bluetooth", "bluetooth", "Can access Bluetooth hardware"),
            ("Controller Area Network bus", "canbus", "Can access CAN bus"),
            ("Application Shared Memory", "per-app-dev-shm", "Can use shared memory for IPC")
        ])
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        # Add Filesystems section
        self._global_add_filesystem_section(listbox, "Filesystems")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._global_add_path_section(listbox, "Persistent", "persistent")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._global_add_path_section(listbox, "Environment", "environment")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._global_add_bus_section(listbox, "System Bus", "system_bus")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        self._global_add_bus_section(listbox, "Session Bus", "session_bus")
        spacing_box = Gtk.ListBoxRow(selectable=False)
        spacing_box.get_style_context().add_class("permissions-spacing-box")
        listbox.add(spacing_box)

        # Add widgets to container
        box_outer.pack_start(scrolled, True, True, 0)
        scrolled.add(listbox)

        # Connect destroy signal
        self.global_options_window.connect("destroy", lambda w: w.destroy())

        # Show window
        self.global_options_window.show_all()

    def _global_add_section(self, listbox, section_title, perm_type=None, section_options=None):
        """Helper method to add a section with multiple options"""
        # Add section header
        row_header = Gtk.ListBoxRow(selectable=False)
        row_header.get_style_context().add_class("permissions-row")
        box_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label_header = Gtk.Label(label=f"{section_title}",
                            use_markup=True, xalign=0)
        label_header.get_style_context().add_class("permissions-header-label")
        box_header.pack_start(label_header, True, True, 0)
        box_header.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), True, True, 0)
        row_header.add(box_header)
        listbox.add(row_header)

        if section_title in ["Persistent", "Environment", "System Bus", "Session Bus"]:
            success, perms = fp_turbo.global_list_other_perm_toggles(perm_type, True, self.system_mode)
            if not success:
                perms = {"paths": []}
        else:
            success, perms = fp_turbo.global_list_other_perm_toggles(perm_type, True, self.system_mode)
            if not success:
                perms = {"paths": []}

        if section_options:
            # Add options
            for display_text, option, description in section_options:
                row = Gtk.ListBoxRow(selectable=False)
                row.get_style_context().add_class("permissions-row")
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
                row.add(hbox)

                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                hbox.pack_start(vbox, True, True, 0)

                label = Gtk.Label(label=display_text, xalign=0)
                label.get_style_context().add_class("permissions-item-label")
                desc = Gtk.Label(label=description, xalign=0)
                desc.get_style_context().add_class("permissions-item-summary")
                vbox.pack_start(label, True, True, 0)
                vbox.pack_start(desc, True, True, 0)

                switch = Gtk.Switch()
                switch.props.valign = Gtk.Align.CENTER

                # Handle portal permissions differently
                if section_title == "Portals":
                    if option in perms:
                        switch.set_active(perms[option] == 'yes')
                        switch.set_sensitive(True)
                    else:
                        switch.set_sensitive(False)
                else:
                    switch.set_active(option in [p.lower() for p in perms["paths"]])
                    switch.set_sensitive(True)

                switch.connect("state-set", self._global_on_switch_toggled, perm_type, option)
                hbox.pack_end(switch, False, True, 0)

                listbox.add(row)

    def _global_on_switch_toggled(self, switch, state, perm_type, option):
        """Handle switch toggle events"""
        success, message = fp_turbo.global_toggle_other_perms(
                perm_type,
                option.lower(),
                state,
                True,
                self.system_mode
            )

        if not success:
            switch.set_active(not state)
            print(f"Error: {message}")

    def _global_on_remove_path(self, button, path, perm_type=None):
        """Handle remove path button click"""
        if perm_type:
            if perm_type == "persistent":
                success, message = fp_turbo.global_remove_file_permissions(
                    path,
                    "persistent",
                    True,
                    self.system_mode
                )
            elif perm_type == "filesystems":
                success, message = fp_turbo.global_remove_file_permissions(
                    path,
                    "filesystems",
                    True,
                    self.system_mode
                )
            else:
                success, message = fp_turbo.global_remove_permission_value(
                    perm_type,
                    path,
                    True,
                    self.system_mode
                )
        else:
            success, message = fp_turbo.global_remove_file_permissions(
                path,
                "filesystems",
                True,
                self.system_mode
            )
        if success:
            # Refresh the current window
            self.global_options_window.destroy()
            self.global_on_options_clicked(None)

    def _global_on_add_path(self, button, perm_type=None):
        """Handle add path button click"""
        dialog = Gtk.Dialog(
            title="Add Filesystem Path",
            parent=self.global_options_window,
            modal=True,
            destroy_with_parent=True,
        )

        # Add buttons separately
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Enter filesystem path")
        dialog.vbox.pack_start(entry, True, True, 0)
        dialog.show_all()

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            path = entry.get_text()
            if perm_type:
                if perm_type == "persistent":
                    success, message = fp_turbo.global_add_file_permissions(
                        path,
                        "persistent",
                        True,
                        self.system_mode
                    )
                elif perm_type == "filesystems":
                    success, message = fp_turbo.global_add_file_permissions(
                        path,
                        "filesystems",
                        True,
                        self.system_mode
                    )
                else:
                    success, message = fp_turbo.global_add_permission_value(
                        perm_type,
                        path,
                        True,
                        self.system_mode
                    )
            else:
                success, message = fp_turbo.global_add_file_permissions(
                    path,
                    "filesystems",
                    True,
                    self.system_mode
                )
            if success:
                # Refresh the current window
                self.global_options_window.destroy()
                self.global_on_options_clicked(None)
                message_type = Gtk.MessageType.INFO
            else:
                message_type = Gtk.MessageType.ERROR
            if message:
                error_dialog = Gtk.MessageDialog(
                    transient_for=None,  # Changed from self
                    modal=True,
                    destroy_with_parent=True,
                    message_type=message_type,
                    buttons=Gtk.ButtonsType.OK,
                    text=message
                )
                error_dialog.run()
                error_dialog.destroy()
        dialog.destroy()

    def _global_add_option(self, parent_box, label_text, description):
        """Helper method to add an individual option"""
        row = Gtk.ListBoxRow(selectable=False)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=50)
        row.add(hbox)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        hbox.pack_start(vbox, True, True, 0)

        label = Gtk.Label(label=label_text, xalign=0)
        desc = Gtk.Label(label=description, xalign=0)
        vbox.pack_start(label, True, True, 0)
        vbox.pack_start(desc, True, True, 0)

        switch = Gtk.Switch()
        switch.props.valign = Gtk.Align.CENTER
        hbox.pack_end(switch, False, True, 0)

        parent_box.add(row)
        return row, switch


    def on_update_clicked(self, button, app):
        """Handle the Remove button click with removal options"""
        details = app.get_details()

        # Create dialog
        dialog = Gtk.Dialog(
            title=f"Update {details['name']}?",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        # Add buttons using the new method
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Update", Gtk.ResponseType.OK)

        # Create content area
        content_area = dialog.get_content_area()
        content_area.set_spacing(12)
        content_area.set_border_width(12)

        content_area.pack_start(Gtk.Label(label=f"Update: {details['id']}?"), False, False, 0)

        # Show dialog
        dialog.show_all()

        # Run dialog
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            # Perform Removal
            def perform_update():
                # Show waiting dialog
                GLib.idle_add(self.show_waiting_dialog, "Updating package...")

                success, message = fp_turbo.update_flatpak(app, self.system_mode)

                # Update UI on main thread
                aid = app.get_details().get("id")
                GLib.idle_add(lambda: self.on_task_complete(success, message, aid))
            # Start spinner and begin installation
            thread = threading.Thread(target=perform_update)
            thread.daemon = True  # Allow program to exit even if thread is still running
            thread.start()

        dialog.destroy()

    def download_screenshot(self, url, local_path):
        """Download a screenshot and save it locally"""
        try:
            # Download the image
            response = requests.get(url, timeout=(5, 20))
            response.raise_for_status()

            # Create the directory if it doesn't exist
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            # Save the image
            with open(local_path, 'wb') as f:
                f.write(response.content)

            return True
        except Exception as e:
            print(f"Error downloading screenshot {url}: {e}")
            return False

    def create_screenshot_slideshow(self, screenshots, app_id):
        # Create main container for slideshow
        slideshow_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        slideshow_box.get_style_context().add_class("details-gallery")
        slideshow_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.FILL, spacing=32, border_width=0)

        self.index_current = 0 # First screenshot is active
        self.screenshot_loop = True # Option to loop the screenshots instead of having limits on the first and last screenshot.

        nav_previous_icon = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string("go-previous-symbolic"), 3)
        nav_previous_icon.set_halign(Gtk.Align.END)
        nav_previous_icon.set_valign(Gtk.Align.CENTER)
        nav_previous_icon.get_style_context().add_class("details-gallery-arrow")

        nav_previous_event_box = Gtk.EventBox()
        nav_previous_event_box.set_size_request(96,-1) # Arrow size (48px + padding 16px) + The margin you want (here 32px)
        nav_previous_event_box.connect('button-release-event',
                            lambda w, e: self._switch_screenshot(
                                current_image, screenshots, dots, self.index_current, app_id, nav_previous_icon, nav_next_icon, "previous"))
        nav_previous_event_box.connect("enter-notify-event", lambda w, e: self.enter_hover_event(nav_previous_icon));
        nav_previous_event_box.connect("leave-notify-event", lambda w, e: self.leave_hover_event(nav_previous_icon));

        nav_previous_event_box.add(nav_previous_icon)
        slideshow_content.pack_start(nav_previous_event_box, False, False, 0)

        # Create main frame for the current screenshot (removed border)
        main_frame = Gtk.Frame(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        main_frame.set_shadow_type(Gtk.ShadowType.NONE)
        main_frame.set_size_request(-1,300) # Minimum size (for when images don't load proprely)
        slideshow_content.pack_start(main_frame, False, False, 0)

        nav_next_icon = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string("go-next-symbolic"), 3)
        nav_next_icon.set_halign(Gtk.Align.START)
        nav_next_icon.set_valign(Gtk.Align.CENTER)
        nav_next_icon.get_style_context().add_class("details-gallery-arrow")

        nav_next_event_box = Gtk.EventBox()
        nav_next_event_box.set_size_request(96,-1)
        nav_next_event_box.connect('button-release-event',
                            lambda w, e: self._switch_screenshot(
                                current_image, screenshots, dots, self.index_current, app_id, nav_previous_icon, nav_next_icon, "next"))
        nav_next_event_box.connect("enter-notify-event", lambda w, e: self.enter_hover_event(nav_next_icon));
        nav_next_event_box.connect("leave-notify-event", lambda w, e: self.leave_hover_event(nav_next_icon));

        nav_next_event_box.add(nav_next_icon)
        slideshow_content.pack_end(nav_next_event_box, False, False, 0)

        slideshow_box.pack_start(slideshow_content, False, False, 0)

        # Create image for current screenshot
        current_image = Gtk.Image(hexpand=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        current_image.get_style_context().add_class("details-gallery-screenshot")
        main_frame.add(current_image)

        # Create navigation dots
        dots = []
        if len(screenshots) > 1:
            # Create box for navigation dots
            nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER)
            nav_box.set_border_width(0)  # Remove border
            slideshow_box.pack_start(nav_box, False, True, 0)
            for i in range(len(screenshots)):
                # Create new EventBox for each dot
                event_box = Gtk.EventBox(valign=Gtk.Align.CENTER)
                event_box.set_border_width(0)

                # Create bullet using Label
                bullet = Gtk.Label(label="⬤", justify=Gtk.Justification.CENTER, yalign=0.58)
                bullet.get_style_context().add_class("details-gallery-bullet")
                if i == self.index_current:
                    bullet.get_style_context().add_class("active")  # First dot is active

                # Add bullet to event box
                event_box.add(bullet)

                # Connect navigation
                event_box.connect('button-release-event',
                                lambda w, e, idx=i: self._switch_screenshot(
                                    current_image, screenshots, dots, idx, app_id, nav_previous_icon, nav_next_icon))
                event_box.connect("enter-notify-event", lambda w, e, idx=i: self.enter_hover_event(bullet, idx, dots));
                event_box.connect("leave-notify-event", lambda w, e, idx=i: self.leave_hover_event(bullet, idx, dots));

                # Add event box to nav box
                nav_box.pack_start(event_box, False, True, 0)

                # Store the event box
                dots.append(event_box)

        # Updates navigation (arrow & dots)
        self._screenshot_navigation_update(screenshots, nav_previous_icon, nav_next_icon, dots)

        # Load first screenshot
        self._load_screenshot(current_image, screenshots[0], app_id)

        return slideshow_box

    def _load_screenshot(self, image, screenshot, app_id):
        """Helper method to load a single screenshot"""
        home_dir = os.path.expanduser("~")

        # Get URL using fp_turbo.screenshot_details() like in your original code
        image_data = fp_turbo.screenshot_details(screenshot)
        url = image_data.get_url()

        local_path = f"{home_dir}/.local/share/flatpost/app-screenshots/{app_id}/{os.path.basename(url)}"

        if os.path.exists(local_path):
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(local_path, -1, 336) # Resizes the screenshot height while preserving aspect ratio
            image.set_from_pixbuf(pb)
        else:
            if fp_turbo.check_internet():
                try:
                    if not self.download_screenshot(url, local_path):
                        print("Failed to download screenshot")
                        return
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_size(local_path, -1, 336) # Resizes the screenshot height while preserving aspect ratio
                    image.set_from_pixbuf(pb)
                except Exception:
                    image.set_from_icon_name('image-missing-symbolic', 6)
            else:
                image.set_from_icon_name('image-missing-symbolic', 6)


    def _switch_screenshot(self, image, screenshots, dots, index_current, app_id, icon_previous, icon_next, direction=None):
        self.index_current = index_current
        if direction == "previous":
            self.index_current -= 1
        if direction == "next":
            self.index_current += 1

        if self.screenshot_loop == True:
            if self.index_current < 0:
                self.index_current = len(screenshots)-1
            if self.index_current > len(screenshots)-1:
                self.index_current = 0

        self.index_current = max(0, min(self.index_current, len(screenshots)-1)) # Security in case the position is not in the list

        self._screenshot_navigation_update(screenshots, icon_previous, icon_next, dots)

        # Load the new screenshot
        self._load_screenshot(image, screenshots[self.index_current], app_id)
        print("Page",self.index_current,"out of",len(screenshots)-1)

    def _screenshot_navigation_update(self, screenshots, icon_previous, icon_next, dots):
        for i, dot in enumerate(dots):
            # Get the bullet label from the event box
            bullet = dot.get_children()[0]
            if i == self.index_current:
                bullet.get_style_context().add_class("active")
            else:
                bullet.get_style_context().remove_class("active")

        if self.screenshot_loop == False:
            if self.index_current == 0:
                icon_previous.get_style_context().add_class("dim-label")
                icon_previous.get_style_context().remove_class("hover-event")
            else:
                icon_previous.get_style_context().remove_class("dim-label")
            if self.index_current == len(screenshots)-1:
                icon_next.get_style_context().add_class("dim-label")
                icon_next.get_style_context().remove_class("hover-event")
            else:
                icon_next.get_style_context().remove_class("dim-label")
        if len(screenshots)-1 == 0:
                icon_previous.set_opacity(0)
                icon_next.set_opacity(0)

    def _create_details_window(self, details):
        """Create and configure the main details window."""
        self.details_window = Gtk.Window(title=f"{details['name']}")
        self.details_window.set_default_size(900, 700)

        # Set header bar
        header_bar = Gtk.HeaderBar(
            title=f"About {details['name']}"
        )
        header_bar.set_show_close_button(True)
        self.details_window.set_titlebar(header_bar)

        # Create main container
        box_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.details_window.add(box_outer)

        return box_outer

    def _create_content_area(self, box_outer):
        """Create the scrolled content area."""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_border_width(0)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_border_width(0)
        content_box.get_style_context().add_class("details-window")
        scrolled.add(content_box)

        box_outer.pack_start(scrolled, True, True, 0)
        return content_box

    def _create_icon_section(self, content_box, details):
        """Create the icon section of the details window."""
        icon_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon_row.set_border_width(0)

        icon_box = Gtk.Box()
        icon_box.set_size_request(88, -1)

        app_icon = Gio.Icon.new_for_string('package-x-generic-symbolic')
        icon_widget = self.create_scaled_icon(app_icon, is_themed=True)

        if details['icon_filename'] and Path(details['icon_path_128'] + "/" + details['icon_filename']).exists():
            icon_widget = self.create_scaled_icon(
                f"{details['icon_path_128']}/{details['icon_filename']}",
                is_themed=False
            )

        icon_widget.set_size_request(64, 64)
        icon_box.pack_start(icon_widget, True, True, 0)

        content_box.pack_start(icon_box, False, True, 0)
        content_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

    def _create_info_section(self, content_box, details, app):
        """Create the information section with name, version, and developer."""
        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28)
        status = self._get_app_status(app)

        # Create the icon section of the details window.
        icon_box = Gtk.Box()
        icon_box.set_size_request(-1, 128)

        app_icon = Gio.Icon.new_for_string('package-x-generic-symbolic')
        icon_widget = self.create_scaled_icon(app_icon, 128, is_themed=True)

        if details['icon_filename'] and Path(details['icon_path_128'] + "/" + details['icon_filename']).exists():
            icon_widget = self.create_scaled_icon(
                f"{details['icon_path_128']}/{details['icon_filename']}",
                128,
                is_themed=False
            )

        icon_widget.set_size_request(128, 128)
        icon_box.pack_start(icon_widget, False, True, 0)

        # Middle column
        middle_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        name_label = Gtk.Label(label=f"{details['name']}")
        name_label.get_style_context().add_class("title-1")
        name_label.set_xalign(0)
        version_label = Gtk.Label(label=f"Version {details['version']}")
        version_label.set_xalign(0)
        developer_label = Gtk.Label(label=f"{details['developer']}")
        developer_label.set_xalign(0)
        developer_label.get_style_context().add_class("dim-label")

        middle_column.pack_start(name_label, False, True, 0)
        middle_column.pack_start(developer_label, False, True, 0)
        middle_column.pack_start(version_label, False, True, 0)

        # Right column
        right_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        right_column.set_valign(Gtk.Align.CENTER)
        id_label = Gtk.Label(label=f"ID: {details['id']}")
        id_label.set_xalign(1)
        id_label.get_style_context().add_class("dim-label")
        kind_label = Gtk.Label(label=f"Kind: {details['kind']}")
        kind_label.set_xalign(1)
        kind_label.get_style_context().add_class("dim-label")
        # right_column.pack_start(id_label, False, True, 0)
        # right_column.pack_start(kind_label, False, True, 0)

        container = right_column
        self._setup_buttons(container, status, app, "info")

        info_box.pack_start(icon_box, False, True, 0)
        info_box.pack_start(middle_column, True, True, 0)
        info_box.pack_start(right_column, False, True, 0)

        content_box.pack_start(info_box, False, True, 0)

    def _create_text_section(self, title, text):
        """Create a text section with title and content."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        if not isinstance(text,str):
            text = "No description provided.\n"

        title_label = Gtk.Label(label=f"{title}")
        title_label.get_style_context().add_class("title-3")
        title_label.set_xalign(0)
        box.pack_start(title_label, False, True, 0)

        text_view = Gtk.TextView()
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD)
        text_view.get_style_context().add_class("details-textview")

        # Parse HTML and insert into TextView
        buffer = text_view.get_buffer()
        # if title == "Description":
        try:

            class TextExtractor(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.text = []

                def handle_data(self, data):
                    self.text.append(data)

                def handle_starttag(self, tag, attrs):
                    if tag == 'li':
                        self.text.append('• ')

                def handle_endtag(self, tag):
                    if tag == 'p':
                        self.text.append('\n')
                    elif tag == 'li':
                        self.text.append('\n')

            # Parse the HTML
            parser = TextExtractor()
            parser.text.append('\n') # For some reasons, description doesn't appear when there's only one line of paragraph, so I added this as a temporary fix.
            parser.feed(text)
            parsed_text = ''.join(parser.text) # Use [:-1] to remove last line if it's /n

            # Add basic HTML styling
            buffer.set_text(parsed_text)
            text_view.set_pixels_below_lines(3)

        except Exception as e:
            # Fallback to plain text if HTML parsing fails
            buffer.set_text(text)
            # buffer.set_text(e)

        box.pack_start(text_view, True, True, 0)
        return box

    def _create_url_section(self, url_type, url):
        """Create a URL section with clickable link."""
        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content_box.get_style_context().add_class("url-list-item")
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        url_type_icon_name = "" # May be better as an array ?
        if "donation" in url_type:
            url_type_icon_name = 'emote-love-symbolic'
        elif "homepage" in url_type:
            url_type_icon_name = 'go-home-symbolic'
        elif "bugtracker" in url_type:
            url_type_icon_name = 'dialog-warning-symbolic'
        elif "Flathub Page" in url_type:
            url_type_icon_name = 'help-about-symbolic'
        else:
            url_type_icon_name = 'user-bookmarks-symbolic'

        url_type_icon = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string(url_type_icon_name), 3)
        url_type_icon.set_size_request(-1, 28)
        url_type_icon.set_valign(Gtk.Align.CENTER)

        title_label = Gtk.Label(label=f"{url_type.capitalize()}", xalign=0)
        title_label.get_style_context().add_class("url-list-item-title")

        url_label = Gtk.Label(label=url)
        url_label.set_use_underline(True)
        url_label.set_halign(Gtk.Align.START)
        url_label.get_style_context().add_class("url-list-item-url")
        url_label.get_style_context().add_class("dim-label")

        url_open_icon = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string("send-to-symbolic"), 2)
        url_open_icon.set_size_request(-1, 24)
        url_open_icon.set_valign(Gtk.Align.CENTER)

        event_box = Gtk.EventBox()
        event_box.connect("button-release-event",
                        lambda w, e: Gio.AppInfo.launch_default_for_uri(url))
        event_box.connect("enter-notify-event", lambda w, e: self.enter_hover_event(content_box)); # These connect signals handles hover
        event_box.connect("leave-notify-event", lambda w, e: self.leave_hover_event(content_box));

        if url:
            content_box.pack_start(url_type_icon, False, True, 0)
            text_box.pack_start(title_label, False, True, 0)
            text_box.pack_start(url_label, False, True, 0)
            content_box.pack_start(text_box, False, True, 8)
            content_box.pack_end(url_open_icon, False, True, 0)
            event_box.add(content_box)
            box.pack_start(event_box, True, True, 0)
        return box

    def on_details_clicked(self, button, app):
        # If this is a cached proxy, resolve a real fp_turbo app object first
        try:
            if getattr(app, "is_cached", False):
                details = app.get_details()
                app_id = details.get("id")
                repo = details.get("repo")

                searcher = fp_turbo.get_reposearcher(self.system_mode)

                real_app = None
                # Try the cached repo first; fallback to scanning enabled repos
                if repo:
                    try:
                        candidate = searcher.search_flatpak(app_id, repo)
                        if candidate:
                            real_app = candidate
                    except Exception:
                        pass

                if real_app is None:
                    for r in fp_turbo.repolist(self.system_mode):
                        if r.get_disabled():
                            continue
                        try:
                            candidate = searcher.search_flatpak(app_id, r.get_name())
                            if candidate:
                                real_app = candidate
                                break
                        except Exception:
                            continue

                if real_app is not None:
                    app = real_app
        except Exception:
            pass

        """Initialize the details window setup process."""
        details = app.get_details()

        # Create window and main container
        box_outer = self._create_details_window(details)

        # Create content area
        content_box = self._create_content_area(box_outer)
        content_main = Gtk.Box(margin_top=32, margin_bottom=16, orientation=Gtk.Orientation.VERTICAL, valign=True)
        content_main.get_style_context().add_class("details-content")
        content_info = Gtk.Box(margin_top=16, margin_bottom=32, orientation=Gtk.Orientation.VERTICAL, valign=True)
        content_info.get_style_context().add_class("details-content")

        # self._create_icon_section(content_box, details)

        # Add info section
        self._create_info_section(content_main, details, app)
        content_box.pack_start(content_main, False, True, 0)

        # Add screenshots
        if len(details['screenshots']) >= 1: # To prevent apps with no screenshot from crashing Flatpost
           screenshot_slideshow = self.create_screenshot_slideshow(details['screenshots'], details['id'])
           screenshot_slideshow.set_border_width(0)
           content_box.pack_start(screenshot_slideshow, False, True, 0)

        # Add summary section
        summary_section = self._create_text_section(details['summary'], details['description'])
        content_info.pack_start(summary_section, False, True, 0)
        # content_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        title_label = Gtk.Label(label=f"Links")
        title_label.get_style_context().add_class("title-3")
        title_label.set_xalign(0)

        # Add URLs section
        urls_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        first = True

        for url_type, url in details['urls'].items():
            if not url:
                continue
            row = self._create_url_section(url_type, url)
            if not first:
                urls_section.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
            urls_section.pack_start(row, False, True, 0)
            first = False

        # Always add Flathub page at end
        if not first:
            urls_section.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        urls_section.pack_start(
            self._create_url_section("Flathub Page", f"https://flathub.org/apps/details/{details['id']}"),
            False, True, 0
        )

        urls_section.get_style_context().add_class("url-list")
        content_info.pack_start(urls_section, False, True, 0)

        content_box.pack_end(content_info, False, True, 0)

        # Connect destroy signal and show window
        self.details_window.connect("destroy", lambda w: w.destroy())
        self.details_window.show_all()
        # With these lines:
        children = self.details_window.get_children()
        if children:
            first_child = children[0]
            if first_child.get_children():
                scrolled = first_child.get_children()[0]
                scrolled.get_vadjustment().set_value(0)
            else:
                scrolled = None
        else:
            scrolled = None


    def on_donate_clicked(self, button, app):
        """Handle the Donate button click"""
        details = app.get_details()
        donation_url = details.get('urls', {}).get('donation', '')
        if donation_url:
            try:
                Gio.AppInfo.launch_default_for_uri(donation_url, None)
            except Exception as e:
                print(f"Error opening donation URL: {str(e)}")

    def on_repo_toggled(self, checkbox, repo):
        enabled = checkbox.get_active()

        # Update style immediately
        if enabled:
            checkbox.get_style_context().remove_class("dim-label")
        else:
            checkbox.get_style_context().add_class("dim-label")

        success, message = fp_turbo.repotoggle(repo.get_name(), enabled, self.system_mode)

        if success:
            self.refresh_local()
        else:
            # revert UI if it failed
            checkbox.handler_block_by_func(self.on_repo_toggled)
            checkbox.set_active(not enabled)
            checkbox.handler_unblock_by_func(self.on_repo_toggled)

        if message:
            dlg = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.INFO if success else Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=message
            )
            dlg.run()
            dlg.destroy()

    def on_repo_delete(self, button, repo):
        """Handle repository deletion"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Are you sure you want to delete the '{repo.get_name()}' repository?"
        )

        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            try:
                fp_turbo.repodelete(repo.get_name(), self.system_mode)
                self.refresh_local()
                self.show_category_apps('repositories')
            except GLib.GError as e:
                # Handle polkit authentication failure
                if "not allowed for user" in str(e):
                    error_dialog = Gtk.MessageDialog(
                        transient_for=self,
                        modal=True,
                        destroy_with_parent=True,
                        message_type=Gtk.MessageType.ERROR,
                        buttons=Gtk.ButtonsType.OK,
                        text="You don't have permission to remove this repository. "
                            "Please try running the application with sudo privileges."
                    )
                    error_dialog.run()
                    error_dialog.destroy()
                else:
                    # Handle other potential errors
                    error_dialog = Gtk.MessageDialog(
                        transient_for=self,
                        modal=True,
                        destroy_with_parent=True,
                        message_type=Gtk.MessageType.ERROR,
                        buttons=Gtk.ButtonsType.OK,
                        text=f"Failed to remove repository: {str(e)}"
                    )
                    error_dialog.run()
                    error_dialog.destroy()

    def on_add_flathub_repo_button_clicked(self, button):
        """Handle the Add Flathub Repository button click"""
        # Add the repository
        success, error_message = fp_turbo.repoadd("https://dl.flathub.org/repo/flathub.flatpakrepo", self.system_mode)
        if error_message:
            error_dialog = Gtk.MessageDialog(
                transient_for=None,  # Changed from self
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=error_message
            )
            error_dialog.run()
            error_dialog.destroy()
        self.refresh_local()
        self.show_category_apps('repositories')

    def on_add_flathub_beta_repo_button_clicked(self, button):
        """Handle the Add Flathub Beta Repository button click"""
        # Add the repository
        success, error_message = fp_turbo.repoadd("https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo", self.system_mode)
        if error_message:
            error_dialog = Gtk.MessageDialog(
                transient_for=None,  # Changed from self
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=error_message
            )
            error_dialog.run()
            error_dialog.destroy()
        self.refresh_local()
        self.show_category_apps('repositories')

    def on_add_repo_button_clicked(self, button=None, file_path=None):
        """Handle the Add Repository button click"""
        response = Gtk.ResponseType.CANCEL
        dialog = Gtk.Dialog(
            title="Install?",
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
        )
        repo_file_path = ""
        # Create file chooser dialog
        if button and not file_path:
            dialog = Gtk.FileChooserDialog(
                title="Select Repository File",
                parent=self,
                action=Gtk.FileChooserAction.OPEN,
                flags=0
            )

            # Add buttons using the new method
            dialog.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL,
                "Open", Gtk.ResponseType.OK
            )

            # Add filter for .flatpakrepo files
            repo_filter = Gtk.FileFilter()
            repo_filter.set_name("Flatpak Repository Files")
            repo_filter.add_pattern("*.flatpakrepo")
            dialog.add_filter(repo_filter)

            # Show all files filter
            all_filter = Gtk.FileFilter()
            all_filter.set_name("All Files")
            all_filter.add_pattern("*")
            dialog.add_filter(all_filter)
            response = dialog.run()
            repo_file_path = dialog.get_filename()
        elif file_path and not button:
            # Create dialog
            dialog = Gtk.Dialog(
                title=f"Install {file_path}?",
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
            )
            # Add buttons using the new method
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Install", Gtk.ResponseType.OK)

            # Create content area
            content_area = dialog.get_content_area()
            content_area.set_spacing(12)
            content_area.set_border_width(12)

            content_area.pack_start(Gtk.Label(label=f"Install {file_path}?"), False, False, 0)

            if self.system_mode is False:
                content_area.pack_start(Gtk.Label(label="Installation Type: User"), False, False, 0)
            else:
                content_area.pack_start(Gtk.Label(label="Installation Type: System"), False, False, 0)
            dialog.show_all()
            response = dialog.run()
            repo_file_path = file_path
        dialog.destroy()

        if response == Gtk.ResponseType.OK and repo_file_path:
            # Add the repository
            success, error_message = fp_turbo.repoadd(repo_file_path, self.system_mode)
            if error_message:
                error_dialog = Gtk.MessageDialog(
                    transient_for=None,  # Changed from self
                    modal=True,
                    destroy_with_parent=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text=error_message
                )
                error_dialog.run()
                error_dialog.destroy()
            self.refresh_local()
            self.show_category_apps('repositories')

    def select_default_category(self):
        """
        Startup behavior:
        - If metadata is already available, go to Trending.
        - Otherwise, show a local page immediately (Installed), and let metadata load in the background.
        """
        # Prefer something local and always available first
        if not getattr(self, "metadata_loaded", False):
            # Installed is fully local
            self.on_category_clicked("installed", "system")
            return

        # Once metadata is available, default to Trending
        self.on_category_clicked("trending", "collections")

def main():
    # Initialize GTK before anything else
    if not Gtk.init_check():
        print("Failed to initialize GTK")
        return 1

    system_mode = False
    system_only_mode = False
    # Check for command line argument
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == '--system-mode':
            system_mode = True
        if arg == '--system-only-mode':
            system_mode = True
            system_only_mode = True
        if arg.endswith('.flatpakref'):
            # Create a temporary window just to handle the installation
            app = MainWindow(system_mode=system_mode, system_only_mode=system_only_mode)
            app.handle_flatpakref_file(arg)
            # Keep the window open for 5 seconds to show the result
            GLib.timeout_add_seconds(5, Gtk.main_quit)
            Gtk.main()
            return
        if arg.endswith('.flatpakrepo'):
            # Create a temporary window just to handle the installation
            app = MainWindow(system_mode=system_mode, system_only_mode=system_only_mode)
            app.handle_flatpakrepo_file(arg)
            # Keep the window open for 5 seconds to show the result
            GLib.timeout_add_seconds(5, Gtk.main_quit)
            Gtk.main()
            return

        if system_mode or system_only_mode:
            if os.getuid() > 0:
                subprocess.run(["xhost", "si:localuser:root"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
                )
                script_path = Path(__file__).resolve()
                os.execvp(
                    "pkexec",
                    [
                        "pkexec",
                        "--disable-internal-agent",
                        "env",
                        f"DISPLAY={os.environ['DISPLAY']}",
                        f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
                        f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '').lower()}",
                        f"ORIG_USER={os.getuid()!s}",
                        f"PKEXEC_UID={os.getuid()!s}",
                        "G_MESSAGES_DEBUG=none",
                        sys.executable,
                        str(script_path),
                        arg,
                    ]
                )
    GLib.set_prgname(APP_ID)
    app = MainWindow(system_mode=system_mode, system_only_mode=system_only_mode)
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()


def cleanup_xhost():
    """Cleanup function to run xhost on exit"""
    try:
        subprocess.run(["xhost", "-si:localuser:root"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
        )
    except Exception as e:
        logger.error(f"Failed to run xhost cleanup: {e}")

if __name__ == "__main__":
    try:
        # Your main application code here
        main()
    finally:
        # This ensures cleanup runs even if main() throws an exception
        cleanup_xhost()
