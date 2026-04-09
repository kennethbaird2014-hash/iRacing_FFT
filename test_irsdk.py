import irsdk
print("IRSDK dir:", dir(irsdk.IRSDK))

ir = irsdk.IRSDK()
print("ir dir:", dir(ir))
print("is_connected in ir:", hasattr(ir, "is_connected"))
print("is_initialized in ir:", hasattr(ir, "is_initialized"))
