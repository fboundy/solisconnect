# Protocols

SolisConnect supports local Modbus, SolisCloud, and dual-protocol entries. All modes use the same Home Assistant entity definitions and the same internal register cache, so adding cloud support does not create a second copy of every entity.

## Modes

| Mode | Reads | Writes | Best use |
| --- | --- | --- | --- |
| Modbus over TCP or serial | Full local register set for the selected inverter model | Full mapped Modbus writes | Preferred when local access is reliable. |
| SolisCloud API | Mapped `inverterDetail` fields plus mapped control CIDs | Verified control CIDs only | Use when local Modbus is unavailable or as a slower remote path. |
| Modbus + SolisCloud failover | One active protocol polls; the backup remains idle until failover | Active protocol first, then healthy backup where possible | Use when Modbus should normally be primary but cloud can cover outages, or the reverse. |
| Modbus + SolisCloud manual | One active protocol polls; a select entity chooses Modbus or cloud | Active protocol first, then healthy backup where possible | Use for maintenance or when you want explicit control of the active transport. |

In dual failover mode the integration monitors the active protocol, switches after sustained health loss, and can return to the configured primary after it has been healthy for a recovery period. In manual mode the `Active Protocol` select entity controls which protocol polls.

## SolisCloud capability

SolisCloud support is intentionally mapped into the same register/entity model as Modbus. Cloud-only mode marks entities unavailable when their registers are not covered by the current cloud mappings.

Current cloud write support is limited to verified CIDs and CID families:

| CID or range | Register(s) | Purpose |
| --- | --- | --- |
| `636` | `43110` | Storage mode bitfield |
| `157` | `43024` | Backup/reserved SOC |
| `158` | `43011` | Over-discharge SOC |
| `160` | `43018` | Force-charge SOC |
| `696` | `43074` | Feed-in power limit |
| `5916-5927` | `43707` bits 0-11 | TOU V2 slot enable switches |
| `5928-5933`, `5965`, `5969`, `5973`, `5977`, `5981`, `5984` | `43708`, `43715`, ... / `43750`, `43757`, ... | TOU V2 slot SOC values |
| `5947-5963`, `5966-5986` | `43709-43710`, ... / `43751-43752`, ... | TOU V2 slot current/voltage values |
| `5946`, `5949`, `5952`, `5955`, `5958`, `5961`, `5964`, `5968`, `5972`, `5976`, `5980`, `5987` | `43711-43714`, ... / `43753-43756`, ... | TOU V2 `HH:MM-HH:MM` time slots |
| `103` | `43141-43170` | V1 Time-Charging (18-value composite string; see `docs/source/tou.md`) |

Cloud writes read the previous CID value, send the control command, update the cache optimistically, and verify after SolisCloud catches up. If the cloud value does not match, SolisConnect restores the value reported by the device. While a write's verify is pending (up to ~15-30s), the regular poll cycle skips republishing that specific register so it can't overwrite the optimistic value with stale cloud-side data in the meantime.

The TOU V2 timed-slot CID family (`5916-5987`) is mapped into SolisConnect's local `43707-43791` register model. Feature detection: SolisCloud CID `6798` reports whether TOU V2 is enabled (`43605` / `0xAA55`); in cloud-only mode, SolisConnect reads this once at first poll and disables the TOU V2 entities if the device doesn't support it. The confirmed HMI-version threshold for V2 support is `0x4B00` (an earlier, incorrect note in this project said `0xFB00`); the config flow now auto-detects `has_v2` from the HMI version on both protocols, independent of the CID `6798` gate.

SolisCloud also reports the inverter's `HMI Version` (register `33001` "DSP Version" and `33002` "HMI Version" are populated from the `inverterDetail` fields `dspmVersionAll`/`hmiVersionAll`) so firmware version sensors work the same in cloud-only mode as they do over Modbus.

The older `Time-Charging` (TOU V1) schedule is now also mapped over cloud via control CID `103` — see `docs/source/tou.md` for the field layout, the Modbus/cloud current-value compromise, and the not-yet-live-verified caveat.

## Operational notes

Home Assistant lists SolisConnect as `local_polling` in `manifest.json`. That is still the best single Home Assistant class for the integration because local Modbus is the full-featured path, even though cloud-only and dual entries can use the SolisCloud API.

If you run another SolisCloud integration with the same key, such as `hultenvp/solis-sensor`, it may add significant SolisCloud `atRead` traffic. Consider disabling the other integration while testing SolisConnect cloud mode.

If old debug logs captured SolisCloud credentials before redaction was added, rotate the SolisCloud key secret and remove old logs.

## References

The local Modbus foundation comes from the upstream project SolisConnect was forked from: [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus). The SolisCloud support was framed with reference to [mkuthan/solis-cloud-control](https://github.com/mkuthan/solis-cloud-control) (Solis Cloud Controls: control CIDs, TOU V2 slots, `yuanzhi` write semantics) and [hultenvp/solis-sensor](https://github.com/hultenvp/solis-sensor) (SolisCloud API access patterns).
