# Time-of-Use (TOU) Controls: V1 vs V2

Solis hybrid inverters expose two independent, non-interacting sets of scheduled charge/discharge controls. SolisConnect maps both, but they cover different register ranges, different slot counts, and different levels of Modbus/SolisCloud parity.

The config flow's "V2 support" toggle is auto-detected from the inverter's HMI firmware version (Modbus register `33002`, or SolisCloud `inverterDetail` field `hmiVersionAll`) whenever that read succeeds: an HMI version at or above `0x4B00` supports V2. Detection silently overrides whatever the installer picked in the form (same pattern as automatic model detection); if the read fails or is unparseable, the form's choice is left as-is rather than blocking setup.

## Summary

| | Time-Charging (V1) | Grid Time of Use V2 |
| --- | --- | --- |
| Enable switch | `switch.solis_time_of_use` — bit 1 of storage-mode register `43110` | `switch.solis_time_of_use_v2_switch` — bitfield register `43707`, one bit per slot |
| Slot count | 3 | 6 |
| Per-slot fields | Start/end hour+minute for charge and discharge | Start/end hour+minute, target SOC, cut-off voltage, battery current, per slot, for charge and discharge |
| Charge/discharge current | One global pair (`43141`/`43142`), not per-slot | Per-slot (register offset `+1` in each slot block) |
| Target SOC | Not available | Per-slot (register offset `+0` in each slot block) |
| Cut-off voltage | Not available | Per-slot (register offset `+2` in each slot block) |
| Modbus registers | `43141-43170` | `43707-43791` |
| SolisCloud support | **Planned, not yet implemented** — control CID `103` exists and covers exactly the 3 vendor-documented slots (see below) | Mapped: control CIDs `5916-5987` |
| SolisCloud enablement gate | N/A | CID `6798` (`0xAA55`) reports whether the inverter supports V2; SolisConnect does not yet enforce this gate (see caveat below) |

## Time-Charging (V1)

The older scheduling mechanism. One shared charge current and one shared discharge current apply across all slots; only the start/end times vary per slot.

- Enable: bit 1 (`Time of Use`) of the storage-mode bitfield at register `43110` (`switch_sensors.py`). This bit requires either `Self-Use Mode` (bit 0) or `Feed-In Priority Mode` (bit 6) to be conflict-free with the other storage-mode bits.
- Global settings: `Time-Charging Charge Current` (`43141`) and `Time-Charging Discharge Current` (`43142`), both in amps, editable as `number` entities.
- Per slot, each slot is a block of paired hour/minute registers exposed as HA `time` entities:
  - Charge Start / Charge End
  - Discharge Start / Discharge End
- Register layout: slot 1 starts at `43143`, slot 2 at `43153`, slot 3 at `43163` (each slot block is 8 registers of data plus a 2-register reserved gap).
- **SolisCloud mapping is planned but not yet implemented.** SolisCloud control CID `103` ("Charge and discharge Settings") can read/write V1 TOU: a single comma-separated string of 18 values covering all 3 slots, with **per-slot** charge/discharge current — unlike Modbus, where current is one pair shared by all slots. Field order per slot: `ChargeCurrent, DischargeCurrent, ChargeStart(HH:mm), ChargeEnd(HH:mm), DischargeStart(HH:mm), DischargeEnd(HH:mm)`. Source: `SolisCloud_control_api_command_list` workbook, "Hybrid Inverter" sheet. See the project's working notes for the full implementation plan.
- The official Solis RS485_MODBUS Hybrid Inverter protocol document only documents slot 1 (`43141-43150`); slots 2-3 (`43153`, `43163`) are undocumented in that particular revision but independently confirmed by SolisCloud control CID `103`, which covers exactly these 3 slots and no more. (An earlier version of SolisConnect also defined a "Slot 4" and "Slot 5" at `43173-43190`; these fell entirely inside the vendor's documented `Reserved` range with no confirmation from any source and have been removed.)

## Grid Time of Use V2

The newer, richer scheduling mechanism with independent settings per slot.

- Enable: dedicated bitfield register `43707` (`Time of Use V2 Switch`), 12 bits — one per slot (bits 0-5 for charge slots 1-6, bits 6-11 for discharge slots 1-6). This is a separate switch entity from the V1 enable bit; the two schedules do not share a switch.
- Per slot (1-6), each slot block is 7 registers, base offset `43708 + (slot - 1) * 7` (charge) and `43750 + (slot - 1) * 7` (discharge) — e.g. Slot 1 charge SOC is `43708`, Slot 2 charge SOC is `43715`:
  - `+0`: cut-off/target SOC (%, editable `number`, 0-100)
  - `+1`: battery current (A, editable `number`, 0-300, ×0.1)
  - `+2`: cut-off voltage (V, editable `number`, 0-300, ×0.1)
  - `+3..+6`: start hour, start minute, end hour, end minute (paired into a `time` entity)
- SolisCloud mapping (`sensor_data/cloud_mapping.py`, `_tou_v2_mappings()`):
  - Slot enable bits -> CIDs `5916-5927` (charge slots 1-6, then discharge slots 1-6), merged into the shared `43707` bitfield by `CloudDataRetrieval._merge_switch_bits`. On a cold cache the merged register is only published once all 12 slot-switch CIDs have been observed in a poll cycle, so a CID transiently missing from one `atReadBatch` response is never mistaken for "off".
  - Target SOC -> CIDs `5928-5933` (charge), `5965, 5969, 5973, 5977, 5981, 5984` (discharge).
  - Battery current -> CIDs `5948-5963` family (charge), `5967-5986` family (discharge).
  - Cut-off voltage -> the paired CID in the same current/voltage CID families.
  - Start/end time pairs -> CIDs `5946, 5949, 5952, 5955, 5958, 5961` (charge), `5964, 5968, 5972, 5976, 5980, 5987` (discharge); the cloud API represents each pair as a single `HH:MM-HH:MM` string, which SolisConnect composes from/decomposes into the paired hour/minute registers.
- **Known caveat:** SolisCloud CID `6798` reports whether the connected inverter actually supports TOU V2 (value `43605` / `0xAA55` = supported). SolisConnect does not yet read this gate, so cloud-only or dual-protocol entries on a V2-unsupported inverter may have TOU V2 writes rejected by the device with no advance warning from the integration.

## Practical implications

- V1 and V2 are independent schedules on the inverter itself; enabling one does not disable the other, and having both enabled is a device-level conflict outside SolisConnect's control — check your inverter's own firmware behavior before enabling both.
- If you only have local Modbus, either scheme is fully readable and writable.
- If you are on SolisCloud-only or a dual-protocol setup where cloud may become active, only V2 is safe to rely on for control; V1 entities will read/write successfully over Modbus but will not fail over to cloud (there is no cloud mapping to fall back to) and will simply stop updating if cloud becomes the active protocol.
