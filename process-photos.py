import json
from datetime import datetime
from PIL import Image, ImageDraw
import logging
from pathlib import Path
import piexif
import os
import shutil
import subprocess
from iptcinfo3 import IPTCInfo
import numpy as np

# --- UPDATED MOVIEPY IMPORT BLOCK ---
MOVIEPY_ERROR = None
try:
    from moviepy.editor import VideoFileClip, CompositeVideoClip, ImageClip, ColorClip

    MOVIEPY_AVAILABLE = True
except Exception as e:
    MOVIEPY_AVAILABLE = False
    MOVIEPY_ERROR = e
# ------------------------------------

# ANSI escape codes for text styling
STYLING = {
    "GREEN": "\033[92m",
    "RED": "\033[91m",
    "BLUE": "\033[94m",
    "BOLD": "\033[1m",
    "RESET": "\033[0m",
}


# Setup log styling
class ColorFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        if record.levelno == logging.INFO and "Finished processing" not in record.msg:
            message = STYLING["GREEN"] + message + STYLING["RESET"]
        elif record.levelno == logging.ERROR:
            message = STYLING["RED"] + message + STYLING["RESET"]
        elif "Finished processing" in record.msg:
            message = STYLING["BLUE"] + STYLING["BOLD"] + message + STYLING["RESET"]
        return message


# Setup logging with styling
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
handler = logger.handlers[0]
handler.setFormatter(ColorFormatter('%(asctime)s - %(levelname)s - %(message)s'))

# Initialize counters
processed_files_count = 0
converted_files_count = 0
combined_files_count = 0
skipped_files_count = 0

# Static IPTC tags
source_app = "BeReal app"
processing_tool = "github/bereal-gdpr-photo-toolkit"

# Define lists to hold the paths of assets to be combined
primary_assets = []
secondary_assets = []

# Define paths
photo_folder = Path('Photos/post/')
bereal_folder = Path('Photos/bereal')
output_folder = Path('Photos/post/__processed')
output_folder_combined = Path('Photos/post/__combined')
output_folder.mkdir(parents=True, exist_ok=True)
output_folder_combined.mkdir(parents=True, exist_ok=True)

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
    return len(list(folder.glob('*.webp'))) + len(list(folder.glob('*.mp4')))


number_of_files = count_files_in_folder(photo_folder)
print(f"Number of files in {photo_folder}: {number_of_files}")

if os.path.exists(bereal_folder):
    number_of_files_old = count_files_in_folder(bereal_folder)
    print(f"Number of (older) files in {bereal_folder}: {number_of_files_old}")
    number_of_files += number_of_files_old

if not MOVIEPY_AVAILABLE:
    logging.warning(STYLING["RED"] + "Video combining skipped because MoviePy failed to load." + STYLING["RESET"])
    logging.warning(STYLING["RED"] + f"Error details: {MOVIEPY_ERROR}" + STYLING["RESET"])

# Settings
print(STYLING["BOLD"] + "\nDo you want to access advanced settings or run with default settings?" + STYLING["RESET"])
print("Default settings are:\n"
      "1. Copied images are converted from WebP to JPEG\n"
      "2. Converted images' filenames do not contain the original filename\n"
      "3. Combined images are created on top of converted, singular images")
advanced_settings = input("\nEnter " + STYLING["BOLD"] + "'yes'" + STYLING[
    "RESET"] + " for advanced settings or press any key to continue with default settings: ").strip().lower()

if advanced_settings != 'yes':
    print("Continuing with default settings.\n")

convert_to_jpeg = 'yes'
keep_original_filename = 'no'
create_combined_images = 'yes'

if advanced_settings == 'yes':
    convert_to_jpeg = None
    while convert_to_jpeg not in ['yes', 'no']:
        convert_to_jpeg = input(
            STYLING["BOLD"] + "\n1. Do you want to convert images from WebP to JPEG? (yes/no): " + STYLING[
                "RESET"]).strip().lower()
        if convert_to_jpeg == 'no':
            print("Your images will not be converted.")
        if convert_to_jpeg not in ['yes', 'no']:
            logging.error("Invalid input. Please enter 'yes' or 'no'.")

    print(STYLING["BOLD"] + "\n2. There are two options for how output files can be named" + STYLING["RESET"] + "\n"
                                                                                                                "Option 1: YYYY-MM-DDTHH-MM-SS_primary/secondary_original-filename.jpeg\n"
                                                                                                                "Option 2: YYYY-MM-DDTHH-MM-SS_primary/secondary.jpeg\n"
                                                                                                                "This will only influence the naming scheme of singular images.")
    keep_original_filename = None
    while keep_original_filename not in ['yes', 'no']:
        keep_original_filename = input(
            STYLING["BOLD"] + "Do you want to keep the original filename in the renamed file? (yes/no): " + STYLING[
                "RESET"]).strip().lower()
        if keep_original_filename not in ['yes', 'no']:
            logging.error("Invalid input. Please enter 'yes' or 'no'.")

    create_combined_images = None
    while create_combined_images not in ['yes', 'no']:
        create_combined_images = input(STYLING[
                                           "BOLD"] + "\n3. Do you want to create combined images like the original BeReal memories? (yes/no): " +
                                       STYLING["RESET"]).strip().lower()
        if create_combined_images not in ['yes', 'no']:
            logging.error("Invalid input. Please enter 'yes' or 'no'.")

