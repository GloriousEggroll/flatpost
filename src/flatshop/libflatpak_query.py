#!/usr/bin/env python3

### Documentation largely taken from:
###
### 1. https://lazka.github.io/pgi-docs/Flatpak-1.0
### 2. https://flathub.org/api/v2/docs#/
###
### Classes AppStreamPackage and AppStreamSearcher extended from original by Tim Tim Lauridsen at:
###
### https://github.com/timlau/yumex-ng/blob/main/yumex/backend/flatpak/search.py

# Original GPL v3 Code Copyright:
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright (C) 2024 Tim Lauridsen
#
# Modifications copyright notice
# Copyright (C) 2025 Thomas Crider
#
# Original code has been completely removed except
# AppStreamPackage and AppStreamSearcher classes
# which have been modified and extended.


import gi
gi.require_version("AppStream", "1.0")
gi.require_version("Flatpak", "1.0")

from gi.repository import Flatpak, GLib, Gio, AppStream
from pathlib import Path
import logging
from enum import IntEnum
import argparse
import requests
from urllib.parse import quote_plus, urlparse
import tempfile
import os
import sys
import json
import time


# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Match(IntEnum):
    NAME = 1
    ID = 2
    SUMMARY = 3
    NONE = 4

class AppStreamComponentKind(IntEnum):
    """AppStream Component Kind enumeration."""

    UNKNOWN = 0
    """Type invalid or not known."""

    GENERIC = 1
    """A generic (= without specialized type) component."""

    DESKTOP_APP = 2
    """An application with a .desktop-file."""

    CONSOLE_APP = 3
    """A console application."""

    WEB_APP = 4
    """A web application."""

    SERVICE = 5
    """A system service launched by the init system."""

    ADDON = 6
    """An extension of existing software, which does not run standalone."""

    RUNTIME = 7
    """An application runtime platform."""

    FONT = 8
    """A font."""

    CODEC = 9
    """A multimedia codec."""

    INPUT_METHOD = 10
    """An input-method provider."""

    OPERATING_SYSTEM = 11
    """A computer operating system."""

    FIRMWARE = 12
    """Firmware."""

    DRIVER = 13
    """A driver."""

    LOCALIZATION = 14
    """Software localization (usually l10n resources)."""

    REPOSITORY = 15
    """A remote software or data source."""

    ICON_THEME = 16
    """An icon theme following the XDG specification."""

class AppStreamPackage:
    def __init__(self, comp: AppStream.Component, remote: Flatpak.Remote) -> None:
        self.component: AppStream.Component = comp
        self.remote: Flatpak.Remote = remote
        self.repo_name: str = remote.get_name()
        bundle: AppStream.Bundle = comp.get_bundle(AppStream.BundleKind.FLATPAK)
        self.flatpak_bundle: str = bundle.get_id()
        self.match = Match.NONE

        # Get icon and description
        self.icon_url = self._get_icon_url()
        self.icon_path_128 = self._get_icon_cache_path("128x128")
        self.icon_path_64 = self._get_icon_cache_path("64x64")
        self.icon_filename = self._get_icon_filename()
        self.description = self.component.get_description()

        # Get URLs from the component
        self.urls = self._get_urls()

        self.developer = self.component.get_developer().get_name()
        self.categories = self._get_categories()

    @property
    def id(self) -> str:
        return self.component.get_id()

    @property
    def name(self) -> str:
        return self.component.get_name()

    @property
    def summary(self) -> str:
        return self.component.get_summary()

    @property
    def version(self) -> str|None:
        releases = self.component.get_releases_plain()
        if releases:
            release = releases.index_safe(0)
            if release:
                version = release.get_version()
                return version
        return None

    @property
    def kind(self):
        kind = self.component.get_kind()
        kind_str = str(kind)

        for member in AppStreamComponentKind:
            if member.name in kind_str:
                return member.name

    def _get_icon_url(self) -> str:
        """Get the remote icon URL from the component"""
        icons = self.component.get_icons()

        # Find the first REMOTE icon
        remote_icon = next((icon for icon in icons if icon.get_kind() == AppStream.IconKind.REMOTE), None)
        return remote_icon.get_url() if remote_icon else ""

    def _get_icon_filename(self) -> str:
        """Get the cached icon filename from the component"""
        icons = self.component.get_icons()

        # Find the first CACHED icon
        cached_icon = next((icon for icon in icons if icon.get_kind() == AppStream.IconKind.CACHED), None)
        return cached_icon.get_filename() if cached_icon else ""

    def _get_icon_cache_path(self, size: str) -> str:

        # Appstream icon cache path for the flatpak repo queried
        icon_cache_path = Path(self.remote.get_appstream_dir().get_path() + "/icons/flatpak/" + size + "/")
        return str(icon_cache_path)

    def _get_urls(self) -> dict:
        """Get URLs from the component"""
        urls = {
            'donation': self._get_url('donation'),
            'homepage': self._get_url('homepage'),
            'bugtracker': self._get_url('bugtracker')
        }
        return urls

    def _get_url(self, url_kind: str) -> str:
        """Helper method to get a specific URL type"""
        # Convert string to AppStream.UrlKind enum
        url_kind_enum = getattr(AppStream.UrlKind, url_kind.upper())
        url = self.component.get_url(url_kind_enum)
        if url:
            return url
        return ""

    def _get_categories(self) -> list:
        categories_fetch = self.component.get_categories()
        categories = []
        for category in categories_fetch:
            categories.append(category.lower())
        return categories

    def search(self, keyword: str) -> Match:
        """Search for keyword in package details"""
        if keyword in self.name.lower():
            return Match.NAME
        elif keyword in self.id.lower():
            return Match.ID
        elif keyword in self.summary.lower():
            return Match.SUMMARY
        else:
            return Match.NONE

    def __str__(self) -> str:
        return f"{self.name} - {self.summary} ({self.flatpak_bundle})"

    def get_details(self) -> dict:
        """Get all package details including icon and description"""
        return {
            "name": self.name,
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "description": self.description,
            "version": self.version,
            "icon_url": self.icon_url,
            "icon_path_128": self.icon_path_128,
            "icon_path_64": self.icon_path_64,
            "icon_filename": self.icon_filename,
            "urls": self.urls,
            "developer": self.developer,
            #"architectures": self.architectures,
            "categories": self.categories,
            "bundle_id": self.flatpak_bundle,
            "match_type": self.match.name,
            "repo": self.repo_name
        }

