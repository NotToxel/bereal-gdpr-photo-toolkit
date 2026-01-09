import json
from datetime import datetime
from PIL import Image, ImageDraw, ImageOps, ExifTags
import logging
from pathlib import Path
import piexif
import os
import time
import shutil
import subprocess
from iptcinfo3 import IPTCInfo

# ANSI escape codes for text styling
STYLING = {
    "GREEN": "\033[92m",
    "RED": "\033[91m",
    "BLUE": "\033[94m",
    "BOLD": "\033[1m",
    "RESET": "\033[0m",
}

#Setup log styling
class ColorFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        if record.levelno == logging.INFO and "Finished processing" not in record.msg:
            message = STYLING["GREEN"] + message + STYLING["RESET"]
        elif record.levelno == logging.ERROR:
            message = STYLING["RED"] + message + STYLING["RESET"]
        elif "Finished processing" in record.msg:  # Identify the summary message
            message = STYLING["BLUE"] + STYLING["BOLD"] + message + STYLING["RESET"]
        return message

# Setup logging with styling
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
handler = logger.handlers[0]  # Get the default handler installed by basicConfig
handler.setFormatter(ColorFormatter('%(asctime)s - %(levelname)s - %(message)s'))

# Initialize counters
processed_files_count = 0
converted_files_count = 0
combined_files_count = 0
skipped_files_count = 0

# Static IPTC tags
source_app = "BeReal app"
processing_tool = "github/bereal-gdpr-photo-toolkit"

# Define lists to hold the paths of images to be combined
primary_images = []
secondary_images = []

# Define paths using pathlib
photo_folder = Path('Photos/post/')
bereal_folder = Path('Photos/bereal')
output_folder = Path('Photos/post/__processed')
output_folder_combined = Path('Photos/post/__combined')
output_folder.mkdir(parents=True, exist_ok=True)  # Create the output folder if it doesn't exist

# Print the paths
print(STYLING["BOLD"] + "\nThe following paths are set for the input and output files:" + STYLING["RESET"])
print(f"Photo folder: {photo_folder}")
if os.path.exists(bereal_folder):
    print(f"Older photo folder: {bereal_folder}")
print(f"Output folder for singular images: {output_folder}")
print(f"Output folder for combined images: {output_folder_combined}")
print("")

# Function to count number of input files
def count_files_in_folder(folder_path):
    folder = Path(folder_path)
    # Count webp and mp4
    file_count = len(list(folder.glob('*.webp'))) + len(list(folder.glob('*.mp4')))
    return file_count

number_of_files = count_files_in_folder(photo_folder)
print(f"Number of files in {photo_folder}: {number_of_files}")

if os.path.exists(bereal_folder):
    number_of_files_old = count_files_in_folder(bereal_folder)
    print(f"Number of (older) files in {bereal_folder}: {number_of_files_old}")
    number_of_files += number_of_files_old

# Settings
print(STYLING["BOLD"] + "\nDo you want to access advanced settings or run with default settings?" + STYLING["RESET"])
print("Default settings are:\n"
"1. Copied images are converted from WebP to JPEG\n"
"2. Converted images' filenames do not contain the original filename\n"
"3. Combined images are created on top of converted, singular images")
advanced_settings = input("\nEnter " + STYLING["BOLD"] + "'yes'" + STYLING["RESET"] + " for advanced settings or press any key to continue with default settings: ").strip().lower()

if advanced_settings != 'yes':
    print("Continuing with default settings.\n")

convert_to_jpeg = 'yes'
keep_original_filename = 'no'
create_combined_images = 'yes'

