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
import struct

# --- MOVIEPY IMPORT BLOCK ---
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
        if record.levelno == logging.INFO and "Finished processing" not in str(record.msg):
            message = STYLING["GREEN"] + super().format(record) + STYLING["RESET"]
        elif record.levelno == logging.ERROR:
            message = STYLING["RED"] + super().format(record) + STYLING["RESET"]
        elif record.levelno == logging.WARNING:
            message = STYLING["BLUE"] + super().format(record) + STYLING["RESET"]
        elif "Finished processing" in str(record.msg):
            message = STYLING["BLUE"] + STYLING["BOLD"] + super().format(record) + STYLING["RESET"]
        else:
            message = super().format(record)
        return message


# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColorFormatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

# --- MOTION PHOTO CONSTANTS & CLASS (Replicating MotionPhoto2) ---
# - Exact XMP template from constants.py
MOTION_PHOTO_XMP_TEMPLATE = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.1.0-jc003">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
        xmlns:Container="http://ns.google.com/photos/1.0/container/"
        xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
      GCamera:MotionPhoto="1"
      GCamera:MotionPhotoVersion="1"
      GCamera:MotionPhotoPresentationTimestampUs="-1">
      <Container:Directory>
        <rdf:Seq>
          <rdf:li rdf:parseType="Resource">
            <Container:Item
              Item:Mime="image/jpeg"
              Item:Semantic="Primary"
              Item:Length="0"
              Item:Padding="{padding}"/>
          </rdf:li>
          <rdf:li rdf:parseType="Resource">
            <Container:Item
              Item:Mime="video/mp4"
              Item:Semantic="MotionPhoto"
              Item:Length="{length}"
              Item:Padding="0"/>
          </rdf:li>
        </rdf:Seq>
      </Container:Directory>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>"""

SAMSUNG_TAG_IDS = {
    "MotionPhoto_Data": bytes([0x00, 0x00, 0x30, 0x0a]),
    "MotionPhoto_Version": bytes([0x00, 0x00, 0x31, 0x0a])
}
SAMSUNG_SEFH_VERSION = 107


# - Logic from SamsungTags.py
class SamsungTags:
    def __init__(self, video_bytes: bytes):
        self.video_bytes = video_bytes
        self.video_size = len(video_bytes)
        self.tags = {"MotionPhoto_Version": bytes("mpv3", "utf-8")}
        self.tags["MotionPhoto_Data"] = self.video_bytes

    def set_image_size(self, image_size: int):
        self.image_size = image_size

    def get_image_padding(self) -> int:
        size = 0
        for tag in SAMSUNG_TAG_IDS:
            if tag in self.tags:
                size += len(SAMSUNG_TAG_IDS[tag])
                size += 4
                size += len(tag)
                if tag == "MotionPhoto_Data": return size
                size += len(self.tags[tag])
        return -1

    def get_video_size(self) -> int:
        return len(self.video_footer()) - self.get_image_padding()

    def video_footer(self) -> bytes:
        tag_data = b''
        tag_offsets = {}
        tag_lengths = {}

        for tag in SAMSUNG_TAG_IDS:
            if tag in self.tags:
                tag_bytes = SAMSUNG_TAG_IDS[tag]
                tag_bytes += struct.pack("<i", len(tag))
                tag_bytes += bytes(tag, "utf-8")
                tag_bytes += self.tags[tag]
                tag_data += tag_bytes
                tag_length = len(tag_bytes)
                tag_lengths[tag] = tag_length
                for preceding_tag in SAMSUNG_TAG_IDS:
                    if preceding_tag in self.tags:
                        tag_offsets[preceding_tag] = tag_length + (tag_offsets.get(preceding_tag, 0))
                        if preceding_tag == tag: break

        sefh = b''
        sefh += bytes("SEFH", "utf-8")
        sefh += struct.pack("<i", SAMSUNG_SEFH_VERSION)
        sefh += struct.pack("<i", len(self.tags))
        for tag in SAMSUNG_TAG_IDS:
            if tag in self.tags:
                sefh += SAMSUNG_TAG_IDS[tag]
                sefh += struct.pack("<i", tag_offsets[tag])
                sefh += struct.pack("<i", tag_lengths[tag])
        sefh_len = len(sefh)
        sefh += struct.pack("<i", sefh_len)
        sefh += bytes("SEFT", "utf-8")

        result = tag_data + sefh
        return result


# --- END MOTION PHOTO CONSTANTS ---

# Initialize counters
processed_files_count = 0
converted_files_count = 0
combined_files_count = 0
skipped_files_count = 0

source_app = "BeReal app"
processing_tool = "github/bereal-gdpr-photo-toolkit"
primary_assets = []
secondary_assets = []

# Define paths
photo_folder = Path('Photos/post/')
bereal_folder = Path('Photos/bereal')
output_folder = Path('Photos/post/__processed')
output_folder_combined = Path('Photos/post/__combined')
output_folder_combined_reversed = Path('Photos/post/__combined_reversed')

output_folder.mkdir(parents=True, exist_ok=True)
output_folder_combined.mkdir(parents=True, exist_ok=True)
output_folder_combined_reversed.mkdir(parents=True, exist_ok=True)

print(STYLING["BOLD"] + "\nThe following paths are set:" + STYLING["RESET"])
print(f"Photo folder: {photo_folder}")
if os.path.exists(bereal_folder):
    print(f"Older photo folder: {bereal_folder}")
print(f"Output folder for singular images: {output_folder}")
print(f"Output folder for combined images: {output_folder_combined}")
print(f"Output folder for reversed combined images: {output_folder_combined_reversed}")
print("")


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
    logging.warning("Video combining skipped because MoviePy failed to load.")


# --- HELPER: INPUT WITH DEFAULT ---
def ask_setting(prompt, default_val):
    default_str = "yes" if default_val == 'yes' else "no"
    full_prompt = f"{prompt} (yes/no) [Default: {STYLING['BOLD']}{default_str}{STYLING['RESET']}]: "
    while True:
        val = input(full_prompt).strip().lower()
        if val == "": return default_val
        if val in ['yes', 'no']: return val
        print(f"Invalid input. Please enter 'yes' or 'no', or press Enter for default.")


# --- MENU SETTINGS ---
print(STYLING["BOLD"] + "\nDo you want to access advanced settings or run with default settings?" + STYLING["RESET"])
print("Default settings are:\n"
      "1. Copied images are converted from WebP to JPEG\n"
      "2. Converted images' filenames do not contain the original filename\n"
      "3. Combined images are created\n"
      "4. Reversed combined images are NOT created\n"
      "5. Motion Photos (Live Photos) are NOT created\n"
      "6. WebP files in combined folders are preserved\n"
      "7. Debug logging is OFF")

advanced_settings = input("\nEnter " + STYLING["BOLD"] + "'yes'" + STYLING[
    "RESET"] + " for advanced settings or press any key to continue with default settings: ").strip().lower()

# Defaults
convert_to_jpeg = 'yes'
keep_original_filename = 'no'
create_combined_images = 'yes'
create_reversed_combined = 'no'
create_motion_photos = 'no'
delete_combined_webp = 'no'
debug_logging = 'no'

if advanced_settings == 'yes':
    print(STYLING["BOLD"] + "\n--- Advanced Configuration ---" + STYLING["RESET"])

    # 1. Convert to JPEG
    convert_to_jpeg = ask_setting("1. Convert images from WebP to JPEG?", 'yes')

    # 2. Filename
    print("\n2. Naming Options:\n"
          "   Option 1: YYYY-MM-DD...original-filename.jpeg (Answer: YES)\n"
          "   Option 2: YYYY-MM-DD...primary/secondary.jpeg (Answer: NO)")
    keep_original_filename = ask_setting("   Keep original filename in output?", 'no')

    # 3. Create Combined
    create_combined_images = ask_setting("\n3. Create combined images (Standard View)?", 'yes')

    if create_combined_images == 'yes':
        # 4. Reversed
        create_reversed_combined = ask_setting("4. Also create reversed combined images (Secondary as background)?",
                                               'no')

        # 5. Motion Photos
        if convert_to_jpeg == 'yes':
            create_motion_photos = ask_setting("5. Create Motion Photos (Live Photos) using BTS video?", 'no')
            # 6. Cleanup
            delete_combined_webp = ask_setting("6. Delete intermediate .webp files in combined folders?", 'no')
        else:
            print(STYLING["BLUE"] + "   (Skipping Motion Photo & WebP cleanup options: JPEG conversion required)" +
                  STYLING["RESET"])

    # 7. Debug Log
    debug_logging = ask_setting("\n7. Enable debug logging to file ('debug_log.txt')?", 'no')

# --- ACTIVATE FILE LOGGING ---
if debug_logging == 'yes':
    try:
        file_handler = logging.FileHandler("debug_log.txt", mode='w', encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
        logging.info("Debug logging enabled.")
    except Exception as e:
        print(f"Failed to setup log file: {e}")

if convert_to_jpeg == 'no' and create_combined_images == 'no':
    print("Script will continue in 5 seconds.")


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
        if location and 'latitude' in location:
            exif_dict['GPS'] = {
                piexif.GPSIFD.GPSLatitudeRef: 'N' if location['latitude'] >= 0 else 'S',
                piexif.GPSIFD.GPSLatitude: _convert_to_degrees(abs(location['latitude'])),
                piexif.GPSIFD.GPSLongitudeRef: 'E' if location['longitude'] >= 0 else 'W',
                piexif.GPSIFD.GPSLongitude: _convert_to_degrees(abs(location['longitude'])),
            }
        if caption:
            exif_dict['0th'][piexif.ImageIFD.ImageDescription] = caption.encode('utf-8')
        piexif.insert(piexif.dump(exif_dict), image_path.as_posix())
    except Exception as e:
        logging.error(f"Failed to update EXIF: {e}")


def update_iptc(image_path, caption):
    try:
        path_str = str(image_path)
        info = IPTCInfo(path_str, force=True)
        if not hasattr(info, '_markers'): info._markers = []
        if caption: info['caption/abstract'] = caption
        info['source'] = source_app
        info['originating program'] = processing_tool
        info.save_as(path_str)
    except Exception as e:
        logging.error(f"Failed to update IPTC: {e}")


def update_video_metadata(video_path, datetime_original, location=None):
    try:
        timestamp = datetime_original.timestamp()
        os.utime(video_path, (timestamp, timestamp))
        try:
            date_str = datetime_original.strftime("%Y:%m:%d %H:%M:%S")
            cmd = ["exiftool", "-overwrite_original", f"-CreateDate={date_str}", f"-DateTimeOriginal={date_str}",
                   str(video_path)]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    if not MOVIEPY_AVAILABLE: return False
    try:
        primary_clip = VideoFileClip(str(primary_path))
        secondary_clip = VideoFileClip(str(secondary_path))

        def fix_dimensions(clip, meta):
            if meta and meta.get('width') and meta.get('height'):
                target_w, target_h = meta['width'], meta['height']
                if abs(clip.w - target_w) > 2: return clip.resize(newsize=(target_w, target_h))
            return clip

        primary_clip = fix_dimensions(primary_clip, primary_meta)
        secondary_clip = fix_dimensions(secondary_clip, secondary_meta)

        corner_radius, outline_size, position, scale_factor = 60, 7, (55, 55), 1 / 3.33333333
        secondary_clip = secondary_clip.resize(scale_factor)
        sec_w, sec_h = secondary_clip.size
        mask_img = Image.new('L', (sec_w, sec_h), 0)
        ImageDraw.Draw(mask_img).rounded_rectangle((0, 0, sec_w, sec_h), corner_radius, fill=255)
        secondary_clip = secondary_clip.set_mask(ImageClip(np.array(mask_img) / 255.0, ismask=True))

        border_w, border_h = sec_w + (outline_size * 2), sec_h + (outline_size * 2)
        border_mask = Image.new('L', (border_w, border_h), 0)
        ImageDraw.Draw(border_mask).rounded_rectangle((0, 0, border_w, border_h), corner_radius + outline_size,
                                                      fill=255)
        border_clip = ColorClip(size=(border_w, border_h), color=(0, 0, 0)).set_mask(
            ImageClip(np.array(border_mask) / 255.0, ismask=True))

        final = CompositeVideoClip(
            [primary_clip, border_clip.set_position((position[0] - outline_size, position[1] - outline_size)),
             secondary_clip.set_position(position)]).set_duration(primary_clip.duration)
        final.write_videofile(str(output_path), codec='libx264', audio_codec='aac', verbose=False, logger=None)
        primary_clip.close();
        secondary_clip.close()
        return True
    except Exception as e:
        logging.error(f"Error combining videos: {e}")
        return False


# --- REPLICATED MOTION PHOTO FUNCTION (Muxer.py Logic) ---
def create_motion_photo(image_path, video_path):
    """
    Muxes an image and a video using the exact MotionPhoto2 method:
    1. Calculate Padding/Length tags.
    2. Write XMP sidecar.
    3. Copy image to temp file (prevents locking).
    4. ExifTool copies XMP to temp file.
    5. Read temp file, append video/footer, write to original.
    """
    try:
        # 1. Read Video Bytes & Init Tags
        with open(video_path, "rb") as f:
            video_bytes = f.read()

        tags = SamsungTags(video_bytes)
        video_size = tags.get_video_size()
        image_padding = tags.get_image_padding()

        # 2. Prepare XMP Sidecar with correct values
        # Replaces placeholders in the constant template
        xmp_content = MOTION_PHOTO_XMP_TEMPLATE.replace('Item:Length="0"', f'Item:Length="{video_size}"')
        xmp_content = xmp_content.replace('Item:Padding="{padding}"', f'Item:Padding="{image_padding}"')
        xmp_content = xmp_content.replace('Item:Length="{length}"', f'Item:Length="{video_size}"')

        # Write XMP to temp file
        xmp_path = image_path.with_suffix('.xmp_temp')
        with open(xmp_path, "w", encoding="utf-8") as f:
            f.write(xmp_content)

        # 3. Work on a Temporary Image File (Muxer.py workflow)
        temp_image_path = image_path.with_name(f"temp_{image_path.name}")
        shutil.copy2(image_path, temp_image_path)

        # 4. Run ExifTool on Temp Image
        # Muxer.py uses -tagsfromfile XMP -xmp IMAGE
        cmd = [
            "exiftool",
            "-overwrite_original",
            "-tagsfromfile", str(xmp_path),
            "-xmp",
            str(temp_image_path)
        ]

        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)

        # Clean up sidecar immediately
        if xmp_path.exists(): xmp_path.unlink()

        if result.returncode != 0:
            logging.error(f"ExifTool failed: {result.stderr.decode('utf-8')}")
            if temp_image_path.exists(): temp_image_path.unlink()
            return False

        # 5. Read Injected Image & Append Video
        with open(temp_image_path, "rb") as f:
            image_bytes = f.read()

        # Cleanup temp image
        if temp_image_path.exists(): temp_image_path.unlink()

        tags.set_image_size(len(image_bytes))
        footer = tags.video_footer()

        # Write Final Result (Overwrite Original)
        with open(image_path, "wb") as f:
            f.write(image_bytes)
            f.write(footer)

        logging.info(f"Successfully muxed Motion Photo: {image_path.name}")
        return True

    except Exception as e:
        logging.error(f"Failed to create Motion Photo for {image_path.name}: {e}")
        # Clean up in case of error
        if 'xmp_path' in locals() and xmp_path.exists(): xmp_path.unlink()
        if 'temp_image_path' in locals() and temp_image_path.exists(): temp_image_path.unlink()
        return False


# --- MAIN PROCESSING LOOP ---
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

        entry_assets = {'primary': None, 'secondary': None, 'bts': None}
        assets_to_process = []

        # EXTRACT METADATA
        if 'primary' in entry:
            p = entry['primary']
            assets_to_process.append({'path': p['path'], 'role': 'primary', 'type': p['mediaType'],
                                      'dims': {'width': p.get('width'), 'height': p.get('height')}})
        if 'secondary' in entry:
            s = entry['secondary']
            assets_to_process.append({'path': s['path'], 'role': 'secondary', 'type': s['mediaType'],
                                      'dims': {'width': s.get('width'), 'height': s.get('height')}})
        if 'btsMedia' in entry:
            b = entry['btsMedia']
            assets_to_process.append({'path': b['path'], 'role': 'bts', 'type': b['mediaType'],
                                      'dims': {'width': b.get('width'), 'height': b.get('height')}})

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

            logging.info(f"Found {role}: {src_path.name}")
            time_str = taken_at.strftime("%Y-%m-%dT%H-%M-%S")
            processed_path = None

            if is_video:
                ext = src_path.suffix
                new_name = f"{time_str}_{role}{ext}" if keep_original_filename != 'yes' else f"{time_str}_{role}_{src_path.name}"
                dest = get_unique_filename(output_folder / new_name)
                shutil.copy2(src_path, dest)
                update_video_metadata(dest, taken_at, location)
                processed_path = dest
            else:
                final_ext = '.jpg' if convert_to_jpeg == 'yes' else '.webp'
                new_name = f"{time_str}_{role}{final_ext}" if keep_original_filename != 'yes' else f"{time_str}_{role}_{src_path.name}"
                dest = get_unique_filename(output_folder / new_name)

                did_convert = False
                if convert_to_jpeg == 'yes' and src_path.suffix.lower() == '.webp':
                    conv_res, success = convert_webp_to_jpg(src_path)
                    if success:
                        src_path = conv_res
                        did_convert = True

                if did_convert:
                    src_path.rename(dest)
                    update_exif(dest, taken_at, location, caption)
                    update_iptc(dest, caption)
                else:
                    shutil.copy2(src_path, dest)
                processed_path = dest

            processed_files_count += 1
            entry_assets[role] = {'path': processed_path, 'is_video': is_video, 'dims': asset['dims']}

        if entry_assets['primary'] and entry_assets['secondary']:
            primary_assets.append({
                'data': entry_assets['primary'],
                'metadata': {'taken_at': taken_at, 'location': location, 'caption': caption},
                'bts': entry_assets['bts']
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
        bts_info = prim.get('bts')

        s_path = sec['path']
        s_is_video = sec['is_video']

        meta = prim['metadata']
        timestamp = p_path.name.split('_')[0]

        try:
            # 1. STANDARD COMBINATION
            if p_is_video and s_is_video:
                combined_filename = f"{timestamp}_combined.mp4"
                combined_path = output_folder_combined / combined_filename
                success = combine_videos(p_path, s_path, combined_path, prim['data']['dims'], sec['dims'])
                if success:
                    update_video_metadata(combined_path, meta['taken_at'], meta['location'])

            elif not p_is_video and not s_is_video:
                ext = '.jpg' if convert_to_jpeg == 'yes' else '.webp'
                combined_filename = f"{timestamp}_combined{ext}"
                combined_path = output_folder_combined / combined_filename

                img = combine_images(p_path, s_path)
                img.save(combined_path, 'JPEG' if convert_to_jpeg == 'yes' else 'WEBP', quality=90)
                combined_files_count += 1
                logging.info(f"Combined image saved: {combined_path.name}")
                update_exif(combined_path, meta['taken_at'], meta['location'], meta['caption'])
                update_iptc(combined_path, meta['caption'])

                # --- MOTION PHOTO CREATION ---
                if create_motion_photos == 'yes' and bts_info and bts_info['path'] and convert_to_jpeg == 'yes':
                    logging.info(f"Attempting Motion Photo creation for {combined_path.name}...")
                    create_motion_photo(combined_path, bts_info['path'])
                # -----------------------------

            # 2. REVERSED COMBINATION
            if create_reversed_combined == 'yes':
                if p_is_video and s_is_video:
                    combined_filename_rev = f"{timestamp}_combined_reversed.mp4"
                    combined_path_rev = output_folder_combined_reversed / combined_filename_rev
                    combine_videos(s_path, p_path, combined_path_rev, sec['dims'], prim['data']['dims'])
                    update_video_metadata(combined_path_rev, meta['taken_at'], meta['location'])

                elif not p_is_video and not s_is_video:
                    ext = '.jpg' if convert_to_jpeg == 'yes' else '.webp'
                    combined_filename_rev = f"{timestamp}_combined_reversed{ext}"
                    combined_path_rev = output_folder_combined_reversed / combined_filename_rev

                    img = combine_images(s_path, p_path)
                    img.save(combined_path_rev, 'JPEG' if convert_to_jpeg == 'yes' else 'WEBP', quality=90)
                    update_exif(combined_path_rev, meta['taken_at'], meta['location'], meta['caption'])
                    update_iptc(combined_path_rev, meta['caption'])

            print("")
        except Exception as e:
            logging.error(f"Error creating combined memory: {e}")

remove_backup_files(output_folder)
remove_backup_files(output_folder_combined)
remove_backup_files(output_folder_combined_reversed)

if delete_combined_webp == 'yes' and convert_to_jpeg == 'yes':
    print(STYLING['BOLD'] + "Cleaning up WebP..." + STYLING['RESET'])
    for folder in [output_folder_combined, output_folder_combined_reversed]:
        for webp_file in folder.glob('*.webp'):
            try:
                webp_file.unlink()
            except:
                pass

logging.info(f"Finished processing. Total processed: {processed_files_count}")