class AppstreamSearcher:
    """Flatpak AppStream Package seacher"""

    def __init__(self, refresh=False) -> None:
        self.remotes: dict[str, list[AppStreamPackage]] = {}
        self.refresh_progress = 0
        self.refresh = refresh

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

    def add_installation(self, inst: Flatpak.Installation):
        """Add enabled flatpak repositories from Flatpak.Installation"""
        remotes = inst.list_remotes()
        for remote in remotes:
            if not remote.get_disabled():
                self.add_remote(remote, inst)

    def add_remote(self, remote: Flatpak.Remote, inst: Flatpak.Installation):
        """Add packages for a given Flatpak.Remote"""
        remote_name = remote.get_name()
        if remote_name not in self.remotes:
            self.remotes[remote_name] = self._load_appstream_metadata(remote, inst)
    def _load_appstream_metadata(self, remote: Flatpak.Remote, inst: Flatpak.Installation) -> list[AppStreamPackage]:
        """load AppStrean metadata and create AppStreamPackage objects"""
        packages = []
        metadata = AppStream.Metadata.new()
        metadata.set_format_style(AppStream.FormatStyle.CATALOG)
        if self.refresh:
            if remote.get_name() == "flathub" or remote.get_name() == "flathub-beta":
                remote.set_gpg_verify(True)
                inst.modify_remote(remote, None)
            inst.update_appstream_full_sync(remote.get_name(), None, None, True)
        appstream_file = Path(remote.get_appstream_dir().get_path() + "/appstream.xml.gz")
        if not appstream_file.exists() and check_internet():
            try:
                if remote.get_name() == "flathub" or remote.get_name() == "flathub-beta":
                    remote.set_gpg_verify(True)
                    inst.modify_remote(remote, None)
                inst.update_appstream_full_sync(remote.get_name(), None, None, True)
            except GLib.Error as e:
                logger.error(f"Failed to update AppStream metadata: {str(e)}")
        if appstream_file.exists():
            metadata.parse_file(Gio.File.new_for_path(appstream_file.as_posix()), AppStream.FormatKind.XML)
            components: AppStream.ComponentBox = metadata.get_components()
            i = 0
            for i in range(components.get_size()):
                component = components.index_safe(i)
                #if component.get_kind() == AppStream.ComponentKind.DESKTOP_APP:
                packages.append(AppStreamPackage(component, remote))
            return packages
        else:
            logger.debug(f"AppStream file not found: {appstream_file}")
            return []

    def search_flatpak_repo(self, keyword: str, repo_name: str) -> list[AppStreamPackage]:
        search_results = []
        packages = self.remotes[repo_name]
        found = None
        for package in packages:
            # Try matching exact ID first
            if keyword is package.id:
                found = package
                break
            # Next try matching exact name
            elif keyword.lower() is package.name.lower():
                found = package
                break
            # Try matching case insensitive ID next
            elif keyword.lower() is package.id.lower():
                found = package
                break
            # General keyword search
            elif keyword.lower() in str(package).lower():
                found = package
                break
        if found:
            search_results.append(found)
        return search_results


    def search_flatpak(self, keyword: str, repo_name=None) -> list[AppStreamPackage]:
        """Search packages matching a keyword"""
        search_results = []
        keyword = keyword

        if not repo_name:
            for remote_name in self.remotes.keys():
                search_results.extend(self.search_flatpak_repo(keyword, remote_name))
        else:
            if repo_name in self.remotes.keys():
                search_results.extend(self.search_flatpak_repo(keyword, repo_name))
        return search_results


    def get_all_apps(self, repo_name=None) -> list[AppStreamPackage]:
        """Get all available apps from specified or all repositories"""
        all_packages = []
        if repo_name:
            if repo_name in self.remotes:
                all_packages = self.remotes[repo_name]
        else:
            for remote_name in self.remotes.keys():
                all_packages.extend(self.remotes[remote_name])
        return all_packages

    def get_categories_summary(self, repo_name=None) -> dict:
        """Get a summary of all apps grouped by category"""
        apps = self.get_all_apps(repo_name)
        categories = {}

        for app in apps:
            for category in app.categories:
                # Normalize category names to match our groups
                normalized_category = category.lower()

                # Map category to its group title
                for group_name, categories_dict in self.category_groups.items():
                    if normalized_category in categories_dict:
                        display_category = categories_dict[normalized_category]
                        break
                else:
                    display_category = normalized_category.title()

                if display_category not in categories:
                    categories[display_category] = []
                categories[display_category].append(app)

        return categories

    def get_subcategories_summary(self, repo_name=None) -> list[tuple[str, str, list[AppStreamPackage]]]:
        """Get a summary of all apps grouped by category and subcategory."""
        apps = self.get_all_apps(repo_name)
        subcategories = []

        # Process each category and its subcategories
        for category, subcategories_dict in self.subcategory_groups.items():
            for subcategory, title in subcategories_dict.items():
                apps_in_subcategory = []
                for app in apps:
                    if category in app.categories and subcategory in app.categories:
                        apps_in_subcategory.append(app)
                if apps_in_subcategory:
                    subcategories.append((category, subcategory, apps_in_subcategory))

        return subcategories

    def get_installed_apps(self, system=False) -> list[tuple[str, str, str]]:
        """Get a list of all installed Flatpak applications with their repository source"""
        installed_refs = []

        installation = get_installation(system)

        def process_installed_refs(inst: Flatpak.Installation, system=False):
            for ref in inst.list_installed_refs():
                app_id = ref.get_name()
                remote_name = ref.get_origin()
                if system is False:
                    installed_refs.append((app_id, remote_name, "user"))
                else:
                    installed_refs.append((app_id, remote_name, "system"))

        # Process both system-wide and user installations
        process_installed_refs(installation, system)

        # Remove duplicates while maintaining order
        seen = set()
        unique_installed = [(ref, repo, repo_type) for ref, repo, repo_type in installed_refs
                        if not (ref in seen or seen.add(ref))]

        return unique_installed

    def check_updates(self, system=False) -> list[tuple[str, str, str]]:
        """Check for available updates for installed Flatpak applications"""
        updates = []

        installation = get_installation(system)

        def check_updates_for_install(inst: Flatpak.Installation, system=False):
            for ref in inst.list_installed_refs_for_update(None):
                app_id = ref.get_name()
                # Get remote name from the installation
                remote_name = ref.get_origin()
                if system is False:
                    updates.append((app_id, remote_name, "user"))
                else:
                    updates.append((app_id, remote_name, "system"))

        # Process both system-wide and user installations
        check_updates_for_install(installation, system)

        return updates

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
            return

        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.collections_db, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Error saving collections data: {str(e)}")

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

    def fetch_flathub_subcategory_apps(self, category: str, subcategory: str) -> dict:
        """Fetch applications from Flathub API for the specified category and subcategory."""
        try:
            # URL encode the category and subcategory to handle special characters
            encoded_category = quote_plus(category)
            encoded_subcategory = quote_plus(subcategory)

            # Construct the API URL for subcategories
            url = f"https://flathub.org/api/v2/collection/category/{encoded_category}/subcategories?subcategory={encoded_subcategory}"

            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                return data
            else:
                print(f"Failed to fetch apps: Status code {response.status_code}")
                return None
        except requests.RequestException as e:
            print(f"Error fetching apps: {str(e)}")
            return None

    def save_subcategories_data(self, filename='subcategories_data.json'):
        """Save all collected subcategories data to a JSON file."""
        if not hasattr(self, 'subcategories_results') or not self.subcategories_results:
            return

        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.subcategories_results, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Error saving subcategories data: {str(e)}")

    def update_subcategories_data(self):
        """Fetch and store data for all subcategories."""
        if not hasattr(self, 'subcategories_results'):
            self.subcategories_results = []

        # Process each category and its subcategories
        for category, subcategories in self.subcategory_groups.items():
            for subcategory, title in subcategories.items():
                api_data = self.fetch_flathub_subcategory_apps(category, subcategory)
                if api_data:
                    self.subcategories_results.append({
                        'category': category,
                        'subcategory': subcategory,
                        'data': api_data
                    })

        # Save the collected data
        self.save_subcategories_data()

    def refresh_local(self, system=False):

        # make sure to reset these to empty before refreshing.
        self.installed_results = []  # Initialize empty list
        self.updates_results = []  # Initialize empty list

        total_categories = sum(len(categories) for categories in self.category_groups.values())
        current_category = 0
        # Search for each app in local repositories
        searcher = get_reposearcher(system)
        search_result = []
        for group_name, categories in self.category_groups.items():
            # Process categories one at a time to keep GUI responsive
            for category, title in categories.items():
                if "installed" in category:
                    installed_apps = searcher.get_installed_apps(system)
                    for app_id, repo_name, repo_type in installed_apps:
                        if repo_name:
                            search_result = searcher.search_flatpak(app_id, repo_name)
                            self.installed_results.extend(search_result)
                elif "updates" in category:
                    updates = searcher.check_updates(system)
                    for repo_name, app_id, repo_type in updates:
                        if repo_name:
                            search_result = searcher.search_flatpak(app_id, repo_name)
                            self.updates_results.extend(search_result)
                # Update progress bar
                self.refresh_progress = (current_category / total_categories) * 100
        # make sure to reset these to empty before refreshing.
        return self.installed_results, self.updates_results


    def retrieve_metadata(self, system=False):
        """Retrieve and refresh metadata for Flatpak repositories."""
        self._initialize_metadata()

        if not check_internet():
            return self._handle_offline_mode()

        searcher = get_reposearcher(system, True)
        self.all_apps = searcher.get_all_apps()

        return self._process_categories(searcher, system)

    def _initialize_metadata(self):
        """Initialize empty lists for metadata storage."""
        self.category_results = []
        self.collection_results = []
        self.subcategories_results = []
        self.installed_results = []
        self.updates_results = []
        self.all_apps = []

    def _handle_offline_mode(self):
        """Handle metadata retrieval when offline."""
        json_path = "collections_data.json"
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                collections_data = json.load(f)
                return self._process_offline_data(collections_data)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Error loading offline data: {str(e)}")
            return None, [], [], [], []

        # Also load subcategories data
        subcategories_path = "subcategories_data.json"
        try:
            with open(subcategories_path, 'r', encoding='utf-8') as f:
                subcategories_data = json.load(f)
                self.subcategories_results = subcategories_data
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Error loading subcategories data: {str(e)}")
            self.subcategories_results = []

    def _process_offline_data(self, collections_data):
        """Process cached collections data when offline."""
        for collection in collections_data:
            category = collection['category']
            if category in self.category_groups['collections']:
                apps = [app['app_id'] for app in collection['data'].get('hits', [])]
                for app_id in apps:
                    search_result = self.search_flatpak(app_id, 'flathub')
                    self.collection_results.extend(search_result)
        return self._get_current_results()

    def _process_categories(self, searcher, system=False):
        """Process categories and retrieve metadata."""
        total_categories = sum(len(categories) for categories in self.category_groups.values())
        current_category = 0

        for group_name, categories in self.category_groups.items():
            for category, title in categories.items():
                if category not in self.category_groups['system']:
                    self._process_category(searcher, category, current_category, total_categories)
                else:
                    self._process_system_category(searcher, category, system)
                current_category += 1
        if self._should_refresh():
            self.update_subcategories_data()

        return self._get_current_results()

    def _process_category(self, searcher, category, current_category, total_categories):
        """Process a single category and retrieve its metadata."""
        json_path = "collections_data.json"
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                collections_data = json.load(f)
                self._update_from_collections(collections_data, category)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Error loading collections data: {str(e)}")

        if self._should_refresh():
            self._refresh_category_data(searcher, category)

        self.refresh_progress = (current_category / total_categories) * 100

    def _update_from_collections(self, collections_data, category):
        """Update results from cached collections data."""
        for collection in collections_data:
            if collection['category'] == category:
                apps = [app['app_id'] for app in collection['data'].get('hits', [])]
                for app_id in apps:
                    search_result = self.search_flatpak(app_id, 'flathub')
                    self.collection_results.extend(search_result)

    def _should_refresh(self):
        """Check if category data needs refresh."""
        json_path = "collections_data.json"
        try:
            mod_time = os.path.getmtime(json_path)
            return (time.time() - mod_time) > 168 * 3600
        except OSError:
            return True

    def _refresh_category_data(self, searcher, category):
        """Refresh category data from Flathub API."""
        try:
            api_data = self.fetch_flathub_category_apps(category)
            if api_data:
                apps = api_data['hits']
                for app in apps:
                    app_id = app['app_id']
                    search_result = searcher.search_flatpak(app_id, 'flathub')
                    if category in self.category_groups['collections']:
                        self.update_collection_results(search_result)
                    else:
                        self.category_results.extend(search_result)
        except requests.RequestException as e:
            logger.error(f"Error refreshing category {category}: {str(e)}")

    def _process_system_category(self, searcher, category, system=False):
        """Process system-related categories."""
        if "installed" in category:
            installed_apps = searcher.get_installed_apps(system)
            for app_id, repo_name, repo_type in installed_apps:
                if repo_name:
                    search_result = searcher.search_flatpak(app_id, repo_name)
                    self.installed_results.extend(search_result)
        elif "updates" in category:
            updates = searcher.check_updates(system)
            for app_id, repo_name, repo_type in updates:
                if repo_name:
                    search_result = searcher.search_flatpak(app_id, repo_name)
                    self.updates_results.extend(search_result)

    def _get_current_results(self):
        """Return current metadata results."""
        return (
            self.category_results,
            self.collection_results,
            self.subcategories_results,
            self.installed_results,
            self.updates_results,
            self.all_apps
        )

