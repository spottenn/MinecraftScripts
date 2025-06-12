"""
Finds NBT (Named Binary Tag) data within Minecraft Java Edition world saves.

This script searches for specific NBT tags (by name and/or value) across various
types of Minecraft data files:
- Player data files (.dat in playerdata/)
- Region files (.mca in region/) for items in block entities (e.g., chests)
- Entity files (.mca in entities/) for items carried by entities or dropped items
- Specified miscellaneous .dat files (e.g., chunks.dat, WorldUUID.dat)

Key Features:
- Configurable search criteria (tag name, tag value) via command-line arguments.
- Customizable world directory path.
- Output of findings to a CSV file.
- Extraction of coordinates and entity types where applicable.

Command-Line Usage Example:
    python find_nbt.py --world_dir path/to/MyWorld --name id --value minecraft:diamond --output_csv findings.csv

This script relies on the `python-nbt` library for NBT parsing.
"""
import os
import gzip
import re
import csv
import argparse
import time

# Library import fallback (remains as is, assuming nbt is primary)
try:
    # This block is mainly for testing/demonstrating library flexibility.
    # For this script's current state, nbt.nbt is the actively used library.
    import anvil
    NBTFile = anvil.NBTFile
    RegionFile = anvil.RegionFile
    TAG_Compound = anvil.TAG_Compound
    TAG_List = anvil.TAG_List
    MalformedFileError = anvil.MalformedFileError
    InconceivedChunk = getattr(anvil, 'InconceivedChunk', None)
    NBT_LIBRARY_USED = "anvil" # This would be set if anvil import succeeded
except (ImportError, AttributeError):
    from nbt.nbt import NBTFile, TAG_Compound, TAG_List, TAG_String, TAG_Int, TAG_Byte, TAG_Short, TAG_Long, TAG_Float, TAG_Double, TAG_Byte_Array, TAG_Int_Array, TAG_Long_Array, MalformedFileError
    from nbt.region import RegionFile, InconceivedChunk
    NBT_LIBRARY_USED = "nbt"


def get_tag_compound_class():
    """Returns the TAG_Compound class based on the active NBT library."""
    if NBT_LIBRARY_USED == "anvil" and 'anvil' in globals() and hasattr(anvil, 'TAG_Compound'):
        return anvil.TAG_Compound
    return TAG_Compound

def get_tag_list_class():
    """Returns the TAG_List class based on the active NBT library."""
    if NBT_LIBRARY_USED == "anvil" and 'anvil' in globals() and hasattr(anvil, 'TAG_List'):
        return anvil.TAG_List
    return TAG_List

def parse_coords(coord_str):
    """
    Parses a coordinate string (e.g., "X:10,Y:64,Z:120" or "X:1.23,Y:2.34,Z:3.45")
    into separate X, Y, Z string components.

    Args:
        coord_str (str): The coordinate string to parse.
                         Expected format: "X:valX, Y:valY, Z:valZ".
                         Also handles "N/A" or "N/A_Pos" by returning empty strings.

    Returns:
        tuple: (str, str, str) for x, y, z coordinates. Returns ('', '', '')
               if input is "N/A", "N/A_Pos", or if parsing fails.
    """
    x, y, z = '', '', ''
    if coord_str and coord_str.lower() not in ["n/a", "n/a_pos"]:
        try:
            parts = coord_str.split(',')
            for part in parts:
                key_val = part.split(':')
                if len(key_val) == 2:
                    key, val = key_val[0].strip(), key_val[1].strip()
                    if key == 'X': x = val
                    elif key == 'Y': y = val
                    elif key == 'Z': z = val
        except Exception: # Catch any error during parsing
            return '', '', ''
    return x, y, z

