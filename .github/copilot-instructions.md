# BSides 2025 Badge Development Guide

## Project Overview
This is a MicroPython-based ESP32-C3 conference badge with OLED display, NeoPixel LEDs, and WiFi connectivity. The architecture follows a screen-based UI pattern with asyncio event handling.

## Core Architecture

### Entry Point Flow
- `main.py` → checks pin 4 (SELECT button) → conditionally imports `bsides25.py`
- `bsides25.py` contains the entire application with asyncio event loop
- Boot sequence: hardware init → load params → show logo → start concurrent tasks

### Screen System Pattern
All UI screens inherit from base `Screen` class in `bsides25.py`:
- `ParamScreen` - parameter adjustment with visual slider (brightness, hue, etc.)
- `ListScreen` - menu navigation with cursor selection
- `TextScreen` - static text display with scrolling
- Custom screens for specific features (Snake game, WiFi scan, etc.)

Navigation hierarchy: `BadgeScreen` (main menu) → various feature screens → return via `BACK` button

### Hardware Abstraction
Hardware pins and settings defined as constants at top of `bsides25.py`:
```python
I2C_SCL = 1, I2C_SDA = 0  # OLED display
NEOPIXEL_PIN = 3          # LED strip
BTN_*_PIN = 4,5,8,9       # Physical buttons
```

## Development Workflow

### Device Programming
Three-step process managed by VS Code tasks:
1. **Erase Flash**: `esptool --port <port> erase_flash`
2. **Flash Firmware**: Write MicroPython binary to flash
3. **Copy Software**: `mpremote <port> fs cp -r software/ :/`

Use VS Code Command Palette → "Tasks: Run Task" → select badge task.

### Code Deployment
- Only edit files in `software/` directory
- Run "Badge: Copy Software to Device" task to deploy changes
- Hold SELECT button while resetting if mpremote fails to connect

### Logo Management
Auto-generated bitmap files in `logos/` directory:
- Each sponsor logo is a separate .py file with framebuf data
- Pattern: `data = bytearray([...])` + `fb = framebuf.FrameBuffer(...)`
- Used by `SponsorsScreen` for cycling display

## Key Patterns

### Persistent Configuration
Parameters stored in `params.json` using Parameter class pattern:
```python
led_brightness = Parameter("Brightness", 10, 100)
params["Brightness"] = led_brightness
```

### Button Handling
IRQ-based with debouncing and auto-repeat for NEXT/PREV buttons:
- Global `button_event` asyncio.Event for coordination
- `_schedule_push()` handles debouncing via micropython.schedule()

### Asyncio Task Structure
Three concurrent tasks in main loop:
- `ui_task()` - screen rendering and button handling
- `neopixel_task()` - LED effects animation at 50 FPS
- `inactivity_task()` - timeout to logo display

### LED Effects System
NeoPixel effects use stateful functions with consistent pattern:
```python
def led_eff_name(np, oldstate):
    # Initialize or update state dict/tuple
    state = oldstate or default_state
    # Render pixels based on parameters and state
    for i in range(len(np)):
        np[i] = hsv_to_rgb(hue, sat, brightness)
    # Return updated state for next frame
    return new_state
```

Available effects (registered in `neopixel_task()`):
- **Off** - all LEDs disabled
- **Rainbow** - hue cycle around ring with rotation
- **Breathe** - uniform brightness pulsing
- **Comet** - moving dot with fading tail
- **Rainbow Comet** - comet with color-cycling head
- **Ping-Pong** - dual bouncing heads with reflection
- **Dual Hue** - gradient between hue and hue+180°
- **Aurora** - organic waves in green/purple
- **Spiral Spin** - rotating brightness wave
- **Olympic** - rotating sections in Olympic ring colors (blue, yellow, red, green, black)
- **Police** - red/blue strobing pattern
- **Cycle_All** - auto-switches effects every 60 seconds

Effects respect user parameters (`led_brightness`, `led_hue`, `led_sat`, `led_speed`) and use HSV color space for smooth transitions. State persistence enables smooth animation across frames.

### WiFi Integration
Badge connects to "bsides-badge" network for name fetching:
- `FetchNameScreen` downloads username from `badge.bsides.ee`
- Stores result in `yourname.txt`
- Device ID generation and persistence in `id.txt`

## File Organization
- `software/main.py` - minimal boot loader
- `software/bsides25.py` - complete application (1600+ lines)
- `software/lib/` - display drivers and text rendering
- `software/logos/` - auto-generated sponsor logo bitmaps
- `hardware/` - PCB schematics and documentation

## Hardware-Specific Notes
- ESP32-C3FH4 with 4MB flash
- 128x64 SSD1306 OLED on I2C
- 16x WS2812B addressable LEDs
- SELECT button (pin 4) also used for boot mode detection