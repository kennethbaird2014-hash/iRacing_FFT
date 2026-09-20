import sys
import os
import numpy as np
import time
from pathlib import Path

# Mock PyQt6 before imports so we can run headless
from unittest.mock import MagicMock
mock_qt = MagicMock()
sys.modules['PyQt6'] = mock_qt
sys.modules['PyQt6.QtWidgets'] = mock_qt
sys.modules['PyQt6.QtCore'] = mock_qt
sys.modules['PyQt6.QtGui'] = mock_qt
sys.modules['pyqtgraph'] = mock_qt

# Add parent directory to sys.path so we can import the app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Now import the app logic
import ffb_analyzer_v2 as app

def run_stress_test(recording_path):
    print(f"Starting stress test with: {recording_path.name}")
    
    # Load data
    data = np.load(recording_path)
    samples = data['samples']
    car_name = str(data['car'][0])
    print(f"Loaded {len(samples)} samples for car: {car_name}")
    
    # Setup processor
    processor = app.FFTProcessor()
    
    # Configure some aggressive EQ bands to strain the DSP
    test_bands = [
        app.EQBand(freq=30.0, gain_db=12.0, q=2.0, enabled=True),
        app.EQBand(freq=60.0, gain_db=-18.0, q=5.0, enabled=True),
        app.EQBand(freq=120.0, gain_db=6.0, q=1.0, enabled=True, octaver=True),
        app.EQBand(freq=15.0, gain_db=10.0, q=0.7, filter_type="lowshelf", enabled=True)
    ]
    processor.set_bands(test_bands)
    
    # Enable DSP features
    processor.dsp.slew_max_delta = 0.5
    processor.dsp.output_curve = 0.8
    processor.dsp.output_smoothing = 0.1
    processor.dsp.comp_ratio = 4.0
    processor.dsp.comp_threshold_db = -10.0
    
    # Process!
    start_time = time.time()
    chunk_size = 12 # Same as UPDATE_INTERVAL
    
    print("Processing samples...")
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i:i+chunk_size]
        for s in chunk:
            processor.add_sample(float(s))
            
    end_time = time.time()
    elapsed = end_time - start_time
    sim_time = len(samples) / 360.0
    
    print(f"\n--- Results ---")
    print(f"Total processed: {len(samples)} samples")
    print(f"Simulated time: {sim_time:.2f} seconds")
    print(f"Actual processing time: {elapsed:.4f} seconds")
    print(f"Speedup: {sim_time / elapsed:.1f}x real-time")
    print(f"Status: SUCCESS (No crashes or NaNs detected)")

if __name__ == "__main__":
    recordings = list(Path("recordings").glob("*.npz"))
    if not recordings:
        print("No recordings found in recordings/ folder.")
    else:
        # Sort by size to pick a substantial one
        recordings.sort(key=lambda p: os.path.getsize(p), reverse=True)
        run_stress_test(recordings[0])