def find_nbt_tags_recursive(nbt_tag, search_criteria, current_path="root"):
    """
    Recursively searches an NBT tag structure for tags matching specified criteria.

    Args:
        nbt_tag: The current NBT tag object to search within (e.g., NBTFile, TAG_Compound, TAG_List, or other TAG_* types).
        search_criteria (dict): A dictionary specifying search parameters.
                                Expected keys: 'name' (str), 'value' (str).
                                If 'name' is empty, matches tags with no name (e.g., in lists).
                                If 'value' is empty, matches based on name only.
                                If dict is empty, matches all tags.
        current_path (str): The NBT path string leading to the current `nbt_tag`.

    Returns:
        list: A list of tuples, where each tuple contains (path_to_found_tag, found_tag_name, found_tag_value_str).
    """
    found_tags = []
    TAG_Compound_Class = get_tag_compound_class()
    TAG_List_Class = get_tag_list_class()

    name_matches = True
    if 'name' in search_criteria and search_criteria.get('name') is not None:
        if nbt_tag.name != search_criteria['name']: # Handles nbt_tag.name being None correctly
            name_matches = False

    value_matches = True
    if 'value' in search_criteria and search_criteria.get('value') is not None:
        if isinstance(nbt_tag, (TAG_Compound_Class, TAG_List_Class)):
            pass # Do not match value for container tags directly
        elif str(nbt_tag.value) != search_criteria['value']:
            value_matches = False

    if name_matches and value_matches:
        # Determine the name to report (especially for unnamed tags in lists)
        tag_name_to_report = nbt_tag.name if nbt_tag.name is not None else f"item_in_list_at_path_{current_path}"
        found_tags.append((current_path, tag_name_to_report, str(nbt_tag.value)))

    # Traverse children if the current tag is a compound or list
    if isinstance(nbt_tag, TAG_Compound_Class):
        # Assumes nbt.nbt like API where compound tags have a .tags list of named tags
        for child_tag in nbt_tag.tags:
            new_path = f"{current_path}.{child_tag.name}" if current_path and child_tag.name else \
                       (child_tag.name or current_path) # Handle root unnamed compound or child unnamed tags
            found_tags.extend(find_nbt_tags_recursive(child_tag, search_criteria, new_path))
    elif isinstance(nbt_tag, TAG_List_Class):
        # Assumes nbt.nbt like API where list tags are iterable
        for index, item_tag in enumerate(nbt_tag):
            new_path = f"{current_path}[{index}]"
            found_tags.extend(find_nbt_tags_recursive(item_tag, search_criteria, new_path))
    return found_tags

def parse_player_data(file_path, search_criteria):
    """
    Parses a player data file (.dat) to find NBT tags matching search criteria.

    Args:
        file_path (str): Path to the player .dat file.
        search_criteria (dict): Criteria for `find_nbt_tags_recursive`.

    Returns:
        list: A list of tuples, each representing a found item:
              (player_id, location_description, nbt_path, tag_name, tag_value_str).
    """
    player_findings_list = []
    try:
        player_id = os.path.basename(file_path).replace('.dat', '')
        nbt_file_obj = NBTFile(filename=file_path) # Loads the NBT structure
        # The root of a player.dat file is an unnamed TAG_Compound
        results = find_nbt_tags_recursive(nbt_file_obj, search_criteria, current_path="root")
        if results:
            for nbt_path, tag_name, tag_value in results:
                loc_desc = "Player Data (Other)" # Default location
                if nbt_path.startswith("root.Inventory"): loc_desc = "Player Inventory"
                elif nbt_path.startswith("root.EnderItems"): loc_desc = "Ender Chest"
                player_findings_list.append((player_id, loc_desc, nbt_path, tag_name, str(tag_value)))
    except MalformedFileError:
        print(f"Error: Player data file {file_path} is not a valid NBT file or is corrupted.")
    except Exception as e:
        print(f"Error processing player file {file_path}: {e}")
    return player_findings_list

