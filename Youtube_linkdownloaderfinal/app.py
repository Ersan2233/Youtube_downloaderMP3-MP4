import sys
import os
import threading
import queue
import configparser
from pathlib import Path
import urllib.request
import io
import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QGroupBox,
    QProgressBar, QTextEdit, QFileDialog, QMessageBox, QSizePolicy, QSpacerItem
)
from PyQt5.QtGui import QPixmap, QImage, QPalette, QColor, QFont, QIcon
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QMutex, QObject, QTimer, QUrl

import yt_dlp # Assuming yt_dlp is already installed

class YtdlpWorker(QObject):
    """
    Worker object for running yt-dlp operations in a separate thread.
    Uses PyQt signals for safe GUI updates.
    """
    log_message = pyqtSignal(str, str) # message, type (info, warning, error)
    progress_update = pyqtSignal(int)   # percent
    download_complete = pyqtSignal()
    download_error = pyqtSignal(str)    # error message
    info_fetched = pyqtSignal(dict)     # video info dictionary
    info_error = pyqtSignal(str)        # error message during info fetch
    thumbnail_loaded = pyqtSignal(QPixmap) # QPixmap for the thumbnail
    thumbnail_error = pyqtSignal()      # Signal if thumbnail loading fails
    operation_finished = pyqtSignal()   # General signal to re-enable UI

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_fetching_info = False # To distinguish between info and download ops

    def _progress_hook(self, d):
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total_bytes:
                percent = (d.get('downloaded_bytes', 0) / total_bytes) * 100
                self.progress_update.emit(int(percent))
        elif d['status'] == 'finished':
            self.progress_update.emit(100)
            self.log_message.emit("Download finished. Processing file(s)...", 'info')
        elif d['status'] == 'error':
            self.download_error.emit("yt-dlp encountered an error during download or post-processing.")

    def fetch_video_info(self, url):
        self._is_fetching_info = True
        try:
            ydl_opts = {
                'quiet': True,
                'simulate': True,
                'force_generic_extractor': True,
                'format': 'bestvideo*+bestaudio/best',
                'noplaylist': True,
                'skip_download': True,
                'progress_hooks': [lambda d: None], # No progress for info fetch
                'nocheckcertificate': True # Added to potentially help with some certificate issues
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                self.info_fetched.emit(info)
        except yt_dlp.utils.DownloadError as e:
            self.info_error.emit(f"Could not fetch info: {e}\nCheck URL or network connection.")
        except Exception as e:
            self.info_error.emit(f"An unexpected error occurred during info fetch: {e}")
        finally:
            self.operation_finished.emit()
            self._is_fetching_info = False

    def download_video(self, url, output_path, file_format, quality, is_playlist, use_custom_filename, filename_template, embed_metadata, video_info):
        self._is_fetching_info = False # Ensure this is false for download operations
        try:
            format_selector = ""
            if file_format == 'mp3':
                format_selector = 'bestaudio/best'
            elif quality == 'Best':
                format_selector = 'bestvideo*+bestaudio/best'
            else:
                height = quality.replace('p', '')
                # Ensure the height is an integer for comparison
                try:
                    height_int = int(height)
                    # Use a format that prioritizes video streams by height and then combines with best audio
                    # This attempts to get the highest resolution up to the selected height
                    format_selector = f'bestvideo[height<={height_int}]+bestaudio/best'
                except ValueError:
                    self.log_message.emit(f"Invalid quality selected: {quality}. Falling back to 'Best'.", 'warning')
                    format_selector = 'bestvideo*+bestaudio/best'


            outtmpl = filename_template if use_custom_filename and filename_template.strip() else '%(title)s.%(ext)s'
            ydl_outtmpl = os.path.join(output_path, outtmpl)

            ydl_opts = {
                'format': format_selector,
                'outtmpl': ydl_outtmpl,
                'noplaylist': not is_playlist,
                'progress_hooks': [self._progress_hook],
                'merge_output_format': file_format if file_format != 'mp3' else None,
                'nocheckcertificate': True # Added to potentially help with some certificate issues
            }

            postprocessors = []
            if file_format == 'mp3':
                postprocessors.append({'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'})
            if embed_metadata:
                postprocessors.append({'key': 'FFmpegMetadata'})
                if video_info and video_info.get('thumbnail'):
                    postprocessors.append({'key': 'EmbedThumbnail'})
                    self.log_message.emit("Attempting to embed thumbnail...", 'info')
                else:
                    self.log_message.emit("Warning: Cannot embed thumbnail as no video info or thumbnail URL was available.", 'warning')
            if postprocessors:
                ydl_opts['postprocessors'] = postprocessors

            self.log_message.emit(f"Starting download of {file_format.upper()}...", 'info')
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            self.download_complete.emit()

        except yt_dlp.utils.DownloadError as e:
            self.download_error.emit(f"Download error: {e}\nPossible issues: Invalid URL, geo-restriction, or missing FFmpeg/dependencies.")
        except Exception as e:
            self.download_error.emit(f"An unexpected error occurred: {e}")
        finally:
            self.operation_finished.emit()

    def download_thumbnail(self, url):
        try:
            with urllib.request.urlopen(url, timeout=10) as u:
                raw_data = u.read()
            image = QImage.fromData(raw_data)
            if image.isNull():
                raise ValueError("Failed to load image data.")

            max_width = 160
            max_height = 90
            scaled_image = image.scaled(max_width, max_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pixmap = QPixmap.fromImage(scaled_image)
            self.thumbnail_loaded.emit(pixmap)
            # FIX: Changed to .emit()
            self.log_message.emit("Thumbnail loaded.", 'info')
        except Exception as e:
            # FIX: Changed to .emit()
            self.log_message.emit(f"Failed to load thumbnail: {e}", 'error')
            self.thumbnail_error.emit()


class YouTubeDownloaderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        # Changed window title as requested
        self.setWindowTitle("YouTube Downloader")
        self.setGeometry(100, 100, 900, 800) # x, y, width, height
        self.setMinimumSize(850, 750)       # Set minimum size for the main window

        # Determine the script directory for relative paths
        self.script_dir = os.path.dirname(os.path.abspath(__file__))

        # Set application icon (cross-platform)
        icon_filename = "youtube_icon.png" # Assuming your icon is named this
        icon_path = os.path.join(self.script_dir, "icons", icon_filename) # Path inside the 'icons' subfolder

        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            print(f"Warning: Icon file not found at {icon_path}. Please ensure '{icon_filename}' is in the 'icons' subfolder next to 'app.py'.")

        self.config_path = Path.home() / ".youtube_downloader_config.ini"

        self.config = configparser.RawConfigParser()
        self._load_settings()

        self.current_video_info = None

        self._create_widgets()
        self._apply_styles()
        self._load_initial_ui_settings()

        # Setup Worker Thread
        self.worker_thread = QThread()
        self.ytdlp_worker = YtdlpWorker()
        self.ytdlp_worker.moveToThread(self.worker_thread)

        # Connect Worker Signals to GUI Slots
        self.ytdlp_worker.log_message.connect(self.log_message)
        self.ytdlp_worker.progress_update.connect(self._update_progress)
        self.ytdlp_worker.download_complete.connect(self._on_download_complete)
        self.ytdlp_worker.download_error.connect(self._on_download_error)
        self.ytdlp_worker.info_fetched.connect(self._display_video_info)
        self.ytdlp_worker.info_error.connect(self._on_info_error)
        self.ytdlp_worker.thumbnail_loaded.connect(self._display_thumbnail)
        self.ytdlp_worker.thumbnail_error.connect(self._on_thumbnail_error)
        self.ytdlp_worker.operation_finished.connect(lambda: self.set_ui_state('enabled'))

        # Connect GUI actions to Worker slots
        self.download_button.clicked.connect(self._start_download_worker)

        self.worker_thread.start() # Start the worker thread

    def closeEvent(self, event):
        """Handle window closing event."""
        self._save_settings()
        self.worker_thread.quit()
        self.worker_thread.wait() # Wait for the thread to finish
        event.accept()

    def _load_settings(self):
        self.config.read(self.config_path)
        if 'SETTINGS' not in self.config:
            self.config['SETTINGS'] = {
                'output_path': str(self.script_dir), # Default to script directory
                'selected_format': 'mp4',
                'selected_quality': 'Best',
                'playlist_var': 'False',
                'custom_filename_var': 'False',
                'filename_template': '%(title)s.%(ext)s',
                'embed_metadata_var': 'False'
            }

    def _save_settings(self):
        self.config['SETTINGS']['output_path'] = self.output_entry.text()
        self.config['SETTINGS']['selected_format'] = self.format_dropdown.currentText()
        self.config['SETTINGS']['selected_quality'] = self.quality_dropdown.currentText()
        self.config['SETTINGS']['playlist_var'] = str(self.playlist_check.isChecked())
        self.config['SETTINGS']['custom_filename_var'] = str(self.custom_filename_check.isChecked())
        self.config['SETTINGS']['filename_template'] = self.filename_template_entry.text()
        self.config['SETTINGS']['embed_metadata_var'] = str(self.embed_metadata_check.isChecked())
        try:
            with open(self.config_path, 'w') as configfile:
                self.config.write(configfile)
            self.log_message("Settings saved.", 'info')
        except Exception as e:
            self.log_message(f"Error saving settings: {e}", 'error')

    def _apply_styles(self):
        # Mimic Tkinter ttkthemes "breeze" light theme
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(230, 230, 230)) # Light gray background
        palette.setColor(QPalette.WindowText, QColor(50, 50, 50))
        palette.setColor(QPalette.Base, QColor(255, 255, 255))
        palette.setColor(QPalette.AlternateBase, QColor(240, 240, 240))
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 220))
        palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
        palette.setColor(QPalette.Text, QColor(50, 50, 50))
        palette.setColor(QPalette.Button, QColor(200, 200, 200)) # Default button color
        palette.setColor(QPalette.ButtonText, QColor(0, 0, 0))
        palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
        palette.setColor(QPalette.Link, QColor(0, 0, 230))
        palette.setColor(QPalette.Highlight, QColor(0, 120, 215)) # Accent blue for selected items
        palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
        self.setPalette(palette)

        # Apply QSS for specific elements (like accent button)
        self.setStyleSheet("""
            QMainWindow {
                background-color: rgb(230, 230, 230);
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid gray;
                border-radius: 5px;
                margin-top: 1ex; /* leave space for title */
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top center; /* position at top center */
                padding: 0 3px;
                background-color: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                  stop: 0 #E0E0E0, stop: 1 #F0F0F0);
            }
            QPushButton#AccentButton { /* Specific ID for accent button */
                background-color: #0078D7;
                color: white;
                font-weight: bold;
                border: 1px solid #005A9C;
                border-radius: 4px;
                padding: 6px 12px;
            }
            QPushButton#AccentButton:hover {
                background-color: #0067B9;
            }
            QPushButton#AccentButton:pressed {
                background-color: #004D80;
            }
            QPushButton { /* Default button style */
                border: 1px solid #ADADAD;
                border-radius: 4px;
                padding: 5px 10px;
                background-color: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                  stop: 0 #F0F0F0, stop: 1 #E0E0E0);
            }
            QPushButton:hover {
                background-color: #E6E6E6;
            }
            QPushButton:pressed {
                background-color: #D6D6D6;
            }
            QLineEdit, QComboBox, QTextEdit {
                border: 1px solid #A0A0A0;
                border-radius: 3px;
                padding: 3px;
                background-color: white;
            }
            QTextEdit {
                font-family: monospace;
            }
            QComboBox::drop-down {
                border: 0px; /* remove arrow border */
            }
            QComboBox::down-arrow {
                image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAcAAAAECAYAAADtH/oJAAAABmJLR0QA/wD/AP+AdzggAAAASklEQVQYlWMwMDAw/g8o/g/EsP/H/4LhD1wZGJjY/g0MDAysD8T0/w+MDHzB9h/x/4PBj4GzDAyM/A/EQmNgYGBh+A/E9P8PiBhYGBgZAgAGkQx0XzC+iAAAAABJRU5ErkJggg==); /* Example: small down arrow */
                width: 7px;
                height: 4px;
                padding-right: 5px;
            }
            QCheckBox::indicator {
                width: 13px;
                height: 13px;
            }
            QLabel#GrayText {
                color: gray;
            }
        """)
        # Set font for text edit
        font = QFont("Consolas" if sys.platform == "win32" else "Monospace")
        font.setPointSize(9)
        self.log_text.setFont(font)


    def _load_initial_ui_settings(self):
        settings = self.config['SETTINGS']
        self.output_entry.setText(settings.get('output_path', str(self.script_dir)))
        self.filename_template_entry.setText(settings.get('filename_template', '%(title)s.%(ext)s'))
        self.format_dropdown.setCurrentText(settings.get('selected_format', 'mp4'))
        self.quality_dropdown.setCurrentText(settings.get('selected_quality', 'Best'))
        self.playlist_check.setChecked(settings.getboolean('playlist_var', False))
        self.custom_filename_check.setChecked(settings.getboolean('custom_filename_var', False))
        self.embed_metadata_check.setChecked(settings.getboolean('embed_metadata_var', False))
        self.toggle_quality_dropdown()
        self.toggle_filename_template_entry()
        self.log_message("Welcome! Provide a URL and select your options.", 'info')
        self.log_message("Note: Merging video and audio requires FFmpeg.", 'info')
        self.log_message("Note: Embedding metadata requires FFmpeg and Mutagen (recommended).", 'info')
        self.log_message("Note: 'yt-dlp' warnings about 'Falling back to generic n function search' and 'nsig extraction failed' are common and often do not prevent successful downloads. If you experience issues, ensure 'yt-dlp' is up to date.",'warning')


    def _create_widgets(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10) # Spacing between major sections

        # URL Input
        self._create_url_input_group(main_layout)
        main_layout.addSpacing(5) # Smaller spacing for separators
        main_layout.addWidget(self._create_separator())

        # Video Preview
        self._create_preview_group(main_layout)
        main_layout.addSpacing(5)
        main_layout.addWidget(self._create_separator())

        # Download Options
        self._create_options_group(main_layout)
        main_layout.addSpacing(5)
        main_layout.addWidget(self._create_separator())

        # Save Location
        self._create_output_path_group(main_layout)
        main_layout.addSpacing(5)
        main_layout.addWidget(self._create_separator())

        # Filename Options
        self._create_filename_options_group(main_layout)
        main_layout.addSpacing(5)
        main_layout.addWidget(self._create_separator())

        # Download Control Buttons
        self._create_download_control_group(main_layout)
        main_layout.addSpacing(5)
        main_layout.addWidget(self._create_separator())

        # Status & Progress Log
        self._create_log_progress_group(main_layout)

        # Crucially, add a stretch at the end so the log consumes most extra space.
        # This will push the buttons to the top and ensure they are always visible.
        # This is equivalent to setting a high weight on the log's row and smaller weights on others.
        # The QVBoxLayout automatically distributes space according to widget's size policies.
        main_layout.addStretch(1)


    def _create_separator(self):
        separator = QLabel()
        separator.setFrameShape(QLabel.HLine)
        separator.setFrameShadow(QLabel.Sunken)
        separator.setFixedHeight(1) # Ensure it's a thin line
        separator.setStyleSheet("QLabel { background-color: lightgray; }")
        return separator

    def _create_url_input_group(self, parent_layout):
        group_box = QGroupBox("YouTube URL")
        layout = QHBoxLayout(group_box)
        layout.setContentsMargins(5, 15, 5, 5) # Adjust margins for GroupBox title
        self.url_entry = QLineEdit()
        self.url_entry.setPlaceholderText("Enter YouTube video or playlist URL here")
        layout.addWidget(self.url_entry, 1) # Stretch factor of 1

        # Connect text changes to trigger info fetch automatically
        # Use QTimer.singleShot to debounce input and prevent too many requests
        self.url_entry.textChanged.connect(self._schedule_fetch_info)
        self._fetch_info_timer = QTimer(self)
        self._fetch_info_timer.setSingleShot(True)
        self._fetch_info_timer.setInterval(700) # 700 ms debounce time
        self._fetch_info_timer.timeout.connect(self._start_fetch_info_worker)

        parent_layout.addWidget(group_box)

    def _schedule_fetch_info(self):
        # Only schedule if the text has changed and is potentially a valid URL
        url_text = self.url_entry.text().strip()
        if self._is_valid_youtube_url(url_text):
            self._fetch_info_timer.start()
        else:
            self._fetch_info_timer.stop() # Stop timer if URL becomes invalid while typing
            self.clear_preview() # Clear preview if URL is clearly not YouTube

    def _is_valid_youtube_url(self, url):
        # A more robust check for YouTube URLs using QUrl
        qurl = QUrl(url)
        if not qurl.isValid():
            return False
        # Common YouTube domains and subdomains
        youtube_domains = [
            "youtube.com", "www.youtube.com", "m.youtube.com",
            "youtu.be", "www.youtu.be"
        ]
        return qurl.host() in youtube_domains or any(qurl.host().endswith(f".{domain}") for domain in youtube_domains if not domain.startswith('www'))


    def _create_preview_group(self, parent_layout):
        group_box = QGroupBox("Video Preview")
        layout = QHBoxLayout(group_box)
        layout.setContentsMargins(5, 15, 5, 5)

        self.thumbnail_label = QLabel("No thumbnail available")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setFixedSize(160, 90) # Fixed size for consistency
        self.thumbnail_label.setFrameShape(QLabel.Box)
        self.thumbnail_label.setFrameShadow(QLabel.Sunken)
        layout.addWidget(self.thumbnail_label)

        info_grid_layout = QGridLayout()
        info_grid_layout.setColumnStretch(1, 1) # Make the second column stretch
        info_grid_layout.addWidget(QLabel("Title:"), 0, 0, Qt.AlignTop)
        self.title_label = QLabel("N/A")
        self.title_label.setWordWrap(True) # Enable word wrap
        self.title_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        info_grid_layout.addWidget(self.title_label, 0, 1)

        info_grid_layout.addWidget(QLabel("Uploader:"), 1, 0, Qt.AlignTop)
        self.uploader_label = QLabel("N/A")
        info_grid_layout.addWidget(self.uploader_label, 1, 1)

        info_grid_layout.addWidget(QLabel("Duration:"), 2, 0, Qt.AlignTop)
        self.duration_label = QLabel("N/A")
        info_grid_layout.addWidget(self.duration_label, 2, 1)

        info_grid_layout.addWidget(QLabel("Views:"), 3, 0, Qt.AlignTop)
        self.views_label = QLabel("N/A")
        info_grid_layout.addWidget(self.views_label, 3, 1)

        # Add a vertical spacer to push content to the top if the frame expands
        info_grid_layout.addItem(QSpacerItem(20, 40, QSizePolicy.Minimum, QSizePolicy.Expanding), 4, 0, 1, 2)

        layout.addLayout(info_grid_layout, 1) # Make the info layout expand horizontally

        parent_layout.addWidget(group_box)


    def _create_options_group(self, parent_layout):
        group_box = QGroupBox("Download Options")
        layout = QGridLayout(group_box)
        layout.setContentsMargins(5, 15, 5, 5)

        layout.addWidget(QLabel("Format:"), 0, 0)
        self.formats = ['mp4', 'mkv', 'webm', 'mp3']
        self.format_dropdown = QComboBox()
        self.format_dropdown.addItems(self.formats)
        layout.addWidget(self.format_dropdown, 0, 1, 1, 2) # Span two columns
        self.format_dropdown.currentIndexChanged.connect(self.toggle_quality_dropdown)
        self.format_dropdown.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.quality_label = QLabel("Quality (Video):")
        layout.addWidget(self.quality_label, 1, 0)
        # Updated to standard resolutions
        self.qualities = ['Best', '1080p', '720p', '480p', '360p', '240p', '144p']
        self.quality_dropdown = QComboBox()
        self.quality_dropdown.addItems(self.qualities)
        layout.addWidget(self.quality_dropdown, 1, 1, 1, 2) # Span two columns
        self.quality_dropdown.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.playlist_check = QCheckBox("Download playlist (if URL is a playlist)")
        layout.addWidget(self.playlist_check, 2, 0, 1, 3)

        self.embed_metadata_check = QCheckBox("Embed metadata (title, thumbnail, etc.) (for audio)")
        layout.addWidget(self.embed_metadata_check, 3, 0, 1, 3)

        layout.setColumnStretch(1, 1) # Ensure the dropdown columns expand
        layout.setColumnStretch(2, 1)

        parent_layout.addWidget(group_box)

    def _create_output_path_group(self, parent_layout):
        group_box = QGroupBox("Save Location")
        layout = QHBoxLayout(group_box)
        layout.setContentsMargins(5, 15, 5, 5)

        self.output_entry = QLineEdit()
        self.output_entry.setText(str(self.script_dir)) # Set default to script directory
        layout.addWidget(self.output_entry, 1) # Stretch factor of 1

        self.desktop_button = QPushButton("Desktop")
        self.desktop_button.clicked.connect(self.set_path_to_desktop)
        layout.addWidget(self.desktop_button)

        self.downloads_button = QPushButton("Downloads")
        self.downloads_button.clicked.connect(self.set_path_to_downloads)
        layout.addWidget(self.downloads_button)

        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.browse_output_path)
        layout.addWidget(self.browse_button)

        parent_layout.addWidget(group_box)

    def _create_filename_options_group(self, parent_layout):
        group_box = QGroupBox("Filename Options")
        layout = QVBoxLayout(group_box)
        layout.setContentsMargins(5, 15, 5, 5)

        self.custom_filename_check = QCheckBox("Use custom filename template")
        self.custom_filename_check.stateChanged.connect(self.toggle_filename_template_entry)
        layout.addWidget(self.custom_filename_check)

        self.filename_template_entry = QLineEdit()
        self.filename_template_entry.setPlaceholderText("%(title)s.%(ext)s")
        layout.addWidget(self.filename_template_entry)

        label_info1 = QLabel("Common variables: %(title)s, %(ext)s, %(id)s, %(uploader)s, %(duration)s, %(upload_date)s")
        label_info1.setObjectName("GrayText") # For QSS styling
        layout.addWidget(label_info1)

        label_info2 = QLabel("Example: '%(uploader)s - %(title)s.%(ext)s'")
        label_info2.setObjectName("GrayText") # For QSS styling
        layout.addWidget(label_info2)

        parent_layout.addWidget(group_box)

    def _create_download_control_group(self, parent_layout):
        # This will be a simple QHBoxLayout for the buttons, centered.
        # No QGroupBox needed here to keep it simpler and just buttons.
        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch(1) # Pushes buttons to center

        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("AccentButton") # For QSS styling
        self.download_button.setMinimumHeight(35) # Make sure buttons have enough height
        buttons_layout.addWidget(self.download_button)

        self.clear_button = QPushButton("Clear All")
        self.clear_button.clicked.connect(self.clear_fields)
        self.clear_button.setMinimumHeight(35)
        buttons_layout.addWidget(self.clear_button)

        buttons_layout.addStretch(1) # Pushes buttons to center

        parent_layout.addLayout(buttons_layout) # Add the layout directly

    def _create_log_progress_group(self, parent_layout):
        group_box = QGroupBox("Status & Progress")
        layout = QVBoxLayout(group_box)
        layout.setContentsMargins(5, 15, 5, 5)

        # Progress Bar and Label
        progress_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False) # Text will be in a separate label
        progress_layout.addWidget(self.progress_bar, 1) # Stretch factor of 1

        self.progress_label = QLabel("[  0% ]")
        self.progress_label.setFixedWidth(60) # Fixed width for percentage
        self.progress_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        progress_layout.addWidget(self.progress_label)
        layout.addLayout(progress_layout)

        # Log Text Area
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True) # Disable editing
        # Set size policy to expanding for vertical fill
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.log_text)

        parent_layout.addWidget(group_box)


    # --- Qt Slots and Helper Functions ---
    def log_message(self, message, message_type='info'):
        cursor = self.log_text.textCursor()
        cursor.movePosition(cursor.End)
        # Apply color based on message_type using HTML
        if message_type == 'error':
            self.log_text.append(f"<span style='color:red;'>{message}</span>")
        elif message_type == 'warning':
            self.log_text.append(f"<span style='color:orange;'>{message}</span>")
        else: # info
            self.log_text.append(message)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum()) # Scroll to bottom

    def _update_progress(self, percent):
        self.progress_bar.setValue(percent)
        self.progress_label.setText(f"[ {percent}% ]")

    def _on_download_complete(self):
        self.log_message("✅ Download complete and file processed successfully!", 'info')
        self.progress_bar.setValue(100)
        self.progress_label.setText("[ 100% ]")

    def _on_download_error(self, message):
        self.log_message(f"ERROR: {message}", 'error')
        QMessageBox.critical(self, "Download Error", message)

    def _on_info_error(self, message):
        self.log_message(f"ERROR: {message}", 'error')
        QMessageBox.critical(self, "Info Fetch Error", message)
        self.clear_preview()

    def _display_thumbnail(self, pixmap):
        self.thumbnail_label.setPixmap(pixmap)
        self.thumbnail_label.setText("") # Clear "No thumbnail" text

    def _on_thumbnail_error(self):
        self.thumbnail_label.setText("Thumbnail failed to load")
        self.thumbnail_label.setPixmap(QPixmap()) # Clear any previous pixmap

    def browse_output_path(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.output_entry.text())
        if directory:
            self.output_entry.setText(directory)
            self.log_message(f"Save location set to: {directory}", 'info')

    def set_path_to_downloads(self):
        downloads_path = str(Path.home() / "Downloads")
        self.output_entry.setText(downloads_path)
        self.log_message(f"Save location set to: {downloads_path}", 'info')

    def set_path_to_desktop(self):
        desktop_path = str(Path.home() / "Desktop")
        self.output_entry.setText(desktop_path)
        self.log_message(f"Save location set to: {desktop_path}", 'info')

    def toggle_quality_dropdown(self):
        is_mp3 = self.format_dropdown.currentText() == 'mp3'
        self.quality_dropdown.setEnabled(not is_mp3)
        self.quality_label.setEnabled(not is_mp3)

    def toggle_filename_template_entry(self):
        state = self.custom_filename_check.isChecked()
        self.filename_template_entry.setEnabled(state)

    def clear_fields(self):
        settings = self.config['SETTINGS']
        self.url_entry.clear()
        self.output_entry.setText(str(self.script_dir)) # Reset to script directory
        self.format_dropdown.setCurrentText(settings.get('selected_format', 'mp4'))
        self.quality_dropdown.setCurrentText(settings.get('selected_quality', 'Best'))
        self.playlist_check.setChecked(settings.getboolean('playlist_var', False))
        self.custom_filename_check.setChecked(settings.getboolean('custom_filename_var', False))
        self.filename_template_entry.setText(settings.get('filename_template', '%(title)s.%(ext)s'))
        self.embed_metadata_check.setChecked(settings.getboolean('embed_metadata_var', False))
        self.toggle_quality_dropdown()
        self.toggle_filename_template_entry()
        self.clear_preview()
        self.log_text.clear()
        self.progress_bar.setValue(0)
        self.progress_label.setText("[  0% ]")
        self.log_message("Fields cleared. Ready for a new download.", 'info')

    def clear_preview(self):
        self.title_label.setText("N/A")
        self.uploader_label.setText("N/A")
        self.duration_label.setText("N/A")
        self.views_label.setText("N/A")
        self.thumbnail_label.setPixmap(QPixmap()) # Clear pixmap
        self.thumbnail_label.setText("No thumbnail available")
        self.current_video_info = None

    def set_ui_state(self, state):
        enable = (state == 'enabled')
        # Iterate through all widgets in the main layout and set their enabled state
        for widget in self.findChildren(QWidget):
            # Exclude log_text and progress bar from full disable
            if widget in [self.log_text, self.progress_bar, self.progress_label]:
                continue
            widget.setEnabled(enable)

        # Re-enable/disable specific widgets based on their state logic
        self.toggle_quality_dropdown()
        self.toggle_filename_template_entry()

    def _start_fetch_info_worker(self):
        url = self.url_entry.text().strip()
        # Only fetch if it's a valid YouTube URL and it's different from the last one
        if not self._is_valid_youtube_url(url) or (self.current_video_info and self.current_video_info.get('webpage_url', '') == url):
            return

        self.clear_preview()
        self.log_message(f"Fetching info for: {url}...", 'info')
        self.set_ui_state('disabled') # Disable UI during fetch
        # Emit signal to worker to start fetching info
        self.ytdlp_worker.fetch_video_info(url)

    def _display_video_info(self, info):
        self.current_video_info = info
        self.log_message("Video info fetched successfully.", 'info')

        self.title_label.setText(info.get('title', 'N/A'))
        self.uploader_label.setText(info.get('uploader', 'N/A'))

        duration_seconds = info.get('duration')
        if duration_seconds is not None:
            # Convert to HH:MM:SS or MM:SS format
            duration_str = str(datetime.timedelta(seconds=duration_seconds))
            # Handle cases like "0:01:23" to "01:23" or "1:23:45"
            parts = duration_str.split(':')
            if len(parts) == 3 and parts[0] == '0':
                duration_str = ":".join(parts[1:])
            self.duration_label.setText(duration_str)
        else:
            self.duration_label.setText("N/A")

        views = info.get('view_count')
        self.views_label.setText(f"{views:,}" if views is not None else "N/A")

        thumbnail_url = info.get('thumbnail')
        if thumbnail_url:
            self.log_message("Downloading thumbnail...", 'info')
            # Use worker thread to download thumbnail
            self.ytdlp_worker.download_thumbnail(thumbnail_url)
        else:
            self.thumbnail_label.setPixmap(QPixmap())
            self.thumbnail_label.setText("No thumbnail available")
            self.log_message("No thumbnail URL found.", 'warning')

        self._update_available_qualities(info)


    def _update_available_qualities(self, info):
        # We'll stick to fixed common resolutions for simplicity as per user request
        # and ensure 'Best' is always an option.
        # The user requested specific formats like 144p, 240p, 360p, etc.
        # If the video doesn't have a specific resolution, 'Best' will attempt to get the highest.
        # This keeps the UI simple and predictable.
        if not info or 'formats' not in info:
            self.log_message("Could not get format details for quality dropdown. Using default qualities.", 'warning')
            self.quality_dropdown.clear()
            self.quality_dropdown.addItems(self.qualities) # Re-add default list
            self.quality_dropdown.setCurrentText('Best')
            return

        # Let's ensure the quality options always show the requested ones,
        # and 'Best' will automatically pick the highest available.
        # The quality selection logic in download_video will handle finding the closest format.
        # No dynamic update needed for the dropdown itself if we always show fixed options.
        current_text = self.quality_dropdown.currentText()
        self.quality_dropdown.clear()
        self.quality_dropdown.addItems(self.qualities)
        if current_text in self.qualities:
            self.quality_dropdown.setCurrentText(current_text)
        else:
            self.quality_dropdown.setCurrentText('Best')

        self.log_message(f"Available video qualities are set to standard options: {', '.join(self.qualities)}", 'info')


    def _start_download_worker(self):
        url = self.url_entry.text().strip()
        output_path = self.output_entry.text().strip()

        if not url:
            QMessageBox.critical(self, "Error", "Please enter a YouTube URL!")
            return
        if not output_path:
            QMessageBox.critical(self, "Error", "Please select an output path!")
            return
        if not self._is_valid_youtube_url(url):
            QMessageBox.critical(self, "Error", "The entered URL does not appear to be a valid YouTube URL.")
            return

        output_path_obj = Path(output_path)
        if not output_path_obj.is_dir():
            try:
                output_path_obj.mkdir(parents=True, exist_ok=True)
                self.log_message(f"Created output directory: {output_path}", 'info')
            except OSError as e:
                QMessageBox.critical(self, "Error", f"Could not create output directory: {e}")
                self.log_message(f"Error creating directory: {e}", 'error')
                return

        self.log_message(f"Preparing download for: {url}", 'info')
        self.set_ui_state('disabled')
        self.progress_bar.setValue(0)
        self.progress_label.setText("[  0% ]")

        # Emit signal to worker to start downloading
        self.ytdlp_worker.download_video(
            url,
            output_path,
            self.format_dropdown.currentText(),
            self.quality_dropdown.currentText(),
            self.playlist_check.isChecked(),
            self.custom_filename_check.isChecked(),
            self.filename_template_entry.text(),
            self.embed_metadata_check.isChecked(),
            self.current_video_info
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YouTubeDownloaderApp()
    window.show()
    sys.exit(app.exec_())