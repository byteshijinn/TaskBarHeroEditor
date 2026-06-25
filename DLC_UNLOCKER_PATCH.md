# DLC Unlocker DLL Patch — Taskbar Hero

Patches `GameAssembly.dll` so the game always reports every DLC as owned, allowing DLC-gated heroes and inventory slots to be unlocked in the save file permanently.

## TL;DR

| Item | Value |
|---|---|
| Target | `DLCManager.haz(uint)` (rename per build, e.g. `hbc`) |
| File offset | varies per build — use `apply` and let sig scan find it |
| Original sig (10) | `40 53 48 83 EC 20 80 79 38 00 8B DA` (push rbx; sub rsp,0x20; cmp byte[rcx+0x38],0; mov ebx,edx) |
| Patched bytes (6) | `B8 01 00 00 00 C3` (mov eax,1; ret) |
| Effect | All DLCs report owned → `IsUnLock=True` survives save/load |
| Safe? | Yes — instance method, not called from `.cctor` |

## Why

The game calls `haz(dlcAppId)` on save and overwrites `HeroSaveData.IsUnLock=False` for any hero whose DLC is "not owned." Save-side flips revert on the next auto-save (~3 seconds). Patching `haz` to always return `true` stops the overwrite.

## Why not patch ACTk

ACTk's `ObscuredCheatingDetector.Check / InjectionDetector.Check` look like obvious targets but they are called from ACTk's own `.cctor` to seed internal state. Returning false from them corrupts class init and the game crashes on launch with `il2cpp_runtime_class_init + mono_type_size` recursion. **Do not patch ACTk.**

## Run

```bash
# 1. Stop the game
Stop-Process -Name TaskBarHero -Force

# 2. Dry-run
python dll_patches.py

# 3. Apply
python dll_patches.py --apply

# 4. Modify save (separate scripts)
python unlock_dlc_heroes.py -w
python unlock_inventory_slots.py -w

# 5. Disable Steam Cloud sync for this game, then launch
```

## After a game update

```bash
cd re_tools/Il2CppDumper
Il2CppDumper.exe GameAssembly.dll   # regenerates dump.cs
python ../../dll_patches.py          # dry-run; checks sig still matches
```

If the prologue changed, find `DLCManager.<isOwned>(uint a)` in the new `dump.cs`, copy its first 10+ bytes, update `sig` in `PATCHES`, re-run.

## Rollback

```bash
python dll_patches.py --rollback
```

Restores the most recent `GameAssembly.dll.pre_*.bak` in the game folder.

## What it does NOT do

- Does not give currency, hero XP, levels, ability points, rune counts, or item drops.
- Does not bypass inventory slot count limits — it only stops the `IsUnlockedByRune` flag from being overwritten on save.
- Does not affect online behaviour (game runs offline; `forceApplyServerData=false`).

See `apply_dll_patches.py` upstream for the canonical version with full verification, multi-patch support, and wildcard sigs.
