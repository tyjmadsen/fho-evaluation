import os
import sys
import requests
from tqdm import tqdm
import hashlib
import shutil
import re

# File information
FILES = {
    'fho_all.gpkg': {
        'size': 1300000000,  # 1.3GB
        'md5': None,  # Add MD5 hash if available
        'description': 'Contains FHO forecast data',
        'drive_id': None  # Add Google Drive file ID here
    },
    'LSRs_flood_allYears.gpkg': {
        'size': 8600000,  # 8.6MB
        'md5': None,  # Add MD5 hash if available
        'description': 'Contains Local Storm Reports',
        'drive_id': None  # Add Google Drive file ID here
    },
    'flood_warnings_all.gpkg': {
        'size': 11000000,  # 11MB
        'md5': None,  # Add MD5 hash if available
        'description': 'Contains Flood Warning data',
        'drive_id': None  # Add Google Drive file ID here
    }
}

def get_google_drive_download_url(file_id):
    """Convert Google Drive file ID to direct download URL"""
    return f"https://drive.google.com/uc?export=download&id={file_id}"

def download_file(url, filename):
    """Download a file with progress bar"""
    # Handle Google Drive links
    if 'drive.google.com' in url:
        session = requests.Session()
        response = session.get(url, stream=True)
        # Handle large files warning
        for key, value in response.cookies.items():
            if key.startswith('download_warning'):
                url = f"{url}&confirm={value}"
                response = session.get(url, stream=True)
                break
    
    total_size = int(response.headers.get('content-length', 0))
    block_size = 1024  # 1 Kibibyte
    
    with open(filename, 'wb') as file, tqdm(
        desc=filename,
        total=total_size,
        unit='iB',
        unit_scale=True,
        unit_divisor=1024,
    ) as progress_bar:
        for data in response.iter_content(block_size):
            size = file.write(data)
            progress_bar.update(size)

def verify_file(filename, expected_size, expected_md5=None):
    """Verify file size and optionally MD5 hash"""
    if not os.path.exists(filename):
        return False
    
    actual_size = os.path.getsize(filename)
    if actual_size != expected_size:
        return False
    
    if expected_md5:
        with open(filename, 'rb') as f:
            file_hash = hashlib.md5(f.read()).hexdigest()
            if file_hash != expected_md5:
                return False
    
    return True

def copy_file(source_path, destination):
    """Copy a file with progress bar"""
    total_size = os.path.getsize(source_path)
    block_size = 1024 * 1024  # 1MB blocks
    
    with open(source_path, 'rb') as src, open(destination, 'wb') as dst, tqdm(
        desc=os.path.basename(destination),
        total=total_size,
        unit='iB',
        unit_scale=True,
        unit_divisor=1024,
    ) as progress_bar:
        while True:
            data = src.read(block_size)
            if not data:
                break
            dst.write(data)
            progress_bar.update(len(data))

def main():
    print("FHO Verification Application Data Setup")
    print("=" * 50)
    print("\nThis application requires three data files. You have several options to obtain them:")
    print("1. Download from Google Drive (recommended)")
    print("2. Copy from a local directory")
    print("3. Skip for now (you can run this script again later)")
    print("\nNote: The data files are large (especially fho_all.gpkg at ~1.3GB).")
    print("Make sure you have enough disk space and a stable internet connection.")
    
    # Check which files need to be obtained
    files_needed = []
    for filename, info in FILES.items():
        if not verify_file(filename, info['size'], info['md5']):
            files_needed.append(filename)
    
    if not files_needed:
        print("\nAll required files are present and verified!")
        return
    
    print("\nThe following files are needed:")
    for filename in files_needed:
        info = FILES[filename]
        print(f"- {filename} ({info['size'] / (1024*1024):.1f} MB): {info['description']}")
    
    for filename in files_needed:
        print(f"\nSetting up {filename}...")
        while True:
            choice = input("Choose an option (1-3): ").strip()
            
            if choice == "1":
                if FILES[filename]['drive_id']:
                    url = get_google_drive_download_url(FILES[filename]['drive_id'])
                else:
                    print("\nTo use Google Drive download:")
                    print("1. Upload the file to your Google Drive")
                    print("2. Right-click the file and select 'Share'")
                    print("3. Set sharing to 'Anyone with the link'")
                    print("4. Copy the file ID from the sharing link")
                    print("   (The ID is the long string between /d/ and /view)")
                    drive_id = input("\nEnter the Google Drive file ID: ").strip()
                    url = get_google_drive_download_url(drive_id)
                
                try:
                    print(f"\nDownloading {filename}...")
                    download_file(url, filename)
                    print(f"Successfully downloaded {filename}")
                    break
                except Exception as e:
                    print(f"Error downloading {filename}: {str(e)}")
                    print("Please try again or choose a different option.")
            
            elif choice == "2":
                source = input("Enter the full path to the file: ").strip()
                if not os.path.exists(source):
                    print("File not found. Please try again.")
                    continue
                try:
                    print(f"\nCopying {filename}...")
                    copy_file(source, filename)
                    print(f"Successfully copied {filename}")
                    break
                except Exception as e:
                    print(f"Error copying {filename}: {str(e)}")
                    print("Please try again or choose a different option.")
            
            elif choice == "3":
                print(f"Skipping {filename}. You can run this script again later.")
                break
            
            else:
                print("Invalid choice. Please enter 1, 2, or 3.")

if __name__ == "__main__":
    main() 