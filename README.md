# cyberdeck-term

A small terminal application that shows a large clock, the date, live weather and
system stats over a cyberpunk "matrix rain" background.

- **Clock and date:** big block-digit clock, centered, with a blinking colon.
  24-hour or 12-hour, optional seconds.
- **Weather:** current conditions, high/low, feels-like, humidity and wind from
  [Open-Meteo](https://open-meteo.com/). No API key needed.
- **System stats:** CPU, memory, disk, temperature and battery, with bars that go
  green, yellow and red.
- **Boot sequence:** a three-second `INITIALISING… LINK ESTABLISHED` intro with the rain fading in.
- **Glitch:** the clock occasionally tears, corrupts, splits into offset cyan and magenta
  copies, and drops out in scan-line flickers, all for a fraction of a second. Rate is
  adjustable, or turn it off.
- **Matrix rain:** falling katakana (or ASCII) in the background, with optional CRT
  scanlines and an adjustable frame rate. It can be turned off.

Python standard library only. Stats are read straight from `/proc`, `/sys` and
`statvfs` (what `df` uses), with `sensors` as a fallback for temperature.

## Requirements

- Linux (developed for Arch)
- Python 3.8+ (with `curses`, included in the standard Arch `python` package)
- A terminal with UTF-8 and 256-colour support
- Optional: `lm_sensors` if your temperature isn't exposed under `/sys`

## Usage

```bash
python3 cyberdeck.py
```

On first launch, with no city configured, it asks for one and saves it. You can
enter `Paris` or `Paris, FR` to pick between places with the same name.

To run it from anywhere:

```bash
install -Dm755 cyberdeck.py ~/.local/bin/cyberdeck
```

### Keys

| Key | Action |
| --- | --- |
| `q` / `Esc` | Quit |
| `s` | Settings menu |

In settings, `↑`/`↓` selects an item and `←`/`→` (or `-`/`+`) changes it. Changes
apply immediately and are saved.

| Setting | Behaviour |
| --- | --- |
| City | `Enter` to type a new city |
| Units | Metric / imperial (any of `←`, `→`, `Enter` toggles) |
| Weather refresh | How often weather is fetched: 5, 10, 15, 30 or 60 minutes |
| Air quality | Show the air quality line on / off (`Enter` toggles) |
| FPS | 5-60, in steps of 5 |
| Rain | Matrix rain on / off (`Enter` toggles) |
| Scanlines | Faint CRT scanlines in the rain on / off (`Enter` toggles) |
| Glitch | Clock glitch effect on / off (`Enter` toggles) |
| Glitch rate | Average bursts per second, 0.05-2.00 |
| Boot intro | The launch intro on / off (`Enter` toggles) |

### Options

| Flag | Description |
| --- | --- |
| `--city NAME` | Weather location by name (this run only) |
| `--lat`, `--lon` | Weather location by coordinates |
| `--units {metric,imperial}` | Units for temperature and wind |
| `--fps N` | Frames per second (5-60) |
| `--12h` | 12-hour clock |
| `--seconds` | Show seconds |
| `--ascii` | ASCII rain instead of katakana (use this if your font shows boxes) |
| `--no-rain` | Disable the background |
| `--no-scanlines` | Disable the CRT scanlines in the rain |
| `--no-air-quality` | Hide air quality |
| `--no-boot` | Skip the boot sequence |
| `--no-glitch` | Disable the clock glitch |
| `--config PATH` | Use a different config file |

Flags apply to the current run only and are not saved.

## Configuration

Settings are stored in `~/.config/cyberdeck/config.json` (or under
`$XDG_CONFIG_HOME`). The file is created when you first choose a city. Delete it
to see the first-launch prompt again.

```json
{
  "location": { "city": "London", "lat": null, "lon": null },
  "units": "metric",
  "fps": 30,
  "glitch": { "enabled": true, "rate": 0.35 },
  "clock_24h": true,
  "show_seconds": false,
  "weather_refresh_minutes": 15,
  "air_quality": true,
  "boot_sequence": true,
  "disk_path": "/",
  "rain": { "enabled": true, "scanlines": true, "charset": "katakana", "speed": 1.0 }
}
```

| Key | Meaning |
| --- | --- |
| `location` | City name, or `lat`/`lon` (which take priority) |
| `units` | `metric` or `imperial` |
| `fps` | Frame rate, 5-60 |
| `clock_24h` | `false` for a 12-hour clock |
| `show_seconds` | Show seconds on the clock |
| `weather_refresh_minutes` | How often weather is fetched |
| `air_quality` | Show air quality (US AQI and PM2.5) in the weather area |
| `boot_sequence` | Play the three-second `INITIALISING… LINK ESTABLISHED` intro at launch (any key skips it) |
| `disk_path` | Mount point shown in the disk bar |
| `glitch.enabled` | Occasional glitch bursts on the clock |
| `glitch.rate` | Average bursts per second (0.05-2.0) |
| `rain.enabled` | Show the matrix rain |
| `rain.scanlines` | Dim every other rain row slightly for a CRT look |
| `rain.charset` | `katakana` or `ascii` |
| `rain.speed` | Rain speed multiplier |

## Notes

- The layout adapts to the terminal size: the stats box collapses to a single
  line, then the clock to plain text, as the window gets smaller.
- If the network is down, the last weather reading stays on screen marked
  `[OFFLINE]` and it retries every minute.

## License

GPL-3.0. See [LICENSE](LICENSE).
