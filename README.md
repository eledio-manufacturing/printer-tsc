# Printer TSC and Brother QL service

This service supports both TSC and Brother QL label printers.

## Setup

```bash
uv sync
uv run main.py
```

## Configuration

Edit `config/config.yaml` to set up your printer.

### TSC Printer
```yaml
printer:
  type: tsc
  address: 192.168.1.1
  port: 9100
  health_poll_interval: 60  # seconds between background status polls; default 60, set 0 to disable
```

### Brother QL Printer
```yaml
printer:
  type: brother_ql
  identifier: usb://0x04f9:0x2015  # or tcp://192.168.1.1:9100
  model: QL-500
  label_size: 62
```

### Logging

Optional `logging` block enables rotating file logging alongside console output:
```yaml
logging:
  path: /var/log/printer-tsc/service.log  # file or dir path (dir gets printer-tsc.log); omit = console only
  level: DEBUG
  max_bytes: 1048576   # rotate at 1MB
  backup_count: 5      # keep 5 rotated files
```

## Change of printer name
* in the `config.yaml` (usually in the `/srv/printer/config/config.yaml`) edit mqtt topic
* keep prefix of topic `printer/`
* in the example above, there is printer name set as `test`
```
mqtt:
  topic: printer/test
```