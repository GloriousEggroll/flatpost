#!/usr/bin/python3

import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Flatpak", "1.0")
gi.require_version("GdkPixbuf", "2.0")
import json
import subprocess
import threading
from pathlib import Path

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

from . import libflatpak_query
from .libflatpak_query import AppStreamComponentKind as AppKind


class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Flatshop")
        # Store search results as an instance variable
        self.all_apps = []
        self.current_component_type = None
        self.category_results = []  # Initialize empty list
        self.subcategory_results = []  # Initialize empty list
        self.collection_results = []  # Initialize empty list
        self.installed_results = []  # Initialize empty list
        self.updates_results = []  # Initialize empty list
        self.system_mode = False
        self.current_page = None  # Track current page
        self.current_group = None  # Track current group (system/collections/categories)

        # Set window size
        self.set_default_size(1280, 720)

        # Enable drag and drop
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self.on_drag_data_received)

        # Define category groups and their titles
        self.category_groups = {
            "system": {
                "installed": "Installed",
                "updates": "Updates",
                "repositories": "Repositories",
            },
            "collections": {
                "trending": "Trending",
                "popular": "Popular",
                "recently-added": "New",
                "recently-updated": "Updated",
            },
            "categories": {
                "office": "Productivity",
                "graphics": "Graphics & Photography",
                "audiovideo": "Audio & Video",
                "education": "Education",
                "network": "Networking",
                "game": "Games",
                "development": "Developer Tools",
                "science": "Science",
                "system": "System",
                "utility": "Utilities",
            },
        }

        # Define subcategories
        self.subcategory_groups = {
            "audiovideo": {
                "audiovideoediting": "Audio & Video Editing",
                "discburning": "Disc Burning",
                "midi": "Midi",
                "mixer": "Mixer",
                "player": "Player",
                "recorder": "Recorder",
                "sequencer": "Sequencer",
                "tuner": "Tuner",
                "tv": "TV",
            },
            "development": {
                "building": "Building",
                "database": "Database",
                "debugger": "Debugger",
                "guidesigner": "GUI Designer",
                "ide": "IDE",
                "profiling": "Profiling",
                "revisioncontrol": "Revision Control",
                "translation": "Translation",
                "webdevelopment": "Web Development",
            },
            "game": {
                "actiongame": "Action Games",
                "adventuregame": "Adventure Games",
                "arcadegame": "Arcade Games",
                "blocksgame": "Blocks Games",
                "boardgame": "Board Games",
                "cardgame": "Card Games",
                "emulator": "Emulators",
                "kidsgame": "Kids' Games",
                "logicgame": "Logic Games",
                "roleplaying": "Role Playing",
                "shooter": "Shooter",
                "simulation": "Simulation",
                "sportsgame": "Sports Games",
                "strategygame": "Strategy Games",
            },
            "graphics": {
                "2dgraphics": "2D Graphics",
                "3dgraphics": "3D Graphics",
                "ocr": "OCR",
                "photography": "Photography",
                "publishing": "Publishing",
                "rastergraphics": "Raster Graphics",
                "scanning": "Scanning",
                "vectorgraphics": "Vector Graphics",
                "viewer": "Viewer",
            },
            "network": {
                "chat": "Chat",
                "email": "Email",
                "feed": "Feed",
                "filetransfer": "File Transfer",
                "hamradio": "Ham Radio",
                "instantmessaging": "Instant Messaging",
                "ircclient": "IRC Client",
                "monitor": "Monitor",
                "news": "News",
                "p2p": "P2P",
                "remoteaccess": "Remote Access",
                "telephony": "Telephony",
                "videoconference": "Video Conference",
                "webbrowser": "Web Browser",
                "webdevelopment": "Web Development",
            },
            "office": {
                "calendar": "Calendar",
                "chart": "Chart",
                "contactmanagement": "Contact Management",
                "database": "Database",
                "dictionary": "Dictionary",
                "email": "Email",
                "finance": "Finance",
                "presentation": "Presentation",
                "projectmanagement": "Project Management",
                "publishing": "Publishing",
                "spreadsheet": "Spreadsheet",
                "viewer": "Viewer",
                "wordprocessor": "Word Processor",
            },
            "system": {
                "emulator": "Emulators",
                "filemanager": "File Manager",
                "filesystem": "Filesystem",
                "filetools": "File Tools",
                "monitor": "Monitor",
                "security": "Security",
                "terminalemulator": "Terminal Emulator",
            },
            "utility": {
                "accessibility": "Accessibility",
                "archiving": "Archiving",
                "calculator": "Calculator",
                "clock": "Clock",
                "compression": "Compression",
                "filetools": "File Tools",
                "telephonytools": "Telephony Tools",
                "texteditor": "Text Editor",
                "texttools": "Text Tools",
            },
        }

        # Add CSS provider for custom styling
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data("""
            .panel-header {
                font-size: 24px;
                font-weight: bold;
                padding: 12px;
                color: white;
            }
            .repo-list-header {
                font-size: 18px;
                padding: 5px;
                color: white;
            }
            .app-list-header {
                font-size: 18px;
                color: white;
                padding-top: 4px;
                padding-bottom: 4px;
            }
            .app-list-summary {
                padding-top: 2px;
                padding-bottom: 2px;
            }
            .app-page-header {
                font-size: 24px;
                font-weight: bold;
                padding: 12px;
                color: white;
            }
            .dark-header {
                background-color: #333333;
                padding: 6px;
                margin: 0;
            }
            .dark-category-button {
                border: 0px;
                padding: 6px;
                margin: 0;
                background: none;
            }
            .dark-category-button-active {
                background-color: #18A3FF;
                color: white;
            }
            .dark-remove-button {
                background-color: #ff4444;
                color: white;
                border: none;
                padding: 6px;
                margin: 0;
            }
            .dark-install-button {
                background-color: #18A3FF;
                color: white;
                border: none;
                padding: 6px;
                margin: 0;
            }
            .repo-item {
                padding: 6px;
                margin: 2px;
                border-bottom: 1px solid #eee;
            }
            .repo-delete-button {
                background-color: #ff4444;
                color: white;
                border: none;
                padding: 6px;
                margin-left: 6px;
            }
            .search-entry {
                padding: 5px;
                border-radius: 4px;
                border: 1px solid #ccc;
            }

            .search-entry:focus {
                border-color: #18A3FF;
                box-shadow: 0 0 0 2px rgba(24, 163, 255, 0.2);
            }
            .item-repo-label {
                background-color: #333333;
                color: white;
                border-radius: 4px;
                margin: 2px;
                padding: 2px 4px;
                font-size: 0.8em;
            }
            .dark-category-button {
                border: 0px;
                padding: 6px;
                margin: 0;
                background: none;
            }

            .dark-category-button-active {
                background-color: #18A3FF;
                color: white;
            }

            .subcategories-scroll {
                border: none;
                background-color: transparent;
                min-height: 40px;
            }

            .subcategories-scroll > GtkViewport {
                border: none;
                background-color: transparent;
            }
            .no-scroll-bars scrollbar {
                min-width: 0px;
                opacity: 0;
                margin-top: -20px;
            }
            .app-window {
                border: 0px;
                margin: 0px;
                padding-right: 20px;
                background: none;
            }
        """)

        # Add CSS provider to the default screen
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css_provider, 600)

        # Create main layout
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.add(self.main_box)

        # Create_header_bar
        self.create_header_bar()

        # Create panels
        self.create_panels()

        self.refresh_data()

        # Select Trending by default
        self.select_default_category()

    def on_drag_data_received(self, widget, context, x, y, data, info, time):
        """Handle drag and drop events"""
        # Check if data is a URI list
        if isinstance(data, int):
            return
        uri = data.get_uris()[0]
        file_path = Gio.File.new_for_uri(uri).get_path()
        if file_path and file_path.endswith(".flatpakref"):
            self.handle_flatpakref_file(file_path)
        if file_path and file_path.endswith(".flatpakrepo"):
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
        self.top_bar.set_hexpand(True)
        self.top_bar.set_vexpand(False)
        self.top_bar.set_spacing(6)
        self.top_bar.set_border_width(0)  # Remove border width
        self.top_bar.set_margin_top(0)  # Remove top margin
        self.top_bar.set_margin_bottom(0)  # Remove bottom margin

        # Add search bar
        self.searchbar = Gtk.SearchBar()  # Use self.searchbar instead of searchbar
        self.searchbar.set_hexpand(False)
        self.searchbar.set_vexpand(False)
        self.searchbar.set_margin_bottom(6)
        self.searchbar.set_margin_bottom(0)  # Remove bottom margin
        self.searchbar.set_margin_top(0)  # Remove top margin

        # Create search entry with icon
        searchentry = Gtk.SearchEntry()
        searchentry.set_placeholder_text("Search applications...")
        searchentry.set_icon_from_gicon(
            Gtk.EntryIconPosition.PRIMARY, Gio.Icon.new_for_string("search")
        )
        searchentry.set_margin_top(0)  # Remove top margin
        searchentry.set_margin_bottom(0)  # Remove bottom margin
        searchentry.set_size_request(-1, 10)  # Set specific height

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
        self.component_type_combo.set_hexpand(False)
        self.component_type_combo.set_vexpand(False)
        self.component_type_combo.set_size_request(150, -1)  # Set width in pixels
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

        # Add repository button
        refresh_metadata_button = Gtk.Button()
        refresh_metadata_button.set_tooltip_text("Refresh metadata")
        refresh_metadata_button_icon = Gio.Icon.new_for_string("system-reboot-symbolic")
        refresh_metadata_button.set_image(
            Gtk.Image.new_from_gicon(refresh_metadata_button_icon, Gtk.IconSize.BUTTON)
        )
        refresh_metadata_button.get_style_context().add_class("dark-install-button")
        refresh_metadata_button.connect("clicked", self.on_refresh_metadata_button_clicked)

        # Create system mode switch box
        system_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Create system mode switch
        self.system_switch = Gtk.Switch()
        self.system_switch.connect("notify::active", self.on_system_mode_toggled)
        self.system_switch.set_hexpand(False)
        self.system_switch.set_vexpand(False)

        # Create system mode label
        system_label = Gtk.Label(label="System")

        # Pack switch and label
        system_box.pack_end(system_label, False, False, 0)
        system_box.pack_end(self.system_switch, False, False, 0)

        # Add system controls to header
        self.top_bar.pack_end(system_box, False, False, 0)

        # Add refresh metadata button
        self.top_bar.pack_end(refresh_metadata_button, False, False, 0)

        # Add the top bar to the main box
        self.main_box.pack_start(self.top_bar, False, True, 0)

    def on_refresh_metadata_button_clicked(self, button):
        self.refresh_data()
        self.refresh_current_page()

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

    def on_system_mode_toggled(self, switch, gparam):
        """Handle system mode toggle switch state changes"""
        desired_state = switch.get_active()

        if desired_state:
            # Request superuser validation
            try:
                # subprocess.run(['pkexec', 'true'], check=True)
                self.system_mode = True
                self.refresh_data()
                self.refresh_current_page()
            except subprocess.CalledProcessError:
                switch.set_active(False)
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Authentication failed",
                    secondary_text="Could not enable system mode",
                )
                dialog.connect("response", lambda d, r: d.destroy())
                dialog.show()
        else:
            if self.system_mode == True:
                self.system_mode = False
                self.refresh_data()
                self.refresh_current_page()
            elif self.system_mode == False:
                self.system_mode = True
                self.refresh_data()
                self.refresh_current_page()

    def populate_repo_dropdown(self):
        # Get list of repositories
        libflatpak_query.repolist(self.system_mode)
        repos = libflatpak_query.repolist()

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
            destroy_with_parent=True,
        )
        dialog.set_size_request(400, 100)

        progress_bar = Gtk.ProgressBar()
        progress_bar.set_text("Initializing...")
        progress_bar.set_show_text(True)
        dialog.vbox.pack_start(progress_bar, True, True, 0)
        dialog.vbox.set_spacing(12)

        # Show the dialog
        dialog.show_all()

        searcher = libflatpak_query.get_reposearcher(self.system_mode)

        # Define thread target function
        def retrieve_metadata():
            try:
                (
                    category_results,
                    collection_results,
                    subcategory_results,
                    installed_results,
                    updates_results,
                    all_apps,
                ) = searcher.retrieve_metadata(self.system_mode)
                self.category_results = category_results
                self.category_results = subcategory_results
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
                    text=f"Error retrieving metadata: {str(e)}",
                )
                dialog.run()
                dialog.destroy()

        # Start the refresh thread
        refresh_thread = threading.Thread(target=retrieve_metadata)
        refresh_thread.start()

        def update_progress():
            while refresh_thread.is_alive():
                progress_bar.set_text("Fetching...")
                progress = searcher.refresh_progress
                progress_bar.set_fraction(progress / 100)
                return True
            else:
                progress_bar.set_fraction(100 / 100)
                dialog.destroy()

        # Start the progress update timer
        GLib.timeout_add_seconds(0.5, update_progress)
        dialog.run()
        if not refresh_thread.is_alive() and dialog.is_active():
            dialog.destroy()

    def refresh_local(self):
        try:
            searcher = libflatpak_query.get_reposearcher(self.system_mode)
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
                text=f"Error refreshing local data: {str(e)}",
            )
            dialog.run()
            dialog.destroy()

    def create_panels(self):
        # Check if panels already exist
        if hasattr(self, "left_panel") and self.left_panel.get_parent():
            self.main_box.remove(self.left_panel)

        if hasattr(self, "right_panel") and self.right_panel.get_parent():
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
        self.panels_box.pack_end(self.right_panel, True, True, 0)  # Right panel expands both ways

        # Add panels container to main box
        self.main_box.pack_start(self.panels_box, True, True, 0)

    def create_grouped_category_panel(self, title, groups):
        # Create container for categories
        panel_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel_container.set_spacing(6)
        panel_container.set_border_width(6)
        panel_container.set_size_request(300, -1)  # Set fixed width
        panel_container.set_hexpand(False)
        panel_container.set_vexpand(True)
        panel_container.set_halign(Gtk.Align.FILL)  # Fill horizontally
        panel_container.set_valign(Gtk.Align.FILL)  # Align to top

        # Create scrollable area
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)  # Expand vertically
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Create container for categories
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        container.set_spacing(6)
        container.set_border_width(6)
        container.set_halign(Gtk.Align.FILL)  # Fill horizontally
        container.set_valign(Gtk.Align.START)  # Align to top
        container.set_hexpand(True)
        container.set_vexpand(False)  # Expand vertically

        # Dictionary to store category widgets
        self.category_widgets = {}

        # Add group headers and categories
        for group_name, categories in groups.items():
            # Create a box for the header
            header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            header_box.get_style_context().add_class("dark-header")
            header_box.set_hexpand(True)  # Make the box expand horizontally

            # Create the label
            group_header = Gtk.Label(label=group_name.upper())
            group_header.get_style_context().add_class("title-2")
            group_header.set_halign(Gtk.Align.START)

            # Add the label to the box
            header_box.pack_start(group_header, False, False, 0)

            # Add the box to the container
            container.pack_start(header_box, False, False, 0)

            # Store widgets for this group
            self.category_widgets[group_name] = []

            # Add categories in the group
            for category, display_title in categories.items():
                # Create a clickable box for each category
                category_box = Gtk.EventBox()
                category_box.set_hexpand(True)
                category_box.set_halign(Gtk.Align.FILL)
                category_box.set_margin_top(2)
                category_box.set_margin_bottom(2)

                # Create label for the category
                category_label = Gtk.Label(label=display_title)
                category_label.set_halign(Gtk.Align.START)
                category_label.set_hexpand(True)
                category_label.get_style_context().add_class("dark-category-button")

                # Add label to the box
                category_box.add(category_label)

                # Connect click event
                category_box.connect(
                    "button-release-event",
                    lambda widget, event, cat=category, grp=group_name: self.on_category_clicked(
                        cat, grp
                    ),
                )

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
            self.show_category_apps(self.current_category)
            return

        # Combine all searchable fields
        searchable_items = []
        for app in self.all_apps:
            details = app.get_details()
            searchable_items.append(
                {
                    "text": f"{details['name']} {details['description']} {details['categories']}".lower(),
                    "app": app,
                    "id": details["id"].lower(),
                    "name": details["name"].lower(),
                }
            )

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
            if (
                component_type_filter
                and item["app"].get_details()["kind"] != component_type_filter
            ):
                continue

            # Check exact ID match
            if item["id"] == search_term:
                exact_id_matches.append(item["app"])
                continue

            # Check exact name match
            if item["name"] == search_term:
                exact_name_matches.append(item["app"])
                continue

            # Check for partial matches longer than 5 characters
            if len(search_term) > 5:
                if search_term in item["id"] or search_term in item["name"]:
                    partial_matches.append(item["app"])
                    continue

            # Check for other matches
            if search_term in item["text"]:
                other_matches.append(item["app"])

        # Combine results in order of priority
        return exact_id_matches + exact_name_matches + partial_matches + other_matches

    def show_search_results(self, apps):
        """Display search results in the right panel"""
        self.display_apps(apps)

    def on_category_clicked(self, category, group, *args):
        # Remove active state from all widgets in all groups
        for group_name in self.category_widgets:
            for widget in self.category_widgets[group_name]:
                widget.get_style_context().remove_class("dark-category-button-active")

        # Add active state to the clicked category
        display_title = self.category_groups[group][category]
        for widget in self.category_widgets[group]:
            if widget.get_children()[0].get_label() == display_title:
                widget.get_style_context().add_class("dark-category-button-active")
                break
        self.current_page = category
        self.current_group = group
        self.update_category_header(category)
        self.update_subcategories_bar(category)
        self.show_category_apps(category)

    def refresh_current_page(self):
        """Refresh the currently displayed page"""
        if self.current_page and self.current_group:
            self.on_category_clicked(self.current_page, self.current_group)

    def update_category_header(self, category):
        """Update the category header text based on the selected category."""
        display_title = ""
        if category in self.category_groups["system"]:
            display_title = self.category_groups["system"][category]
        if category in self.category_groups["collections"]:
            display_title = self.category_groups["collections"][category]
        elif category in self.category_groups["categories"]:
            display_title = self.category_groups["categories"][category]
        else:  # Find the parent category and get the title
            for parent_category, subcategories in self.subcategory_groups.items():
                if category in subcategories:
                    display_title = subcategories[category]
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

        # Add category header
        self.category_header = Gtk.Label(label="")
        self.category_header.get_style_context().add_class("panel-header")
        self.category_header.set_hexpand(True)
        self.category_header.set_halign(Gtk.Align.START)
        self.right_panel.pack_start(self.category_header, False, False, 0)

        # Create subcategories bar (initially hidden)
        self.subcategories_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.subcategories_bar.set_hexpand(True)
        self.subcategories_bar.set_spacing(6)
        self.subcategories_bar.set_border_width(6)
        # self.subcategories_bar.get_style_context().add_class("dark-header")
        self.subcategories_bar.set_visible(False)
        self.subcategories_bar.set_halign(Gtk.Align.FILL)  # Ensure full width
        self.right_panel.pack_start(self.subcategories_bar, False, False, 0)

        # Create scrollable area
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)

        # Create container for applications
        self.right_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.right_container.set_spacing(6)
        self.right_container.set_border_width(6)
        self.right_container.set_hexpand(True)  # Add this line
        self.right_container.set_vexpand(True)  # Add this line
        self.right_container.get_style_context().add_class("app-window")
        scrolled_window.add(self.right_container)
        self.right_panel.pack_start(scrolled_window, True, True, 0)
        return self.right_panel

    def update_subcategories_bar(self, category):
        """Update the subcategories bar based on the current category."""
        # Clear existing subcategories
        for child in self.subcategories_bar.get_children():
            child.destroy()

        # Create pan start button
        pan_start = Gtk.Button()
        pan_start_icon = Gio.Icon.new_for_string("pan-start-symbolic")
        pan_start.set_image(Gtk.Image.new_from_gicon(pan_start_icon, Gtk.IconSize.BUTTON))
        pan_start.get_style_context().add_class("dark-category-button")
        pan_start.connect("clicked", self.on_pan_start)

        # Create scrolled window
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(False)
        self.scrolled_window.set_size_request(-1, 40)
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.scrolled_window.set_min_content_width(0)  # Allow shrinking below content size
        self.scrolled_window.set_max_content_width(-1)  # No artificial width limit
        self.scrolled_window.set_overlay_scrolling(False)
        self.scrolled_window.get_style_context().add_class("no-scroll-bars")

        # Create container for subcategories
        container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        container.set_spacing(6)
        container.set_border_width(6)
        container.set_hexpand(True)
        container.set_halign(Gtk.Align.CENTER)
        container.set_homogeneous(False)

        # Check if the category has subcategories
        if category in self.subcategory_groups:
            # Add subcategories
            for subcategory, title in self.subcategory_groups[category].items():
                # Create clickable box for subcategory
                subcategory_box = Gtk.EventBox()
                subcategory_box.set_hexpand(False)
                subcategory_box.set_halign(Gtk.Align.START)
                subcategory_box.set_margin_top(2)
                subcategory_box.set_margin_bottom(2)

                # Create label for subcategory
                subcategory_label = Gtk.Label(label=title)
                subcategory_label.set_halign(Gtk.Align.START)
                subcategory_label.set_hexpand(False)
                subcategory_label.get_style_context().add_class("dark-category-button")

                # Add label to box
                subcategory_box.add(subcategory_label)

                # Connect click event
                subcategory_box.connect(
                    "button-release-event",
                    lambda widget, event, subcat=subcategory: self.on_subcategory_clicked(subcat),
                )

                # Store widget in group
                container.pack_start(subcategory_box, False, False, 0)

            # Add container to scrolled window
            self.scrolled_window.add(container)

            # Create pan end button
            pan_end = Gtk.Button()
            pan_end_icon = Gio.Icon.new_for_string("pan-end-symbolic")
            pan_end.set_image(Gtk.Image.new_from_gicon(pan_end_icon, Gtk.IconSize.BUTTON))
            pan_end.get_style_context().add_class("dark-category-button")
            pan_end.connect("clicked", self.on_pan_end)

            # Show the bar and force a layout update
            self.subcategories_bar.get_style_context().add_class("dark-header")
            self.subcategories_bar.set_visible(True)
            self.subcategories_bar.pack_start(pan_start, False, False, 0)
            self.subcategories_bar.pack_start(self.scrolled_window, True, True, 0)
            self.subcategories_bar.pack_start(pan_end, False, False, 0)
            # self.subcategories_bar.pack_start(container, True, True, 0)
            self.subcategories_bar.queue_resize()
            self.subcategories_bar.show_all()
        else:
            # Check if current category is a subcategory
            is_subcategory = False
            parent_category = None
            for parent, subcategories in self.subcategory_groups.items():
                if category in subcategories:
                    is_subcategory = True
                    parent_category = parent
                    break

            if is_subcategory:
                # Add parent category and current subcategory
                container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                container.set_spacing(6)
                container.set_border_width(6)
                container.set_hexpand(True)
                container.set_halign(Gtk.Align.CENTER)

                # Create parent category box
                parent_box = Gtk.EventBox()
                parent_box.set_hexpand(False)
                parent_box.set_halign(Gtk.Align.START)
                parent_box.set_margin_top(2)
                parent_box.set_margin_bottom(2)

                # Create parent label
                parent_label = Gtk.Label(label=self.category_groups["categories"][parent_category])
                parent_label.set_halign(Gtk.Align.START)
                parent_label.set_hexpand(False)
                parent_label.get_style_context().add_class("dark-category-button")

                # Add label to box
                parent_box.add(parent_label)

                # Connect click event
                parent_box.connect(
                    "button-release-event",
                    lambda widget,
                    event,
                    cat=parent_category,
                    grp="categories": self.on_category_clicked(cat, grp),
                )

                # Add parent box to container
                container.pack_start(parent_box, False, False, 0)

                # Create current subcategory box
                subcategory_box = Gtk.EventBox()
                subcategory_box.set_hexpand(False)
                subcategory_box.set_halign(Gtk.Align.START)
                subcategory_box.set_margin_top(2)
                subcategory_box.set_margin_bottom(2)

                # Create subcategory label
                subcategory_label = Gtk.Label(
                    label=self.subcategory_groups[parent_category][category]
                )
                subcategory_label.set_halign(Gtk.Align.START)
                subcategory_label.set_hexpand(False)
                subcategory_label.get_style_context().add_class("dark-category-button")

                # Add label to box
                subcategory_box.add(subcategory_label)

                # Connect click event
                subcategory_box.connect(
                    "button-release-event",
                    lambda widget, event, subcat=category: self.on_subcategory_clicked(subcat),
                )

                # Add subcategory box to container
                container.pack_start(subcategory_box, False, False, 0)

                # Add container to scrolled window
                self.scrolled_window.add(container)
                self.subcategories_bar.get_style_context().add_class("dark-header")
                # Show the bar and force a layout update
                self.subcategories_bar.set_visible(True)
                self.subcategories_bar.pack_start(self.scrolled_window, True, True, 0)
                # self.subcategories_bar.pack_start(container, True, True, 0)
                self.subcategories_bar.queue_resize()
                self.subcategories_bar.show_all()
            else:
                self.subcategories_bar.get_style_context().remove_class("dark-header")
                # Hide the bar and force a layout update
                self.subcategories_bar.set_visible(False)

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

    def on_subcategory_clicked(self, subcategory):
        """Handle subcategory button clicks."""
        # Update the current page to the subcategory
        self.current_page = subcategory
        self.current_group = "subcategories"
        self.update_category_header(subcategory)
        self.update_subcategories_bar(subcategory)
        self.show_category_apps(subcategory)

    # Create and connect buttons
    def create_button(self, callback, app, label=None, condition=None):
        """Create a button with optional visibility condition"""
        button = Gtk.Button()
        if label:
            button = Gtk.Button(label=label)
        button.get_style_context().add_class("app-button")
        if condition is not None:
            if not condition(app):
                return None
        button.connect("clicked", callback, app)
        return button

    def clear_container(self, container):
        """Clear all widgets from a container"""
        for child in container.get_children():
            child.destroy()

    def get_app_priority(self, kind):
        """Convert AppKind to numeric priority for sorting"""
        priorities = {"DESKTOP_APP": 0, "ADDON": 1, "RUNTIME": 2}
        return priorities.get(kind, 3)

    def show_category_apps(self, category):
        # Initialize apps list
        apps = []

        # Load system data
        if "installed" in category:
            apps.extend([app for app in self.installed_results])
        if "updates" in category:
            apps.extend([app for app in self.updates_results])

        if ("installed" in category) or ("updates" in category):
            # Sort apps by component type priority
            if apps:
                apps.sort(key=lambda app: self.get_app_priority(app.get_details()["kind"]))

        # Load collections data
        try:
            with open("collections_data.json", "r", encoding="utf-8") as f:
                collections_data = json.load(f)

                # Find the specific category in collections data
                category_entry = next(
                    (entry for entry in collections_data if entry["category"] == category), None
                )

                if category_entry:
                    # Get all app IDs in this category
                    app_ids_in_category = [hit["app_id"] for hit in category_entry["data"]["hits"]]

                    # Filter apps based on presence in category
                    apps.extend(
                        [
                            app
                            for app in self.collection_results
                            if app.get_details()["id"] in app_ids_in_category
                        ]
                    )
                else:
                    # Fallback to previous behavior if category isn't in collections
                    apps.extend(
                        [
                            app
                            for app in self.collection_results
                            if category in app.get_details()["categories"]
                        ]
                    )

        except (IOError, json.JSONDecodeError) as e:
            print(f"Error reading collections data: {str(e)}")
            apps.extend(
                [
                    app
                    for app in self.collection_results
                    if category in app.get_details()["categories"]
                ]
            )

        if "repositories" in category:
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
            right_label.set_halign(Gtk.Align.END)  # Align right
            header_bar.pack_end(right_label, False, False, 0)

            # Add header bar to container
            self.right_container.pack_start(header_bar, False, False, 0)

            # Get list of repositories
            repos = libflatpak_query.repolist(self.system_mode)

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
            add_icon = Gio.Icon.new_for_string("list-add")
            add_repo_button.set_image(Gtk.Image.new_from_gicon(add_icon, Gtk.IconSize.BUTTON))
            add_repo_button.get_style_context().add_class("dark-install-button")
            add_repo_button.connect("clicked", self.on_add_repo_button_clicked)

            add_flathub_repo_button = Gtk.Button(label="Add Flathub Repo")
            add_flathub_repo_button.get_style_context().add_class("dark-install-button")
            add_flathub_repo_button.connect("clicked", self.on_add_flathub_repo_button_clicked)

            add_flathub_beta_repo_button = Gtk.Button(label="Add Flathub Beta Repo")
            add_flathub_beta_repo_button.get_style_context().add_class("dark-install-button")
            add_flathub_beta_repo_button.connect(
                "clicked", self.on_add_flathub_beta_repo_button_clicked
            )

            # Check for existing Flathub repositories and disable buttons accordingly
            flathub_url = "https://dl.flathub.org/repo/"
            flathub_beta_url = "https://dl.flathub.org/beta-repo/"

            existing_urls = [repo.get_url().rstrip("/") for repo in repos]
            add_flathub_repo_button.set_sensitive(flathub_url.rstrip("/") not in existing_urls)
            add_flathub_beta_repo_button.set_sensitive(
                flathub_beta_url.rstrip("/") not in existing_urls
            )

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
                delete_icon = Gio.Icon.new_for_string("list-remove")
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
            apps = [app for app in apps if app.get_details()["kind"] == component_type_filter]

        self.display_apps(apps)

    def create_scaled_icon(self, icon, is_themed=False):
        if is_themed:
            # For themed icons, create a pixbuf directly using the icon theme
            icon_theme = Gtk.IconTheme.get_default()
            pb = icon_theme.load_icon(icon.get_names()[0], 64, Gtk.IconLookupFlags.FORCE_SIZE)
        else:
            # For file-based icons
            pb = GdkPixbuf.Pixbuf.new_from_file(icon)

        # Scale to 64x64 using high-quality interpolation
        scaled_pb = pb.scale_simple(
            64,
            64,  # New dimensions
            GdkPixbuf.InterpType.BILINEAR,  # High-quality scaling
        )

        # Create the image widget from the scaled pixbuf
        return Gtk.Image.new_from_pixbuf(scaled_pb)

    def display_apps(self, apps):
        for child in self.right_container.get_children():
            child.destroy()
        # Create a dictionary to group apps by ID
        apps_by_id = {}
        for app in apps:
            details = app.get_details()
            app_id = details["id"]

            # If app_id isn't in dictionary, add it
            if app_id not in apps_by_id:
                apps_by_id[app_id] = {"app": app, "repos": set()}

            # Add repository to the set
            repo_name = details.get("repo", "unknown")
            apps_by_id[app_id]["repos"].add(repo_name)

        # Display each unique application
        for app_id, app_data in apps_by_id.items():
            app = app_data["app"]
            details = app.get_details()
            is_installed = False
            for package in self.installed_results:
                if details["id"] == package.id:
                    is_installed = True
                    break
            is_updatable = False
            for package in self.updates_results:
                if details["id"] == package.id:
                    is_updatable = True
                    break

            # Create application container
            app_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            app_container.set_spacing(12)
            app_container.set_margin_top(6)
            app_container.set_margin_bottom(6)

            # Add icon placeholder
            icon_box = Gtk.Box()
            icon_box.set_size_request(88, -1)

            # Create and add the icon
            app_icon = Gio.Icon.new_for_string("package-x-generic-symbolic")
            icon_widget = self.create_scaled_icon(app_icon, is_themed=True)

            if details["icon_filename"]:
                if Path(details["icon_path_128"] + "/" + details["icon_filename"]).exists():
                    icon_widget = self.create_scaled_icon(
                        f"{details['icon_path_128']}/{details['icon_filename']}", is_themed=False
                    )

            icon_widget.set_size_request(64, 64)
            icon_box.pack_start(icon_widget, True, True, 0)

            # Create right side layout for text
            right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            right_box.set_spacing(4)
            right_box.set_hexpand(True)

            # Add title
            title_label = Gtk.Label(label=details["name"])
            title_label.get_style_context().add_class("app-list-header")
            title_label.set_halign(Gtk.Align.START)
            title_label.set_yalign(0.5)  # Use yalign instead of valign
            title_label.set_hexpand(True)

            # Add repository labels
            kind_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            kind_box.set_spacing(4)
            kind_box.set_halign(Gtk.Align.START)
            kind_box.set_valign(Gtk.Align.START)

            kind_label = Gtk.Label(label=details["kind"])
            kind_label.get_style_context().add_class("item-repo-label")
            kind_label.set_halign(Gtk.Align.START)
            kind_box.pack_end(kind_label, False, False, 0)

            # Add repository labels
            repo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            repo_box.set_spacing(4)
            repo_box.set_halign(Gtk.Align.END)
            repo_box.set_valign(Gtk.Align.END)

            # Add repository labels
            for repo in sorted(app_data["repos"]):
                repo_label = Gtk.Label(label=repo)
                repo_label.get_style_context().add_class("item-repo-label")
                repo_label.set_halign(Gtk.Align.END)
                repo_box.pack_end(repo_label, False, False, 0)

            # Add summary
            desc_label = Gtk.Label(label=details["summary"])
            desc_label.set_halign(Gtk.Align.START)
            desc_label.set_yalign(0.5)
            desc_label.set_hexpand(True)
            desc_label.set_line_wrap(True)
            desc_label.set_line_wrap_mode(Gtk.WrapMode.WORD)
            desc_label.get_style_context().add_class("dim-label")
            desc_label.get_style_context().add_class("app-list-summary")

            # Create buttons box
            buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            buttons_box.set_spacing(6)
            buttons_box.set_margin_top(4)
            buttons_box.set_halign(Gtk.Align.END)

            # Install/Remove button
            if is_installed:
                button = self.create_button(
                    self.on_remove_clicked, app, None, condition=lambda x: True
                )
                add_rm_icon = "list-remove"
                add_rm_style = "dark-remove-buton"
            else:
                button = self.create_button(
                    self.on_install_clicked, app, None, condition=lambda x: True
                )
                add_rm_icon = "list-add"
                add_rm_style = "dark-install-buton"

            if button:
                use_icon = Gio.Icon.new_for_string(add_rm_icon)
                button.set_image(Gtk.Image.new_from_gicon(use_icon, Gtk.IconSize.BUTTON))
                button.get_style_context().add_class(add_rm_style)
            buttons_box.pack_end(button, False, False, 0)

            # Add Update button if available
            if is_updatable:
                update_button = self.create_button(
                    self.on_update_clicked, app, None, condition=lambda x: True
                )
                if update_button:
                    update_icon = Gio.Icon.new_for_string("system-software-update-symbolic")
                    update_button.set_image(
                        Gtk.Image.new_from_gicon(update_icon, Gtk.IconSize.BUTTON)
                    )
                    update_button.get_style_context().add_class("dark-install-button")
                    buttons_box.pack_end(update_button, False, False, 0)

            # Details button
            details_btn = self.create_button(self.on_details_clicked, app, None)
            if details_btn:
                details_icon = Gio.Icon.new_for_string("question")
                details_btn.set_image(Gtk.Image.new_from_gicon(details_icon, Gtk.IconSize.BUTTON))
                details_btn.get_style_context().add_class("dark-install-button")
                buttons_box.pack_end(details_btn, False, False, 0)

            # Donate button with condition
            donate_btn = self.create_button(
                self.on_donate_clicked,
                app,
                None,
                condition=lambda x: x.get_details().get("urls", {}).get("donation", ""),
            )
            if donate_btn:
                donate_icon = Gio.Icon.new_for_string("donate")
                donate_btn.set_image(Gtk.Image.new_from_gicon(donate_icon, Gtk.IconSize.BUTTON))
                donate_btn.get_style_context().add_class("dark-install-button")
                buttons_box.pack_end(donate_btn, False, False, 0)

            # Add widgets to right box
            right_box.pack_start(title_label, False, False, 0)
            right_box.pack_start(kind_box, False, False, 0)
            right_box.pack_start(repo_box, False, False, 0)
            right_box.pack_start(desc_label, False, False, 0)
            right_box.pack_start(buttons_box, False, True, 0)

            # Add to container
            app_container.pack_start(icon_box, False, False, 0)
            app_container.pack_start(right_box, True, True, 0)
            self.right_container.pack_start(app_container, False, False, 0)
            self.right_container.pack_start(
                Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
            )

        self.right_container.show_all()  # Show all widgets after adding them

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
        id = ""
        if button and app:
            details = app.get_details()
            title = f"Install {details['name']}?"
            label = f"Install: {details['id']}?"
            id = details["id"]
        # this is a stupid workaround for our button creator
        # so that we can use the same function in drag and drop
        # which of course does not have a button object
        elif app and not button:
            title = f"Install {app}?"
            label = f"Install: {app}?"
        else:
            message_type = Gtk.MessageType.ERROR
            finished_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
                message_type=message_type,
                buttons=Gtk.ButtonsType.OK,
                text="Error: No app specified",
            )
            finished_dialog.run()
            finished_dialog.destroy()
            return
        # Create dialog
        dialog = Gtk.Dialog(
            title=title,
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

        # Create repository dropdown
        repo_combo = Gtk.ComboBoxText()

        content_area.pack_start(Gtk.Label(label=label), False, False, 0)

        # Search for available repositories containing this app
        searcher = libflatpak_query.get_reposearcher(self.system_mode)
        if self.system_mode is False:
            content_area.pack_start(Gtk.Label(label="Installation Type: User"), False, False, 0)
        else:
            content_area.pack_start(Gtk.Label(label="Installation Type: System"), False, False, 0)

        # Populate repository dropdown
        if button and app:
            available_repos = set()
            repos = libflatpak_query.repolist(self.system_mode)
            for repo in repos:
                if not repo.get_disabled():
                    search_results = searcher.search_flatpak(id, repo.get_name())
                    if search_results:
                        available_repos.add(repo)

            # Add repositories to dropdown
            if available_repos:
                repo_combo.remove_all()  # Clear any existing items

                # Add all repositories
                for repo in available_repos:
                    repo_combo.append_text(repo.get_name())

                # Only show dropdown if there are multiple repositories
                if len(available_repos) >= 2:
                    # Remove and re-add with dropdown visible
                    content_area.pack_start(repo_combo, False, False, 0)
                    repo_combo.set_button_sensitivity(Gtk.SensitivityType.AUTO)
                    repo_combo.set_active(0)
                else:
                    # Remove and re-add without dropdown
                    content_area.remove(repo_combo)
                    repo_combo.set_active(0)
            else:
                repo_combo.remove_all()  # Clear any existing items
                repo_combo.append_text("No repositories available")
                content_area.remove(repo_combo)
            # Show dialog
            dialog.show_all()
        else:
            # Show dialog
            dialog.show_all()

        # Run dialog
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            selected_repo = None
            if button and app:
                selected_repo = repo_combo.get_active_text()

            # Perform installation
            def perform_installation():
                # Show waiting dialog
                GLib.idle_add(self.show_waiting_dialog)
                if button and app:
                    success, message = libflatpak_query.install_flatpak(
                        app, selected_repo, self.system_mode
                    )
                else:
                    success, message = libflatpak_query.install_flatpakref(app, self.system_mode)
                GLib.idle_add(lambda: self.on_task_complete(dialog, success, message))
                # Start spinner and begin installation

            thread = threading.Thread(target=perform_installation)
            thread.daemon = True  # Allow program to exit even if thread is still running
            thread.start()
        dialog.destroy()

    def on_task_complete(self, dialog, success, message):
        """Handle tasl completion"""
        # Update UI
        message_type = Gtk.MessageType.INFO
        if not success:
            message_type = Gtk.MessageType.ERROR
        if message:
            finished_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
                message_type=message_type,
                buttons=Gtk.ButtonsType.OK,
                text=message,
            )
            finished_dialog.run()
            finished_dialog.destroy()
        self.refresh_local()
        self.refresh_current_page()
        self.waiting_dialog.destroy()

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

                success, message = libflatpak_query.remove_flatpak(app, None, self.system_mode)

                # Update UI on main thread
                GLib.idle_add(lambda: self.on_task_complete(dialog, success, message))

            # Start spinner and begin installation
            thread = threading.Thread(target=perform_removal)
            thread.daemon = True  # Allow program to exit even if thread is still running
            thread.start()

        dialog.destroy()

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

                success, message = libflatpak_query.update_flatpak(app, None, self.system_mode)

                # Update UI on main thread
                GLib.idle_add(lambda: self.on_task_complete(dialog, success, message))

            # Start spinner and begin installation
            thread = threading.Thread(target=perform_update)
            thread.daemon = True  # Allow program to exit even if thread is still running
            thread.start()

        dialog.destroy()

    def on_details_clicked(self, button, app):
        """Handle the Details button click"""
        details = app.get_details()
        print(f"Showing details for: {details['name']}")
        # Implement details view here
        # Could open a new window with extended information

    def on_donate_clicked(self, button, app):
        """Handle the Donate button click"""
        details = app.get_details()
        donation_url = details.get("urls", {}).get("donation", "")
        if donation_url:
            try:
                Gio.AppInfo.launch_default_for_uri(donation_url, None)
            except Exception as e:
                print(f"Error opening donation URL: {str(e)}")

    def on_repo_toggled(self, checkbox, repo):
        """Handle repository enable/disable toggle"""
        repo.set_disabled(checkbox.get_active())
        # Update the UI to reflect the new state
        checkbox.get_parent().set_sensitive(True)
        if checkbox.get_active():
            checkbox.get_style_context().remove_class("dim-label")
            success, message = libflatpak_query.repotoggle(repo.get_name(), True, self.system_mode)
            message_type = Gtk.MessageType.INFO
            if success:
                self.refresh_local()
            else:
                if message:
                    message_type = Gtk.MessageType.ERROR
            if message:
                dialog = Gtk.MessageDialog(
                    transient_for=None,  # Changed from self
                    modal=True,
                    destroy_with_parent=True,
                    message_type=message_type,
                    buttons=Gtk.ButtonsType.OK,
                    text=message,
                )
                dialog.run()
                dialog.destroy()
        else:
            checkbox.get_style_context().add_class("dim-label")
            success, message = libflatpak_query.repotoggle(
                repo.get_name(), False, self.system_mode
            )
            message_type = Gtk.MessageType.INFO
            if success:
                self.refresh_local()
            else:
                if message:
                    message_type = Gtk.MessageType.ERROR
            if message:
                dialog = Gtk.MessageDialog(
                    transient_for=None,  # Changed from self
                    modal=True,
                    destroy_with_parent=True,
                    message_type=message_type,
                    buttons=Gtk.ButtonsType.OK,
                    text=message,
                )
                dialog.run()
                dialog.destroy()

    def on_repo_delete(self, button, repo):
        """Handle repository deletion"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Are you sure you want to delete the '{repo.get_name()}' repository?",
        )

        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            try:
                libflatpak_query.repodelete(repo.get_name(), self.system_mode)
                self.refresh_local()
                self.show_category_apps("repositories")
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
                        "Please try running the application with sudo privileges.",
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
                        text=f"Failed to remove repository: {str(e)}",
                    )
                    error_dialog.run()
                    error_dialog.destroy()

    def on_add_flathub_repo_button_clicked(self, button):
        """Handle the Add Flathub Repository button click"""
        # Add the repository
        success, error_message = libflatpak_query.repoadd(
            "https://dl.flathub.org/repo/flathub.flatpakrepo", self.system_mode
        )
        if error_message:
            error_dialog = Gtk.MessageDialog(
                transient_for=None,  # Changed from self
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=error_message,
            )
            error_dialog.run()
            error_dialog.destroy()
        self.refresh_local()
        self.show_category_apps("repositories")

    def on_add_flathub_beta_repo_button_clicked(self, button):
        """Handle the Add Flathub Beta Repository button click"""
        # Add the repository
        success, error_message = libflatpak_query.repoadd(
            "https://dl.flathub.org/beta-repo/flathub-beta.flatpakrepo", self.system_mode
        )
        if error_message:
            error_dialog = Gtk.MessageDialog(
                transient_for=None,  # Changed from self
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=error_message,
            )
            error_dialog.run()
            error_dialog.destroy()
        self.refresh_local()
        self.show_category_apps("repositories")

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
                flags=0,
            )

            # Add buttons using the new method
            dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)

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
                content_area.pack_start(
                    Gtk.Label(label="Installation Type: User"), False, False, 0
                )
            else:
                content_area.pack_start(
                    Gtk.Label(label="Installation Type: System"), False, False, 0
                )
            dialog.show_all()
            response = dialog.run()
            repo_file_path = file_path
        dialog.destroy()

        if response == Gtk.ResponseType.OK and repo_file_path:
            # Add the repository
            success, error_message = libflatpak_query.repoadd(repo_file_path, self.system_mode)
            if error_message:
                error_dialog = Gtk.MessageDialog(
                    transient_for=None,  # Changed from self
                    modal=True,
                    destroy_with_parent=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text=error_message,
                )
                error_dialog.run()
                error_dialog.destroy()
            self.refresh_local()
            self.show_category_apps("repositories")

    def select_default_category(self):
        # Select Trending by default
        if "collections" in self.category_widgets and self.category_widgets["collections"]:
            self.on_category_clicked("trending", "collections")


def main():
    # Check for command line argument
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.endswith(".flatpakref"):
            # Create a temporary window just to handle the installation
            app = MainWindow()
            app.handle_flatpakref_file(arg)
            # Keep the window open for 5 seconds to show the result
            GLib.timeout_add_seconds(5, Gtk.main_quit)
            Gtk.main()
            return
        if arg.endswith(".flatpakrepo"):
            # Create a temporary window just to handle the installation
            app = MainWindow()
            app.handle_flatpakrepo_file(arg)
            # Keep the window open for 5 seconds to show the result
            GLib.timeout_add_seconds(5, Gtk.main_quit)
            Gtk.main()
            return
    app = MainWindow()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