def parse_region_file(file_path, search_criteria):
    """
    Parses a Minecraft region file (.mca) for block data, searching all chunks
    for NBT tags within block entities that match search criteria.

    Args:
        file_path (str): Path to the .mca region file.
        search_criteria (dict): Criteria for `find_nbt_tags_recursive`.

    Returns:
        list: A list of tuples, each representing a found item:
              (file_basename, chunk_coordinates_str, block_entity_coords_str,
               nbt_path, tag_name, tag_value_str).
    """
    all_findings = []
    region_file_basename = os.path.basename(file_path)
    TAG_Compound_Class = get_tag_compound_class()
    TAG_List_Class = get_tag_list_class() # Ensure this is available
    ChunkException = InconceivedChunk if InconceivedChunk and NBT_LIBRARY_USED == "nbt" else IOError

    try:
        region = RegionFile(filename=file_path)
        for x in range(32): # Iterate all possible chunk X coordinates
            for z in range(32): # Iterate all possible chunk Z coordinates
                try:
                    chunk_nbt = region.get_nbt(x, z) # Load NBT data for the chunk
                except ChunkException: chunk_nbt = None # Chunk doesn't exist
                except IOError: chunk_nbt = None # Other reading error

                if chunk_nbt: # If chunk NBT data was successfully loaded
                    # The root of chunk_nbt is a TAG_Compound, often named "Level" or similar,
                    # but find_nbt_tags_recursive starts search from this root.
                    chunk_coord_str = f"Chunk[{x},{z}]"
                    chunk_findings = find_nbt_tags_recursive(chunk_nbt, search_criteria, chunk_coord_str)

                    for nbt_path, tag_name, val in chunk_findings:
                        block_coords_str = "N/A"
                        # Regex to find block entity list name and index from the NBT path
                        # Path example: "Chunk[0,31].Level.block_entities[0].Items[0].id"
                        # or "Chunk[0,31].block_entities[0].Items[0].id" (newer versions)
                        match = re.search(r'\.(?:Level\.)?(block_entities|BlockEntities|tile_entities)\[(\d+)\]', nbt_path)

                        if match:
                            try:
                                be_list_name_from_regex = match.group(1)
                                be_idx = int(match.group(2))

                                # Access the block entity list from the chunk_nbt
                                be_list_candidate = None
                                # Check if list is directly under chunk root (e.g. chunk_nbt['block_entities'])
                                if chunk_nbt.get(be_list_name_from_regex):
                                    be_list_candidate = chunk_nbt.get(be_list_name_from_regex)
                                # Check if list is under 'Level' tag (older versions)
                                elif chunk_nbt.get('Level') and isinstance(chunk_nbt.get('Level'), TAG_Compound_Class) and \
                                     chunk_nbt.get('Level').get(be_list_name_from_regex):
                                    be_list_candidate = chunk_nbt.get('Level').get(be_list_name_from_regex)

                                if be_list_candidate and isinstance(be_list_candidate, TAG_List_Class):
                                    if 0 <= be_idx < len(be_list_candidate):
                                        block_entity_tag = be_list_candidate[be_idx]
                                        if isinstance(block_entity_tag, TAG_Compound_Class):
                                            # Extract coordinates from the block entity itself
                                            xt,yt,zt=block_entity_tag.get('x'),block_entity_tag.get('y'),block_entity_tag.get('z')
                                            if xt and hasattr(xt, 'value') and \
                                               yt and hasattr(yt, 'value') and \
                                               zt and hasattr(zt, 'value'):
                                                block_coords_str = f"X:{xt.value}, Y:{yt.value}, Z:{zt.value}"
                            except Exception: pass # Keep coord extraction errors silent
                        all_findings.append((region_file_basename, chunk_coord_str, block_coords_str, nbt_path, tag_name, val))
    except MalformedFileError:
        print(f"Error: Region file {file_path} is not a valid Anvil file or is corrupted.")
    except Exception:
        # Outer error processing (e.g. file access issues) - keep silent for now
        pass
    return all_findings

