import os
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
import zipfile

# Create a session for connection pooling
session = requests.Session()
session.verify = False  # Disable SSL verification for speed
# Suppress SSL warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_direct_url(file_id):
    """Get the direct download URL by handling the virus scan warning page."""
    url = f"https://drive.google.com/uc?id={file_id}&export=download&confirm=t"
    response = session.get(url)
    
    if "virus scan warning" in response.text.lower():
        soup = BeautifulSoup(response.text, 'html.parser')
        form = soup.find('form', {'id': 'download-form'})
        if form and 'action' in form.attrs:
            inputs = form.find_all('input', {'type': 'hidden'})
            params = {input['name']: input['value'] for input in inputs}
            return form['action'] + '?' + '&'.join(f"{k}={v}" for k, v in params.items())
    
    return url

def download_and_extract(file_id, zip_name="FHO_eval_data.zip"):
    """Download zip file from Google Drive and extract its contents."""
    if os.path.exists(zip_name):
        print(f"Removing existing {zip_name}...")
        os.remove(zip_name)
    
    print(f"Downloading {zip_name}...")
    
    # Get the direct download URL
    url = get_direct_url(file_id)
    
    # Stream the download with progress bar
    response = session.get(url, stream=True)
    total_size = int(response.headers.get('content-length', 0))
    
    # Use a larger chunk size (1MB) for faster downloads
    chunk_size = 1024 * 1024  # 1MB chunks
    
    with open(zip_name, 'wb') as f, tqdm(
        desc=zip_name,
        total=total_size,
        unit='iB',
        unit_scale=True,
        unit_divisor=1024,
        mininterval=0.5  # Update progress bar every 0.5 seconds
    ) as pbar:
        for chunk in response.iter_content(chunk_size=chunk_size):
            size = f.write(chunk)
            pbar.update(size)
    
    if os.path.exists(zip_name) and os.path.getsize(zip_name) > 1000:
        print(f"Successfully downloaded {zip_name}")
        print("Extracting files...")
        with zipfile.ZipFile(zip_name, 'r') as zip_ref:
            zip_ref.extractall('.')
        print("Extraction complete!")
        # Clean up zip file
        os.remove(zip_name)
        return True
    else:
        print(f"Failed to download {zip_name} or file is too small")
        if os.path.exists(zip_name):
            os.remove(zip_name)
        return False

def main():
    # New file ID for the zip file
    file_id = "1N6k1bwP39mflp_nbbiskDivv7t_AOqnp"
    
    print("Starting download...")
    success = download_and_extract(file_id)
    print("\nDownload and extraction completed!" if success else "\nDownload failed!")

if __name__ == "__main__":
    main() 