def install_flatpak(app: AppStreamPackage, repo_name=None, system=False) -> tuple[bool, str]:
    """
    Install a Flatpak package.

    Args:
        app (AppStreamPackage): The package to install.
        repo_name (str): Optional repository name to use for installation
        system (Optional[bool]): Whether to operate on user or system installation

    Returns:
        tuple[bool, str]: (success, message)
    """

    if not repo_name:
        repo_name = "flathub"

    installation = get_installation(system)

    transaction = Flatpak.Transaction.new_for_installation(installation)

    # Add the install operation
    transaction.add_install(repo_name, app.flatpak_bundle, None)
    # Run the transaction
    try:
        transaction.run()
    except GLib.Error as e:
        return False, f"Installation failed: {e}"
    return True, f"Successfully installed {app.id}"

def install_flatpakref(ref_file, system=False):
    """Add a new repository using a .flatpakrepo file"""
    # Get existing repositories
    installation = get_installation(system)

    if not ref_file.endswith('.flatpakref'):
        return False, "Flatpak ref file path or URL must end with .flatpakref extension."

    if not os.path.exists(ref_file):
        return False, f"Flatpak ref file '{ref_file}' does not exist."

    # Read the flatpakref file
    try:
        with open(ref_file, 'rb') as f:
            repo_data = f.read()
    except IOError as e:
        return False, f"Failed to read flatpakref file: {str(e)}"

    # Convert the data to GLib.Bytes
    repo_bytes = GLib.Bytes.new(repo_data)

    installation = get_installation(system)

    transaction = Flatpak.Transaction.new_for_installation(installation)

    # Add the install operation
    transaction.add_install_flatpakref(repo_bytes)
    # Run the transaction
    try:
        transaction.run()
    except GLib.Error as e:
        return False, f"Installation failed: {e}"
    return True, f"Successfully installed {ref_file}"


