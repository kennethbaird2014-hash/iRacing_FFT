import irsdk
import sys
import os

def check_ibt(file_path):
    ibt = irsdk.IBT()
    try:
        ibt.open(file_path)
        print(f"File: {os.path.basename(file_path)}")
        names = ibt.var_headers_names
        if names:
            print("Variables found:", len(names))
            st_exists = "SteeringWheelTorque_ST" in names
            print(f"SteeringWheelTorque_ST: {'YES' if st_exists else 'NO'}")
            print(f"SteeringWheelTorque: {'YES' if 'SteeringWheelTorque' in names else 'NO'}")
            
            # Check length of the list if it exists
            if st_exists:
                data = ibt.get_all("SteeringWheelTorque_ST")
                if data:
                    print(f"Data length: {len(data)} (Type: {type(data[0])})")
        ibt.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    path = os.path.expanduser("~/Documents/iRacing/telemetry/acuraarx06gtp_bathurst 2026-03-10 23-24-22.ibt")
    check_ibt(path)
