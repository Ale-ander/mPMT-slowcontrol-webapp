# mPMT-data-plotter
mPMT data plotter and slowcontrol webapp, based on [flask](https://flask.palletsprojects.com/en/stable/). 

Then, to start the server, launch:

```bash
python web_monitor.py --host 192.168.16.67 --rc-port 9000 --interval 1.0 --port 5555
```

Optional arguments:

| Argument | Description | Default |
|-----------|--------------|----------|
| `--host` | RunControl / HV Modbus host IP | `TESTER_0_IP` from `.env` or `127.0.0.1` |
| `--rc-port` | RunControl TCP port | `9000` |
| `--channels` | Channel list, e.g. `1-19` or `1,2,3` | `1-19` |
| `--interval` | Polling interval in seconds | `1.0` |
| `--port` | HTTP web port (0 = auto) | `5555` |

---