def remove_flatpak(app: AppStreamPackage, repo_name=None, system=False) -> tuple[bool, str]:
    """
    Remove a Flatpak package using transactions.

    Args:
        app (AppStreamPackage): The package to install.
        system (Optional[bool]): Whether to operate on user or system installation

    Returns:
        Tuple[bool, str]: (success, message)
    """
    if not repo_name:
        repo_name = "flathub"

    # Get the appropriate installation based on user parameter
    installation = get_installation(system)

    # Create a new transaction for removal
    transaction = Flatpak.Transaction.new_for_installation(installation)
    transaction.add_uninstall(app.flatpak_bundle)
    # Run the transaction
    try:
        transaction.run()
    except GLib.Error as e:
        return False, f"Failed to remove {app.id}: {e}"
    return True, f"Successfully removed {app.id}"

def update_flatpak(app: AppStreamPackage, repo_name=None, system=False) -> tuple[bool, str]:
    """
    Remove a Flatpak package using transactions.

    Args:
        app (AppStreamPackage): The package to install.
        system (Optional[bool]): Whether to operate on user or system installation

    Returns:
        Tuple[bool, str]: (success, message)
    """
    if not repo_name:
        repo_name = "flathub"

    # Get the appropriate installation based on user parameter
    installation = get_installation(system)

    # Create a new transaction for removal
    transaction = Flatpak.Transaction.new_for_installation(installation)
    transaction.add_update(app.flatpak_bundle)
    # Run the transaction
    try:
        transaction.run()
    except GLib.Error as e:
        return False, f"Failed to update {app.id}: {e}"
    return True, f"Successfully updated {app.id}"

