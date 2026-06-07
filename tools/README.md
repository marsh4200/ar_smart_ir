# ar_smart_ir — irdb code importer

Pull IR codes from the **irdb** project (`probonopd/irdb`, the "Wikipedia of IR
codes") and write them as `ar_smart_ir` device-code files.

irdb stores codes as `protocol,device,subdevice,function`. These tools render
each function to **Pronto hex** (`commandsEncoding: "Pronto"`), which your
`controller.py` transcoder fans out to Broadlink / Xiaomi / MQTT / LOOKin /
ESPHome / Tuya automatically — so one imported file works on every controller.

## Files

- `ir_render.py` — protocol → Pronto renderer.
  Supported: **NEC family** (NEC/NEC1/NEC2/NECx1/NECx2/Apple/TiVo/Pioneer),
  **Sony SIRC** (12/15/20), **RC5 / RC6**, **Panasonic/Kaseikyo**, **JVC**.
  The NEC path is verified bit-for-bit against the integration's own
  `helpers.Helper.compact_nec_hex_to_lirc` encoder.
- `irdb_import.py` — fetches the irdb index + code sets from
  `raw.githubusercontent.com` and writes JSON files in the native schema.

## Usage

```bash
# One good code set per TV brand, numbered from 40001:
python3 tools/irdb_import.py --device-type TV --platform media_player \
    --start-code 40001 --per-brand 1

# Specific brands, 2 sets each:
python3 tools/irdb_import.py --device-type TV \
    --brands Samsung,LG,Sony,Panasonic,JVC --per-brand 2 --start-code 40001

# Preview without writing:
python3 tools/irdb_import.py --device-type TV --dry-run
```

Other irdb device types you can pass to `--device-type`: `TV`, `DVD`,
`Receiver`, `Audio`, `Projector`, `Set Top Box`, etc. (browse the index at
`https://raw.githubusercontent.com/probonopd/irdb/master/codes/index`).

## Notes

- Unsupported protocols (e.g. Sharp, RCA-38, Blaupunkt) are **skipped cleanly**
  and reported in the run summary, never emitted as broken codes. Add them to
  `ir_render.py` if you need them.
- Device files are numbered from `--start-code` (default 40001) so they don't
  collide with the existing hand-curated 1000–9999 sets.
- irdb power keys are often a single toggle `POWER`; in that case both `on` and
  `off` map to the toggle. Single-digit keys become `Channel N` sources so the
  media_player digit-tuning path works.
