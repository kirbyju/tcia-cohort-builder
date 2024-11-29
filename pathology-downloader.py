import sys
import os
import requests
import pandas as pd
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QFileDialog,
                             QProgressBar, QTextEdit, QLineEdit)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
import time

def load_manifest(excel_path):
    """
    Load pathology manifest from Excel, with more robust parsing

    Attempts to find columns flexibly:
    - Looks for 'imageUrl' or similar columns
    - Tries multiple patient ID column names
    """
    # Read the Excel file
    df = pd.read_excel(excel_path)

    # List of possible URL column names
    url_columns = ['imageUrl', 'image_url', 'url', 'Image URL']

    # List of possible patient ID column names
    patient_id_columns = ['Case ID', 'Patient ID', 'case_id', 'Case ID']

    # Find the first matching URL column
    url_column = next((col for col in url_columns if col in df.columns), None)

    # Find the first matching patient ID column
    patient_id_column = next((col for col in patient_id_columns if col in df.columns), None)

    if not url_column:
        raise ValueError("Could not find image URL column in the Excel file.")

    if not patient_id_column:
        raise ValueError("Could not find patient ID column in the Excel file.")

    # Rename columns to standard names for consistency
    df = df.rename(columns={
        url_column: 'imageUrl',
        patient_id_column: 'Case ID'
    })

    return df[['imageUrl', 'Case ID']]

class PathologyDownloadThread(QThread):
    progress_signal = pyqtSignal(int, int, str)
    complete_signal = pyqtSignal(bool)

    # data loading
    def __init__(self, data, download_dir):
        super().__init__()
        # If data is a string (file path), load the manifest
        if isinstance(data, str):
            data = load_manifest(data)

        self.data = data
        self.download_dir = download_dir

    def run(self):
        total_images = len(self.data)

        for idx, (_, row) in enumerate(self.data.iterrows(), 1):
            url = row['imageUrl']
            patient_id = row['Case ID']

            # Extract path after '/ross/' to create subdirectory structure
            sub_path = url.split('/ross/', 1)[-1]
            file_path = os.path.join(self.download_dir, sub_path)

            try:
                # Create directories if they don't exist
                os.makedirs(os.path.dirname(file_path), exist_ok=True)

                # Download file
                response = requests.get(url, stream=True, timeout=30)
                response.raise_for_status()

                with open(file_path, 'wb') as file:
                    for chunk in response.iter_content(chunk_size=8192):
                        file.write(chunk)

                # Emit progress
                self.progress_signal.emit(idx, total_images, f"Downloaded: {file_path}")

            except Exception as e:
                self.progress_signal.emit(idx, total_images, f"Failed to download {patient_id}: {str(e)}")

        self.complete_signal.emit(True)

class PathologyDownloadManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.initUI()

    def initUI(self):
        self.setWindowTitle('TCIA Pathology Image Downloader')
        self.setGeometry(100, 100, 600, 500)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout()
        central_widget.setLayout(main_layout)

        # Excel File Selection
        file_layout = QHBoxLayout()
        self.file_path_input = QLineEdit()
        file_path_button = QPushButton('Select Excel File')
        file_path_button.clicked.connect(self.select_excel_file)
        file_layout.addWidget(self.file_path_input)
        file_layout.addWidget(file_path_button)
        main_layout.addLayout(file_layout)

        # Download Directory Selection
        dir_layout = QHBoxLayout()
        self.download_dir_input = QLineEdit()
        download_dir_button = QPushButton('Select Download Directory')
        download_dir_button.clicked.connect(self.select_download_directory)
        dir_layout.addWidget(self.download_dir_input)
        dir_layout.addWidget(download_dir_button)
        main_layout.addLayout(dir_layout)

        # Start Download Button
        download_button = QPushButton('Start Download')
        download_button.clicked.connect(self.start_download)
        main_layout.addWidget(download_button)

        # Progress Bar
        self.progress_bar = QProgressBar()
        main_layout.addWidget(self.progress_bar)

        # Log Display
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        main_layout.addWidget(self.log_display)

    def select_excel_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, 'Select Excel File', '', 'Excel Files (*.xlsx *.xls)')
        if file_path:
            self.file_path_input.setText(file_path)

    def select_download_directory(self):
        directory = QFileDialog.getExistingDirectory(self, 'Select Download Directory')
        if directory:
            self.download_dir_input.setText(directory)

    def start_download(self):
        excel_path = self.file_path_input.text()
        download_dir = self.download_dir_input.text()

        if not excel_path or not download_dir:
            self.log_display.append("Please select both Excel file and download directory.")
            return

        try:
            # Load pathology data
            pathology_data = pd.read_excel(excel_path)

            # Clear previous logs
            self.log_display.clear()
            self.progress_bar.setValue(0)

            # Create download thread
            self.download_thread = PathologyDownloadThread(pathology_data, download_dir)
            self.download_thread.progress_signal.connect(self.update_progress)
            self.download_thread.complete_signal.connect(self.download_complete)
            self.download_thread.start()

        except Exception as e:
            self.log_display.append(f"Error: {str(e)}")

    def update_progress(self, current, total, message):
        progress_percent = int((current / total) * 100)
        self.progress_bar.setValue(progress_percent)
        self.log_display.append(message)

    def download_complete(self, success):
        if success:
            self.log_display.append("Download process completed!")
        else:
            self.log_display.append("Download process failed.")

def main():
    app = QApplication(sys.argv)
    ex = PathologyDownloadManager()
    ex.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