def get_installation(system=False):
    if system is False:
        installation = Flatpak.Installation.new_user()
    else:
        installation = Flatpak.Installation.new_system()
    return installation

def get_reposearcher(system=False, refresh=False):
    installation = get_installation(system)
    searcher = AppstreamSearcher(refresh)
    searcher.add_installation(installation)
    return searcher

def check_internet():
    """Check if internet connection is available."""
    try:
        requests.head('https://flathub.org', timeout=3)
        return True
    except requests.ConnectionError:
        return False

def repotoggle(repo, toggle=True, system=False):
    """
    Enable or disable a Flatpak repository

    Args:
        repo (str): Name of the repository to toggle
        enable (toggle): True to enable, False to disable

    Returns:
        tuple: (success, error_message)
    """

    if not repo:
        return False, "Repository name cannot be empty"

    installation = get_installation(system)

    try:
        remote = installation.get_remote_by_name(repo)
        if not remote:
            return False, f"Repository '{repo}' not found."

        remote.set_disabled(not toggle)

        # Modify the remote's disabled status
        success = installation.modify_remote(
            remote,
            None
        )
        if success:
            if toggle:
                message = f"Successfully enabled {repo}."
            else:
                message = f"Successfully disabled {repo}."
            return True, message

    except GLib.GError as e:
        return False, f"Failed to toggle repository: {str(e)}"

    return False, "Operation failed"

