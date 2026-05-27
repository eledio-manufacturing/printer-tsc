# Printer TSC and Brother QL service

This service supports both TSC and Brother QL label printers.

## Configuration

Edit `config/config.yaml` to set up your printer.

### TSC Printer
```yaml
printer:
  type: tsc
  address: 192.168.1.1
  port: 9100
```

### Brother QL Printer
```yaml
printer:
  type: brother_ql
  identifier: usb://0x04f9:0x2015  # or tcp://192.168.1.1:9100
  model: QL-500
  label_size: 62
```

## Change of printer name
* in the `config.yaml` (usually in the `/srv/printer/config/config.yaml`) edit mqtt topic
* keep prefix of topic `printer/`
* in the example above, there is printer name set as `test`
```
mqtt:
  topic: printer/test
```