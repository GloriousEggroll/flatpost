#!/usr/bin/python3

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Flatpak", "1.0")
from gi.repository import Gtk, Gio, Gdk
import sqlite3
import requests
from urllib.parse import quote_plus
import libflatpak_query
from libflatpak_query import AppstreamSearcher, Flatpak
import json

class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__()

        # Store search results as an instance variable
        self.category_results = []  # Initialize empty list
        self.collection_results = []  # Initialize empty list

        # Set window size
        self.set_default_size(1280, 720)

        # Define category groups and their titles
        self.category_groups = {
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

        # Define subcategories for Games
        self.subcategories = {
            'Emulator': 'Emulators',
            'Launcher': 'Game Launchers',
            'Tool': 'Game Tools'
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
        """)

        # Add CSS provider to the default screen
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            600
        )

        # Create main layout
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(self.main_box)

        # Create panels
        self.create_panels()

        self.refresh_data()

        # Select Trending by default
        self.select_default_category()

    def update_collection_results(self, new_collection_results):
        """Update search results by replacing existing items and adding new ones."""
        # Create a set of existing app_ids for efficient lookup
        existing_app_ids = {app.id for app in self.collection_results}

        # Create a list to store the updated results
        updated_results = []

        # First add all existing results
        updated_results.extend(self.collection_results)

        # Add new results, replacing any existing ones
        for new_result in new_collection_results:
            app_id = new_result.id
            if app_id in existing_app_ids:
                # Replace existing result
                for i, existing in enumerate(updated_results):
                    if existing.id == app_id:
                        updated_results[i] = new_result
                        break
            else:
                # Add new result
                updated_results.append(new_result)

        self.collection_results = updated_results

    def refresh_data(self):

        total_categories = sum(len(categories) for categories in self.category_groups.values())
        current_category = 0
        msg = "Fetching metadata, please wait..."
        dialog = Gtk.Dialog(
            title=msg,
            parent=self,
            modal=True,
            destroy_with_parent=True
        )

        # Set dialog size
        dialog.set_size_request(400, 100)

        # Create progress bar
        progress_bar = Gtk.ProgressBar()
        progress_bar.set_text(msg)

        # Add progress bar to dialog
        dialog.vbox.pack_start(progress_bar, True, True, 0)
        dialog.vbox.set_spacing(12)

        # Show the dialog and all its children
        dialog.show_all()

        # Search for each app in local repositories
        searcher = AppstreamSearcher()
        searcher.add_installation(Flatpak.Installation.new_user())

        for group_name, categories in self.category_groups.items():
            # Process categories one at a time to keep GUI responsive
            for category, title in categories.items():

                # Preload the currently saved collections data first
                try:
                    with open("collections_data.json", 'r', encoding='utf-8') as f:
                        collections_data = json.load(f)
                        for collection in collections_data:
                            if collection['category'] == category:
                                apps =  [app['app_id'] for app in collection['data'].get('hits', [])]
                                for app_id in apps:
                                    search_result = searcher.search_flatpak(app_id, 'flathub')
                                    self.collection_results.extend(search_result)
                except (IOError, json.JSONDecodeError) as e:
                    print(f"Error loading collections data: {str(e)}")

                # Try to get apps from Flathub API if internet is available
                if self.check_internet():
                    api_data = self.fetch_flathub_category_apps(category)
                    if api_data:
                        apps = api_data['hits']

                        for app in apps:
                            app_id = app['app_id']
                            # Search for the app in local repositories
                            search_result = searcher.search_flatpak(app_id, 'flathub')
                            self.category_results.extend(search_result)
                else:
                    apps = searcher.get_all_apps('flathub')
                    for app in apps:
                        details = app.get_details()
                        if category in details['categories']:
                            search_result = searcher.search_flatpak(details['name'], 'flathub')
                            self.category_results.extend(search_result)

                current_category += 1

                # Update progress bar
                progress = (current_category / total_categories) * 100
                progress_bar.set_fraction(progress / 100)

                # Force GTK to process events
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)

        self.save_collections_data()

        # load collections from json file again
        # we do this in one go after all of the data from each category has been saved to the json file.
        # this time we update entries that already exist and add new entries that don't exist.
        for group_name, categories in self.category_groups.items():
            for category, title in categories.items():
                if category in self.category_groups['collections']:
                    try:
                        with open("collections_data.json", 'r', encoding='utf-8') as f:
                            collections_data = json.load(f)
                            for collection in collections_data:
                                if collection['category'] == category:
                                    apps =  [app['app_id'] for app in collection['data'].get('hits', [])]
                                    new_results = []
                                    for app_id in apps:
                                        search_result = searcher.search_flatpak(app_id, 'flathub')
                                        new_results.extend(search_result)
                                    self.update_collection_results(new_results)
                    except (IOError, json.JSONDecodeError) as e:
                        print(f"Error loading collections data: {str(e)}")

        dialog.destroy()

    def create_panels(self):
        # Create left panel with grouped categories
        self.create_grouped_category_panel("Categories", self.category_groups)

        # Create right panel
        self.right_panel = self.create_applications_panel("Applications")

    def create_grouped_category_panel(self, title, groups):
        # Create scrollable area
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_size_request(300, -1)  # Set fixed width
        scrolled_window.set_hexpand(False)  # Don't expand horizontally
        scrolled_window.set_vexpand(True)   # Expand vertically

        # Create container for categories
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        container.set_spacing(6)
        container.set_border_width(6)
        container.set_halign(Gtk.Align.FILL)  # Fill horizontally
        container.set_valign(Gtk.Align.START)  # Align to top

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
                category_box.connect("button-release-event",
                                lambda widget, event, cat=category, grp=group_name:
                                self.on_category_clicked(cat, grp))

                # Store widget in group
                self.category_widgets[group_name].append(category_box)
                container.pack_start(category_box, False, False, 0)

        # Add container to scrolled window
        scrolled_window.add(container)

        # Pack the scrolled window directly into main box
        self.main_box.pack_start(scrolled_window, False, False, 0)

    def on_category_clicked(self, category, group):
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

        self.update_category_header(category)
        self.show_category_apps(category)

    def update_category_header(self, category):
        """Update the category header text based on the selected category."""
        if category in self.category_groups['collections']:
            display_title = self.category_groups['collections'][category]
        elif category in self.category_groups['categories']:
            display_title = self.category_groups['categories'][category]
        elif category in self.subcategories:
            display_title = self.subcategories[category]
        else:
            display_title = category.capitalize()

        self.category_header.set_label(display_title)

    def create_applications_panel(self, title):
        # Create right panel
        self.right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Add category header
        self.category_header = Gtk.Label(label="")
        self.category_header.get_style_context().add_class("panel-header")
        self.category_header.set_hexpand(True)
        self.category_header.set_halign(Gtk.Align.START)
        self.right_panel.pack_start(self.category_header, False, False, 0)  # Pack header first

        # Create scrollable area
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_hexpand(True)
        scrolled_window.set_vexpand(True)

        # Create container for applications
        self.right_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.right_container.set_spacing(6)
        self.right_container.set_border_width(6)
        scrolled_window.add(self.right_container)

        self.right_panel.pack_start(scrolled_window, True, True, 0)  # Pack scrolled window second

        self.main_box.pack_end(self.right_panel, True, True, 0)
        return self.right_container

    def check_internet(self):
        """Check if internet connection is available."""
        try:
            requests.head('https://flathub.org', timeout=3)
            return True
        except requests.ConnectionError:
            return False

    def fetch_flathub_category_apps(self, category):
        """Fetch applications from Flathub API for the specified category."""
        try:
            # URL encode the category to handle special characters
            encoded_category = quote_plus(category)

            # Determine the base URL based on category type
            if category in self.category_groups['collections']:
                url = f"https://flathub.org/api/v2/collection/{encoded_category}"
            else:
                url = f"https://flathub.org/api/v2/collection/category/{encoded_category}"

            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()

                # If this is a collections category, save it to our collections database
                if category in self.category_groups['collections']:
                    if not hasattr(self, 'collections_db'):
                        self.collections_db = []
                    self.collections_db.append({
                        'category': category,
                        'data': data
                    })

                return data
            else:
                print(f"Failed to fetch apps: Status code {response.status_code}")
                return None
        except requests.RequestException as e:
            print(f"Error fetching apps: {str(e)}")
            return None

    def save_collections_data(self, filename='collections_data.json'):
        """Save all collected collections data to a JSON file."""
        if not hasattr(self, 'collections_db') or not self.collections_db:
            print("No collections data available to save")
            return

        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.collections_db, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Error saving collections data: {str(e)}")


    def show_category_apps(self, category):
        # Clear existing content
        for child in self.right_container.get_children():
            child.destroy()

        # Load collections data
        try:
            with open("collections_data.json", 'r', encoding='utf-8') as f:
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
                    apps = [
                        app for app in self.collection_results
                        if app.get_details()['id'] in app_ids_in_category
                    ]
                else:
                    # Fallback to previous behavior if category isn't in collections
                    apps = [
                        app for app in self.collection_results
                        if category in app.get_details()['categories']
                    ]

        except (IOError, json.JSONDecodeError) as e:
            print(f"Error reading collections data: {str(e)}")
            apps = [
                app for app in self.collection_results
                if category in app.get_details()['categories']
            ]
        # Display each application
        for app in apps:
            details = app.get_details()

            # Create application container
            app_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            app_container.set_spacing(12)
            app_container.set_margin_top(6)
            app_container.set_margin_bottom(6)

            # Add icon placeholder
            icon_box = Gtk.Box()
            icon_box.set_size_request(148, -1)

            # Create and add the icon
            icon = Gtk.Image.new_from_file(f"{details['icon_path_64']}/{details['icon_filename']}")
            icon.set_size_request(48, 48)  # Set a reasonable size for the icon
            icon_box.pack_start(icon, True, True, 0)  # Add icon to the box

            # Create right side layout for text
            right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            right_box.set_spacing(4)
            right_box.set_hexpand(True)

            # Add title
            title_label = Gtk.Label(label=details['name'])
            title_label.get_style_context().add_class("title-1")
            title_label.set_halign(Gtk.Align.START)
            title_label.set_hexpand(True)

            # Add summary
            desc_label = Gtk.Label(label=details['summary'])
            desc_label.set_halign(Gtk.Align.START)
            desc_label.set_hexpand(True)
            desc_label.set_line_wrap(True)
            desc_label.set_line_wrap_mode(Gtk.WrapMode.WORD)
            desc_label.get_style_context().add_class("dim-label")

            # Add separator
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)

            # Add to container
            right_box.pack_start(title_label, False, False, 0)
            right_box.pack_start(desc_label, False, False, 0)
            app_container.pack_start(icon_box, False, False, 0)
            app_container.pack_start(right_box, True, True, 0)
            self.right_container.pack_start(app_container, False, False, 0)
            self.right_container.pack_start(separator, False, False, 0)

        self.right_container.show_all()  # Show all widgets after adding them

    def select_default_category(self):
        # Select Trending by default
        if 'collections' in self.category_widgets and self.category_widgets['collections']:
            self.on_category_clicked('trending', 'collections')

def main():
    app = MainWindow()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