def repolist(system=False):
    installation = get_installation(system)
    repos = installation.list_remotes()
    return repos

def repodelete(repo, system=False):
    installation = get_installation(system)
    installation.remove_remote(repo)

def repoadd(repofile, system=False):
    """Add a new repository using a .flatpakrepo file"""
    # Get existing repositories
    installation = get_installation(system)
    existing_repos = installation.list_remotes()

    if not repofile.endswith('.flatpakrepo'):
        return False, "Repository file path or URL must end with .flatpakrepo extension."

    if repofile_is_url(repofile):
        try:
            local_path = download_repo(repofile)
            repofile = local_path
            print(f"\nRepository added successfully: {repofile}")
        except:
            return False, f"Repository file '{repofile}' could not be downloaded."

    if not os.path.exists(repofile):
        return False, f"Repository file '{repofile}' does not exist."

    # Get repository title from file name
    title = os.path.basename(repofile).replace('.flatpakrepo', '')

    # Check for duplicate title (case insensitive)
    existing_titles = [repo.get_name().casefold() for repo in existing_repos]

    if title.casefold() in existing_titles:
        return False, "A repository with this title already exists."

    # Read the repository file
    try:
        with open(repofile, 'rb') as f:
            repo_data = f.read()
    except IOError as e:
        return False, f"Failed to read repository file: {str(e)}"

    # Convert the data to GLib.Bytes
    repo_bytes = GLib.Bytes.new(repo_data)

    # Create a new remote from the repository file
    try:
        remote = Flatpak.Remote.new_from_file(title, repo_bytes)

        # Get URLs and normalize them by removing trailing slashes
        new_url = remote.get_url().rstrip('/')
        existing_urls = [repo.get_url().rstrip('/') for repo in existing_repos]

        # Check if URL already exists
        if new_url in existing_urls:
            return False, f"A repository with URL '{new_url}' already exists."
        user = "user"
        if system:
            user = "system"
        remote.set_gpg_verify(True)
        installation.add_remote(remote, True, None)
    except GLib.GError as e:
        return False, f"Failed to add repository: {str(e)}"
    return True, f"{remote.get_name()} repository successfully added for {user} installation."