if advanced_settings == 'yes':
    convert_to_jpeg = None
    while convert_to_jpeg not in ['yes', 'no']:
        convert_to_jpeg = input(STYLING["BOLD"] + "\n1. Do you want to convert images from WebP to JPEG? (yes/no): " + STYLING["RESET"]).strip().lower()
        if convert_to_jpeg == 'no':
            print("Your images will not be converted. No additional metadata will be added.")
        if convert_to_jpeg not in ['yes', 'no']:
            logging.error("Invalid input. Please enter 'yes' or 'no'.")

    print(STYLING["BOLD"] + "\n2. There are two options for how output files can be named" + STYLING["RESET"] + "\n"
    "Option 1: YYYY-MM-DDTHH-MM-SS_primary/secondary_original-filename.jpeg\n"
    "Option 2: YYYY-MM-DDTHH-MM-SS_primary/secondary.jpeg\n"
    "This will only influence the naming scheme of singular images.")
    keep_original_filename = None
    while keep_original_filename not in ['yes', 'no']:
        keep_original_filename = input(STYLING["BOLD"] + "Do you want to keep the original filename in the renamed file? (yes/no): " + STYLING["RESET"]).strip().lower()
        if keep_original_filename not in ['yes', 'no']:
            logging.error("Invalid input. Please enter 'yes' or 'no'.")

    create_combined_images = None
    while create_combined_images not in ['yes', 'no']:
        create_combined_images = input(STYLING["BOLD"] + "\n3. Do you want to create combined images like the original BeReal memories? (yes/no): " + STYLING["RESET"]).strip().lower()
        if create_combined_images not in ['yes', 'no']:
            logging.error("Invalid input. Please enter 'yes' or 'no'.")

if convert_to_jpeg == 'no' and create_combined_images == 'no':
    print("You chose not to convert images nor do you want to output combined images.\n"
    "The script will therefore only copy images to a new folder and rename them according to your choice without adding metadata or creating new files.\n"
    "Script will continue to run in 5 seconds.")

# Function to convert WEBP to JPEG
def convert_webp_to_jpg(image_path):
    if image_path.suffix.lower() == '.webp':
        jpg_path = image_path.with_suffix('.jpg')
        try:
            with Image.open(image_path) as img:
                img.convert('RGB').save(jpg_path, "JPEG", quality=80)
                logging.info(f"Converted {image_path} to JPEG.")
            return jpg_path, True
        except Exception as e:
            logging.error(f"Error converting {image_path} to JPEG: {e}")
            return None, False
    else:
        return image_path, False

# Helper function to convert latitude and longitude to EXIF-friendly format
def _convert_to_degrees(value):
    d = int(value)
    m = int((value - d) * 60)
    s = (value - d - m/60) * 3600.00
    d = (d, 1)
    m = (m, 1)
    s = (int(s * 100), 100)
    return (d, m, s)

# Function to update EXIF data (Images Only)
def update_exif(image_path, datetime_original, location=None, caption=None):
    try:
        exif_dict = piexif.load(image_path.as_posix())
        if '0th' not in exif_dict: exif_dict['0th'] = {}
        if 'Exif' not in exif_dict: exif_dict['Exif'] = {}

        exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = datetime_original.strftime("%Y:%m:%d %H:%M:%S")
        
        if location and 'latitude' in location and 'longitude' in location:
            gps_ifd = {
                piexif.GPSIFD.GPSLatitudeRef: 'N' if location['latitude'] >= 0 else 'S',
                piexif.GPSIFD.GPSLatitude: _convert_to_degrees(abs(location['latitude'])),
                piexif.GPSIFD.GPSLongitudeRef: 'E' if location['longitude'] >= 0 else 'W',
                piexif.GPSIFD.GPSLongitude: _convert_to_degrees(abs(location['longitude'])),
            }
            exif_dict['GPS'] = gps_ifd

        if caption:
            exif_dict['0th'][piexif.ImageIFD.ImageDescription] = caption.encode('utf-8')
        
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, image_path.as_posix())
        logging.info(f"Updated EXIF data for {image_path.name}.")
    except Exception as e:
        logging.error(f"Failed to update EXIF data for {image_path}: {e}")

# Function to update IPTC information (Images Only)
def update_iptc(image_path, caption):
    try:
        # Convert Path object to string for the library, but keep Path object for logging
        image_path_str = str(image_path)
        info = IPTCInfo(image_path_str, force=True)
        if not hasattr(info, '_markers'): info._markers = []
        if caption:
            info['caption/abstract'] = caption
        info['source'] = source_app
        info['originating program'] = processing_tool
        info.save_as(image_path_str)
        logging.info(f"Updated IPTC data for {image_path.name}")
    except Exception as e:
        logging.error(f"Failed to update IPTC for {image_path}: {e}")

