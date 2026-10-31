---
name: remote-flash
description: Bootstrap and flash firmware to a bench device via the dfu-remote daemon
user-invocable: true
---

Same as `/flash <device> <firmware_file>` with the DFU backend: follow
`../flash/SKILL.md` (relative to this skill's resolved directory) from "Bench"
through "Cold boot". Kept as its own command for muscle memory.

Usage: `/remote-flash <device> <firmware_file>`