if convert_to_jpeg == 'no' and create_combined_images == 'no':
    print("Script will continue to run in 5 seconds.")


# --- HELPER FUNCTIONS ---

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


def _convert_to_degrees(value):
    d = int(value)
    m = int((value - d) * 60)
    s = (value - d - m / 60) * 3600.00
    return ((d, 1), (m, 1), (int(s * 100), 100))


def update_exif(image_path, datetime_original, location=None, caption=None):
    try:
        exif_dict = piexif.load(image_path.as_posix())
        if '0th' not in exif_dict: exif_dict['0th'] = {}
        if 'Exif' not in exif_dict: exif_dict['Exif'] = {}

        exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = datetime_original.strftime("%Y:%m:%d %H:%M:%S")
        logging.info(f"Found datetime: {datetime_original}")
        logging.info(f"Added capture date and time.")

        if location and 'latitude' in location:
            logging.info(f"Found location: {location}")
            exif_dict['GPS'] = {
                piexif.GPSIFD.GPSLatitudeRef: 'N' if location['latitude'] >= 0 else 'S',
                piexif.GPSIFD.GPSLatitude: _convert_to_degrees(abs(location['latitude'])),
                piexif.GPSIFD.GPSLongitudeRef: 'E' if location['longitude'] >= 0 else 'W',
                piexif.GPSIFD.GPSLongitude: _convert_to_degrees(abs(location['longitude'])),
            }
            logging.info(f"Added GPS location.")

        if caption:
            logging.info(f"Found caption: {caption}")
            exif_dict['0th'][piexif.ImageIFD.ImageDescription] = caption.encode('utf-8')
            logging.info(f"Updated title with caption.")

        piexif.insert(piexif.dump(exif_dict), image_path.as_posix())
        logging.info(f"Updated EXIF data for {image_path}.")
    except Exception as e:
        logging.error(f"Failed to update EXIF: {e}")


def update_iptc(image_path, caption):
    try:
        path_str = str(image_path)
        info = IPTCInfo(path_str, force=True)
        if not hasattr(info, '_markers'): info._markers = []
        if caption:
            info['caption/abstract'] = caption
            logging.info(f"Caption added to converted image.")
        info['source'] = source_app
        info['originating program'] = processing_tool
        info.save_as(path_str)
        logging.info(f"Updated IPTC data.")
    except Exception as e:
        logging.error(f"Failed to update IPTC: {e}")


def update_video_metadata(video_path, datetime_original, location=None):
    try:
        timestamp = datetime_original.timestamp()
        os.utime(video_path, (timestamp, timestamp))
        logging.info(f"Updated video file modification time.")
        try:
            date_str = datetime_original.strftime("%Y:%m:%d %H:%M:%S")
            cmd = ["exiftool", "-overwrite_original", f"-CreateDate={date_str}", f"-DateTimeOriginal={date_str}",
                   str(video_path)]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logging.info(f"Updated internal video metadata using ExifTool.")
        except:
            pass
    except Exception as e:
        logging.error(f"Failed to update video metadata: {e}")


def get_unique_filename(path):
    if not path.exists(): return path
    prefix, suffix = path.stem, path.suffix
    counter = 1
    while path.exists():
        path = path.with_name(f"{prefix}_{counter}{suffix}")
        counter += 1
    return path


def remove_backup_files(directory):
    for filename in os.listdir(directory):
        if filename.endswith('~'):
            try:
                os.remove(os.path.join(directory, filename))
                print(f"Removed backup file: {filename}")
            except:
                pass


# --- COMBINING FUNCTIONS ---

