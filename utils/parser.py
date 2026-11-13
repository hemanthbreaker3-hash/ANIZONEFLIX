import re

def parse_filename(filename):
    """
    Intelligently extracts metadata from filename/caption.
    Returns: { season, episode, quality, audio, codec, title }
    """
    data = {
        "season": 1,
        "episode": 1,
        "quality": "HD",
        "audio": "Japanese",
        "codec": "H.264",
        "title": filename
    }

    # Extract Episode
    ep_match = re.search(r'(?:EP|Episode|E| -)\s*(\d+)', filename, re.IGNORECASE)
    if ep_match:
        data["episode"] = int(ep_match.group(1))
    else:
        # Fallback to loose digit if it looks like an episode
        ep_match_alt = re.search(r'\s+(\d{1,3})(?:\s+|\[|\()', filename)
        if ep_match_alt:
            data["episode"] = int(ep_match_alt.group(1))

    # Extract Season
    s_match = re.search(r'(?:S|Season)\s*(\d+)', filename, re.IGNORECASE)
    if s_match:
        data["season"] = int(s_match.group(1))

    # Extract Quality
    if re.search(r'480p', filename): data["quality"] = "480p"
    elif re.search(r'720p', filename): data["quality"] = "720p"
    elif re.search(r'1080p', filename): data["quality"] = "1080p"
    elif re.search(r'2160p|4K', filename, re.IGNORECASE): data["quality"] = "4K"

    # Extract Audio
    if re.search(r'Dual|Multi', filename, re.IGNORECASE): data["audio"] = "Multi-Audio"
    elif re.search(r'Hindi|Hin', filename, re.IGNORECASE): data["audio"] = "Hindi"
    elif re.search(r'English|Eng', filename, re.IGNORECASE): data["audio"] = "English"

    # Extract Codec
    if re.search(r'HEVC|x265|H\.265', filename, re.IGNORECASE): data["codec"] = "HEVC"
    elif re.search(r'AVC|x264|H\.264', filename, re.IGNORECASE): data["codec"] = "AVC"

    # Attempt to clean title
    t = filename
    # Remove leading tags like [SubsPlease]
    t = re.sub(r'^\[.*?\]\s*', '', t)
    # Split by common markers
    clean_title = re.split(r'S\d+|E\d+|EP\d+|Episode\s*\d+|\[|\(|\d{3,4}p| - ', t, flags=re.IGNORECASE)[0]
    data["title"] = clean_title.strip(" .-_")

    return data


def parse_ultra_advanced_group(text: str):
    """
    Parses the Ultra Advanced Group input message.
    Returns: (parsed_boxes, errors)
    parsed_boxes: list of dicts:
       [
          {
             "box_name": "Naruto",
             "roman": "I",
             "groups": [
                {
                   "group_name": "Quality",
                   "buttons": { "480P": "https://example.com/480", ... }
                }
             ]
          }
       ]
    errors: list of error strings
    """
    if not text or not text.strip():
        return [], ["Empty message received."]

    lines = text.split("\n")
    parsed_boxes = []
    errors = []

    current_box = None
    current_group = None

    # Roman numeral regex matching "I. BOX NAME: Naruto" (case insensitive)
    roman_pattern = re.compile(r"^\s*([MDCLXVI]+)\s*\.\s*(BOX\s+NAME\s*:\s*)(.*)$", re.IGNORECASE)
    # Group pattern matching serial numbers "1. Quality"
    group_pattern = re.compile(r"^\s*(\d+)\s*\.\s*(.*)$")

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue

        # Check if it is a Box line
        roman_match = roman_pattern.match(stripped)
        if roman_match:
            roman_num = roman_match.group(1).upper()
            box_name_part = roman_match.group(3).strip()
            if not box_name_part:
                errors.append(f"Line {idx}: Box name is empty for section '{roman_num}.'")
                continue

            # Start a new box
            current_box = {
                "box_name": box_name_part,
                "roman": roman_num,
                "groups": []
            }
            parsed_boxes.append(current_box)
            current_group = None
            continue

        # Check if it is a Group line
        group_match = group_pattern.match(stripped)
        if group_match:
            serial = group_match.group(1)
            group_name = group_match.group(2).strip()
            if not group_name:
                errors.append(f"Line {idx}: Group name is empty for serial '{serial}.'")
                continue

            if not current_box:
                errors.append(f"Line {idx}: Group '{group_name}' defined before any Box Name section.")
                continue

            current_group = {
                "group_name": group_name,
                "buttons": {}
            }
            current_box["groups"].append(current_group)
            continue

        # Must be a Button line
        if ":" in stripped:
            if not current_group:
                errors.append(f"Line {idx}: Button line '{stripped}' found before any Group was defined.")
                continue

            button_name, button_link = stripped.split(":", 1)
            button_name = button_name.strip()
            button_link = button_link.strip()

            if not button_name:
                errors.append(f"Line {idx}: Button name is empty in line '{stripped}'.")
                continue

            if not button_link:
                errors.append(f"Line {idx}: Missing link/URL in button line. Expected: '{button_name} : URL'")
                continue

            if not (button_link.startswith("http://") or button_link.startswith("https://")):
                errors.append(f"Line {idx}: Invalid URL protocol in '{button_link}'. URL must start with http:// or https://")
                continue

            current_group["buttons"][button_name] = button_link
        else:
            if current_group:
                errors.append(f"Line {idx}: Invalid button:\n\"{stripped}\"\nExpected:\n\"{stripped} : https://example.com/link\"")
            else:
                errors.append(f"Line {idx}: Unknown or malformed line format: '{stripped}'")

    # Final sanity check: make sure every box has groups, and every group has buttons
    for box in parsed_boxes:
        if not box["groups"]:
            errors.append(f"Box '{box['box_name']}' has no groups defined.")
        for grp in box["groups"]:
            if not grp["buttons"]:
                errors.append(f"Group '{grp['group_name']}' under box '{box['box_name']}' has no buttons.")

    return parsed_boxes, errors


def parse_bulk_box_names(text: str):
    """
    Parses the Bulk Box Creation input message.
    Returns: (parsed_box_names, errors)
    """
    if not text or not text.strip():
        return [], ["Empty message received."]

    lines = text.split("\n")
    box_names = []
    errors = []
    seen = set()

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue

        match = re.match(r"^\s*\d+\s*\.\s*(.*)$", stripped)
        if match:
            box_name = match.group(1).strip()
            if not box_name:
                errors.append(f"Line {idx}: Box name is empty.")
                continue

            if box_name.lower() in seen:
                continue
            seen.add(box_name.lower())
            box_names.append(box_name)
        else:
            errors.append(f"Line {idx}: Invalid format. Expected: 'Number. Box Name'")

    return box_names, errors