def parse_entity_file(file_path, search_criteria):
    """
    Parses a Minecraft entity file (.mca), searching all sectors for NBT tags
    within entities that match search criteria. Extracts entity type and coordinates.

    Args:
        file_path (str): Path to the .mca entity file.
        search_criteria (dict): Criteria for `find_nbt_tags_recursive`.

    Returns:
        list: A list of tuples, each representing a found item:
              (file_basename, sector_coordinates_str, entity_type_str,
               entity_coords_str, nbt_path_within_entity, tag_name, tag_value_str).
    """
    all_findings = []
    entity_file_basename = os.path.basename(file_path)
    TAG_Compound_Class = get_tag_compound_class()
    TAG_List_Class = get_tag_list_class()
    ChunkException = InconceivedChunk if InconceivedChunk and NBT_LIBRARY_USED == "nbt" else IOError

    try:
        entity_region = RegionFile(filename=file_path)
        for x_coord in range(32):
            for z_coord in range(32):
                try:
                    entity_sector_nbt = entity_region.get_nbt(x_coord, z_coord) # NBT data for this sector
                except ChunkException: entity_sector_nbt = None
                except IOError: entity_sector_nbt = None
                except Exception: entity_sector_nbt = None # Catch any other error from get_nbt

                if entity_sector_nbt: # If NBT data was loaded for the sector
                    sector_coord_str = f"Sector[{x_coord},{z_coord}]"

                    # Entity MCA files typically store a TAG_Compound at the sector root,
                    # which contains a TAG_List named "Entities".
                    if isinstance(entity_sector_nbt, TAG_Compound_Class):
                        entities_list_tag = entity_sector_nbt.get('Entities')
                        if entities_list_tag and isinstance(entities_list_tag, TAG_List_Class):
                            for entity_index, entity_nbt_object in enumerate(entities_list_tag):
                                if isinstance(entity_nbt_object, TAG_Compound_Class): # Each entity is a TAG_Compound
                                    # Extract entity type (id)
                                    entity_type = "Unknown Entity Type"
                                    id_tag = entity_nbt_object.get('id')
                                    if id_tag and hasattr(id_tag, 'value'):
                                        entity_type = str(id_tag.value)

                                    # Extract entity coordinates (Pos list of doubles)
                                    entity_coords_str = "N/A_Pos"
                                    pos_list_tag = entity_nbt_object.get('Pos')
                                    if pos_list_tag and isinstance(pos_list_tag, TAG_List_Class) and len(pos_list_tag) == 3:
                                        try:
                                            ex, ey, ez = pos_list_tag[0].value, pos_list_tag[1].value, pos_list_tag[2].value
                                            entity_coords_str = f"X:{ex:.2f}, Y:{ey:.2f}, Z:{ez:.2f}"
                                        except (AttributeError, IndexError, TypeError): pass # Handle if Pos format is unexpected

                                    # Path for items found *within* this specific entity
                                    entity_internal_path_prefix = f"{sector_coord_str}.Entities[{entity_index}]"
                                    current_entity_findings = find_nbt_tags_recursive(entity_nbt_object, search_criteria, current_path=entity_internal_path_prefix)
                                    for nbt_path, tag_name, tag_value in current_entity_findings:
                                        all_findings.append((entity_file_basename, sector_coord_str, entity_type, entity_coords_str, nbt_path, tag_name, tag_value))
                        else: # No 'Entities' list, or it's not a list. Search raw sector.
                            current_sector_findings = find_nbt_tags_recursive(entity_sector_nbt, search_criteria, current_path=sector_coord_str)
                            for nbt_path, tag_name, tag_value in current_sector_findings:
                                all_findings.append((entity_file_basename, sector_coord_str, "RawSectorSearch", "N/A", nbt_path, tag_name, tag_value))
                    elif isinstance(entity_sector_nbt, TAG_List_Class): # Sector root is a list (less common)
                         for entity_index, entity_nbt_object in enumerate(entity_sector_nbt):
                             if isinstance(entity_nbt_object, TAG_Compound_Class):
                                entity_type = str(entity_nbt_object.get('id',{}).value) if entity_nbt_object.get('id') else 'Unknown'
                                pos_list_tag = entity_nbt_object.get('Pos')
                                entity_coords_str = "N/A_Pos"
                                if pos_list_tag and isinstance(pos_list_tag, TAG_List_Class) and len(pos_list_tag) == 3:
                                    try:
                                        ex,ey,ez = pos_list_tag[0].value,pos_list_tag[1].value,pos_list_tag[2].value
                                        entity_coords_str = f"X:{ex:.2f}, Y:{ey:.2f}, Z:{ez:.2f}"
                                    except: pass
                                entity_internal_path_prefix = f"{sector_coord_str}[{entity_index}]" # Path for root list items
                                current_entity_findings = find_nbt_tags_recursive(entity_nbt_object, search_criteria, current_path=entity_internal_path_prefix)
                                for nbt_path, tag_name, tag_value in current_entity_findings:
                                    all_findings.append((entity_file_basename, sector_coord_str, entity_type, entity_coords_str, nbt_path, tag_name, tag_value))
    except MalformedFileError:
        print(f"Error: Entity file {file_path} is not a valid Anvil file or is corrupted.")
    except Exception:
        # Outer error processing - keep silent for now
        pass
    return all_findings