# Function to update Video Metadata (File System Date + Best Effort ExifTool)
def update_video_metadata(video_path, datetime_original, location=None):
    try:
        # 1. Update File System Timestamp (Supported natively)
        # This ensures the video sorts correctly in most file explorers
        timestamp = datetime_original.timestamp()
        os.utime(video_path, (timestamp, timestamp))
        logging.info(f"Updated file modification time for {video_path.name}.")

        # 2. Try to update internal metadata using ExifTool (if installed)
        # This is a best-effort approach. If ExifTool is not in PATH, it will skip silently.
        try:
            date_str = datetime_original.strftime("%Y:%m:%d %H:%M:%S")
            cmd = [
                "exiftool",
                "-overwrite_original",
                f"-CreateDate={date_str}",
                f"-DateTimeOriginal={date_str}",
                str(video_path)
            ]
            
            # If location is present, try to add it (ExifTool handles this differently for videos, simpler to skip or just add basic tags)
            # For robustness, we stick to Date which is most important.
            
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            logging.info(f"Updated internal video metadata using ExifTool for {video_path.name}.")
        except (FileNotFoundError, subprocess.CalledProcessError):
            # ExifTool not found or failed, ignore.
            pass

    except Exception as e:
        logging.error(f"Failed to update video metadata for {video_path}: {e}")

# Function to handle deduplication
def get_unique_filename(path):
    if not path.exists():
        return path
    else:
        prefix = path.stem
        suffix = path.suffix
        counter = 1
        while path.exists():
            path = path.with_name(f"{prefix}_{counter}{suffix}")
            counter += 1
        return path