def repofile_is_url(string):
    """Check if a string is a valid URL"""
    try:
        result = urlparse(string)
        return all([result.scheme, result.netloc])
    except:
        return False

def download_repo(url):
    """Download a repository file from URL to /tmp/"""
    try:
        # Create a deterministic filename based on the URL
        url_path = urlparse(url).path
        filename = os.path.basename(url_path) or 'repo'
        tmp_path = Path(tempfile.gettempdir()) / f"{filename}"

        # Download the file
        with requests.get(url, stream=True) as response:
            response.raise_for_status()

            # Write the file in chunks, overwriting if it exists
            with open(tmp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

        return str(tmp_path)
    except requests.RequestException as e:
        raise argparse.ArgumentTypeError(f"Failed to download repository file: {str(e)}")
    except IOError as e:
        raise argparse.ArgumentTypeError(f"Failed to save repository file: {str(e)}")


def main():
    parser = argparse.ArgumentParser(description='Search Flatpak packages')
    parser.add_argument('--id', help='Application ID to search for')
    parser.add_argument('--repo', help='Filter results to specific repository')
    parser.add_argument('--list-all', action='store_true', help='List all available apps')
    parser.add_argument('--categories', action='store_true', help='Show apps grouped by category')
    parser.add_argument('--subcategories', action='store_true',
                       help='Show apps grouped by subcategory')
    parser.add_argument('--list-installed', action='store_true',
                       help='List all installed Flatpak applications')
    parser.add_argument('--check-updates', action='store_true',
                       help='Check for available updates')
    parser.add_argument('--list-repos', action='store_true',
                       help='List all configured Flatpak repositories')
    parser.add_argument('--add-repo', type=str, metavar='REPO_FILE',
                       help='Add a new repository from a .flatpakrepo file')
    parser.add_argument('--remove-repo', type=str, metavar='REPO_NAME',
                       help='Remove a Flatpak repository')
    parser.add_argument('--toggle-repo', type=str,
                       metavar=('ENABLE/DISABLE'),
                       help='Enable or disable a repository')
    parser.add_argument('--install', type=str, metavar='APP_ID',
                       help='Install a Flatpak package')
    parser.add_argument('--remove', type=str, metavar='APP_ID',
                       help='Remove a Flatpak package')
    parser.add_argument('--update', type=str, metavar='APP_ID',
                       help='Update a Flatpak package')
    parser.add_argument('--system', action='store_true', help='Install as system instead of user')
    parser.add_argument('--refresh', action='store_true', help='Install as system instead of user')
    parser.add_argument('--refresh-local', action='store_true', help='Install as system instead of user')

    args = parser.parse_args()

    # Handle repository operations
    if args.toggle_repo:
        handle_repo_toggle(args)
        return

    if args.list_repos:
        handle_list_repos(args)
        return

    if args.add_repo:
        handle_add_repo(args)
        return

    if args.remove_repo:
        handle_remove_repo(args)
        return

    # Handle package operations
    searcher = get_reposearcher(args.system)

    if args.install:
        handle_install(args, searcher)
        return

    if args.remove:
        handle_remove(args, searcher)
        return

    if args.update:
        handle_update(args, searcher)
        return

    # Handle information operations
    if args.list_installed:
        handle_list_installed(args, searcher)
        return

    if args.check_updates:
        handle_check_updates(args, searcher)
        return

    if args.list_all:
        handle_list_all(args, searcher)
        return

    if args.categories:
        handle_categories(args, searcher)
        return

    if args.subcategories:
        handle_subcategories(args, searcher)
        return

    if args.id:
        handle_search(args, searcher)
        return

    print("Missing options. Use -h for help.")

def handle_repo_toggle(args):
    repo_name = args.repo
    if not repo_name:
        print("Error: must specify a repo.")
        sys.exit(1)

    get_status = args.toggle_repo.lower() in ['true', 'enable']
    try:
        success, message = repotoggle(repo_name, get_status, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_list_repos(args):
    repos = repolist(args.system)
    print("\nConfigured Repositories:")
    for repo in repos:
        print(f"- {repo.get_name()} ({repo.get_url()})")

def handle_add_repo(args):
    try:
        success, message = repoadd(args.add_repo, args.system)
        print(f"{message}")
    except GLib.Error as e:
        print(f"{str(e)}")

def handle_remove_repo(args):
    repodelete(args.remove_repo, args.system)
    print(f"\nRepository removed successfully: {args.remove_repo}")

def handle_install(args, searcher):
    if args.install.endswith('.flatpakref'):
        try:
            success, message = install_flatpakref(args.install, args.system)
            result_message = f"{message}"
        except GLib.Error as e:
            result_message = f"Installation of {args.install} failed: {str(e)}"
        print(result_message)
    else:
        packagelist = searcher.search_flatpak(args.install, args.repo)
        result_message = ""
        for package in packagelist:
            try:
                success, message = install_flatpak(package, args.repo, args.system)
                result_message = f"{message}"
                break
            except GLib.Error as e:
                result_message = f"Installation of {args.install} failed: {str(e)}"
                pass
        print(result_message)

def handle_remove(args, searcher):
    packagelist = searcher.search_flatpak(args.remove, args.repo)
    result_message = ""
    for package in packagelist:
        try:
            success, message = remove_flatpak(package, args.repo, args.system)
            result_message = f"{message}"
            break
        except GLib.Error as e:
            result_message = f"Removal of {args.remove} failed: {str(e)}"
            pass
    print(result_message)

def handle_update(args, searcher):
    packagelist = searcher.search_flatpak(args.update, args.repo)
    result_message = ""
    for package in packagelist:
        try:
            success, message = update_flatpak(package, args.repo, args.system)
            result_message = f"{message}"
            break
        except GLib.Error as e:
            result_message = f"Update of {args.update} failed: {str(e)}"
            pass
    print(result_message)

def handle_list_installed(args, searcher):
    installed_apps = searcher.get_installed_apps(args.system)
    print(f"\nInstalled Flatpak Applications ({len(installed_apps)}):")
    for app_id, repo_name, repo_type in installed_apps:
        print(f"{app_id} (Repository: {repo_name}, Installation: {repo_type})")

def handle_check_updates(args, searcher):
    updates = searcher.check_updates(args.system)
    print(f"\nAvailable Updates ({len(updates)}):")
    for repo_name, app_id, repo_type in updates:
        print(f"{app_id} (Repository: {repo_name}, Installation: {repo_type})")

def handle_list_all(args, searcher):
    apps = searcher.get_all_apps(args.repo)
    for app in apps:
        details = app.get_details()
        print(f"Name: {details['name']}")
        print(f"Categories: {', '.join(details['categories'])}")
        print("-" * 50)

def handle_categories(args, searcher):
    categories = searcher.get_categories_summary(args.repo)
    for category, apps in categories.items():
        print(f"\n{category.upper()}:")
        for app in apps:
            print(f"  - {app.name} ({app.id})")

def handle_subcategories(args, searcher):
    """Handle showing apps grouped by subcategory."""
    subcategories = searcher.get_subcategories_summary(args.repo)
    for category, subcategory, apps in subcategories:
        print(f"\n{category.upper()} > {subcategory.upper()}:")
        for app in apps:
            print(f"  - {app.name} ({app.id})")

def handle_search(args, searcher):
    if args.repo:
        search_results = searcher.search_flatpak(args.id, args.repo)
    else:
        search_results = searcher.search_flatpak(args.id)

    if search_results:
        for package in search_results:
            details = package.get_details()
            print(f"Name: {details['name']}")
            print(f"ID: {details['id']}")
            print(f"Kind: {details['kind']}")
            print(f"Summary: {details['summary']}")
            print(f"Description: {details['description']}")
            print(f"Version: {details['version']}")
            print(f"Icon URL: {details['icon_url']}")
            print(f"Icon PATH 128x128: {details['icon_path_128']}")
            print(f"Icon PATH 64x64: {details['icon_path_64']}")
            print(f"Icon FILE: {details['icon_filename']}")
            print(f"Developer: {details['developer']}")
            print(f"Categories: {details['categories']}")
            urls = details['urls']
            print(f"Donation URL: {urls['donation']}")
            print(f"Homepage URL: {urls['homepage']}")
            print(f"Bug Tracker URL: {urls['bugtracker']}")
            print(f"Bundle ID: {details['bundle_id']}")
            print(f"Match Type: {details['match_type']}")
            print(f"Repo: {details['repo']}")
            print("-" * 50)

if __name__ == "__main__":
    main()