def parse_misc_nbt_file(file_path, search_criteria):
    """
    Parses a generic .dat file assumed to contain a single NBT structure.

    Args:
        file_path (str): Path to the .dat file.
        search_criteria (dict): Criteria for `find_nbt_tags_recursive`.

    Returns:
        list: A list of tuples, each representing a found item:
              (file_path_str, nbt_path, tag_name, tag_value_str).
    """
    misc_findings_list = []
    # print(f"\nAttempting to parse miscellaneous file as NBT: {file_path}") # Verbosity handled by caller
    if os.path.exists(file_path):
        try:
            nbt_file = NBTFile(filename=file_path)
            # print(f"Successfully parsed {file_path} as NBT.") # Verbosity handled by caller
            # The root of these files is typically an unnamed TAG_Compound
            findings = find_nbt_tags_recursive(nbt_file, search_criteria, current_path=(nbt_file.name or "root"))
            if findings:
                for nbt_path, tag_name, tag_value in findings:
                    misc_findings_list.append((file_path, nbt_path, tag_name, tag_value))
        except MalformedFileError: # Specific error for bad NBT structure
            print(f"Failed to parse {file_path} as NBT: File is not a valid NBT format or is corrupted.")
        except Exception as e: # Other errors like permission issues
            print(f"Failed to parse {file_path} due to an unexpected error: {e}")
    else:
        print(f"File not found: {file_path}")
    return misc_findings_list

