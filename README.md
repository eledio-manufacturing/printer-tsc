# Printer TSC service

## Change of printer name
* in the `config.yaml` (usually in the `/srv/printer/config/config.yaml`) edit mqtt topic
* keep prefix of topic `printer/`
* in the example above, there is printer name set as `test`
```
mqtt:
  topic: printer/test
```