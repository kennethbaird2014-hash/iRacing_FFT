import irsdk, time

ir = irsdk.IRSDK()
ir.startup()
time.sleep(1) # wait for connection

if ir.is_initialized and ir.is_connected:
    print("Found iRacing")
    for key in ['PlayerCarName', 'CarIdx', 'DriverInfo', 'PlayerCarPath', 'PlayerCarDesignStr', 'PlayerCarTeamIncidentCount']:
        try:
            val = ir[key]
            print(f"{key}: {val}")
        except Exception as e:
            pass
            
    # Print keys that have car in them
    car_keys = [k for k in ir.var_headers_names if 'car' in k.lower()]
    print("Car keys:", car_keys)
else:
    print("iRacing not running, using mock info for planning.")