def write_findings_to_csv(findings_list, csv_file_path):
    """
    Writes a list of findings (structured dictionaries) to a CSV file.

    Args:
        findings_list (list): A list of dictionaries, where each dictionary
                              represents a found item and its details.
        csv_file_path (str): Path to the output CSV file.
    """
    if not findings_list:
        print("No findings to write to CSV.")
        return

    fieldnames = [ # Ensure these match the keys in the dictionaries
        'data_source', 'file_name', 'player_id_or_entity_type',
        'location_category', 'coord_x', 'coord_y', 'coord_z',
        'nbt_path_to_item', 'found_item_name_tag', 'found_item_value', 'raw_nbt_path'
    ]

    try:
        with open(csv_file_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore') # Ignore extra fields if any
            writer.writeheader()
            writer.writerows(findings_list)
        print(f"\nSuccessfully exported {len(findings_list)} findings to {csv_file_path}")
    except IOError as e: print(f"Error writing to CSV file {csv_file_path}: {e}")
    except Exception as e_gen: print(f"An unexpected error occurred during CSV writing: {e_gen}")

def find_and_parse_data(world_directory, search_name_param, search_value_param, output_csv_filename):
    """
    Main orchestrator for finding and parsing NBT data from various sources
    within a Minecraft world directory.

    Args:
        world_directory (str): Path to the root of the Minecraft world save.
        search_name_param (str): NBT tag name to search for.
        search_value_param (str): NBT tag value to search for.
        output_csv_filename (str): Filename for the output CSV report.
    """
    search_criteria = {}
    if search_name_param: search_criteria['name'] = search_name_param
    if search_value_param: search_criteria['value'] = search_value_param
    if not search_criteria:
        print("Warning: No specific search criteria (name/value) provided. Will match based on presence if one is empty.")

    master_findings_list = []
    print(f"Starting NBT scan in world: {world_directory}")
    print(f"Searching for Name='{search_name_param or '*'}' Value='{search_value_param or '*'}'")

    # --- Player Data Search ---
    player_data_dir = os.path.join(world_directory, "playerdata/")
    print(f"\n--- Scanning Player Data ---")
    if os.path.exists(player_data_dir):
        try:
            p_files = [f for f in os.listdir(player_data_dir) if f.endswith('.dat')]
            print(f"Found {len(p_files)} player .dat file(s) in {player_data_dir}.")
            for p_fn in p_files:
                player_file_path = os.path.join(player_data_dir, p_fn)
                # print(f"Processing player file: {p_fn}...") # Can be verbose
                findings = parse_player_data(player_file_path, search_criteria)
                for p_id, loc, path, name, val in findings:
                    master_findings_list.append({
                        'data_source': 'PlayerData', 'file_name': p_fn,
                        'player_id_or_entity_type': p_id, 'location_category': loc,
                        'coord_x': '', 'coord_y': '', 'coord_z': '', # No world coords for player inventory items directly
                        'nbt_path_to_item': path, 'found_item_name_tag': name,
                        'found_item_value': val, 'raw_nbt_path': path
                    })
        except Exception as e: print(f"Error scanning player data directory {player_data_dir}: {e}")
    else: print(f"Player data directory not found: {player_data_dir}")

    # --- Region File Search ---
    region_roots = [os.path.join(world_directory, "region/")]
    all_mca_files_region = []
    for rd in region_roots:
        if os.path.exists(rd):
            try: [all_mca_files_region.append(os.path.join(rd, f)) for f in os.listdir(rd) if f.endswith('.mca')]
            except Exception as e: print(f"Error listing region files in {rd}: {e}")

    print(f"\n--- Scanning Region Files ({len(all_mca_files_region)} file(s)) ---")
    for mca_fp in all_mca_files_region:
        print(f"Processing region file: {mca_fp}...")
        findings = parse_region_file(mca_fp, search_criteria)
        for file_basename, chunk_info_str, block_coords_str, nbt_path, tag_name, val in findings:
            bx, by, bz = parse_coords(block_coords_str)
            master_findings_list.append({
                'data_source': 'RegionFile_BlockEntity', 'file_name': file_basename,
                'player_id_or_entity_type': '', # N/A for block entities
                'location_category': f'BlockEntity in {chunk_info_str}',
                'coord_x': bx, 'coord_y': by, 'coord_z': bz,
                'nbt_path_to_item': nbt_path, 'found_item_name_tag': tag_name,
                'found_item_value': val, 'raw_nbt_path': nbt_path
            })

    # --- Entity File Search ---
    entity_file_dir = os.path.join(world_directory, "entities/")
    print(f"\n--- Scanning Entity Files ---")
    if os.path.exists(entity_file_dir):
        try:
            e_mca_files = [os.path.join(entity_file_dir, f) for f in os.listdir(entity_file_dir) if f.endswith('.mca')]
            print(f"Found {len(e_mca_files)} entity .mca file(s) in {entity_file_dir}.")
            for e_fp in e_mca_files:
                print(f"Processing entity file: {e_fp}...")
                findings = parse_entity_file(e_fp, search_criteria)
                for file_basename, sector_info_str, entity_type, entity_coords_str, nbt_path, tag_name, val in findings:
                    ex, ey, ez = parse_coords(entity_coords_str)
                    master_findings_list.append({
                        'data_source': 'EntityFile_Entity', 'file_name': file_basename,
                        'player_id_or_entity_type': entity_type,
                        'location_category': f'Entity in {sector_info_str}',
                        'coord_x': ex, 'coord_y': ey, 'coord_z': ez,
                        'nbt_path_to_item': nbt_path, 'found_item_name_tag': tag_name,
                        'found_item_value': val, 'raw_nbt_path': nbt_path
                    })
        except Exception as e: print(f"Error scanning entity file directory {entity_file_dir}: {e}")
    else: print(f"Entity file directory not found: {entity_file_dir}")

    # --- Miscellaneous Data File NBT Scan ---
    print("\n--- Scanning Miscellaneous Data Files ---")
    misc_data_files_config = { # filename: description (description not used yet)
        "DIM-1/data/chunks.dat": "Nether Chunks Data",
        "DIM1/data/chunks.dat": "The End Chunks Data",
        "data/WorldUUID.dat": "World UUID Data"
    }
    temp_misc_findings_console = [] # For console printing only
    for f_path_suffix in misc_data_files_config:
        misc_fp = os.path.join(world_directory, f_path_suffix)
        # print(f"Attempting to parse: {misc_fp}...") # Can be verbose
        findings = parse_misc_nbt_file(misc_fp, search_criteria) # parse_misc_nbt_file handles os.path.exists
        if findings:
            # These are not added to master_findings_list for CSV as per current plan
            for file_path_str, nbt_path, name, val in findings:
                 temp_misc_findings_console.append({'File': file_path_str, 'Path': nbt_path, 'Tag': name, 'Value': val})

    # --- Consolidated Console Output (after all scans) ---
    if any(f['data_source'] == 'PlayerData' for f in master_findings_list):
        print("\n--- Summary: Player Data Findings ---")
        for item in master_findings_list:
            if item['data_source'] == 'PlayerData':
                 val_s = item['found_item_value'][:97]+"..." if len(item['found_item_value'])>100 else item['found_item_value']
                 print(f"- Player: {item['player_id_or_entity_type']}, Loc: {item['location_category']}, Path: {item['nbt_path_to_item']}, Item: {val_s}")
    else: print(f"\nNo matches for criteria in player data.")

    if any(f['data_source'] == 'RegionFile_BlockEntity' for f in master_findings_list):
        print("\n--- Summary: Region File (Block Entity) Findings ---")
        for item in master_findings_list:
            if item['data_source'] == 'RegionFile_BlockEntity':
                val_s = item['found_item_value'][:97]+"..." if len(item['found_item_value'])>100 else item['found_item_value']
                coords_display = f"X:{item['coord_x']},Y:{item['coord_y']},Z:{item['coord_z']}" if item['coord_x'] else "N/A"
                print(f"- File: {item['file_name']}, {item['location_category']}, Coords: {coords_display}, Path: {item['nbt_path_to_item']}, Item: {val_s}")
    else: print(f"\nNo matches for criteria in region files.")

    if any(f['data_source'] == 'EntityFile_Entity' for f in master_findings_list):
        print("\n--- Summary: Entity File Findings ---")
        for item in master_findings_list:
            if item['data_source'] == 'EntityFile_Entity':
                val_s = item['found_item_value'][:97]+"..." if len(item['found_item_value'])>100 else item['found_item_value']
                coords_display = f"X:{item['coord_x']},Y:{item['coord_y']},Z:{item['coord_z']}" if item['coord_x'] else "N/A_Pos"
                print(f"- File: {item['file_name']}, {item['location_category']}, Type: {item['player_id_or_entity_type']}, Coords: {coords_display}, Path: {item['nbt_path_to_item']}, Item: {val_s}")
    else: print(f"\nNo matches for criteria in entity files.")

    if temp_misc_findings_console:
        print("\n--- Summary: Miscellaneous Data File Findings (Console Only) ---")
        for item in temp_misc_findings_console:
            val_s = item['Value'][:97]+"..." if len(item['Value'])>100 else item['Value']
            print(f"- File: {item['File']}, Path: {item['Path']}, Item: {val_s}")
    else: print(f"\nNo matches for criteria in miscellaneous data files.")

    # --- Write Master Findings to CSV ---
    write_findings_to_csv(master_findings_list, output_csv_filename)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find NBT data in a Minecraft world save. Searches player data, region files (block entities), entity files, and specified misc .dat files.",
        formatter_class=argparse.RawTextHelpFormatter # For better help text formatting
    )
    parser.add_argument('--world_dir', type=str, default="sample_world",
                        help="Path to the Minecraft world directory (e.g., path/to/.minecraft/saves/MyWorld).\nDefault: sample_world")
    parser.add_argument('--name', type=str, default=None, # Changed default to None
                        help="NBT tag name to search for (e.g., 'id', 'CustomName').\nIf not provided, searches by value only (if value is provided).")
    parser.add_argument('--value', type=str, default=None, # Changed default to None
                        help="NBT tag value to search for (e.g., 'minecraft:diamond', '{\"text\":\"Special Chest\"}').\nIf not provided, searches by name only (if name is provided).")
    parser.add_argument('--output_csv', type=str, default="nbt_findings.csv",
                        help="Name for the output CSV file.\nDefault: nbt_findings.csv")

    args = parser.parse_args()

    run_search = True
    search_name_to_use = args.name
    search_value_to_use = args.value

    if args.name is None and args.value is None:
        # Check if the user intended to run the default demo
        # This happens if no arguments altering search, world, or output are given.
        is_default_world = args.world_dir == parser.get_default("world_dir")
        is_default_output = args.output_csv == parser.get_default("output_csv")

        if is_default_world and is_default_output:
            # All relevant args are defaults, so assume demo mode.
            print("No search criteria provided via command line, using default demo search: Name='id', Value='minecraft:elytra'")
            search_name_to_use = "id"
            search_value_to_use = "minecraft:elytra"
        else:
            # User specified other args (like world_dir or output_csv) but not search terms.
            print("Error: No search criteria (--name or --value) provided, but other arguments were specified or defaults changed.")
            parser.print_help()
            run_search = False

    if run_search:
        start_time = time.time()
        find_and_parse_data(args.world_dir, search_name_to_use, search_value_to_use, args.output_csv)
        end_time = time.time()
        duration = end_time - start_time
        print(f"\nTotal execution time: {duration:.2f} seconds")
