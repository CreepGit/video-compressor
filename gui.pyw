from PySide6 import QtWidgets, QtCore
import os
import re
import sys
import time
import random
import json
from ffmpeg import FFmpeg, FFmpegFileNotFound, Progress
from dataclasses import dataclass


@dataclass
class ItemEntry:
    display: str
    full_path: str


APP_NAME = "Video Compressor"


def get_appdata_path():
    """Gets local app data directory path"""
    appdata_path = os.getenv("LOCALAPPDATA")
    if not appdata_path:
        appdata_path = os.path.expanduser("~/.local/share")
    return os.path.join(appdata_path, APP_NAME)


try:
    with open(os.path.join(get_appdata_path(), "settings.json"), "r") as json_file:
        settings = json.load(json_file)
except FileNotFoundError:
    settings = {}


def save_settings():
    os.makedirs(get_appdata_path(), exist_ok=True)
    with open(os.path.join(get_appdata_path(), "settings.json"), "w") as json_file:
        json.dump(settings, json_file)


class Lister(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.items: list[ItemEntry] = []
        self.exclude_filter_text = settings.get("exclude_filter", "")
        self.output_folder_text = settings.get("output_folder", "")
        self.input_folder_text = settings.get("input_folder", "")
        self.rename_regex_text = settings.get("rename_regex", "")
        self.progress_numbers = {} # path : int %
        self.workers = {}
        self.initUI()
        # Init Settings
        self.select_input_folder(self.input_folder_text)
        self.set_output_folder(self.output_folder_text)
        self.exclude_filter.setText(self.exclude_filter_text)
        self.output_regex.setText(self.rename_regex_text)
        # Go
        self._render_list()

    def initUI(self):
        self.layout = QtWidgets.QVBoxLayout(self)

        # region Widgets
        self.select_folder_button = QtWidgets.QPushButton("Select Folder", self)
        self.select_folder_button.clicked.connect(self.dialog_input_folder)

        self.selected_folder_label = QtWidgets.QLabel("Selected Folder: ", self)

        self.content_list = QtWidgets.QListWidget(self)

        self.exclude_filter = QtWidgets.QLineEdit(self)
        self.exclude_filter.setPlaceholderText("Regex Exclude Filter")
        self.exclude_filter.textChanged.connect(self._set_exclude_filter)

        self.output_folder = QtWidgets.QPushButton("Select Output Folder", self)
        self.output_folder.clicked.connect(self.dialog_output_folder)

        self.output_folder_label = QtWidgets.QLabel("Selected Output Folder: ", self)

        self.regex_label = QtWidgets.QLabel("Rename Regex. Picks the first regex group as the new name.", self)

        self.output_regex = QtWidgets.QLineEdit(self)
        self.output_regex.setPlaceholderText("Same as output if empty.")
        self.output_regex.textChanged.connect(self._set_rename_regex)

        self.compress_button = QtWidgets.QPushButton("Compress", self)
        self.compress_button.clicked.connect(self.start_compressing)
        self.compress_button.setDisabled(True)
        # endregion

        self.layout.addWidget(self.select_folder_button)
        self.layout.addWidget(self.selected_folder_label)
        self.layout.addWidget(self.content_list)
        self.layout.addWidget(self.exclude_filter)
        self.layout.addWidget(self.output_folder)
        self.layout.addWidget(self.output_folder_label)
        self.layout.addWidget(self.regex_label)
        self.layout.addWidget(self.output_regex)
        self.layout.addWidget(self.compress_button)

        self.setLayout(self.layout)

    def select_input_folder(self, folder: str):
        def add_folder_contents(folder: str):
            for item in os.listdir(folder):
                combined_path = os.path.join(folder, item)
                full_path = os.path.abspath(combined_path)
                if os.path.isdir(combined_path):
                    add_folder_contents(combined_path)
                else:
                    icon = "🎞️" if full_path.endswith(".mp4") else "📄"
                    self.items.append(
                        ItemEntry(
                            display=f"{icon} {item}",
                            full_path=full_path,
                        )
                    )
 
        if folder:
            self.selected_folder_label.setText(f"Selected Folder: {folder}")
            add_folder_contents(folder=folder)
            self.input_folder_text = folder
            settings["input_folder"] = folder
        else:
            self.selected_folder_label.setText("Selected Folder: ")
            self.input_folder_text = ""
            settings["input_folder"] = ""

        self._render_list()

    def dialog_input_folder(self):
        self.items.clear()
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Folder")
        self.select_input_folder(folder)
        save_settings()

    def dialog_output_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Output Folder"
        )
        self.set_output_folder(folder)
        save_settings()
    
    def set_output_folder(self, folder: str):
        if folder:
            self.output_folder_label.setText(f"Selected Output Folder: {folder}")
            self.output_folder.setText(f"Selected Output Folder: {folder}")
            self.compress_button.setDisabled(False)
            self.output_folder_text = folder
            settings["output_folder"] = folder
        else:
            self.output_folder_label.setText("Selected Output Folder: ")
            self.compress_button.setDisabled(True)
            self.output_folder_text = ""
            settings["output_folder"] = ""

        save_settings()

    # region Compressing
    def start_compressing(self):
        keep_paths: list[str] = []
        for item in self.items:
            if self.exclude_filter_text:
                if re.search(self.exclude_filter_text, item.display):
                    continue
            keep_paths.append(item.full_path)

        class Worker(QtCore.QThread):
            progress = QtCore.Signal(int)

            def __init__(worker, file_path: str):
                super().__init__()
                worker.file_path = file_path
                worker.ffmpeg = None

            def run(worker):
                print(f"Compressing {worker.file_path}")
                try:
                    max_duration = None
                    try:
                        probe = FFmpeg(executable='ffprobe').input(worker.file_path, print_format='json', show_streams=None,)
                        media = json.loads(probe.execute())
                        max_duration = media["streams"][0]["duration"]
                        max_duration = float(max_duration)
                    except Exception as e:
                        print(f"Error loading metadata: {e}")
                    
                    filename = os.path.basename(worker.file_path)
                    worker.ffmpeg = (
                        FFmpeg()
                        .option("y")
                        .input(worker.file_path)
                        .output(
                            f"{self.output_folder_text}/output_{filename}",
                            {"codec:v": "libx264", "b:v": "2M"},
                            preset="superfast",
                            # crf=35,
                        )
                    )
                    @worker.ffmpeg.on("progress")
                    def on_progress(progress: Progress):
                        if max_duration:
                            worker.progress.emit(100*progress.time.total_seconds()/max_duration)
                    worker.ffmpeg.execute()
                except FFmpegFileNotFound as exception:
                    print("An exception has been occurred!")
                    print("- Message from ffmpeg:", exception.message)
                    print("- Arguments to execute ffmpeg:", exception.arguments)
                    worker.progress.emit(-1)
                except Exception as e:
                    print(f"Error: {e}")
                    print()
                    print()
                    worker.progress.emit(-1)
            
            def stop(worker):
                # worker.terminate()
                if worker.ffmpeg:
                    print("Killed video process")
                    try:
                        worker.ffmpeg.terminate()
                    finally:
                        pass


        def progress_report(path: str, value: int):
            self.progress_numbers[path] = value
            self._render_list()

        def progress_done(path: str):
            self.progress_numbers[path] = 100
            self._render_list()
            del self.workers[path]
            if self.workers:
                next_worker = list(self.workers.values())[0]
                next_worker.start()
            else:
                print("All done")
        
        self.workers = {}
        for path in keep_paths:
            worker = Worker(path)
            worker.progress.connect(lambda i, path=path: progress_report(path, i))
            worker.finished.connect(lambda path=path: progress_done(path))
            self.workers[path] = worker
        
        list(self.workers.values())[0].start()
    # endregion

    def _get_new_name(self, old_name: str):
        if not self.rename_regex_text:
            return None
        try:
            result = re.match(self.rename_regex_text, old_name).group(1)
        except AttributeError:
            return None
        except IndexError:
            return None
        except re.error:
            return None
        if not result:
            return None
        return result

    def _render_list(self):
        """Render the list of items in the gui"""
        self.content_list.clear()
        for item in self.items:
            prefix = ""
            progress = ""
            if self.exclude_filter_text:
                if re.search(self.exclude_filter_text, item.display):
                    prefix = "❌ "
            progress_value = self.progress_numbers.get(item.full_path)
            if progress_value:
                progress = f"[{progress_value:2d}%]"
            
            old_file_name = os.path.basename(item.full_path)
            new_file_name = self._get_new_name(old_file_name)

            new_file_text = f" -> {new_file_name}" if new_file_name else ""

            self.content_list.addItem(f"{prefix}{progress}{item.display}{new_file_text}")

    def _set_rename_regex(self, text: str):
        self.rename_regex_text = text
        settings["rename_regex"] = text
        save_settings()
        self._render_list()

    def _set_exclude_filter(self, text: str):
        self.exclude_filter_text = text
        settings["exclude_filter"] = text
        save_settings()
        self._render_list()
    
    def closeEvent(self, event):
        for worker in self.workers.values():
            worker.stop()
        return super().closeEvent(event)


if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    w = Lister()
    w.resize(800, 650)
    w.show()
    w.setWindowTitle("Video Compressor")
    sys.exit(app.exec())