def combine_images_with_resizing(primary_path, secondary_path):
    corner_radius = 60
    outline_size = 7
    position = (55, 55)

    primary_image = Image.open(primary_path)
    secondary_image = Image.open(secondary_path)

    scaling_factor = 1/3.33333333  
    width, height = secondary_image.size
    new_width = int(width * scaling_factor)
    new_height = int(height * scaling_factor)
    resized_secondary_image = secondary_image.resize((new_width, new_height), Image.Resampling.LANCZOS)

    if resized_secondary_image.mode != 'RGBA':
        resized_secondary_image = resized_secondary_image.convert('RGBA')

    mask = Image.new('L', (new_width, new_height), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, new_width, new_height), corner_radius, fill=255)
    resized_secondary_image.putalpha(mask)

    combined_image = Image.new("RGB", primary_image.size)
    combined_image.paste(primary_image, (0, 0))    

    outline_layer = Image.new('RGBA', combined_image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(outline_layer)
    outline_box = [position[0] - outline_size, position[1] - outline_size, position[0] + new_width + outline_size, position[1] + new_height + outline_size]
    draw.rounded_rectangle(outline_box, corner_radius + outline_size, fill=(0, 0, 0, 255))

    combined_image.paste(outline_layer, (0, 0), outline_layer)
    combined_image.paste(resized_secondary_image, position, resized_secondary_image)

    return combined_image

def remove_backup_files(directory):
    for filename in os.listdir(directory):
        if filename.endswith('~'):
            file_path = os.path.join(directory, filename)
            try:
                os.remove(file_path)
                print(f"Removed backup file: {file_path}")
            except Exception as e:
                print(f"Failed to remove backup file {file_path}: {e}")

# Load the JSON file
try:
    with open('posts.json', encoding="utf8") as f:
        data = json.load(f)
except FileNotFoundError:
    logging.error("JSON file not found. Please check the path.")
    exit()

# Process files
for entry in data:
    try:
        taken_at = datetime.strptime(entry['takenAt'], "%Y-%m-%dT%H:%M:%S.%fZ")
        location = entry.get('location')
        caption = entry.get('caption')
        
        # Prepare list of assets to process for this entry
        # Structure: (source_filename, role, is_video, is_placeholder)
        assets_to_process = []

        # Check Primary
        primary_data = entry.get('primary')
        if primary_data:
            path = primary_data['path']
            is_video = primary_data.get('mediaType') == 'video'
            assets_to_process.append({'path': path, 'role': 'primary', 'is_video': is_video, 'is_placeholder': False})
            
            # If Primary is video, check for Primary Placeholder (needed for combined image)
            if is_video and 'primaryPlaceholder' in entry:
                ph_path = entry['primaryPlaceholder']['path']
                assets_to_process.append({'path': ph_path, 'role': 'primary', 'is_video': False, 'is_placeholder': True})

        # Check Secondary
        secondary_data = entry.get('secondary')
        if secondary_data:
            path = secondary_data['path']
            is_video = secondary_data.get('mediaType') == 'video'
            assets_to_process.append({'path': path, 'role': 'secondary', 'is_video': is_video, 'is_placeholder': False})

            # If Secondary is video, check for Secondary Placeholder
            if is_video and 'secondaryPlaceholder' in entry:
                ph_path = entry['secondaryPlaceholder']['path']
                assets_to_process.append({'path': ph_path, 'role': 'secondary', 'is_video': False, 'is_placeholder': True})

        # Check BTS Media
        bts_data = entry.get('btsMedia')
        if bts_data:
            path = bts_data['path']
            is_video = bts_data.get('mediaType') == 'video'
            assets_to_process.append({'path': path, 'role': 'bts', 'is_video': is_video, 'is_placeholder': False})

        # Temporary holders for combined image generation
        current_entry_primary_img = None
        current_entry_secondary_img = None

        for asset in assets_to_process:
            filename = Path(asset['path']).name
            
            # Check locations
            src_path = photo_folder / filename
            if not src_path.exists():
                src_path = bereal_folder / filename
            
            if not src_path.exists():
                logging.warning(f"File not found: {filename}. Skipping.")
                skipped_files_count += 1
                continue

            logging.info(f"Found asset: {src_path.name} ({asset['role']})")

            # Determine new filename
            time_str = taken_at.strftime("%Y-%m-%dT%H-%M-%S")
            original_stem = src_path.stem
            
            # Logic:
            # If Video: Just Copy/Rename. No JPEG conversion.
            # If Image: Convert if requested.
            
            final_path = None
            processed_as_jpeg = False

            if asset['is_video']:
                # Handling Video
                extension = src_path.suffix
                if keep_original_filename == 'yes':
                    new_filename = f"{time_str}_{asset['role']}_{src_path.name}"
                else:
                    new_filename = f"{time_str}_{asset['role']}{extension}"
                
                final_path = output_folder / new_filename
                final_path = get_unique_filename(final_path)
                
                shutil.copy2(src_path, final_path)
                update_video_metadata(final_path, taken_at, location)
            
            else:
                # Handling Image (or Placeholder)
                converted_path = None
                converted = False
                
                if convert_to_jpeg == 'yes':
                    converted_path, converted = convert_webp_to_jpg(src_path)
                    if converted_path is None:
                        skipped_files_count += 1
                        continue
                    if converted: converted_files_count += 1
                    processed_as_jpeg = True

                # Determine output name
                if convert_to_jpeg == 'yes':
                    ext = '.jpg'
                    name_part = converted_path.name if keep_original_filename == 'yes' else f"{time_str}_{asset['role']}{ext}"
                    # If it is a placeholder, strictly append _placeholder to avoid conflict with video name if user kept orig filename
                    if asset['is_placeholder'] and keep_original_filename != 'yes':
                         name_part = f"{time_str}_{asset['role']}_placeholder{ext}"
                else:
                    ext = '.webp'
                    name_part = f"{time_str}_{asset['role']}_{original_stem}{ext}" if keep_original_filename == 'yes' else f"{time_str}_{asset['role']}{ext}"
                    if asset['is_placeholder'] and keep_original_filename != 'yes':
                         name_part = f"{time_str}_{asset['role']}_placeholder{ext}"

                # Additional check: If keeping original filename, and we have both video and placeholder, they might clash if extension differs but stem is same. 
                # Usually suffixes differ (.mp4 vs .webp).
                if keep_original_filename == 'yes':
                     # Append placeholder tag to filename if it is one, to distinguish from the main image/video if needed
                     if asset['is_placeholder']:
                         new_filename = f"{time_str}_{asset['role']}_placeholder_{src_path.name}"
                         if convert_to_jpeg == 'yes': new_filename = Path(new_filename).with_suffix('.jpg').name
                     else:
                         new_filename = f"{time_str}_{asset['role']}_{converted_path.name}" if convert_to_jpeg == 'yes' else f"{time_str}_{asset['role']}_{src_path.name}"
                else:
                    new_filename = name_part

                final_path = output_folder / new_filename
                final_path = get_unique_filename(final_path)

                if processed_as_jpeg and converted:
                    converted_path.rename(final_path)
                    update_exif(final_path, taken_at, location, caption)
                    # FIX: Pass Path object directly, function handles string conversion
                    update_iptc(final_path, caption)
                else:
                    shutil.copy2(src_path, final_path)

            processed_files_count += 1
            logging.info(f"Saved to {final_path.name}")

            # Store for Combination Logic
            # We want the 'primary' and 'secondary' IMAGES.
            # If the primary/secondary was a video, we rely on the placeholder we just processed.
            if asset['role'] == 'primary':
                if not asset['is_video'] and not asset['is_placeholder']:
                    # Standard Image BeReal
                    current_entry_primary_img = final_path
                elif asset['is_placeholder']:
                    # Video BeReal Placeholder
                    current_entry_primary_img = final_path
            
            if asset['role'] == 'secondary':
                if not asset['is_video'] and not asset['is_placeholder']:
                    current_entry_secondary_img = final_path
                elif asset['is_placeholder']:
                    current_entry_secondary_img = final_path

        # Add to global lists for combined generation if we found both parts
        if current_entry_primary_img and current_entry_secondary_img:
            primary_images.append({
                'path': current_entry_primary_img,
                'taken_at': taken_at,
                'location': location,
                'caption': caption
            })
            secondary_images.append(current_entry_secondary_img)
        
        print("")

    except Exception as e:
        logging.error(f"Error processing entry {entry}: {e}")

# Create combined images if user chose 'yes'
if create_combined_images == 'yes':
    output_folder_combined.mkdir(parents=True, exist_ok=True)

    for primary_path, secondary_path in zip(primary_images, secondary_images):
        primary_new_path = primary_path['path']
        primary_taken_at = primary_path['taken_at']
        primary_location = primary_path['location']
        primary_caption = primary_path['caption']

        timestamp_prefix = primary_new_path.name.split('_')[0]

        combined_filename = f"{timestamp_prefix}_combined.webp"
        
        try:
            combined_image = combine_images_with_resizing(primary_new_path, secondary_path)
            
            combined_image_path = output_folder_combined / (combined_filename)
            combined_image.save(combined_image_path, 'JPEG')
            combined_files_count += 1

            logging.info(f"Combined image saved: {combined_image_path.name}")

            update_exif(combined_image_path, primary_taken_at, primary_location, primary_caption)
            # FIX: Pass Path object directly
            update_iptc(combined_image_path, primary_caption)

            if convert_to_jpeg == 'yes':
                converted_path, converted = convert_webp_to_jpg(combined_image_path)
                if converted:
                    update_exif(converted_path, primary_taken_at, primary_location, primary_caption)
                    # FIX: Pass Path object directly
                    update_iptc(converted_path, primary_caption)
        except Exception as e:
            logging.error(f"Error creating combined image for {primary_new_path}: {e}")
        print("")

# Clean up backup files
print(STYLING['BOLD'] + "Removing backup files left behind by iptcinfo3" + STYLING["RESET"])
remove_backup_files(output_folder)
if create_combined_images == 'yes': remove_backup_files(output_folder_combined)
print("")

# Summary
logging.info(f"Finished processing.\nNumber of input-files: {number_of_files}\nTotal files processed: {processed_files_count}\nFiles converted: {converted_files_count}\nFiles skipped: {skipped_files_count}\nFiles combined: {combined_files_count}")