def combine_images(primary_path, secondary_path):
    corner_radius = 60
    outline_size = 7
    position = (55, 55)

    primary = Image.open(primary_path)
    secondary = Image.open(secondary_path)

    scale = 1 / 3.33333333
    w, h = secondary.size
    new_w, new_h = int(w * scale), int(h * scale)
    secondary = secondary.resize((new_w, new_h), Image.Resampling.LANCZOS)

    if secondary.mode != 'RGBA': secondary = secondary.convert('RGBA')

    mask = Image.new('L', (new_w, new_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, new_w, new_h), corner_radius, fill=255)
    secondary.putalpha(mask)

    combined = Image.new("RGB", primary.size)
    combined.paste(primary, (0, 0))

    outline_layer = Image.new('RGBA', combined.size, (0, 0, 0, 0))
    outline_box = [position[0] - outline_size, position[1] - outline_size, position[0] + new_w + outline_size,
                   position[1] + new_h + outline_size]
    ImageDraw.Draw(outline_layer).rounded_rectangle(outline_box, corner_radius + outline_size, fill=(0, 0, 0, 255))

    combined.paste(outline_layer, (0, 0), outline_layer)
    combined.paste(secondary, position, secondary)
    return combined


def combine_videos(primary_path, secondary_path, output_path, primary_meta=None, secondary_meta=None):
    if not MOVIEPY_AVAILABLE:
        logging.error("Cannot combine videos: MoviePy not installed.")
        return False

    try:
        logging.info(f"Combining videos: {primary_path.name}")

        primary_clip = VideoFileClip(str(primary_path))
        secondary_clip = VideoFileClip(str(secondary_path))

        # --- DIMENSION FIX (STRETCH/RESIZE) ---
        def fix_dimensions(clip, meta, name):
            if meta and meta.get('width') and meta.get('height'):
                target_w = meta['width']
                target_h = meta['height']

                # Check for mismatch (allow small rounding difference of 2px)
                if abs(clip.w - target_w) > 2 or abs(clip.h - target_h) > 2:
                    logging.info(f"Resizing {name} from {clip.w}x{clip.h} to {target_w}x{target_h} based on metadata.")
                    # We forcibly resize to the metadata dimensions to fix stretching
                    return clip.resize(newsize=(target_w, target_h))
            return clip

        primary_clip = fix_dimensions(primary_clip, primary_meta, "primary")
        secondary_clip = fix_dimensions(secondary_clip, secondary_meta, "secondary")
        # -------------------------------------

        # Parameters
        corner_radius = 60
        outline_size = 7
        position = (55, 55)
        scale_factor = 1 / 3.33333333

        # Resize secondary
        secondary_clip = secondary_clip.resize(scale_factor)
        sec_w, sec_h = secondary_clip.size

        # Mask
        mask_img = Image.new('L', (sec_w, sec_h), 0)
        ImageDraw.Draw(mask_img).rounded_rectangle((0, 0, sec_w, sec_h), corner_radius, fill=255)
        mask_arr = np.array(mask_img) / 255.0
        mask_clip = ImageClip(mask_arr, ismask=True)
        secondary_clip = secondary_clip.set_mask(mask_clip)

        # Border
        border_w = sec_w + (outline_size * 2)
        border_h = sec_h + (outline_size * 2)
        border_mask_img = Image.new('L', (border_w, border_h), 0)
        ImageDraw.Draw(border_mask_img).rounded_rectangle((0, 0, border_w, border_h), corner_radius + outline_size,
                                                          fill=255)
        border_mask_arr = np.array(border_mask_img) / 255.0
        border_clip = ColorClip(size=(border_w, border_h), color=(0, 0, 0)).set_mask(
            ImageClip(border_mask_arr, ismask=True))

        # Sync Duration
        duration = primary_clip.duration
        secondary_clip = secondary_clip.set_duration(duration)
        border_clip = border_clip.set_duration(duration)

        # Composite
        border_pos = (position[0] - outline_size, position[1] - outline_size)
        final = CompositeVideoClip([
            primary_clip,
            border_clip.set_position(border_pos),
            secondary_clip.set_position(position)
        ])

        final.write_videofile(str(output_path), codec='libx264', audio_codec='aac', verbose=False, logger=None)

        primary_clip.close()
        secondary_clip.close()
        return True
    except Exception as e:
        logging.error(f"Error combining videos: {e}")
        return False


# --- MAIN LOOP ---

try:
    with open('posts.json', encoding="utf8") as f:
        data = json.load(f)
except FileNotFoundError:
    logging.error("JSON file not found.")
    exit()

