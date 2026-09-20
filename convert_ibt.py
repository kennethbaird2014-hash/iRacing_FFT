import irsdk
import numpy as np
import os
import yaml
from pathlib import Path
from datetime import datetime

# Import the same constant used in the main app
SAMPLE_RATE = 360
RECORDINGS_DIR = Path("recordings")

def convert_ibt_to_npz(ibt_path, output_dir):
    ibt = irsdk.IBT()
    try:
        ibt.open(str(ibt_path))
        
        # Get session info to find car name
        session_info = ""
        try:
            # session_info_offset is relative to start of file
            offset = ibt._header.session_info_offset
            length = ibt._header.session_info_len
            session_info_raw = ibt._shared_mem[offset:offset+length].decode('cp1252', errors='ignore')
            # Extract YAML part (it might have some trailing nulls or garbage)
            session_info_yaml = session_info_raw.split('\n...')[0] + '\n...'
            session_data = yaml.safe_load(session_info_yaml)
            
            # Find car name (similar logic to ffb_analyzer_v2.py)
            car_name = "unknown"
            if 'DriverInfo' in session_data:
                driver_info = session_data['DriverInfo']
                player_car_idx = session_data.get('DriverInfo', {}).get('DriverCarIdx', 0)
                for d in driver_info.get('Drivers', []):
                    if d.get('CarIdx') == player_car_idx:
                        car_name = d.get('CarScreenName') or d.get('CarPath') or "unknown"
                        break
        except Exception as e:
            print(f"  Warning: Could not parse session info for car name: {e}")
            car_name = "unknown"

        # Get torque data
        torque_data = []
        if "SteeringWheelTorque_ST" in ibt.var_headers_names:
            print("  Using 360Hz data (SteeringWheelTorque_ST)")
            raw_data = ibt.get_all("SteeringWheelTorque_ST")
            # Flatten list of lists
            for tick in raw_data:
                torque_data.extend(tick)
        elif "SteeringWheelTorque" in ibt.var_headers_names:
            print("  Using 60Hz data (upsampling to 360Hz)")
            raw_data = ibt.get_all("SteeringWheelTorque")
            # Upsample by repeating 6 times per tick
            for val in raw_data:
                torque_data.extend([val] * 6)
        else:
            print("  Error: No steering torque variable found.")
            return

        if not torque_data:
            print("  Error: No data recorded in file.")
            return

        # Prepare output path
        ts = os.path.getmtime(ibt_path)
        dt = datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")
        safe_car = "".join(c if c.isalnum() or c in "-_ " else "_" for c in car_name)
        safe_car = safe_car.strip().replace(" ", "_") or "unknown"
        
        # Use IBT filename base to keep it unique
        ibt_base = Path(ibt_path).stem.replace(" ", "_")
        output_path = output_dir / f"{ibt_base}.npz"
        
        # Save as npz
        arr = np.array(torque_data, dtype=np.float32)
        np.savez_compressed(str(output_path), samples=arr, car=np.array([car_name]))
        
        print(f"  Success: Saved {len(torque_data)} samples to {output_path.name}")
        ibt.close()
        return True
        
    except Exception as e:
        print(f"  Error converting {ibt_path}: {e}")
        if hasattr(ibt, 'close'): ibt.close()
        return False

def main():
    source_dir = Path(os.path.expanduser("~/Documents/iRacing/telemetry"))
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    
    ibt_files = list(source_dir.glob("*.ibt"))
    print(f"Found {len(ibt_files)} .ibt files in {source_dir}")
    
    # Let's start with the most recent 10 to test
    ibt_files.sort(key=os.path.getmtime, reverse=True)
    
    success_count = 0
    # For now, let's process them all or a good chunk
    max_files = 100 # Can increase this later
    
    for i, ibt_file in enumerate(ibt_files[:max_files]):
        print(f"[{i+1}/{min(len(ibt_files), max_files)}] Processing {ibt_file.name}...")
        if convert_ibt_to_npz(ibt_file, RECORDINGS_DIR):
            success_count += 1
            
    print(f"\nBulk conversion complete. Converted {success_count} files.")

if __name__ == "__main__":
    main()