for entry in data:
    try:
        taken_at = datetime.strptime(entry['takenAt'], "%Y-%m-%dT%H:%M:%S.%fZ")
        location = entry.get('location')
        caption = entry.get('caption')

        entry_assets = {'primary': None, 'secondary': None}
        assets_to_process = []

        # EXTRACT METADATA AND DIMENSIONS
        if 'primary' in entry:
            p = entry['primary']
            assets_to_process.append({
                'path': p['path'],
                'role': 'primary',
                'type': p['mediaType'],
                'dims': {'width': p.get('width'), 'height': p.get('height')}
            })

        if 'secondary' in entry:
            s = entry['secondary']
            assets_to_process.append({
                'path': s['path'],
                'role': 'secondary',
                'type': s['mediaType'],
                'dims': {'width': s.get('width'), 'height': s.get('height')}
            })

        if 'btsMedia' in entry:
            b = entry['btsMedia']
            assets_to_process.append({
                'path': b['path'],
                'role': 'bts',
                'type': b['mediaType'],
                'dims': {'width': b.get('width'), 'height': b.get('height')}
            })

        for asset in assets_to_process:
            filename = Path(asset['path']).name
            role = asset['role']
            is_video = (asset['type'] == 'video')

            src_path = photo_folder / filename
            if not src_path.exists(): src_path = bereal_folder / filename

            if not src_path.exists():
                logging.warning(f"File not found: {filename}. Skipping.")
                skipped_files_count += 1
                continue

            logging.info(f"Found image: {src_path}")

            time_str = taken_at.strftime("%Y-%m-%dT%H-%M-%S")
            processed_path = None

            if is_video:
                # Video Processing
                ext = src_path.suffix
                new_name = f"{time_str}_{role}{ext}" if keep_original_filename != 'yes' else f"{time_str}_{role}_{src_path.name}"
                dest = get_unique_filename(output_folder / new_name)
                shutil.copy2(src_path, dest)
                update_video_metadata(dest, taken_at, location)
                processed_path = dest
            else:
                # Image Processing
                converted = False
                final_ext = '.jpg' if convert_to_jpeg == 'yes' else '.webp'

                if convert_to_jpeg == 'yes':
                    conv_res, success = convert_webp_to_jpg(src_path)
                    if success:
                        src_path = conv_res
                        converted = True

                new_name = f"{time_str}_{role}{final_ext}" if keep_original_filename != 'yes' else f"{time_str}_{role}_{src_path.name}"
                dest = get_unique_filename(output_folder / new_name)

                if converted:
                    src_path.rename(dest)
                    update_exif(dest, taken_at, location, caption)
                    update_iptc(dest, caption)
                else:
                    shutil.copy2(src_path, dest)
                processed_path = dest

            processed_files_count += 1
            logging.info(f"Sucessfully processed {role} image.")

            if role in ['primary', 'secondary']:
                entry_assets[role] = {
                    'path': processed_path,
                    'is_video': is_video,
                    'dims': asset['dims']
                }

        # Queue for Combination
        if entry_assets['primary'] and entry_assets['secondary']:
            primary_assets.append({
                'data': entry_assets['primary'],
                'metadata': {'taken_at': taken_at, 'location': location, 'caption': caption}
            })
            secondary_assets.append(entry_assets['secondary'])

        print("")

    except Exception as e:
        logging.error(f"Error processing entry {entry}: {e}")

# --- COMBINATION PHASE ---

if create_combined_images == 'yes':
    print(STYLING['BOLD'] + "Generating Combined Memories..." + STYLING['RESET'])

    for prim, sec in zip(primary_assets, secondary_assets):
        p_path = prim['data']['path']
        p_is_video = prim['data']['is_video']
        p_dims = prim['data']['dims']

        s_path = sec['path']
        s_is_video = sec['is_video']
        s_dims = sec['dims']

        meta = prim['metadata']
        timestamp = p_path.name.split('_')[0]

        try:
            if p_is_video and s_is_video:
                combined_filename = f"{timestamp}_combined.mp4"
                combined_path = output_folder_combined / combined_filename

                # PASS DIMENSIONS TO FIX STRETCHING
                success = combine_videos(p_path, s_path, combined_path,
                                         primary_meta=p_dims,
                                         secondary_meta=s_dims)

                if success:
                    combined_files_count += 1
                    logging.info(f"Combined image saved: {combined_path.name}")
                    update_video_metadata(combined_path, meta['taken_at'], meta['location'])
                    logging.info(f"Metadata added to combined image.")

            elif not p_is_video and not s_is_video:
                combined_filename = f"{timestamp}_combined.webp"
                combined_path = output_folder_combined / combined_filename

                img = combine_images(p_path, s_path)
                img.save(combined_path, 'JPEG' if convert_to_jpeg == 'yes' else 'WEBP', quality=90)

                combined_files_count += 1
                logging.info(f"Combined image saved: {combined_path.name}")

                update_exif(combined_path, meta['taken_at'], meta['location'], meta['caption'])
                logging.info(f"Metadata added to combined image.")
                update_iptc(combined_path, meta['caption'])

                if convert_to_jpeg == 'yes':
                    pass

            print("")
        except Exception as e:
            logging.error(f"Error creating combined memory for {p_path.name}: {e}")

remove_backup_files(output_folder)
remove_backup_files(output_folder_combined)

logging.info(
    f"Finished processing.\nNumber of input-files: {number_of_files}\nTotal files processed: {processed_files_count}\nFiles converted: {converted_files_count}\nFiles skipped: {skipped_files_count}\nFiles combined: {combined_files_count}")