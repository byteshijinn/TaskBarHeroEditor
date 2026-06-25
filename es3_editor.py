#!/usr/bin/env python3
"""tkinter desktop app to browse and edit SaveFile_Live.es3 (EasySave3 JSON).

Password is hardcoded; modify PASSWORD at the top if the game changes it.

Run:
    /opt/homebrew/bin/python3.13 es3_search.py
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

import demjson3
from Crypto.Cipher import AES
from es3_modifier import ES3 as _ES3Raw
from es3_modifier import DecryptionException, InvalidDataException


PASSWORD = "emuMqG3bLYJ938ZDCfieWJ"
DEFAULT_PATH = r"C:\Users\Administrator\AppData\LocalLow\TesseractStudio\TaskBarHero\SaveFile_Live.es3"


# SystemInfo HMAC key (bfky) - captured at Awake, static across all saves.
# Reverse engineered from gameassembly.dll 0xA0C3B0 (data_prep) + 0x2EB6D80 (HMAC setup)
# via x64dbg. The game computes:
#   new_SystemInfo = HMAC-SHA256(bfky, (AccountSaveData + "|" + PlayerSaveData + "|" + ownerSteamId).encode("utf-8"))
# then base64-encodes the 32-byte result and stores it under the SystemInfo key.
BFKY = bytes.fromhex("93d9429e9b72f22fdb3413193763eaba1e8cfae995f61466a81a36a609d8e456")


def compute_systeminfo(asd_json: str, psd_json: str, owner_steam_id: str) -> bytes:
    """Compute the 32-byte SystemInfo HMAC-SHA256 for a given save state."""
    msg = (asd_json + "|" + psd_json + "|" + owner_steam_id).encode("utf-8")
    return hmac.new(BFKY, msg, hashlib.sha256).digest()


def compute_systeminfo_b64(asd_json: str, psd_json: str, owner_steam_id: str) -> str:
    """Compute the SystemInfo hash and return its base64 string (as stored in the save)."""
    return base64.b64encode(compute_systeminfo(asd_json, psd_json, owner_steam_id)).decode("ascii")


def fatal_error(title: str, message: str) -> None:
    try:
        messagebox.showerror(title, message)
    finally:
        sys.exit(1)


def _parse_bool(s: str) -> bool:
    s = s.strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    raise ValueError(f"expected true/false, got {s!r}")


def _serialize_bool(b: bool) -> str:
    return "true" if b else "false"


def _pkcs7_pad(data: bytes, block_size: int) -> bytes:
    pad_len = block_size - len(data) % block_size
    return data + bytes([pad_len] * pad_len)


def _encrypt_es3(plaintext: bytes, password: str) -> bytes:
    iv = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), iv, 100, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return iv + cipher.encrypt(_pkcs7_pad(plaintext, AES.block_size))


def _infer_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict) and all(k in value for k in ("x", "y", "z")):
        return "Vector3"
    if isinstance(value, list):
        return "System.Collections.Generic.List`1[…]"
    return "object"


# ---------------------------------------------------------------------------
# Table schema for the per-collection tabs (Heroes, Stash, Runes, Attributes).
# Each entry maps a tab name to:
#   - data_path: a 1-tuple like ("heroSaveDatas",) of the nested key inside
#                the parsed PlayerSaveData dict (after get_save_data()).
#                The top-level key in self.file.data holds a string; we
#                demjson3-decode it once and treat it as a dict.
#   - columns: (key, label, width, type) for each column.
# ---------------------------------------------------------------------------

EQUIPMENT_SLOTS: list[tuple[int, str]] = [
    (0, "MainWeapon"),
    (1, "SubWeapon"),
    (2, "Helmet"),
    (3, "Armor"),
    (4, "Gloves"),
    (5, "Boots"),
    (6, "Ring1"),
    (7, "Ring2"),
    (8, "Amulet"),
    (9, "Earing"),
]


CUBE_TYPE_NAMES: dict[int, str] = {
    0: "Crafting",
    1: "Synthesis",
    2: "Decoration",
    3: "Engraving",
    4: "Inscription",
    5: "Extraction",
    6: "Alchemy",
    7: "Offering",
}

# ---------------------------------------------------------------------------
# Hero class mapping.
# EHeroType (0-5) and EEquipClassType (1-6) are the two naming systems in the
# IL2CPP dump. The heroKey in the save uses 1..6 * 100 + tier (101, 201, ...),
# which maps 1:1 to EEquipClassType. The 6 classes known in the live game are:
#   1 -> Knight    (uses SWORD + SHIELD)
#   2 -> Ranger    (uses BOW + ARROW)
#   3 -> Sorcerer  (uses STAFF + ORB)
#   4 -> Priest    (uses SCEPTER + TOME)
#   5 -> Hunter    (uses CROSSBOW + BOLT)
#   6 -> Slayer    (uses AXE + HATCHET)
#
# IMPORTANT: The current game (TBH v1.00.11) only ships tier 1 in its
# HeroInfoData table. When the user adds a hero row for a tier 2/3 key
# (e.g. 102, 203), the game silently drops it on the next auto-save because
# the row has no matching HeroInfoData entry. This is harmless (the save still
# loads) but the row will not appear in the in-game hero collection.
#
# The editor's bulk macros therefore only operate on the 6 tier 1 keys
# (see KNOWN_HERO_TIERS = [1]). The per-hero "Add Hero..." dialog still
# accepts any positive integer, so advanced users can experiment with
# speculative tier 2/3 rows.
# ---------------------------------------------------------------------------
HERO_KEY_TO_NAME: dict[int, str] = {
    1: "Knight",   2: "Ranger",   3: "Sorcerer",
    4: "Priest",   5: "Hunter",   6: "Slayer",
}
# Tier suffix. The game's data only ships tier 1 today; we still list a few
# additional tiers so the user can pre-populate any future content the
# patches might unlock.
KNOWN_HERO_TIERS: list[int] = [1]  # tier 2/3 are speculative; the game drops them on auto-save

# Stage box (loot box) schema.  The game uses a dict with three parallel lists
# (one entry per box).  See EBoxType in the IL2CPP dump (TypeDefIndex 2722).
EBOX_TYPE_NAMES: dict[int, str] = {
    0: "Normal",
    1: "Boss",
    2: "ActBoss",
}
BOX_DEFAULT_TYPE: int = 0
BOX_DEFAULT_QTY: int = 1


def box_type_name(box_type):
    """Human-readable name for an EBoxType value (0=Normal, 1=Boss, 2=ActBoss)."""
    try:
        bt = int(box_type)
    except (TypeError, ValueError):
        return f"Type{box_type}"
    return EBOX_TYPE_NAMES.get(bt, f"Type{bt}")


def list_stage_box_item_keys():
    """Return the catalog ItemKeys for STAGEBOX items (the items boxes can hold).

    These are the 9xxxxx catalog ids.  The result is cached in-process.
    Reads the same catalog that the rest of the editor uses (a {id: item_dict}
    map produced by _load_catalog)."""
    if hasattr(list_stage_box_item_keys, "_cache"):
        return list_stage_box_item_keys._cache
    try:
        items = _load_catalog() or {}
    except Exception:
        items = {}
    out = []
    for k, it in items.items():
        if not isinstance(it, dict):
            continue
        cat_type = str(it.get("type", "")).upper()
        if cat_type == "STAGEBOX":
            try:
                out.append(int(k))
            except (TypeError, ValueError):
                pass
    if not out:
        # Fallback to the legacy 9xxxxx range filter
        for k in items:
            try:
                ik = int(k)
            except (TypeError, ValueError):
                continue
            if 900000 <= ik < 1000000:
                out.append(ik)
    out.sort()
    list_stage_box_item_keys._cache = out
    return out




def hero_class_id(hero_key):
    """Return the class id (1..6) embedded in a heroKey.

    Example: 201 -> 2 (Ranger),  601 -> 6 (Slayer)."""
    if hero_key is None:
        return 0
    try:
        hk = int(hero_key)
    except (TypeError, ValueError):
        return 0
    return hk // 100


def hero_tier(hero_key):
    """Return the tier suffix (1..9) embedded in a heroKey.

    Example: 201 -> 1,  503 -> 3."""
    if hero_key is None:
        return 0
    try:
        hk = int(hero_key)
    except (TypeError, ValueError):
        return 0
    return hk % 100


def hero_class_name(hero_key):
    """Return a human-readable class label like 'Knight' for heroKey=101.

    Falls back to 'Class<N>' if the class is unknown."""
    cid = hero_class_id(hero_key)
    if cid in HERO_KEY_TO_NAME:
        return HERO_KEY_TO_NAME[cid]
    return f"Class{cid}" if cid else "?"


def hero_label(hero_key):
    """Return a compact label like 'Knight #101' for a heroKey.

    Tier 1 is omitted (just the class name). Higher tiers show as 'T2', 'T3'.."""
    try:
        hk = int(hero_key)
    except (TypeError, ValueError):
        return str(hero_key)
    cls = hero_class_name(hk)
    t = hero_tier(hk)
    if t <= 1:
        return f"{cls} #{hk}"
    return f"{cls} #{hk} (T{t})"


def list_known_hero_keys():
    """Enumerate every heroKey the user might want to add.

    The order matches the in-game UI: 101, 201, ..., 601, then 102, 202, ...
    """
    out = []
    # Tier 1 always first
    for cid in sorted(HERO_KEY_TO_NAME):
        out.append(cid * 100 + 1)
    # Then any higher tiers
    for t in KNOWN_HERO_TIERS:
        if t == 1:
            continue
        for cid in sorted(HERO_KEY_TO_NAME):
            out.append(cid * 100 + t)
    return out


def new_hero_row(hero_key, *, level=1, unlocked=True):
    """Build a fresh heroSaveDatas row for the given heroKey.

    The schema matches HeroSaveData from the IL2CPP dump (TypeDefIndex 2919).
    Default values are chosen to match what a freshly-unlocked tier-1 hero
    would look like in-game, with all equipment slots empty and all skills
    unset."""
    try:
        hk = int(hero_key)
    except (TypeError, ValueError):
        hk = 0
    try:
        lv = max(0, int(level))
    except (TypeError, ValueError):
        lv = 1
    return {
        "heroKey": hk,
        "HeroLevel": lv,
        "IsUnLock": bool(unlocked),
        "HeroExp": 0.0,
        "AbilityPoint": lv,
        "AllocatedHeroAbilityPoint": 0,
        "equippedItemIds": [0] * 10,
        "equippedSKillKey": [-1, -1, -1],
        "unlockedAttributeGroupKeys": [],
    }


TABLES: dict[str, dict] = {
    "Characters": {
        "data_path": ("heroSaveDatas",),
        "columns": [
            ("heroKey",                       "Hero",       60,  "int"),
            ("HeroLevel",                     "Level",      50,  "int"),
            ("HeroExp",                       "Exp",        90,  "float"),
            ("AbilityPoint",                  "Ability",    60,  "int"),
            ("AllocatedHeroAbilityPoint",     "Allocated",  60,  "int"),
            ("IsUnLock",                      "Unlocked",   70,  "bool"),
            ("equippedItemIds",               "Equipment",  100, "list"),
            ("equippedSKillKey",              "Skills",     100, "list"),
            ("unlockedAttributeGroupKeys",    "Groups",     80,  "list"),
        ],
    },
    "Stash": {
        "data_path": ("stashSaveDatas",),
        "columns": [
            ("Index",         "Slot",     50,  "int"),
            ("IsUnLock",      "Locked",   60,  "bool"),
            ("ItemUniqueId",  "UID",      100, "str"),
            ("ItemName",      "Name",     200, "str"),
            ("Grade",         "Grade",    80,  "str"),
            ("Type",          "Type",     70,  "str"),
            ("Gear",          "Gear",     80,  "str"),
            ("Lv",            "Lv",       50,  "int"),
        ],
    },
    "Rune Tree": {
        "data_path": ("RuneSaveData",),
        "columns": [
            ("RuneKey", "Rune Key", 80, "int"),
            ("Level",   "Level",     60, "int"),
        ],
    },
    "Skills": {
        "data_path": ("attributeSaveDatas",),
        "columns": [
            ("Key",   "Skill Key", 100, "int"),
            ("Level", "Level",     60,  "int"),
        ],
    },
    "Inventory": {
        "data_path": ("inventorySaveDatas",),
        "columns": [
            ("Index",          "Slot",         50,  "int"),
            ("IsUnlock",       "Unlocked",     70,  "bool"),
            ("IsUnlockedByRune","Rune",        60,  "bool"),
            ("ItemUniqueId",   "UID",          100, "str"),
            ("ItemName",       "Name",         200, "str"),
            ("Grade",          "Grade",        80,  "str"),
            ("Type",           "Type",         70,  "str"),
            ("Gear",           "Gear",         80,  "str"),
            ("Lv",             "Lv",           50,  "int"),
        ],
    },
    "Items": {
        "data_path": ("itemSaveDatas",),
        "columns": [
            ("ItemKey",   "Item Key",  80,  "int"),
            ("ItemName",  "Name",      220, "str"),
            ("Grade",     "Grade",     80,  "str"),
            ("Type",      "Type",      70,  "str"),
            ("Gear",      "Gear",      80,  "str"),
            ("Lv",        "Lv",        50,  "int"),
            ("UniqueId",  "Unique ID", 130, "int"),
            ("IsBlocked", "Blocked",   60,  "bool"),
            ("IsChaotic", "Chaotic",   70,  "bool"),
        ],
    },
    "Pet": {
        "data_path": ("PetSaveData",),
        "columns": [
            ("PetKey",    "Pet Key", 80,  "int"),
            ("IsUnlock",  "Unlocked", 70, "bool"),
            ("IsViewed",  "Viewed",  60,  "bool"),
        ],
    },
    "Trading Stash": {
        "data_path": ("tradingStashSaveDatas",),
        "columns": [
            ("Index",         "Slot",     50,  "int"),
            ("IsUnLock",      "Locked",   60,  "bool"),
            ("ItemUniqueId",  "UID",      100, "str"),
            ("ItemName",      "Name",     200, "str"),
            ("Grade",         "Grade",    80,  "str"),
            ("Type",          "Type",     70,  "str"),
            ("Gear",          "Gear",     80,  "str"),
            ("Lv",            "Lv",       50,  "int"),
        ],
    },
    "Cubes": {
        "data_path": ("cubeRecipeSaveDatas",),
        "columns": [
            ("CubeKey",            "Cube Key",   100, "int"),
            ("CubeRecipeTypeInt",  "Type",       60,  "int"),
            ("MaxUnlockRecipeKey", "Max Unlock", 120, "int"),
            ("SystemName",         "System",     140, "str"),
        ],
    },
    "Aggregate": {
        "data_path": ("aggregateSaveDatas",),
        "columns": [
            ("Type",    "Type",  70,  "int"),
            ("SubKey",  "SubKey", 80, "int"),
            ("Value",   "Value", 100, "int"),
        ],
    },
}

GROUPS: dict[str, list[str]] = {
    "Growth":   ["Characters", "Skills", "Rune Tree", "Pet"],
    "InvStash": ["Inventory", "Stash", "Trading Stash", "Items"],
    "Crafting": ["Cubes", "Aggregate"],
}


_ITEM_CATALOG: dict | None = None
_USE_ZH: bool = False


def _load_catalog() -> dict:
    """Lazy-load item_catalog.json into a {id: item_dict} map. Returns {} on error."""
    global _ITEM_CATALOG
    if _ITEM_CATALOG is not None:
        return _ITEM_CATALOG
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "item_catalog.json")
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        _ITEM_CATALOG = {int(item["id"]): item for item in items if "id" in item}
    except (OSError, ValueError):
        _ITEM_CATALOG = {}
    return _ITEM_CATALOG


def set_use_zh(use_zh: bool) -> None:
    global _USE_ZH
    _USE_ZH = use_zh


def _catalog_get(item_key: int | None) -> dict | None:
    """Return the catalog entry for an item key, or None."""
    if item_key is None:
        return None
    try:
        return _load_catalog().get(int(item_key))
    except (TypeError, ValueError):
        return None


def resolve_item_name(item_key: int | None) -> str:
    """Return the catalog name (en-US or zh-Hans), or str(item_key) if not found."""
    item = _catalog_get(item_key)
    if not item:
        return str(item_key) if item_key is not None else ""
    name = item.get("name", {})
    if isinstance(name, dict):
        key = "zh-Hans" if _USE_ZH else "en-US"
        return name.get(key) or str(item_key)
    return str(item_key)


def _resolve_attr(item_key: int | None, attr: str, default: str = "") -> str:
    """Return item[attr] for the given catalog key. Returns default if not in catalog."""
    item = _catalog_get(item_key)
    if not item:
        return default
    val = item.get(attr)
    return str(val) if val is not None else default


def _resolve_item_by_uid(uid: int | str | None) -> dict | None:
    """Find the itemSaveDatas entry for a given UniqueId. Returns the dict or None."""
    if not uid:
        return None
    try:
        target_uid = int(uid)
    except (TypeError, ValueError):
        return None
    if not target_uid:
        return None
    psd = get_save_data() or {}
    for it in psd.get("itemSaveDatas", []) or []:
        if isinstance(it, dict) and int(it.get("UniqueId", 0)) == target_uid:
            return it
    return None


GRADE_COLORS: dict[str, str] = {
    "COMMON":    "#888888",
    "UNCOMMON":  "#2e7d32",
    "RARE":      "#1565c0",
    "LEGENDARY": "#ef6c00",
    "IMMORTAL":  "#ffd700",
    "ARCANA":    "#c2185b",
    "BEYOND":    "#6a1b9a",
    "CELESTIAL": "#00838f",
    "DIVINE":    "#a1887f",
    "COSMIC":    "#ad1457",
}


# Canonical grade ordering. The 3rd digit of an item's ItemKey (itemKey[2])
# encodes the rarity level: 0=COMMON, 1=UNCOMMON, 2=RARE, 3=LEGENDARY,
# 4=IMMORTAL, 5=ARCANA, 6=BEYOND, 7=CELESTIAL, 8=DIVINE, 9=COSMIC.
# Keep this list in sync with item_search.KNOWN_GRADES so dropdowns in the
# Items tab and the New Item / Batch Add dialog sort by rarity level, not
# alphabetically.
KNOWN_GRADES_ORDER: tuple[str, ...] = (
    "COMMON",
    "UNCOMMON",
    "RARE",
    "LEGENDARY",
    "IMMORTAL",
    "ARCANA",
    "BEYOND",
    "CELESTIAL",
    "DIVINE",
    "COSMIC",
)

TYPE_ICONS: dict[str, str] = {
    "GEAR":     "G",
    "MATERIAL": "M",
    "STAGEBOX": "B",
}


def _grade_color(grade: str) -> str:
    return GRADE_COLORS.get(grade, "#444444")


def _type_icon(item_type: str) -> str:
    return TYPE_ICONS.get(item_type, "?")


def _affix_text(item_key: int | None) -> str:
    """Return the catalog affix field as a string, or empty if not present."""
    item = _catalog_get(item_key)
    if not item:
        return ""
    affix = item.get("affix")
    if affix is None:
        return "(none)"
    if isinstance(affix, dict):
        return ", ".join(f"{k}: {v}" for k, v in affix.items())
    return str(affix)


def _slug_text(item_key: int | None) -> str:
    item = _catalog_get(item_key)
    if not item:
        return ""
    return item.get("slug", "") or ""


def _icon_path(item_key: int | None) -> str:
    item = _catalog_get(item_key)
    if not item:
        return ""
    return item.get("icon", "") or ""


def _find_uid_in_list(rows: list, uid: int) -> int | None:
    """Find the index of a row in `rows` whose ItemUniqueId == uid. Returns None."""
    if not uid:
        return None
    for i, r in enumerate(rows):
        if isinstance(r, dict):
            try:
                if int(r.get("ItemUniqueId", 0)) == uid:
                    return i
            except (TypeError, ValueError):
                pass
    return None


def _clear_uid_at(rows: list, idx: int) -> None:
    """Set ItemUniqueId to 0 at the given index in-place."""
    if 0 <= idx < len(rows) and isinstance(rows[idx], dict):
        rows[idx]["ItemUniqueId"] = 0


def _set_uid_at(rows: list, idx: int, uid: int) -> None:
    """Set ItemUniqueId at the given index in-place."""
    if 0 <= idx < len(rows) and isinstance(rows[idx], dict):
        rows[idx]["ItemUniqueId"] = uid


def build_unique_to_key_map() -> dict[int, int]:
    """Build a runtime map: itemSaveDatas UniqueId -> ItemKey.
    Used by Inventory/Stash/TradingStash to show item names from UniqueId."""
    psd = get_save_data() or {}
    items = psd.get("itemSaveDatas", []) or []
    result: dict[int, int] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        uid = it.get("UniqueId")
        ikey = it.get("ItemKey")
        if uid is not None and ikey is not None:
            try:
                result[int(uid)] = int(ikey)
            except (TypeError, ValueError):
                pass
    return result


def _psd_parsed() -> dict:
    """Return the parsed PlayerSaveData dict, or {} if not loaded yet / not JSON."""
    import es3_search as _self
    pass  # placeholder; real implementation below


def get_nested(obj: Any, path: list[str | int]) -> Any:
    """Walk a path like ['PlayerSaveData', 'heroSaveDatas', 0, 'HeroLevel'].
    Returns the value or raises KeyError/IndexError/TypeError on miss."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur[key]
        elif isinstance(cur, list):
            cur = cur[int(key)]
        else:
            raise TypeError(f"cannot index {type(cur).__name__} with {key!r}")
    return cur


def set_nested(obj: dict, path: list[str | int], value: Any) -> None:
    """Same as get_nested, but assigns. The path must already exist
    (no auto-creation of intermediate containers — keeps callers honest)."""
    cur: Any = obj
    for key in path[:-1]:
        cur = cur[key] if isinstance(cur, dict) else cur[int(key)]
    last = path[-1]
    if isinstance(cur, dict):
        cur[last] = value
    elif isinstance(cur, list):
        cur[int(last)] = value
    else:
        raise TypeError(f"cannot assign into {type(cur).__name__}")


# ---------------------------------------------------------------------------
# Save data accessor helpers.
# The top-level ES3 file holds string-typed fields whose values are nested
# JSON. We lazy-decode the PlayerSaveData JSON once and re-encode on save.
# `get_save_data()` returns the *inner* parsed dict directly, so paths
# like `["currenySaveDatas", 0, "Quantity"]` work without prefix.
# ---------------------------------------------------------------------------

_DATA_CACHE: dict = {}


def get_save_data() -> dict:
    """Return the parsed PlayerSaveData dict (the inner JSON, not the
    {__type, value} wrapper). Mutates the file's data so writes propagate.
    """
    if "data" in _DATA_CACHE:
        return _DATA_CACHE["data"]
    self_file = _get_self_file()
    if self_file is None:
        return {}
    psd_entry = self_file.data.get("PlayerSaveData")
    if isinstance(psd_entry, dict) and "__type" in psd_entry:
        raw = psd_entry.get("value")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                try:
                    parsed = demjson3.decode(raw)
                except Exception:
                    parsed = {}
            psd_entry["value"] = parsed
    _DATA_CACHE["data"] = psd_entry["value"] if isinstance(psd_entry, dict) else {}
    return _DATA_CACHE["data"]


def get_account_data() -> dict:
    """Return the parsed AccountSaveData dict (decoded in place from its
    string value, same pattern as get_save_data for PlayerSaveData).
    Returns {} if the value is not valid JSON.
    """
    self_file = _get_self_file()
    if self_file is None:
        return {}
    entry = self_file.data.get("AccountSaveData")
    if isinstance(entry, dict) and "__type" in entry:
        raw = entry.get("value")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                try:
                    parsed = demjson3.decode(raw)
                except Exception:
                    parsed = {}
            entry["value"] = parsed
    return entry["value"] if isinstance(entry, dict) else {}


def mark_save_dirty() -> None:
    self_file = _get_self_file()
    if self_file is not None:
        self_file.dirty = True


def _get_self_file() -> Any:
    """Return the current ES3File via the global _APP_REF set in App.__init__."""
    return _APP_REF[0] if _APP_REF else None


_APP_REF: list = [None]  # 1-slot; [0] holds the current ES3File


class ES3File:
    """Wraps an ES3 JSON-mode save file: load, edit in memory, save (with .bak)."""

    def __init__(self, path: str, data: dict) -> None:
        self.path: str = path
        self.data: dict = data
        self.original_data: dict = copy.deepcopy(data)
        self.dirty: bool = False

    @classmethod
    def load(cls, path: str, *, password: str = PASSWORD) -> "ES3File":
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            fatal_error("Save file not found", f"Cannot open: {path}")
            raise
        except OSError as exc:
            fatal_error("Cannot read save file", f"{path}\n\n{exc}")
            raise
        try:
            obj = _ES3Raw(raw, password).load()
        except DecryptionException as exc:
            fatal_error("Wrong password / not an ES3 file",
                        f"AES decryption failed.\n\n{exc}")
            raise
        except InvalidDataException as exc:
            fatal_error("Decrypted data is not valid ES3 JSON",
                        f"{path}\n\n{exc}")
            raise
        if not isinstance(obj, dict):
            fatal_error(
                "Unexpected ES3 shape",
                f"Expected a JSON object at top level, got {type(obj).__name__}",
            )
            raise
        return cls(path, obj)

    def apply_edit(self, key: str, new_value: Any) -> None:
        if key in self.data:
            entry = self.data[key]
            if isinstance(entry, dict) and "__type" in entry:
                entry["value"] = new_value
            else:
                self.data[key] = new_value
        else:
            self.data[key] = {"__type": _infer_type_name(new_value),
                              "value": new_value}
        self.dirty = True

    def save(self, path: str | None = None, *, make_backup: bool = True) -> None:
        target = path or self.path
        if make_backup:
            # Save rolling pool of last 5 backups: <path>.bak1 .. <path>.bak5
            # Shift older -> newer: bak5 deleted, bak4->bak5, ..., bak1->bak2
            for i in range(5, 1, -1):
                src = f"{target}.bak{i-1}"
                dst = f"{target}.bak{i}"
                if os.path.exists(src):
                    try:
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.rename(src, dst)
                    except OSError:
                        pass
            bak1 = f"{target}.bak1"
            if os.path.exists(target):
                try:
                    if os.path.exists(bak1):
                        os.remove(bak1)
                    with open(target, "rb") as f:
                        original_bytes = f.read()
                    with open(bak1, "wb") as f:
                        f.write(original_bytes)
                except OSError as exc:
                    messagebox.showerror(
                        "Backup failed",
                        f"Could not write backup:\n{bak1}\n\n{exc}",
                    )
                    raise
        # Pre-process: any string-typed field whose value is no longer a string
        # (e.g. the user decoded its JSON content via the JSON tab and edited the dict)
        # must be re-encoded to a compact JSON string for ES3 to write back.
        serializable: dict = {}
        for key, entry in self.data.items():
            if (isinstance(entry, dict)
                    and entry.get("__type") == "string"
                    and not isinstance(entry.get("value"), str)):
                serializable[key] = {
                    **entry,
                    "value": json.dumps(entry["value"],
                                        ensure_ascii=False,
                                        separators=(",", ":")),
                }
            else:
                serializable[key] = entry
        # Recompute SystemInfo = HMAC-SHA256(bfky, ASD + "|" + PSD + "|" + ownerSteamId) base64
        asd_entry = serializable.get("AccountSaveData")
        psd_entry = serializable.get("PlayerSaveData")
        if (isinstance(asd_entry, dict) and isinstance(asd_entry.get("value"), str)
                and isinstance(psd_entry, dict) and isinstance(psd_entry.get("value"), str)):
            try:
                asd_json = asd_entry["value"]
                psd_json = psd_entry["value"]
                asd_parsed = json.loads(asd_json)
                owner_steam_id = str(asd_parsed.get("ownerSteamId", ""))
                si_b64 = compute_systeminfo_b64(asd_json, psd_json, owner_steam_id)
                si_entry = serializable.get("SystemInfo")
                if isinstance(si_entry, dict) and si_entry.get("__type") == "string":
                    serializable["SystemInfo"] = {**si_entry, "value": si_b64}
            except (json.JSONDecodeError, TypeError, ValueError):
                # Leave SystemInfo as-is if inputs are malformed
                pass
        serialized = json.dumps(serializable, ensure_ascii=False, separators=(",", ":"))
        ciphertext = _encrypt_es3(serialized.encode("utf-8"), PASSWORD)
        with open(target, "wb") as f:
            f.write(ciphertext)
        if path is not None:
            self.path = path
        self.dirty = False

    def restore_from_backup(self) -> bool:
        bak = self.path + ".bak1"
        if not os.path.exists(bak):
            return False
        with open(bak, "rb") as f:
            raw = f.read()
        obj = _ES3Raw(raw, PASSWORD).load()
        self.data = obj
        self.original_data = copy.deepcopy(obj)
        self.dirty = False
        return True


class _BatchAddDialog:
    """Search-and-pick dialog for batch-adding items. Spawned by
    App._cmd_batch_add_items. Filters the catalog by text/type/grade/
    gear/level; user multi-selects rows; on confirm, posts a list of
    selected catalog ids to the callback.

    Reuses the catalog loaded into es3_search._ITEM_CATALOG so this stays
    a single-file feature (no import from item_search required)."""

    # Column layout: (key, label, width, anchor)
    COLUMNS = (
        ("id", "ID", 80, "e"),
        ("name", "Name", 240, "w"),
        ("level", "Lv", 50, "e"),
        ("type", "Type", 90, "w"),
        ("gear", "Gear", 90, "w"),
        ("grade", "Grade", 90, "w"),
    )

    # Language switch labels (matches item_search.py LANGUAGES).
    LANG_EN = "EN"
    LANG_ZH = "中文"
    LANGUAGES = (LANG_EN, LANG_ZH)
    KNOWN_TYPES = ("GEAR", "MATERIAL", "STAGEBOX")
    KNOWN_GRADES = (
        "COMMON", "UNCOMMON", "RARE", "LEGENDARY", "IMMORTAL",
        "ARCANA", "BEYOND", "CELESTIAL", "DIVINE", "COSMIC",
    )
    KNOWN_GEARS = (
        "SWORD", "DAGGER", "BOW", "STAFF", "SHIELD", "AXE", "HATCHET",
        "MACE", "SPEAR", "CROSSBOW", "ARMOR", "HELMET", "GLOVES", "BOOTS",
        "BRACER", "RING", "AMULET", "EARING", "SCEPTER", "ORB", "TOME",
        "ARROW", "BOLT",
    )
    _FACET_ALL = "All"
    _ZH = {
        "All": "全部",
        "GEAR": "装备", "MATERIAL": "材料", "STAGEBOX": "关卡宝箱",
        "COMMON": "普通", "UNCOMMON": "高级", "RARE": "稀有",
        "LEGENDARY": "传说", "IMMORTAL": "不朽", "ARCANA": "奥秘",
        "BEYOND": "超越", "CELESTIAL": "天堂", "DIVINE": "神器", "COSMIC": "宇宙",
        "SWORD": "剑", "DAGGER": "匕香", "BOW": "弓", "STAFF": "法杖", "SHIELD": "盾牌",
        "AXE": "斧", "HATCHET": "短柄斧", "MACE": "锤", "SPEAR": "矛", "CROSSBOW": "弓染",
        "ARMOR": "护甲", "HELMET": "头盔", "GLOVES": "手套", "BOOTS": "靴子", "BRACER": "护腕",
        "RING": "戒指", "AMULET": "护身符", "EARING": "耳环",
        "SCEPTER": "权杖", "ORB": "法球", "TOME": "魔典",
        "ARROW": "箭", "BOLT": "弓枪",
    }
    _ZH_TO_EN = {v: k for k, v in _ZH.items()}

    def _label_for(self, canonical: str) -> str:
        if self.var_lang.get() == self.LANG_ZH:
            return self._ZH.get(canonical, canonical)
        return canonical

    def _to_canonical(self, value: str) -> str:
        if self.var_lang.get() == self.LANG_ZH:
            return self._ZH_TO_EN.get(value, value)
        return value

    def __init__(self, parent: "App",
                 on_confirm,
                 catalog: dict) -> None:
        """Args:
          parent     — the App instance (used for transient + geometry).
          on_confirm — callable(selected_ids: list[int],
                                 multiplier: int,
                                 bind_to_slots: bool) -> None
                       Called when the user clicks "Add".
          catalog    — {id: catalog_item_dict}.
        """
        self.parent = parent
        self.on_confirm = on_confirm
        self.catalog = catalog
        self.items = list(catalog.values())
        # Build haystacks for fast text search (EN + ZH + any other
        # locale so the user can find items regardless of UI language).
        self._haystacks = []
        for it in self.items:
            name_dict = it.get("name") or {}
            en = name_dict.get("en-US", "") or ""
            zh = name_dict.get("zh-Hans", "") or ""
            other = " ".join(v for k, v in name_dict.items()
                             if k not in ("en-US", "zh-Hans") and v)
            parts = [en, zh, other, str(it.get("id", "")),
                     it.get("slug", "") or "",
                     it.get("type", "") or "",
                     it.get("gear", "") or ""]
            self._haystacks.append(" ".join(parts).lower())
        # Pre-compute facet index sets so search() is fast
        self._idx_type: dict[str, set[int]] = {}
        self._idx_grade: dict[str, set[int]] = {}
        self._idx_gear: dict[str, set[int]] = {}
        self._idx_level: dict[int, set[int]] = {}
        for i, it in enumerate(self.items):
            t = it.get("type") or ""
            self._idx_type.setdefault(t, set()).add(i)
            g = it.get("grade") or ""
            self._idx_grade.setdefault(g, set()).add(i)
            ge = it.get("gear") or ""
            if ge:
                self._idx_gear.setdefault(ge, set()).add(i)
            lv = it.get("level")
            if isinstance(lv, int):
                self._idx_level.setdefault(lv, set()).add(i)

        self.top = tk.Toplevel(parent)
        self.top.title("Batch Add Items — pick & apply")
        self.top.geometry("900x600")
        self.top.transient(parent)
        self.top.grab_set()

        self.var_query = tk.StringVar()
        self.var_type = tk.StringVar(value="All")
        self.var_grade = tk.StringVar(value="All")
        self.var_gear = tk.StringVar(value="All")
        # Canonical (English) form of the filter values, kept in sync
        # with the displayed (possibly translated) var_type/grade/gear
        # via the trace_add callbacks below. _refresh() and
        # _on_lang_change() read from these so the filter logic is
        # locale-independent.
        self._type_canon = tk.StringVar(value="All")
        self._grade_canon = tk.StringVar(value="All")
        self._gear_canon = tk.StringVar(value="All")
        self.var_lv_min = tk.StringVar()
        self.var_lv_max = tk.StringVar()
        self.var_show_deleted = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="")
        self.var_multiplier = tk.StringVar(value="1")
        self.var_bind = tk.BooleanVar(value=True)
        self.var_lang = tk.StringVar(value=self.LANG_EN)
        self._after_id: str | None = None
        # Save the es3_search._USE_ZH flag at open-time so we can restore
        # it on close (so the main window's name display isn't affected).
        self._saved_use_zh = _USE_ZH
        self.top.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_filter_row1()
        self._build_filter_row2()
        self._build_treeview()
        self._build_footer()
        self._refresh()
        # Apply the dialog's initial lang preference to the global
        # _USE_ZH so resolve_item_name() returns names in the chosen
        # locale.
        set_use_zh(self.var_lang.get() == self.LANG_ZH)

    # --- UI construction -------------------------------------------------
    def _build_filter_row1(self) -> None:
        """Top filter row: Search + Lang combo. Mirrors item_search.py's
        first filter row (sans the Copy buttons we don't need here)."""
        frame = ttk.Frame(self.top, padding=(8, 8, 8, 2))
        frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(frame, text="Search:").pack(side=tk.LEFT)
        ent = ttk.Entry(frame, textvariable=self.var_query, width=36)
        ent.pack(side=tk.LEFT, padx=(6, 4))
        ent.bind("<KeyRelease>", lambda _e: self._schedule_refresh())
        ttk.Button(frame, text="Clear",
                   command=lambda: (self.var_query.set(""),
                                    self._refresh())
                   ).pack(side=tk.LEFT)

        # Language switch
        ttk.Label(frame, text="Lang:").pack(side=tk.LEFT, padx=(12, 0))
        self._lang_combo = ttk.Combobox(
            frame, textvariable=self.var_lang,
            values=self.LANGUAGES, state="readonly", width=6,
        )
        self._lang_combo.pack(side=tk.LEFT, padx=(4, 0))
        self.var_lang.trace_add("write", lambda *_: self._on_lang_change())

    def _build_filter_row2(self) -> None:
        """Second filter row: Type / Grade / Gear / Lv / Show deleted.
        On its own row so the top row stays compact and the dialog fits
        in 900x600 without horizontal clipping."""
        frame = ttk.Frame(self.top, padding=(8, 0, 8, 4))
        frame.pack(side=tk.TOP, fill=tk.X)

        # Type
        ttk.Label(frame, text="Type:").pack(side=tk.LEFT)
        type_values = [self._label_for(v) for v in (self._FACET_ALL, *self.KNOWN_TYPES)]
        self._type_combo = ttk.Combobox(
            frame, textvariable=self.var_type,
            values=type_values, state="readonly", width=12,
        )
        self._type_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.var_type.trace_add("write", self._on_type_display_change)
        self._type_canon.trace_add("write", self._on_type_canon_change)

        # Grade — order by itemKey[2] rarity level (matches item_search.py)
        ttk.Label(frame, text="Grade:").pack(side=tk.LEFT)
        present_grades = {it.get("grade") for it in self.items if it.get("grade")}
        ordered = [g for g in self.KNOWN_GRADES if g in present_grades]
        all_grades_canon = [self._FACET_ALL] + ordered
        grade_values = [self._label_for(v) for v in all_grades_canon]
        self._grade_combo = ttk.Combobox(
            frame, textvariable=self.var_grade,
            values=grade_values, state="readonly", width=12,
        )
        self._grade_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.var_grade.trace_add("write", self._on_grade_display_change)
        self._grade_canon.trace_add("write", self._on_grade_canon_change)

        # Gear
        ttk.Label(frame, text="Gear:").pack(side=tk.LEFT)
        all_gears_canon = [self._FACET_ALL] + sorted(
            {it.get("gear") for it in self.items if it.get("gear")})
        gear_values = [self._label_for(v) for v in all_gears_canon]
        self._gear_combo = ttk.Combobox(
            frame, textvariable=self.var_gear,
            values=gear_values, state="readonly", width=12,
        )
        self._gear_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.var_gear.trace_add("write", self._on_gear_display_change)
        self._gear_canon.trace_add("write", self._on_gear_canon_change)

        # Level range
        ttk.Label(frame, text="Lv:").pack(side=tk.LEFT, padx=(12, 0))
        ttk.Entry(frame, textvariable=self.var_lv_min, width=5,
                  justify="right").pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(frame, text="–").pack(side=tk.LEFT, padx=(2, 2))
        ttk.Entry(frame, textvariable=self.var_lv_max, width=5,
                  justify="right").pack(side=tk.LEFT)
        self.var_lv_min.trace_add("write", lambda *_: self._schedule_refresh())
        self.var_lv_max.trace_add("write", lambda *_: self._schedule_refresh())

        # Show deleted
        ttk.Checkbutton(
            frame, text="Show deleted",
            variable=self.var_show_deleted,
            command=self._refresh,
        ).pack(side=tk.LEFT, padx=(12, 0))

    def _on_filter_display_changed(self, var_display, var_canon) -> None:
        """Sync canonical state when the combobox display value
        changes. Translates the displayed label back to the English
        canonical form so the filter is locale-independent."""
        v = var_display.get()
        if not v:
            return
        if self.var_lang.get() == self.LANG_ZH:
            canon = self._ZH_TO_EN.get(v, v)
        else:
            canon = v
        if var_canon.get() != canon:
            var_canon.set(canon)

    def _on_filter_canon_changed(self, var_display, var_canon) -> None:
        """Sync display when the canonical state changes (only happens
        programmatically in _on_lang_change). Translates the canonical
        to the active locale and updates the combobox display."""
        v = var_canon.get()
        if not v:
            return
        label = self._label_for(v)
        if var_display.get() != label:
            var_display.set(label)

    def _on_type_display_change(self, *_):
        self._on_filter_display_changed(self.var_type, self._type_canon)
        if not getattr(self, "_lang_change_pending", False):
            self._schedule_refresh()

    def _on_type_canon_change(self, *_):
        self._on_filter_canon_changed(self.var_type, self._type_canon)

    def _on_grade_display_change(self, *_):
        self._on_filter_display_changed(self.var_grade, self._grade_canon)
        if not getattr(self, "_lang_change_pending", False):
            self._schedule_refresh()

    def _on_grade_canon_change(self, *_):
        self._on_filter_canon_changed(self.var_grade, self._grade_canon)

    def _on_gear_display_change(self, *_):
        self._on_filter_display_changed(self.var_gear, self._gear_canon)
        if not getattr(self, "_lang_change_pending", False):
            self._schedule_refresh()

    def _on_gear_canon_change(self, *_):
        self._on_filter_canon_changed(self.var_gear, self._gear_canon)

    def _on_lang_change(self) -> None:
        """Lang combo changed: rebuild the Type/Grade/Gear combobox
        value lists for the new locale, push the new display values
        (the canon traces keep _type_canon in sync), update _USE_ZH
        so resolve_item_name() re-resolves, and refresh the table.
        """
        present_grades = {it.get("grade") for it in self.items if it.get("grade")}
        ordered_grades = [g for g in self.KNOWN_GRADES if g in present_grades]
        all_grades_canon = [self._FACET_ALL] + ordered_grades
        all_gears_canon = [self._FACET_ALL] + sorted(
            {it.get("gear") for it in self.items if it.get("gear")})
        self._type_combo["values"] = [self._label_for(v) for v in (self._FACET_ALL, *self.KNOWN_TYPES)]
        self._grade_combo["values"] = [self._label_for(v) for v in all_grades_canon]
        self._gear_combo["values"] = [self._label_for(v) for v in all_gears_canon]
        set_use_zh(self.var_lang.get() == self.LANG_ZH)
        # Now push the new display values; the canon-trace will be a
        # no-op since canon didn't change.
        new_type = self._label_for(self._type_canon.get() or self._FACET_ALL)
        new_grade = self._label_for(self._grade_canon.get() or self._FACET_ALL)
        new_gear = self._label_for(self._gear_canon.get() or self._FACET_ALL)
        if self.var_type.get() != new_type:
            self.var_type.set(new_type)
        if self.var_grade.get() != new_grade:
            self.var_grade.set(new_grade)
        if self.var_gear.get() != new_gear:
            self.var_gear.set(new_gear)
        # The .set() calls above re-fire the var_* traces, which now
        # schedule a refresh each. Suppress those for the duration of
        # this method so the explicit _refresh() below is the only one
        # that runs.
        self._lang_change_pending = True
        try:
            self._refresh()
        finally:
            self._lang_change_pending = False

    def _on_close(self) -> None:
        # Restore the global _USE_ZH that was in effect before the
        # dialog was opened, so the main window's name display isn't
        # affected by the dialog's language choice.
        set_use_zh(self._saved_use_zh)
        self.top.destroy()

    def _build_treeview(self) -> None:
        frame = ttk.Frame(self.top)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)
        cols = tuple(c[0] for c in self.COLUMNS)
        self.tree = ttk.Treeview(
            frame, columns=cols, show="headings",
            selectmode="extended", height=14,
        )
        for key, label, w, anchor in self.COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=w, anchor=anchor, stretch=True)
        self.tree.tag_configure("deleted", foreground="#999999")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_change)
        # Ctrl-A = select all visible
        self.tree.bind("<Control-a>", self._on_select_all)
        self.tree.bind("<Control-A>", self._on_select_all)

    def _build_footer(self) -> None:
        bar = ttk.Frame(self.top, padding=(8, 4))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        # Multiplier
        ttk.Label(bar, text="×").pack(side=tk.LEFT)
        mult = ttk.Spinbox(
            bar, from_=1, to=99, textvariable=self.var_multiplier,
            width=4,
        )
        mult.pack(side=tk.LEFT, padx=(2, 12))
        # Bind toggle
        ttk.Checkbutton(
            bar, text="Place into Inventory/Stash empty slots",
            variable=self.var_bind,
        ).pack(side=tk.LEFT)
        # Status
        ttk.Label(bar, textvariable=self.var_status,
                  foreground="#666").pack(side=tk.LEFT, padx=(16, 0))
        # Buttons
        ttk.Button(bar, text="Cancel", command=self.top.destroy
                   ).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bar, text="Add", command=self._on_add
                   ).pack(side=tk.RIGHT, padx=4)

    # --- Behaviour --------------------------------------------------------
    def _schedule_refresh(self) -> None:
        """Coalesce rapid keystrokes (mirrors item_search.py)."""
        if self._after_id is not None:
            try:
                self.top.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.top.after(80, self._refresh)

    def _parse_lv(self, var: tk.StringVar) -> int | None:
        s = var.get().strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None

    def _refresh(self) -> None:
        # Compute candidate index set. The combobox values are
        # localised (e.g. "All" / "全部"), but the _idx_* maps are
        # keyed by canonical English values, so we normalise first.
        candidates: set[int] | None = None
        t = self._to_canonical(self.var_type.get())
        if t != self._FACET_ALL:
            candidates = set(self._idx_type.get(t, ()))
        g = self._to_canonical(self.var_grade.get())
        if g != self._FACET_ALL:
            gset = set(self._idx_grade.get(g, ()))
            candidates = gset if candidates is None else (candidates & gset)
        ge = self._to_canonical(self.var_gear.get())
        if ge != self._FACET_ALL:
            geset = set(self._idx_gear.get(ge, ()))
            candidates = geset if candidates is None else (candidates & geset)
        lvmin = self._parse_lv(self.var_lv_min)
        lvmax = self._parse_lv(self.var_lv_max)
        if lvmin is not None or lvmax is not None:
            lvset: set[int] = set()
            for lv, idxs in self._idx_level.items():
                if (lvmin is None or lv >= lvmin) and (
                        lvmax is None or lv <= lvmax):
                    lvset |= idxs
            candidates = lvset if candidates is None else (candidates & lvset)
        if candidates is None:
            candidates = set(range(len(self.items)))

        q = self.var_query.get().strip().lower()
        include_deleted = self.var_show_deleted.get()

        # Populate the tree
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i in sorted(candidates):
            it = self.items[i]
            if not include_deleted and it.get("deleted") is True:
                continue
            if q and q not in self._haystacks[i]:
                continue
            name = _resolve_name_from_catalog(it)
            # Translate the canonical facet values to the active locale
            # so the table cells match item_search.py's display.
            type_v = it.get("type", "") or ""
            gear_v = it.get("gear", "") or ""
            grade_v = it.get("grade", "") or ""
            values = (
                it.get("id", ""),
                name,
                it.get("level") if it.get("level") is not None else "",
                self._label_for(type_v) if type_v else "",
                self._label_for(gear_v) if gear_v else "",
                self._label_for(grade_v) if grade_v else "",
            )
            tag = "deleted" if it.get("deleted") is True else "ok"
            self.tree.insert("", "end", iid=str(i), values=values, tags=(tag,))
        self._on_select_change()

    def _on_select_change(self, _evt=None) -> None:
        n = len(self.tree.selection())
        sel_ids = [self.items[int(i)].get("id")
                   for i in self.tree.selection() if int(i) < len(self.items)]
        try:
            mult = max(1, int(self.var_multiplier.get() or "1"))
        except ValueError:
            mult = 1
        self.var_status.set(
            f"Selected: {n}   |   Will add: {n * mult} item(s)")

    def _on_select_all(self, _evt=None) -> str:
        items = self.tree.get_children()
        self.tree.selection_set(items)
        return "break"

    def _on_add(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(
                "Batch add",
                "Pick at least one item in the table (Ctrl-click for multi-select).",
                parent=self.top,
            )
            return
        try:
            mult = int(self.var_multiplier.get() or "1")
        except ValueError:
            mult = 1
        if mult < 1:
            mult = 1
        if mult > 99:
            mult = 99
        ids = [self.items[int(i)].get("id")
               for i in sel if int(i) < len(self.items)]
        bind = bool(self.var_bind.get())
        self.top.destroy()
        self.on_confirm(ids, mult, bind)


def _resolve_name_from_catalog(item: dict) -> str:
    """Return the catalog item's name in the active UI locale."""
    nm = item.get("name") or {}
    if not nm:
        return str(item.get("id", ""))
    # Use zh-Hans if _USE_ZH, else en-US, else first available
    for key in (("zh-Hans" if _USE_ZH else "en-US"), "en-US", "zh-Hans"):
        v = nm.get(key)
        if v:
            return v
    return next(iter(nm.values()))


class App(tk.Tk):
    """6-tab editor for SaveFile_Live.es3.

    Tabs (b2 + c1 + d1 + e3):
        1. Heroes   — 6 cards on top, click to expand full panel below
        2. Storage  — left sidebar 4 buttons, center grid, right slot detail
        3. Items    — top search box, 129-row table, selected → EnchantData
        4. Cubes    — 8 recipes + cube level/exp
        5. Skills   — 132 rows (Skills / Rune / Pets via sub-buttons)
        6. Resources — Gold/Stage/Account/PlayTime with inline [Set] boxes
    """
    # ------------------------------------------------------------------
    def __init__(self, file: ES3File) -> None:
        super().__init__()
        self.file = file
        _APP_REF[0] = file
        _DATA_CACHE.clear()
        self.title(f"ES3 Save Editor — {os.path.basename(file.path)}")
        self.geometry("1400x850")
        self._dirty = False
        self._undo_stack: list = []  # each entry: (label, deepcopy of PSD)
        self._redo_stack: list = []  # mirror of _undo_stack for redo
        self._MAX_UNDO = 50
        self._undo_pre_dirty_snap = None  # captured on first _begin_mutation
        self._last_edit_pre = None  # (label, deepcopy) of pre-mutation state
        self._last_edit_diff: tuple[str, list] | None = None  # (label, diffs)
        # Cumulative-diff state: baseline = PSD right after load/save, log =
        # chronological list of every successful _commit_mutation call. The
        # "Diff" popup uses these to show "all changes since baseline" + a
        # scrollable log of what was done, in addition to per-edit diffs.
        import copy
        self._baseline_psd: dict | None = (
            copy.deepcopy(get_save_data()) if get_save_data() is not None else None
        )
        self._mutation_log: list[dict] = []  # {ts, label, diffs, dirty_at}
        self._build_topbar()
        self._build_macros_bar()
        self._build_notebook()
        self._build_statusbar()
        self._build_heroes_tab()
        self._build_storage_tab()
        self._build_items_tab()
        self._build_cubes_tab()
        self._build_skills_tab()
        self._build_resources_tab()
        self._refresh_topbar()
        self._refresh_integrity()
        self.bind("<Control-z>", lambda _e: self._cmd_undo())
        self.bind("<Command-z>", lambda _e: self._cmd_undo())  # macOS
        self.bind("<Control-Shift-Z>", lambda _e: self._cmd_redo())
        self.bind("<Command-Shift-Z>", lambda _e: self._cmd_redo())  # macOS
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Top / bottom bars
    # ------------------------------------------------------------------
    def _build_topbar(self) -> None:
        self._topbar_vars: dict = {}
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(side=tk.TOP, fill=tk.X)
        # Left: 📁 Open… + editable path Entry
        ttk.Button(bar, text="📁 Open…",
                   command=self._cmd_open).pack(side=tk.LEFT, padx=(0, 4))
        self._path_var = tk.StringVar(value=self.file.path)
        self._path_entry = ttk.Entry(bar, textvariable=self._path_var,
                                     font=("Menlo", 10))
        self._path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        self._path_entry.bind("<Return>", lambda _e: self._cmd_open_path())
        # Stats (right of path, before right-aligned buttons)
        for label, kind in [("Gold", "gold"), ("Stage", "stage"),
                            ("Account Lv", "account_lv"),
                            ("Play Time", "play_time")]:
            ttk.Label(bar, text=f"{label}:").pack(side=tk.LEFT, padx=(8, 2))
            var = tk.StringVar(value="…")
            self._topbar_vars[kind] = var
            ttk.Label(bar, textvariable=var, font=("", 10, "bold"),
                      foreground="#1565c0").pack(side=tk.LEFT)
        ttk.Button(bar, text="Save", command=self._cmd_save).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bar, text="Save As…", command=self._cmd_save_as).pack(side=tk.RIGHT, padx=2)
        self._undo_btn = ttk.Button(bar, text="Undo", command=self._cmd_undo)
        self._undo_btn.pack(side=tk.RIGHT, padx=2)
        self._redo_btn = ttk.Button(bar, text="Redo", command=self._cmd_redo)
        self._redo_btn.pack(side=tk.RIGHT, padx=2)
        self._diff_btn = ttk.Button(bar, text="Diff", command=self._cmd_show_diff,
                                    state="disabled")
        self._diff_btn.pack(side=tk.RIGHT, padx=2)
        ttk.Button(bar, text="Reload", command=self._cmd_reload).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bar, text="Backup", command=self._cmd_backup).pack(side=tk.RIGHT, padx=2)
        self._dirty_lbl = ttk.Label(bar, text="●", foreground="#aaaaaa",
                                    font=("", 14, "bold"))
        self._dirty_lbl.pack(side=tk.RIGHT, padx=(8, 2))

    # ------------------------------------------------------------------
    # Quick Macros bar (one-click common edits)
    # ------------------------------------------------------------------
    def _build_macros_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 0))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(bar, text="⚡ Macros:", font=("", 9, "bold"),
                  foreground="#666").pack(side=tk.LEFT, padx=(0, 8))
        macros = [
            ("💰 Gold 999M", self._cmd_macro_gold_max),
            ("📈 Max Stage 99k", self._cmd_macro_max_stage),
            ("🧙 Unlock Heroes", self._cmd_macro_unlock_heroes),
            ("⚔ Hero Lv 99", self._cmd_macro_hero_lv_99),
            ("🗡 Cube Lv 999", self._cmd_macro_cube_lv_999),
            ("🔓 Unlock Slots", self._cmd_macro_unlock_slots),
            ("❌ Clear Empties", self._cmd_macro_clear_empties),
            # Hero unlock / add controls (added in v3)
            ("➕ Add Hero…", self._cmd_macro_add_hero),
            ("🧙+ Unlock+Add All", self._cmd_macro_unlock_and_add_heroes),
            ("📦 Batch Add Items…", self._cmd_batch_add_items),
        ]
        for label, cmd in macros:
            ttk.Button(bar, text=label, command=cmd,
                       ).pack(side=tk.LEFT, padx=2)

    def _cmd_macro_gold_max(self) -> None:
        self._begin_mutation("Gold → 999M")
        repo = self._get_repo()
        old = repo.get_gold()
        repo.set_gold(999_000_000)
        self._commit_mutation("Gold → 999M")
        self._refresh_topbar()
        self._refresh_integrity()
        self.var_status.set(f"Gold: {old:,} → 999,000,000")

    def _cmd_macro_max_stage(self) -> None:
        self._begin_mutation("Max Stage → 99k")
        repo = self._get_repo()
        old = repo.get_max_stage()
        repo.set_max_stage(99_999)
        self._commit_mutation("Max Stage → 99k")
        self._refresh_topbar()
        self.var_status.set(f"Max Stage: {old} → 99,999")

    def _cmd_macro_unlock_heroes(self) -> None:
        self._begin_mutation("Unlock all heroes")
        n = self._get_repo().unlock_all_heroes()
        self._commit_mutation("Unlock all heroes")
        self._render_hero_cards()
        self.var_status.set(f"Unlocked {n} hero(es)")

    def _cmd_macro_hero_lv_99(self) -> None:
        self._begin_mutation("Hero Lv → 99")
        n = self._get_repo().set_all_hero_levels(99)
        self._commit_mutation("Hero Lv → 99")
        if self._selected_hero_key is not None:
            self._render_hero_detail(self._selected_hero_key)
        self._render_hero_cards()
        self.var_status.set(f"Set HeroLevel=99 on {n} hero(es)")

    def _cmd_macro_add_hero(self) -> None:
        """Open a dialog to add or unlock a specific heroKey.

        Useful when the user wants only a single new hero (e.g. just
        Knight #102) without bulk-unlocking every tier 2/3 variant."""
        from tkinter import simpledialog
        repo = self._get_repo()
        existing_keys = {int(h.get("heroKey", -1))
                         for h in (repo.psd.get("heroSaveDatas") or [])
                         if isinstance(h, dict)}
        missing = [hk for hk in list_known_hero_keys() if hk not in existing_keys]
        present = sorted(existing_keys)
        if missing:
            default = str(missing[0])
        elif present:
            default = str(present[-1] + 100)
        else:
            default = "101"
        hint = ("Hero key (1..6 * 100 + tier, e.g. 101, 202, 603):\n\n"
                "Present: " + ", ".join(str(k) for k in present) + "\n"
                "Missing: " + ", ".join(str(k) for k in missing))
        try:
            raw = simpledialog.askstring(
                "Add / unlock hero", hint, initialvalue=default, parent=self)
        except Exception:
            return
        if not raw:
            return
        try:
            hk = int(str(raw).strip())
        except ValueError:
            messagebox.showerror("Invalid hero key",
                                 f"Could not parse {raw!r} as an integer.")
            return
        if hk <= 0 or hk % 100 > 9:
            messagebox.showerror(
                "Invalid hero key",
                f"Hero key {hk} is out of range. "
                "Expected a positive integer ending in 1..9.")
            return
        if not messagebox.askyesno(
                "Add / unlock hero",
                f"Add or unlock hero {hero_label(hk)}?\n\n"
                "If the hero already exists, this will just set "
                "IsUnLock=True. Otherwise a new row is created at level 1."):
            return
        self._begin_mutation(f"Add / unlock hero {hk}")
        changed = self._get_repo().unlock_hero(hk, level=1)
        self._commit_mutation(f"Add / unlock hero {hk}")
        self._render_hero_cards()
        self._select_hero(hk)
        if changed:
            self.var_status.set(f"Added new hero {hero_label(hk)}")
        else:
            self.var_status.set(f"Hero {hero_label(hk)} already unlocked")

    def _cmd_macro_unlock_and_add_heroes(self) -> None:
        """Bulk-unlock every known hero, adding rows for any that are not
        yet present in the save.

        This is the strong version of _cmd_macro_unlock_heroes: it both
        flips IsUnLock on existing rows AND inserts a new tier-1 row for
        every class that the player has not encountered in-game yet."""
        if not messagebox.askyesno(
                "Unlock + add all heroes",
                "This will:\n"
                "  - Set IsUnLock=True for every hero already in the save\n"
                "  - Add a new tier-1 row (level 1) for every missing class\n\n"
                "Proceed?"):
            return
        self._begin_mutation("Unlock + add all heroes")
        existing, added = self._get_repo().unlock_all_heroes_including_missing(level=1)
        self._commit_mutation("Unlock + add all heroes")
        self._render_hero_cards()
        msg = f"Unlocked {existing} existing, added {added} new hero(es)"
        self.var_status.set(msg)
        messagebox.showinfo("Heroes updated", msg)

    def _cmd_macro_cube_lv_999(self) -> None:

        self._begin_mutation("Cube Lv → 999")
        repo = self._get_repo()
        old = repo.get_account_level()
        repo.set_account_level(999)
        # Also grant a healthy amount of cube exp
        repo.set_account_exp(repo.get_account_exp() + 9_999_999)
        self._commit_mutation("Cube Lv → 999")
        self._refresh_topbar()
        self._refresh_integrity()
        self.var_status.set(f"Cube Level: {old} → 999 (+9,999,999 exp)")

    def _cmd_macro_unlock_slots(self) -> None:
        self._begin_mutation("Unlock all slots")
        n = self._get_repo().unlock_all_slots()
        self._commit_mutation("Unlock all slots")
        # Re-render whichever storage table is active
        active = getattr(self, "_storage_table_active", None)
        if active:
            self._render_storage_grid(active)
        self._refresh_integrity()
        self.var_status.set(f"Unlocked {n} slot(s)")

    def _cmd_macro_clear_empties(self) -> None:
        if not messagebox.askyesno(
                "Clear empty slots?",
                "Remove all rows with ItemUniqueId=0 from "
                "Inventory / Stash / Trading.\n\n"
                "(Index values are preserved.)\n\nProceed?"):
            return
        self._begin_mutation("Clear empty slots")
        n = self._get_repo().clear_empty_slots()
        self._commit_mutation("Clear empty slots")
        active = getattr(self, "_storage_table_active", None)
        if active:
            self._render_storage_grid(active)
        self._refresh_integrity()
        self.var_status.set(f"Removed {n} empty slot row(s)")

    def _cmd_batch_add_items(self) -> None:
        """Open the search-and-pick dialog for batch-adding items. The
        dialog posts a list of selected catalog ItemKey ids plus a
        multiplier and a bind-to-slots toggle; we apply the change as
        one undoable batch via _run_batch_add."""
        # Lazily load the catalog (shared with the rest of the app).
        catalog = _load_catalog()
        if not catalog:
            messagebox.showerror(
                "Batch add",
                "Item catalog is empty. Make sure item_catalog.json "
                "is next to es3_search.py.")
            return
        # The dialog's on_confirm gets called from inside the dialog.
        # We wrap it so the user gets a 'session is dirty' warning only
        # if they try to leave with selections un-applied (Tk will close
        # the dialog on Cancel, which is the natural escape).
        def _on_confirm(ids, mult, bind):
            self._run_batch_add(ids, mult, bind)
        _BatchAddDialog(self, _on_confirm, catalog)

    def _run_batch_add(self, item_keys, multiplier: int = 1,
                       bind_to_slots: bool = True) -> None:
        """Apply a batch of item additions. Creates N new itemSaveDatas
        entries per ItemKey (one per copy × multiplier), and — if
        bind_to_slots — places each new UID into the first empty slot
        in inventorySaveDatas, then stashSaveDatas. Truncates (stops
        placing) when both tables run out; remaining items stay in
        itemSaveDatas as orphan UIDs.

        Args:
          item_keys     — list of catalog ItemKey ids (ints).
          multiplier    — copies per item (1..99).
          bind_to_slots — if True, also place UIDs into empty slots.
        """
        if not item_keys:
            return
        multiplier = max(1, min(99, int(multiplier)))
        label = f"Batch add {len(item_keys)} item(s) ×{multiplier}"
        self._begin_mutation(label)
        repo = self._get_repo()
        psd = get_save_data()
        items = psd.setdefault("itemSaveDatas", [])

        # Find a free UID range. Reuse the highest existing UID + 1.
        max_uid = 0
        for it in items:
            try:
                max_uid = max(max_uid, int(it.get("UniqueId", 0)))
            except (TypeError, ValueError):
                pass
        new_uids: list[int] = []
        for ikey in item_keys:
            for _ in range(multiplier):
                max_uid += 1
                uid = max_uid
                repo.create_item(uid, int(ikey))
                new_uids.append(uid)

        placed = 0
        truncated = 0
        if bind_to_slots:
            for uid in new_uids:
                slot = repo.find_empty_slot("inventorySaveDatas")
                if slot is None:
                    slot = repo.find_empty_slot("stashSaveDatas")
                if slot is None:
                    truncated += 1
                    continue
                # find_empty_slot returns the Index; bind to the first
                # matching table. We try Inv first, then Stash.
                if repo.set_slot_uid("inventorySaveDatas", slot, uid):
                    placed += 1
                elif repo.set_slot_uid("stashSaveDatas", slot, uid):
                    placed += 1
                else:
                    truncated += 1

        self._commit_mutation(label)
        # Refresh affected tabs
        if hasattr(self, "_storage_table_active"):
            try:
                self._render_storage_grid(self._storage_table_active)
            except Exception:
                pass
        self._render_items_table()
        self._refresh_integrity()
        # Status summary
        if bind_to_slots:
            self.var_status.set(
                f"Batch add: {len(new_uids)} item(s) created, "
                f"{placed} placed in slots, {truncated} truncated "
                f"(Inventory/Stash full)")
        else:
            self.var_status.set(
                f"Batch add: {len(new_uids)} item(s) created in "
                f"itemSaveDatas (no slot binding)")

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.var_status = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.var_status,
                  foreground="#444").pack(side=tk.LEFT)
        self._integrity_lbl = ttk.Label(bar, text="", foreground="#888")
        self._integrity_lbl.pack(side=tk.RIGHT)

    def _build_notebook(self) -> None:
        self._nb = ttk.Notebook(self)
        self._nb.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._tab_heroes = ttk.Frame(self._nb, padding=8)
        self._nb.add(self._tab_heroes, text="🧙 Heroes")
        self._tab_storage = ttk.Frame(self._nb, padding=8)
        self._nb.add(self._tab_storage, text="📦 Storage")
        self._tab_items = ttk.Frame(self._nb, padding=8)
        self._nb.add(self._tab_items, text="📜 Items")
        self._tab_cubes = ttk.Frame(self._nb, padding=8)
        self._nb.add(self._tab_cubes, text="🔮 Cubes")
        self._tab_skills = ttk.Frame(self._nb, padding=8)
        self._nb.add(self._tab_skills, text="⚔ Skills")
        self._tab_resources = ttk.Frame(self._nb, padding=8)
        self._nb.add(self._tab_resources, text="💰 Resources")

    # ==================================================================
    # 1. Heroes tab  (b2: 6 cards on top, click → expand below)
    # ==================================================================
    def _build_heroes_tab(self) -> None:
        # Top: 6 cards
        self._hero_cards_frame = ttk.Frame(self._tab_heroes)
        self._hero_cards_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 8))
        # Bottom: detail panel (built per click)
        self._hero_detail_frame = ttk.Frame(self._tab_heroes)
        self._hero_detail_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._hero_card_widgets: list[dict] = []  # per hero: {btn, data}
        self._selected_hero_key: int | None = None
        self._render_hero_cards()

    def _render_hero_cards(self) -> None:
        for w in self._hero_cards_frame.winfo_children():
            w.destroy()
        self._hero_card_widgets = []
        repo = self._get_repo()
        # Render the heroes in the order they appear in heroSaveDatas, then
        # any "known but missing" heroKeys at the end as faded placeholders
        # so the user can see what they could add.
        present_rows = [h for h in (repo.psd.get("heroSaveDatas") or [])
                        if isinstance(h, dict) and "heroKey" in h]
        present_rows.sort(key=lambda h: int(h.get("heroKey", 0)))
        present_keys = {int(h.get("heroKey", -1)) for h in present_rows}
        # Use repo.list_heroes() so the order matches in-game collection
        # order (heroKey ascending).
        for h in repo.list_heroes():
            hk = int(h.get("heroKey", 0))
            self._build_hero_card(hk, h, present=True)
        # Then a section header for "missing" heroKeys
        missing = [hk for hk in list_known_hero_keys() if hk not in present_keys]
        if missing:
            sep = ttk.Separator(self._hero_cards_frame, orient="vertical")
            sep.pack(side=tk.LEFT, fill=tk.Y, padx=4)
            ttk.Label(self._hero_cards_frame,
                      text="Not in save\n(click \u2795 Add Hero)",
                      font=("", 9, "italic"),
                      foreground="#999", justify="center").pack(side=tk.LEFT,
                                                                padx=4, pady=8)
            for hk in missing[:8]:  # cap to keep the bar from spilling
                self._build_hero_card(hk, None, present=False)

    def _build_hero_card(self, hk: int, h, *, present: bool) -> None:
        """Render a single hero card. When present=False the card is a
        faded placeholder indicating a heroKey that is not in the save."""
        repo = self._get_repo()
        # Slightly wider to fit the class name comfortably
        card = ttk.Frame(self._hero_cards_frame, relief="ridge",
                         borderwidth=2, padding=6, width=220, height=170)
        card.pack(side=tk.LEFT, padx=4, pady=4)
        card.pack_propagate(False)
        cls = hero_class_name(hk)
        tier_suffix = ""
        t = hero_tier(hk)
        if t > 1:
            tier_suffix = f" T{t}"
        title_text = f"{cls} #{hk}{tier_suffix}"
        if not present:
            title_text += "  (missing)"
        ttk.Label(card, text=title_text, font=("", 11, "bold"),
                  foreground=("#222" if present else "#999")
                  ).pack(anchor="w")
        if h is None:
            ttk.Label(card, text="Not unlocked",
                      font=("", 9), foreground="#999").pack(anchor="w")
            ttk.Button(card, text="➕ Add", width=8,
                       command=lambda hk=hk: self._quick_add_hero(hk)
                       ).pack(anchor="w", pady=(8, 0))
            return
        unlocked = bool(h.get("IsUnLock"))
        lock_tag = "" if unlocked else "  🔒"
        ttk.Label(card, text=f"Lv {h.get('HeroLevel', 0)}{lock_tag}",
                  font=("", 10), foreground=("#222" if unlocked else "#a00")
                  ).pack(anchor="w")
        # Gear strip: first letter of the equipped item's gear, 6 cells
        equipped = list(h.get("equippedItemIds") or [])
        while len(equipped) < 10:
            equipped.append(0)
        strip = ttk.Frame(card)
        strip.pack(anchor="w", pady=(6, 2))
        cells = []
        for i in range(6):
            uid = int(equipped[i] or 0)
            ch = "·"
            if uid:
                it = repo.find_item_by_uid(uid)
                if it is not None:
                    g = repo.gear_of(it)
                    if g:
                        ch = g[0]
            lbl = tk.Label(strip, text=ch, width=3, height=1,
                           font=("TkFixedFont", 10, "bold"),
                           relief="groove", borderwidth=1,
                           bg="#eef" if unlocked else "#f5e0e0")
            lbl.pack(side=tk.LEFT, padx=1)
            cells.append(lbl)
        acc_filled = sum(1 for i in range(6, 10) if int(equipped[i] or 0))
        ttk.Label(card, text=f"Accessories: {acc_filled}/4",
                  font=("", 9), foreground="#666").pack(anchor="w")
        # Footer buttons: select-on-click is bound to the frame, so the
        # buttons stop propagation themselves.
        footer = ttk.Frame(card)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        if unlocked:
            ttk.Button(footer, text="Lock", width=6,
                       command=lambda hk=hk: self._quick_lock_hero(hk)
                       ).pack(side=tk.LEFT, padx=1)
        else:
            ttk.Button(footer, text="Unlock", width=6,
                       command=lambda hk=hk: self._quick_unlock_hero(hk)
                       ).pack(side=tk.LEFT, padx=1)
        ttk.Button(footer, text="Del", width=4,
                   command=lambda hk=hk: self._quick_delete_hero(hk)
                   ).pack(side=tk.RIGHT, padx=1)
        # Click anywhere on the card (except buttons) to open detail
        card.bind("<Button-1>", lambda _e, hk=hk: self._select_hero(hk))
        for child in card.winfo_children():
            # Footer buttons handle their own clicks; skip them
            if child is footer:
                continue
            try:
                child.bind("<Button-1>",
                           lambda _e, hk=hk: self._select_hero(hk))
            except tk.TclError:
                pass
        self._hero_card_widgets.append({"key": hk, "card": card, "cells": cells})

    def _quick_add_hero(self, hk: int) -> None:
        """Add a new hero row at level 1 and select it."""
        self._begin_mutation(f"Add hero {hk}")
        self._get_repo().unlock_hero(hk, level=1)
        self._commit_mutation(f"Add hero {hk}")
        self._render_hero_cards()
        self._select_hero(hk)
        self.var_status.set(f"Added hero {hero_label(hk)}")

    def _quick_lock_hero(self, hk: int) -> None:
        """Flip IsUnLock to False on the row for hk."""
        self._begin_mutation(f"Lock hero {hk}")
        self._get_repo().lock_hero(hk)
        self._commit_mutation(f"Lock hero {hk}")
        self._render_hero_cards()
        if self._selected_hero_key == hk:
            self._render_hero_detail(hk)
        self.var_status.set(f"Locked hero {hero_label(hk)}")

    def _quick_unlock_hero(self, hk: int) -> None:
        """Flip IsUnLock to True on the row for hk (no level change)."""
        self._begin_mutation(f"Unlock hero {hk}")
        self._get_repo().set_hero_field(hk, "IsUnLock", True)
        self._commit_mutation(f"Unlock hero {hk}")
        self._render_hero_cards()
        if self._selected_hero_key == hk:
            self._render_hero_detail(hk)
        self.var_status.set(f"Unlocked hero {hero_label(hk)}")

    def _quick_delete_hero(self, hk: int) -> None:
        """Remove the row for hk from heroSaveDatas."""
        if not messagebox.askyesno(
                "Delete hero?",
                f"Remove hero {hero_label(hk)} from the save?\n"
                "The game will treat it as never unlocked. This cannot be "
                "undone with Ctrl-Z (use the Undo button)."):
            return
        self._begin_mutation(f"Delete hero {hk}")
        self._get_repo().delete_hero(hk)
        self._commit_mutation(f"Delete hero {hk}")
        self._render_hero_cards()
        # If we just deleted the selected hero, clear the detail panel.
        if self._selected_hero_key == hk:
            self._selected_hero_key = None
            for w in self._hero_detail_frame.winfo_children():
                w.destroy()
        self.var_status.set(f"Deleted hero {hero_label(hk)}")
    def _reset_hero_row(self, hk: int) -> None:
        """Replace the existing row for hk with a fresh level-1 row.

        Useful when the user added a hero via ➕ Add and the game still
        treats it as "not yet met" - this wipes the cached state so the
        game has to re-initialize it on the next scene load."""
        if not messagebox.askyesno(
                "Reset hero?",
                f"Reset hero {hero_label(hk)} to a fresh level-1 row?\n"
                "All level, exp, ability points, equipment, skills and "
                "attribute groups will be cleared."):
            return
        self._begin_mutation(f"Reset hero {hk}")
        repo = self._get_repo()
        repo.delete_hero(hk)
        repo.add_hero(hk, level=1, unlocked=True)
        self._commit_mutation(f"Reset hero {hk}")
        self._render_hero_cards()
        self._select_hero(hk)
        self.var_status.set(f"Reset hero {hero_label(hk)}")


    def _select_hero(self, hero_key: int) -> None:
        self._selected_hero_key = hero_key
        # Highlight selected card
        for c in self._hero_card_widgets:
            try:
                c["card"].configure(relief="sunken" if c["key"] == hero_key else "ridge")
            except tk.TclError:
                pass
        self._render_hero_detail(hero_key)

    def _render_hero_detail(self, hero_key: int) -> None:
        for w in self._hero_detail_frame.winfo_children():
            w.destroy()
        repo = self._get_repo()
        h = repo.find_hero(hero_key)
        if h is None:
            ttk.Label(self._hero_detail_frame,
                      text=f"{hero_label(hero_key)}: not in save (use ➕ Add)")\
                .pack(anchor="w")
            ttk.Button(self._hero_detail_frame, text="➕ Add this hero",
                       command=lambda hk=hero_key: self._quick_add_hero(hk)
                       ).pack(anchor="w", pady=4)
            return
        # Header: class name + tier + IsUnLock toggle
        header = ttk.Frame(self._hero_detail_frame)
        header.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        ttk.Label(header, text=hero_label(hero_key), font=("", 12, "bold")
                  ).pack(side=tk.LEFT)
        def _apply_unlocked(v, hk=hero_key):
            self._begin_mutation(f"Hero {hk} IsUnLock")
            self._get_repo().set_hero_field(
                hk, "IsUnLock", parse_value(v, Field.BOOL))
            self._commit_mutation(f"Hero {hk} IsUnLock")
            # Re-render so the cards + status reflect the new state
            self._render_hero_cards()
            self._render_hero_detail(hk)
        # The bool combo packs its own row, so wrap it in a small left-aligned
        # sub-frame so it sits next to the title label.
        bool_frame = ttk.Frame(header)
        bool_frame.pack(side=tk.LEFT, padx=12)
        self._add_inline_bool_combo(
            bool_frame, "Unlocked",
            h.get("IsUnLock", False), _apply_unlocked)
        ttk.Button(header, text="➕ Force re-add (reset to Lv1)",
                   command=lambda hk=hero_key: self._reset_hero_row(hk)
                   ).pack(side=tk.RIGHT, padx=4)
        # Two columns: Stats (left) + Equipment (right). Skills span the full
        # width at the bottom — they only have 3 rows so this gives the 10
        # equipment rows room to breathe.
        cols = ttk.Frame(self._hero_detail_frame)
        cols.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 6))
        cols.columnconfigure(0, weight=1, uniform="hero_cols")
        cols.columnconfigure(1, weight=2, uniform="hero_cols")
        # ---- Left column: Stats
        scalars = ttk.LabelFrame(cols, text="Stats", padding=8)
        scalars.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        for label, field, ctype in [("Level", "HeroLevel", Field.INT),
                                     ("HeroExp", "HeroExp", Field.FLOAT),
                                     ("AbilityPoint", "AbilityPoint", Field.INT),
                                     ("Allocated", "AllocatedHeroAbilityPoint", Field.INT)]:
            self._add_inline_entry(scalars, label, h.get(field, 0),
                                   lambda v, hk=hero_key, f=field, ct=ctype:
                                       self._set_hero(hk, f, v, ct),
                                   ctype)
        # ---- Right column: Equipment (10 slots — main + 4 accessories)
        equip = ttk.LabelFrame(cols, text="Equipment (10 slots)", padding=8)
        equip.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        equipped = list(h.get("equippedItemIds") or [])
        while len(equipped) < 10:
            equipped.append(0)
        for i in range(10):
            self._render_equip_row(equip, hero_key, i, int(equipped[i]))
        # ---- Skills (3) — read-only display (game controls this in-app)
        skills = ttk.LabelFrame(self._hero_detail_frame, text="Skills (3, read-only)", padding=8)
        skills.pack(side=tk.TOP, fill=tk.X)
        skill_list = list(h.get("equippedSKillKey") or [])
        while len(skill_list) < 3:
            skill_list.append(-1)
        for i in range(3):
            sk = int(skill_list[i])
            ttk.Label(skills, text=f"Skill[{i}]:", width=10, anchor="w"
                      ).pack(side=tk.LEFT, padx=(4, 2))
            ttk.Label(skills, text=str(sk), width=8, anchor="w",
                      font=("", 10, "bold"), foreground="#444"
                      ).pack(side=tk.LEFT, padx=4)

    def _render_equip_row(self, parent: ttk.Frame, hero_key: int,
                          slot_idx: int, uid: int) -> None:
        repo = self._get_repo()
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=f"Equip[{slot_idx}]", width=12,
                  anchor="w").pack(side=tk.LEFT)
        var = tk.StringVar(value=str(uid))
        ent = ttk.Entry(row, textvariable=var, width=20)
        ent.pack(side=tk.LEFT, padx=4)
        name_var = tk.StringVar()
        grade_color = "#1565c0"  # default blue
        if uid:
            it = repo.find_item_by_uid(uid)
            if it is not None:
                name_var.set(resolve_item_name(it.get("ItemKey")))
                ck = _catalog_get(it.get("ItemKey"))
                if ck:
                    grade_color = _grade_color(str(ck.get("grade", "")))
        name_label = ttk.Label(row, textvariable=name_var, foreground=grade_color,
                               width=20, anchor="w")
        name_label.pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="Pick…",
                   command=lambda v=var, hk=hero_key, idx=slot_idx, nv=name_var, nl=name_label:
                       self._pick_hero_equip_uid(hk, idx, v, nv, nl)
                   ).pack(side=tk.LEFT, padx=2)
        ttk.Button(row, text="Unequip",
                   command=lambda hk=hero_key, idx=slot_idx: self._cmd_unequip(hk, idx)
                   ).pack(side=tk.LEFT, padx=2)
        ttk.Button(row, text="Swap…",
                   command=lambda hk=hero_key, idx=slot_idx: self._cmd_swap_from_hero(hk, idx)
                   ).pack(side=tk.LEFT, padx=2)

    # ==================================================================
    # 2. Storage tab  (d1: left sidebar 4 buttons, center grid, right detail)
    # ==================================================================
    def _build_storage_tab(self) -> None:
        self._storage_sidebar = ttk.Frame(self._tab_storage, width=140)
        self._storage_sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        ttk.Label(self._storage_sidebar, text="Tables", font=("", 10, "bold")
                  ).pack(anchor="w", pady=(0, 4))
        self._storage_btns: dict[str, ttk.Button] = {}
        for tbl, label in (("inventorySaveDatas", "Inventory"),
                            ("stashSaveDatas", "Stash"),
                            ("tradingStashSaveDatas", "Trading"),
                            ("boxData", "Boxes")):
            b = ttk.Button(self._storage_sidebar, text=label, width=14,
                           command=(lambda t=tbl: self._select_storage_table(t)))
            b.pack(fill=tk.X, pady=2)
            self._storage_btns[tbl] = b
        # Right: grid + detail
        right = ttk.Frame(self._tab_storage)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(right, text="Slots", font=("", 10, "bold")).pack(anchor="w")
        self._storage_grid_frame = ttk.Frame(right)
        self._storage_grid_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        # Detail panel below grid
        ttk.Separator(right, orient="horizontal").pack(fill=tk.X, pady=6)
        ttk.Label(right, text="Slot detail", font=("", 10, "bold")).pack(anchor="w")
        self._storage_detail_frame = ttk.Frame(right)
        self._storage_detail_frame.pack(fill=tk.X, pady=(4, 0))
        self._selected_storage: tuple[str, int] | None = None
        self._select_storage_table("stashSaveDatas")  # default

    def _reset_storage_detail_frame(self) -> None:
        # Destroy the current Slot-detail frame and create a fresh one.
        # Why: ttk.Frame keeps its allocated geometry even after children
        # are destroyed, so emptying the detail panel does not return the
        # freed space to the grid above. Recreating the widget is the
        # simplest way to make the layout reflow and give the grid back
        # its full height.
        parent = self._storage_detail_frame.master
        self._storage_detail_frame.destroy()
        new = ttk.Frame(parent)
        new.pack(fill=tk.X, pady=(4, 0))
        self._storage_detail_frame = new

    def _select_storage_table(self, table: str) -> None:
        self._selected_storage_table = table
        # Highlight selected button
        for t, b in self._storage_btns.items():
            try:
                b.configure(style="Selected.TButton" if t == table else "TButton")
            except tk.TclError:
                pass
        self._reset_storage_detail_frame()
        self._selected_storage = None
        if table == "boxData":
            self._storage_table_active = "boxData"
            self._render_boxes_grid()
            ttk.Label(self._storage_detail_frame,
                      text="Select a box above to edit its type / UID / qty."
                      ).pack(anchor="w", padx=4)
            return
        # Inv / Stash / Trading: show only the big table on entry. The
        # Slot-detail panel stays empty until a row is picked. Switching
        # sub-tabs (or coming back from Boxes) always re-initializes, so
        # stale detail content from another table/Boxes never lingers and
        # the grid reclaims the full height (the detail frame is recreated
        # so the layout reflows).
        self._render_storage_grid(table)

    def _render_storage_grid(self, table: str) -> None:
        for w in self._storage_grid_frame.winfo_children():
            w.destroy()
        repo = self._get_repo()
        rows = repo.psd.get(table) or []
        if not rows:
            ttk.Label(self._storage_grid_frame, text="(no rows)").pack(anchor="w")
            return
        # Use a Treeview for the grid (efficient for 300+ rows)
        cols = ("idx", "uid", "name", "grade", "lv")
        tree = ttk.Treeview(self._storage_grid_frame, columns=cols,
                            show="headings", selectmode="browse", height=14)
        for cid, label, w in [("idx", "#", 50), ("uid", "UID", 160),
                              ("name", "Name", 220),
                              ("grade", "Grade", 90),
                              ("lv", "Lv", 50)]:
            tree.heading(cid, text=label)
            tree.column(cid, width=w, anchor="w")
        # Per-grade row tag for color coding
        for grade, color in GRADE_COLORS.items():
            tree.tag_configure(f"grade_{grade}", foreground=color)
        tree.tag_configure("grade_empty", foreground="#888888")
        vsb = ttk.Scrollbar(self._storage_grid_frame, orient="vertical",
                            command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        for r in rows:
            if not isinstance(r, dict):
                continue
            idx = r.get("Index", -1)
            uid = int(r.get("ItemUniqueId", 0))
            it = repo.find_item_by_uid(uid) if uid else None
            ikey = it.get("ItemKey") if it else None
            name = resolve_item_name(ikey) if ikey is not None else ""
            ck = _catalog_get(ikey) if ikey is not None else None
            grade = str(ck.get("grade", "")) if ck else ""
            lv = int(ck.get("level") or 0) if ck else 0
            tag = f"grade_{grade}" if grade in GRADE_COLORS else "grade_empty"
            tree.insert("", "end", iid=str(int(idx)),
                        values=(idx, uid, name, grade, lv if lv else ""),
                        tags=(tag,))
        tree.bind("<<TreeviewSelect>>", lambda _e, t=table: self._on_storage_row_pick(t))
        tree.bind("<Double-Button-1>", lambda _e: self._cmd_move_storage_to_hero())
        # Right-click context menu
        tree.bind("<Button-3>", lambda e, t=table: self._show_storage_context_menu(e, t))
        tree.bind("<Button-2>", lambda e, t=table: self._show_storage_context_menu(e, t))  # macOS
        tree.bind("<Control-Button-1>", lambda e, t=table: self._show_storage_context_menu(e, t))  # fallback
        self._storage_tree = tree
        self._storage_table_active = table

    def _show_storage_context_menu(self, event, table: str) -> None:
        tree = self._storage_tree
        # Identify the row under the cursor; fall back to selected row
        row_id = tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
        if not tree.selection():
            return
        idx = int(tree.selection()[0])
        repo = self._get_repo()
        row = repo._find_slot_row(table, idx)
        if row is None:
            return
        self._selected_storage = (table, idx)
        is_empty = not int(row.get("ItemUniqueId", 0) or 0)
        menu = tk.Menu(self, tearoff=0)
        if not is_empty:
            menu.add_command(label="Move to Hero…",
                             command=self._cmd_move_storage_to_hero)
            if table != "stashSaveDatas":
                menu.add_command(label="Move to Stash",
                                 command=self._cmd_drop_to_stash)
            menu.add_separator()
            menu.add_command(label="Swap with…",
                             command=self._cmd_swap_with_storage)
            menu.add_separator()
            menu.add_command(label="Clear slot",
                             command=self._cmd_clear_slot)
        else:
            menu.add_command(label="(empty slot — no actions)",
                             state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _cmd_swap_with_storage(self) -> None:
        """Swap a hero equip slot with the selected storage slot.
        Mirrors the existing _cmd_swap_from_hero but with the storage side
        chosen by the user (the active selection)."""
        if not self._selected_storage:
            messagebox.showinfo("Swap", "Pick a storage row first.")
            return
        src_table, src_idx = self._selected_storage
        # Pick a hero slot
        choice = self._pick_hero_slot()
        if choice is None:
            return
        hero_key, equip_idx = choice
        self._begin_mutation(f"Swap {src_table}[{src_idx}] → hero {hero_key}")
        try:
            self._get_repo().swap_hero_equip_with_slot(
                hero_key, equip_idx, src_table, src_idx)
        except Exception as e:
            messagebox.showerror("Swap failed", str(e))
            return
        self._commit_mutation(f"Swap {src_table}[{src_idx}] → hero {hero_key}")
        self._render_storage_grid(src_table)
        self._render_hero_cards()
        if self._selected_hero_key == hero_key:
            self._render_hero_detail(hero_key)
        self.var_status.set(
            f"Swapped hero {hero_key}[{equip_idx}] ↔ {src_table}[{src_idx}]")

    def _on_storage_row_pick(self, table: str) -> None:
        sel = self._storage_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        self._selected_storage = (table, idx)
        self._render_storage_detail(table, idx)

    def _render_storage_detail(self, table: str, idx: int) -> None:
        for w in self._storage_detail_frame.winfo_children():
            w.destroy()
        repo = self._get_repo()
        row = repo._find_slot_row(table, idx)
        if row is None:
            ttk.Label(self._storage_detail_frame, text="(slot not found)").pack(anchor="w")
            return
        ttk.Label(self._storage_detail_frame,
                  text=f"{table}[{idx}]", font=("", 10, "bold")).pack(anchor="w")
        # Status row: Unlocked (bool), UID, ItemKey
        top = ttk.Frame(self._storage_detail_frame)
        top.pack(fill=tk.X, pady=4)
        def _apply_locked(v, t=table, i=idx):
            self._begin_mutation(f"Set {t}[{i}] IsUnLock")
            self._get_repo().set_slot_locked(
                t, i, parse_value(v, Field.BOOL))
            self._commit_mutation(f"Set {t}[{i}] IsUnLock")
        self._add_inline_bool_combo(
            top, "Unlocked",
            row.get("IsUnLock", False), _apply_locked)
        ttk.Label(top, text="UID:").pack(side=tk.LEFT, padx=(12, 0))
        uid_var = tk.StringVar(value=str(row.get("ItemUniqueId", 0)))
        ttk.Entry(top, textvariable=uid_var, width=22).pack(side=tk.LEFT, padx=4)
        def _apply_uid(v, t=table, i=idx):
            self._begin_mutation(f"Set {t}[{i}] UID")
            self._get_repo().set_slot_uid(t, i, parse_value(v, Field.INT))
            self._commit_mutation(f"Set {t}[{i}] UID")
        ttk.Button(top, text="Set",
                   command=lambda v=uid_var, fn=_apply_uid: fn(v.get())
                   ).pack(side=tk.LEFT, padx=2)
        uid = int(row.get("ItemUniqueId", 0))
        if uid:
            it = repo.find_item_by_uid(uid)
            if it is not None:
                ttk.Label(top, text="ItemKey:").pack(side=tk.LEFT, padx=(16, 0))
                key_var = tk.StringVar(value=str(it.get("ItemKey", 0)))
                ttk.Entry(top, textvariable=key_var, width=18).pack(side=tk.LEFT, padx=4)
                def _apply_itemkey(v, u=uid):
                    self._begin_mutation(f"Set UID {u} ItemKey")
                    self._get_repo().set_item_field(u, "ItemKey", parse_value(v, Field.INT))
                    self._commit_mutation(f"Set UID {u} ItemKey")
                ttk.Button(top, text="Set",
                           command=lambda v=key_var, fn=_apply_itemkey: fn(v.get())
                           ).pack(side=tk.LEFT, padx=2)
                # Cross-table buttons
                ttk.Button(top, text="→ Hero…",
                           command=lambda: self._cmd_move_storage_to_hero()
                           ).pack(side=tk.LEFT, padx=(16, 2))
                ttk.Button(top, text="Drop to Stash",
                           command=self._cmd_drop_to_stash
                           ).pack(side=tk.LEFT, padx=2)
                ttk.Button(top, text="Clear",
                           command=self._cmd_clear_slot
                           ).pack(side=tk.LEFT, padx=2)
                # EnchantData
                self._render_enchant_table(self._storage_detail_frame, uid)

    # --- Box (loot box) panel --------------------------------------------
    def _render_boxes_grid(self) -> None:
        for w in self._storage_grid_frame.winfo_children():
            w.destroy()
        repo = self._get_repo()
        boxes = repo.list_boxes()
        # Toolbar: Add Normal / Add Boss / Add ActBoss / Clear
        toolbar = ttk.Frame(self._storage_grid_frame)
        toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        ttk.Button(toolbar, text="+ Normal",
                   command=lambda: self._cmd_box_add(0)
                   ).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="+ Boss",
                   command=lambda: self._cmd_box_add(1)
                   ).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="+ ActBoss",
                   command=lambda: self._cmd_box_add(2)
                   ).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=4)
        ttk.Button(toolbar, text="Clear all",
                   command=self._cmd_box_clear
                   ).pack(side=tk.LEFT, padx=2)
        ttk.Label(toolbar, text=f"({len(boxes)} box(es))",
                  foreground="#666").pack(side=tk.LEFT, padx=8)
        if not boxes:
            ttk.Label(self._storage_grid_frame,
                      text="(no boxes - click a button above to add one)"
                      ).pack(anchor="w", padx=8, pady=4)
            return
        # Treeview of boxes
        cols = ("idx", "type", "uid", "name", "grade", "qty")
        tree = ttk.Treeview(self._storage_grid_frame, columns=cols,
                            show="headings", selectmode="browse", height=10)
        for cid, label, w in [("idx", "#", 40), ("type", "Type", 80),
                              ("uid", "UID", 160), ("name", "Item", 220),
                              ("grade", "Grade", 80), ("qty", "Qty", 50)]:
            tree.heading(cid, text=label)
            tree.column(cid, width=w, anchor="w")
        for grade, color in GRADE_COLORS.items():
            tree.tag_configure(f"grade_{grade}", foreground=color)
        tree.tag_configure("grade_empty", foreground="#888")
        tree.tag_configure("orphan", foreground="#c2185b")
        vsb = ttk.Scrollbar(self._storage_grid_frame, orient="vertical",
                            command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        for b in boxes:
            it = b.get("item")
            ikey = it.get("ItemKey") if it else None
            ck = _catalog_get(ikey) if ikey is not None else None
            grade = str(ck.get("grade", "")) if ck else ""
            name = resolve_item_name(ikey) if ikey is not None else "(orphan UID)"
            if not it:
                tag = "orphan"
            elif grade in GRADE_COLORS:
                tag = f"grade_{grade}"
            else:
                tag = "grade_empty"
            tree.insert("", "end", iid=str(int(b["index"])),
                        values=(b["index"], box_type_name(b["box_type"]),
                                b["uid"], name, grade, b["qty"]),
                        tags=(tag,))
        tree.bind("<<TreeviewSelect>>", lambda _e: self._on_box_pick(tree))
        tree.bind("<Button-3>",
                  lambda e: self._show_box_context_menu(e, tree))
        self._boxes_tree = tree

    def _on_box_pick(self, tree) -> None:
        sel = tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        self._render_box_detail(idx)

    def _render_box_detail(self, index: int) -> None:
        for w in self._storage_detail_frame.winfo_children():
            w.destroy()
        repo = self._get_repo()
        boxes = repo.list_boxes()
        if index < 0 or index >= len(boxes):
            ttk.Label(self._storage_detail_frame, text="(box not found)").pack(anchor="w")
            return
        b = boxes[index]
        ttk.Label(self._storage_detail_frame,
                  text=f'Box #{index} - {box_type_name(b["box_type"])}',
                  font=("", 10, "bold")).pack(anchor="w")
        # Type selector
        type_row = ttk.Frame(self._storage_detail_frame)
        type_row.pack(fill=tk.X, pady=2)
        ttk.Label(type_row, text="Type:", width=12, anchor="w").pack(side=tk.LEFT)
        type_var = tk.StringVar(value=box_type_name(b["box_type"]))
        type_combo = ttk.Combobox(type_row, textvariable=type_var,
                                  values=tuple(EBOX_TYPE_NAMES.values()),
                                  state="readonly", width=12)
        type_combo.pack(side=tk.LEFT, padx=4)
        def _apply_type(idx=index, var=type_var):
            label = var.get()
            for code, name in EBOX_TYPE_NAMES.items():
                if name == label:
                    self._begin_mutation(f"Box {idx} type")
                    self._get_repo().set_box_field(idx, "box_type", code)
                    self._commit_mutation(f"Box {idx} type")
                    self._render_boxes_grid()
                    self._render_box_detail(idx)
                    return
        ttk.Button(type_row, text="Set",
                   command=_apply_type).pack(side=tk.LEFT, padx=2)
        # UID + ItemKey
        uid_row = ttk.Frame(self._storage_detail_frame)
        uid_row.pack(fill=tk.X, pady=2)
        ttk.Label(uid_row, text="UID:", width=12, anchor="w").pack(side=tk.LEFT)
        uid_var = tk.StringVar(value=str(b["uid"]))
        uid_entry = ttk.Entry(uid_row, textvariable=uid_var, width=20)
        uid_entry.pack(side=tk.LEFT, padx=4)
        it = b.get("item")
        ikey = it.get("ItemKey") if it else None
        def _apply_uid(idx=index, var=uid_var):
            try:
                new_uid = int(var.get().strip())
            except ValueError:
                messagebox.showerror("Invalid UID", f"Could not parse {var.get()!r} as integer.")
                return
            self._begin_mutation(f"Box {idx} uid")
            self._get_repo().set_box_field(idx, "uid", new_uid)
            self._commit_mutation(f"Box {idx} uid")
            self._render_boxes_grid()
            self._render_box_detail(idx)
        ttk.Button(uid_row, text="Set",
                   command=_apply_uid).pack(side=tk.LEFT, padx=2)
        ttk.Button(uid_row, text="Pick UID...",
                   command=lambda idx=index: self._cmd_box_pick_uid(idx)
                   ).pack(side=tk.LEFT, padx=2)
        # ItemKey editor — only when the UID resolves to an existing item.
        # Mirrors the row in _render_storage_detail so the user can swap a
        # box's referenced item's ItemKey without picking a new UID.
        if it is not None and isinstance(it, dict):
            ikey = it.get("ItemKey")
            ikey_row = ttk.Frame(self._storage_detail_frame)
            ikey_row.pack(fill=tk.X, pady=2)
            ttk.Label(ikey_row, text="ItemKey:", width=12, anchor="w"
                      ).pack(side=tk.LEFT)
            ikey_var = tk.StringVar(value=str(ikey) if ikey is not None else "0")
            ikey_entry = ttk.Entry(ikey_row, textvariable=ikey_var, width=20)
            ikey_entry.pack(side=tk.LEFT, padx=4)
            ikey_name = ttk.Label(
                ikey_row,
                text=resolve_item_name(ikey) if ikey is not None else "(no key)",
                foreground=self._color_for_uid(int(b["uid"])) if b.get("uid") else "#1565c0",
                width=24, anchor="w",
            )
            ikey_name.pack(side=tk.LEFT, padx=4)
            def _apply_itemkey(idx=index, var=ikey_var, name_lbl=ikey_name):
                try:
                    new_key = int(var.get().strip())
                except ValueError:
                    messagebox.showerror(
                        "Invalid ItemKey",
                        f"Could not parse {var.get()!r} as integer.")
                    return
                self._begin_mutation(f"Box {idx} itemkey")
                self._get_repo().set_item_field(int(b["uid"]), "ItemKey", new_key)
                self._commit_mutation(f"Box {idx} itemkey")
                self._render_boxes_grid()
                self._render_box_detail(idx)
            ttk.Button(ikey_row, text="Set", command=_apply_itemkey
                       ).pack(side=tk.LEFT, padx=2)
        # Quantity
        qty_row = ttk.Frame(self._storage_detail_frame)
        qty_row.pack(fill=tk.X, pady=2)
        ttk.Label(qty_row, text="Quantity:", width=12, anchor="w").pack(side=tk.LEFT)
        qty_var = tk.StringVar(value=str(b["qty"]))
        qty_entry = ttk.Entry(qty_row, textvariable=qty_var, width=8)
        qty_entry.pack(side=tk.LEFT, padx=4)
        def _apply_qty(idx=index, var=qty_var):
            try:
                new_q = int(var.get().strip())
            except ValueError:
                messagebox.showerror("Invalid qty", f"Could not parse {var.get()!r} as integer.")
                return
            self._begin_mutation(f"Box {idx} qty")
            self._get_repo().set_box_field(idx, "qty", new_q)
            self._commit_mutation(f"Box {idx} qty")
            self._render_boxes_grid()
            self._render_box_detail(idx)
        ttk.Button(qty_row, text="Set",
                   command=_apply_qty).pack(side=tk.LEFT, padx=2)
        # Delete
        ttk.Separator(self._storage_detail_frame, orient="horizontal"
                      ).pack(fill=tk.X, pady=4)
        del_row = ttk.Frame(self._storage_detail_frame)
        del_row.pack(fill=tk.X, pady=2)
        ttk.Button(del_row, text="Delete this box",
                   command=lambda idx=index: self._cmd_box_delete(idx)
                   ).pack(side=tk.LEFT, padx=2)


    def _show_box_context_menu(self, event, tree) -> None:
        row_id = tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
        if not tree.selection():
            return
        idx = int(tree.selection()[0])
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Set type to Normal",
                         command=lambda i=idx: self._cmd_box_set_type(i, 0))
        menu.add_command(label="Set type to Boss",
                         command=lambda i=idx: self._cmd_box_set_type(i, 1))
        menu.add_command(label="Set type to ActBoss",
                         command=lambda i=idx: self._cmd_box_set_type(i, 2))
        menu.add_separator()
        menu.add_command(label="Pick UID...",
                         command=lambda i=idx: self._cmd_box_pick_uid(i))
        menu.add_separator()
        menu.add_command(label="Delete",
                         command=lambda i=idx: self._cmd_box_delete(i))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _cmd_box_add(self, box_type: int) -> None:
        """Open the STAGEBOX picker, then add a new box with the item attached.

        The new box is created in the same mutation as the (possibly new)
        itemSaveDatas row, so the user never sees an orphan box in the grid.
        Cancellling the picker leaves the existing BoxData unchanged."""
        ikey = self._pick_stagebox_item(
            title=f"Add {box_type_name(box_type)} box",
            prompt=f"Pick the STAGEBOX item for the new {box_type_name(box_type)} box:",
        )
        if ikey is None:
            return
        self._add_box_with_item(box_type, ikey)

    def _add_box_with_item(self, box_type: int, item_key: int) -> None:
        """Add a new box and attach a (possibly new) item in a single mutation.

        If itemSaveDatas already has a row for this ItemKey we reuse its UID.
        Otherwise we synthesize a fresh 63-bit non-zero UID (with collision
        check against existing UIDs) and create a minimal itemSaveDatas row
        via repo.create_item. The new box is appended to BoxData and its UID
        is set to the chosen UID in the same mutation.
        """
        repo = self._get_repo()
        existing = repo.find_item_by_key(item_key)
        if existing is not None:
            uid = int(existing.get("UniqueId", 0))
            if uid <= 0:
                messagebox.showerror("Bad item",
                                     f"ItemKey {item_key} has no UniqueId.")
                return
            self._begin_mutation(f"Add {box_type_name(box_type)} box (existing item)")
            new_idx = repo.add_box(box_type=box_type, uid=uid, qty=1)
            self._commit_mutation(f"Add {box_type_name(box_type)} box (existing item)")
            action = "attached existing"
        else:
            import random
            uid = random.getrandbits(63) | (1 << 62)
            existing_uids = {int(it.get("UniqueId", 0))
                              for it in (repo.psd.get("itemSaveDatas") or [])
                              if isinstance(it, dict)}
            while uid in existing_uids:
                uid = random.getrandbits(63) | (1 << 62)
            self._begin_mutation(f"Add {box_type_name(box_type)} box (new item)")
            repo.create_item(uid, item_key)
            new_idx = repo.add_box(box_type=box_type, uid=uid, qty=1)
            self._commit_mutation(f"Add {box_type_name(box_type)} box (new item)")
            action = "created new"
        self._render_boxes_grid()
        self._render_box_detail(new_idx)
        self.var_status.set(
            f"Added {box_type_name(box_type)} box #{new_idx} ({action} item, uid={uid})")

    def _pick_stagebox_item(self, title: str, prompt: str) -> int | None:
        """Show a modal listbox of STAGEBOX items. Returns the picked ItemKey
        or None if the user cancelled or no STAGEBOX items exist in the catalog.
        """
        keys = list_stage_box_item_keys()
        if not keys:
            messagebox.showerror("No STAGEBOX items",
                                 "The catalog has no STAGEBOX items to pick from.")
            return None
        choices = [f"{k:>6}  {resolve_item_name(k)}" for k in keys]
        result: list[int | None] = [None]  # mutable container for the closure
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.geometry("520x460")
        ttk.Label(dlg, text=prompt, font=("", 10, "bold")
                  ).pack(anchor="w", padx=8, pady=(8, 4))
        listbox = tk.Listbox(dlg, font=("TkFixedFont", 10), selectmode=tk.SINGLE)
        vsb = ttk.Scrollbar(dlg, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=vsb.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=4)
        vsb.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8), pady=4)
        for c in choices:
            listbox.insert(tk.END, c)
        if listbox.size() > 0:
            listbox.selection_set(0)
            listbox.activate(0)
            listbox.focus_set()
        def on_ok():
            s = listbox.curselection()
            if not s:
                return
            result[0] = keys[int(s[0])]
            dlg.destroy()
        def on_cancel():
            dlg.destroy()
        listbox.bind("<Double-Button-1>", lambda _e: on_ok())
        listbox.bind("<Return>", lambda _e: on_ok())
        btn_row = ttk.Frame(dlg)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=8)
        ttk.Button(btn_row, text="Cancel", command=on_cancel).pack(side=tk.RIGHT, padx=2)
        ttk.Button(btn_row, text="OK", command=on_ok).pack(side=tk.RIGHT, padx=2)
        dlg.wait_window(dlg)
        return result[0]

    def _cmd_box_clear(self) -> None:
        if not messagebox.askyesno(
                "Clear all boxes?",
                "Remove every box from BoxData.\n\n"
                "Indices are NOT preserved. This cannot be undone with Ctrl-Z."):
            return
        self._begin_mutation("Clear all boxes")
        n = self._get_repo().clear_boxes()
        self._commit_mutation("Clear all boxes")
        self._render_boxes_grid()
        for w in self._storage_detail_frame.winfo_children():
            w.destroy()
        self.var_status.set(f"Removed {n} box(es)")

    def _cmd_box_delete(self, index: int) -> None:
        if not messagebox.askyesno(
                "Delete box?",
                f"Remove box #{index}?"):
            return
        self._begin_mutation(f"Delete box {index}")
        self._get_repo().delete_box(index)
        self._commit_mutation(f"Delete box {index}")
        self._render_boxes_grid()
        for w in self._storage_detail_frame.winfo_children():
            w.destroy()
        self.var_status.set(f"Deleted box #{index}")

    def _cmd_box_set_type(self, index: int, box_type: int) -> None:
        self._begin_mutation(f"Box {index} type -> {box_type_name(box_type)}")
        self._get_repo().set_box_field(index, "box_type", box_type)
        self._commit_mutation(f"Box {index} type -> {box_type_name(box_type)}")
        self._render_boxes_grid()
        self._render_box_detail(index)

    def _cmd_box_pick_uid(self, index: int) -> None:
        """Open the item picker and attach the chosen item to the box at index.

        The picker only shows STAGEBOX items (catalog ids in the 9xxxxx
        range). If the user picks an item whose UID is not already in
        itemSaveDatas, a fresh row is created automatically (this is the
        same flow as the hero equipment picker)."""
        ikey = self._pick_stagebox_item(
            title="Pick a STAGEBOX item",
            prompt="Pick the item this box should reference:",
        )
        if ikey is None:
            return
        self._attach_item_to_box(index, ikey)

    def _attach_item_to_box(self, index: int, item_key: int) -> None:
        """Find or create an item with ItemKey, set the box uid to its UID.

        If itemSaveDatas already has a row for this ItemKey we reuse its
        UID. Otherwise we synthesize a fresh UniqueId, append a minimal
        itemSaveDatas row, and use that UID. The newly created item has
        no equipment, enchant, or inscription data - the game will fill
        those in when the box is actually opened."""
        repo = self._get_repo()
        existing = repo.find_item_by_key(item_key)
        if existing is not None:
            uid = int(existing.get("UniqueId", 0))
            if uid <= 0:
                messagebox.showerror("Bad item",
                                     f"ItemKey {item_key} has no UniqueId.")
                return
            self._begin_mutation(f"Box {index} uid -> existing item")
            repo.set_box_field(index, "uid", uid)
            self._commit_mutation(f"Box {index} uid -> existing item")
            action = "attached existing"
        else:
            import random
            uid = random.getrandbits(63) | (1 << 62)
            existing_uids = {int(it.get("UniqueId", 0))
                              for it in (repo.psd.get("itemSaveDatas") or [])
                              if isinstance(it, dict)}
            while uid in existing_uids:
                uid = random.getrandbits(63) | (1 << 62)
            self._begin_mutation(f"Box {index} uid -> new item")
            repo.create_item(uid, item_key)
            repo.set_box_field(index, "uid", uid)
            self._commit_mutation(f"Box {index} uid -> new item")
            action = "created new"
        self._render_boxes_grid()
        self._render_box_detail(index)
        self.var_status.set(f"Box #{index}: {action} item (uid={uid})")


    def _render_enchant_table(self, parent: ttk.Frame, uid: int) -> None:
        it = self._get_repo().find_item_by_uid(uid)
        if it is None:
            return
        ed = it.get("EnchantData") or []
        if not isinstance(ed, list) or not ed:
            return
        keys = ("StatModKey", "Tier", "Value", "RecipeType",
                "ModType", "MaterialKey", "StatType")
        # Header
        ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=4)
        head = ttk.Frame(parent); head.pack(fill=tk.X)
        ttk.Label(head, text=f"EnchantData ({len(ed)} rows × {len(keys)} keys)",
                  font=("", 10, "bold")).pack(side=tk.LEFT)
        # Action buttons (right of header)
        ttk.Button(head, text="Edit Row…",
                   command=lambda u=uid: self._edit_enchant_row(u)
                   ).pack(side=tk.RIGHT, padx=2)
        ttk.Button(head, text="Copy From…",
                   command=lambda u=uid: self._copy_enchant_from(u)
                   ).pack(side=tk.RIGHT, padx=2)
        ttk.Button(head, text="Zero All",
                   command=lambda u=uid: self._zero_all_enchant(u)
                   ).pack(side=tk.RIGHT, padx=2)
        # Compact Treeview
        cols = ("idx",) + keys
        tree_wrap = ttk.Frame(parent)
        tree_wrap.pack(fill=tk.X, pady=2)
        tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=6)
        widths = {"idx": 32, "StatModKey": 90, "Tier": 50, "Value": 60,
                  "RecipeType": 80, "ModType": 70, "MaterialKey": 90, "StatType": 70}
        for cid in cols:
            tree.heading(cid, text=cid)
            tree.column(cid, width=widths.get(cid, 80), anchor="w")
        tree.tag_configure("enchant_zero", foreground="#888")
        tree.tag_configure("enchant_nonzero", foreground="#1565c0",
                           background="#fffbe6")
        for idx, row in enumerate(ed):
            if not isinstance(row, dict):
                continue
            values = [idx] + [int(row.get(k, 0) or 0) for k in keys]
            # Highlight rows with any non-zero field
            has_value = any(int(row.get(k, 0) or 0) != 0 for k in keys)
            tag = "enchant_nonzero" if has_value else "enchant_zero"
            tree.insert("", "end", iid=str(idx), values=values, tags=(tag,))
        tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # Double-click a row to open the editor pre-loaded with that row's data
        tree.bind("<Double-Button-1>",
                  lambda _e, t=tree, u=uid: self._edit_enchant_row(
                      u, row_idx=int(t.selection()[0]) if t.selection() else 0))
        # Store the tree on the parent so other tabs don't accidentally re-use it
        # (we don't keep a strong ref; it's GC'd when the detail panel is rebuilt)
        # Hint label
        ttk.Label(parent, text="(double-click a row to edit it; non-zero rows highlighted)",
                  font=("", 8), foreground="#888").pack(anchor="w", pady=(0, 4))

    def _edit_enchant_row(self, uid: int, row_idx: int = 0) -> None:
        """Open a dialog to edit a single EnchantData row (7 fields)."""
        it = self._get_repo().find_item_by_uid(uid)
        if it is None:
            return
        ed = it.get("EnchantData")
        if not isinstance(ed, list) or not ed:
            return
        row_idx = max(0, min(row_idx, len(ed) - 1))
        target = ed[row_idx] if isinstance(ed[row_idx], dict) else {}
        keys = ("StatModKey", "Tier", "Value", "RecipeType",
                "ModType", "MaterialKey", "StatType")
        top = tk.Toplevel(self)
        top.title(f"Edit EnchantData row {row_idx} — UID {uid}")
        top.geometry("520x340"); top.transient(self); top.grab_set()
        ttk.Label(top, text=f"UID {uid}, row {row_idx}",
                  font=("", 10, "bold"), padding=(8, 8)).pack(anchor="w")
        body = ttk.Frame(top, padding=(8, 0))
        body.pack(fill=tk.BOTH, expand=True)
        row_vars: dict = {}
        for i, k in enumerate(keys):
            r = ttk.Frame(body); r.pack(fill=tk.X, pady=2)
            ttk.Label(r, text=k, width=14, anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar(value=str(int(target.get(k, 0) or 0)))
            ttk.Entry(r, textvariable=v, width=18).pack(side=tk.LEFT, padx=4)
            row_vars[k] = v
        # Quick actions
        acts = ttk.Frame(top, padding=(8, 0))
        acts.pack(fill=tk.X)
        def zero_this():
            for v in row_vars.values():
                v.set("0")
        ttk.Button(acts, text="Zero this row", command=zero_this).pack(side=tk.LEFT)
        btns = ttk.Frame(top, padding=8); btns.pack(fill=tk.X)
        def on_ok():
            self._begin_mutation(f"Edit enchant UID {uid}[{row_idx}]")
            for k in keys:
                target[k] = parse_value(row_vars[k].get(), Field.INT)
            self._commit_mutation(f"Edit enchant UID {uid}[{row_idx}]")
            self.var_status.set(f"UID {uid} enchant[{row_idx}] updated")
            # Re-render whichever detail panel is open
            if self._selected_storage:
                self._render_storage_detail(*self._selected_storage)
            self._render_items_detail()
            top.destroy()
        def on_cancel():
            top.destroy()
        ttk.Button(btns, text="Apply", command=on_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=on_cancel).pack(side=tk.RIGHT)
        top.bind("<Escape>", lambda _e: on_cancel())

    def _zero_all_enchant(self, uid: int) -> None:
        if not messagebox.askyesno(
                "Zero all enchant rows?",
                f"Set all 6 EnchantData rows of UID {uid} to 0.\n\nProceed?"):
            return
        self._begin_mutation("Zero all enchant")
        if not self._get_repo().zero_enchant(uid):
            return
        self._commit_mutation("Zero all enchant")
        if self._selected_storage:
            self._render_storage_detail(*self._selected_storage)
        self._render_items_detail()
        self.var_status.set(f"UID {uid}: all enchant rows zeroed")

    def _copy_enchant_from(self, target_uid: int) -> None:
        """Open a small picker to choose another item, then copy its
        EnchantData rows onto target_uid. The target keeps its own UniqueId
        and other fields; only the 6 rows' contents are overwritten."""
        repo = self._get_repo()
        target = repo.find_item_by_uid(target_uid)
        if target is None:
            return
        # Build a simple listbox picker over itemSaveDatas
        items = [r for r in (repo.psd.get("itemSaveDatas") or [])
                 if isinstance(r, dict) and int(r.get("UniqueId", 0) or 0)]
        if not items:
            return
        top = tk.Toplevel(self)
        top.title(f"Copy EnchantData onto UID {target_uid} from…")
        top.geometry("620x520"); top.transient(self); top.grab_set()
        ttk.Label(top, text=f"Source item (copy its 6 EnchantData rows onto UID {target_uid}):",
                  padding=(8, 8)).pack(anchor="w")
        # Filter: non-zero source first, then zero
        def nz_count(it):
            ed = it.get("EnchantData") or []
            if not isinstance(ed, list):
                return 0
            return sum(1 for r in ed if isinstance(r, dict)
                       and any(int(r.get(k, 0) or 0) != 0
                               for k in ("StatModKey", "Tier", "Value",
                                         "RecipeType", "ModType",
                                         "MaterialKey", "StatType")))
        items.sort(key=lambda it: -nz_count(it))
        # Grade filter
        filt = ttk.Frame(top, padding=(8, 0))
        filt.pack(fill=tk.X)
        ttk.Label(filt, text="Grade:").pack(side=tk.LEFT)
        grade_var = tk.StringVar(value="(Non-zero first)")
        grade_combo = ttk.Combobox(
            filt, textvariable=grade_var,
            values=["(Non-zero first)", "(All)"] + sorted(GRADE_COLORS.keys()),
            state="readonly", width=18)
        grade_combo.pack(side=tk.LEFT, padx=(4, 0))
        body = ttk.Frame(top, padding=(8, 0))
        body.pack(fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(body, font=("TkFixedFont", 10), selectmode=tk.SINGLE)
        vsb = ttk.Scrollbar(body, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=vsb.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        src_uids: list = []

        def rebuild():
            listbox.delete(0, tk.END)
            src_uids.clear()
            g_filter = grade_var.get()
            for r in items:
                uid = int(r.get("UniqueId", 0))
                ikey = r.get("ItemKey")
                ck = _catalog_get(ikey) if ikey is not None else None
                grade = str(ck.get("grade", "")) if ck else ""
                if g_filter not in ("(All)", "(Non-zero first)"):
                    if grade != g_filter:
                        continue
                n = nz_count(r)
                listbox.insert(tk.END, f"uid={uid:>20}  nz={n}/6  {grade[:6]:<6}  {resolve_item_name(ikey)[:32]}")
                src_uids.append(uid)
            if listbox.size() > 0:
                listbox.selection_set(0)
                listbox.activate(0)
                listbox.focus_set()
        grade_combo.bind("<<ComboboxSelected>>", lambda _e: rebuild())
        rebuild()
        sel = {"choice": None}
        def on_ok():
            s = listbox.curselection()
            if not s:
                return
            sel["choice"] = src_uids[int(s[0])]
            top.destroy()
        def on_cancel():
            top.destroy()
        listbox.bind("<Double-Button-1>", lambda _e: on_ok())
        listbox.bind("<Return>", lambda _e: on_ok())
        top.bind("<Escape>", lambda _e: on_cancel())
        btns = ttk.Frame(top, padding=8); btns.pack(fill=tk.X)
        ttk.Button(btns, text="Copy", command=on_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=on_cancel).pack(side=tk.RIGHT)
        self.wait_window(top)
        if sel["choice"] is None or sel["choice"] == target_uid:
            return
        self._begin_mutation("Copy enchant")
        if not repo.copy_enchant(sel["choice"], target_uid):
            return
        self._commit_mutation("Copy enchant")
        if self._selected_storage:
            self._render_storage_detail(*self._selected_storage)
        self._render_items_detail()
        self.var_status.set(
            f"UID {target_uid} enchant ← UID {sel['choice']}")

    # ==================================================================
    # 3. Items tab  (c1: single search box, 129 rows)
    # ==================================================================
    def _build_items_tab(self) -> None:
        top = ttk.Frame(self._tab_items)
        top.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        ttk.Label(top, text="Search:").pack(side=tk.LEFT)
        self._items_search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self._items_search_var, width=30)
        ent.pack(side=tk.LEFT, padx=4)
        self._items_search_var.trace_add("write", lambda *_: self._render_items_table())
        ttk.Label(top, text="(name / UID)").pack(side=tk.LEFT, padx=(0, 12))
        # Grade filter — order by itemKey[2] rarity level (matches item_search.py)
        ttk.Label(top, text="Grade:").pack(side=tk.LEFT, padx=(0, 2))
        self._items_grade_var = tk.StringVar(value="(All)")
        grade_combo = ttk.Combobox(
            top, textvariable=self._items_grade_var,
            values=["(All)"] + list(KNOWN_GRADES_ORDER),
            state="readonly", width=12)
        grade_combo.pack(side=tk.LEFT, padx=4)
        grade_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_items_table())
        # + New Item (pops the same STAGEBOX picker used by the boxes tab).
        # Use it to create a fresh itemSaveDatas row, optionally as a
        # scaffold for a new box. See BOXES.md for the canonical field order.
        ttk.Button(top, text="+ New Item",
                   command=self._cmd_items_new).pack(side=tk.LEFT, padx=(12, 2))
        # Tree
        tree_wrap = ttk.Frame(self._tab_items)
        tree_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        cols = ("uid", "key", "name", "grade", "lv", "type")
        self._items_tree = ttk.Treeview(tree_wrap, columns=cols,
                                        show="headings", selectmode="browse")
        for cid, label, w in [("uid", "UID", 180), ("key", "ItemKey", 90),
                              ("name", "Name", 220), ("grade", "Grade", 90),
                              ("lv", "Lv", 50), ("type", "Type", 100)]:
            self._items_tree.heading(cid, text=label)
            self._items_tree.column(cid, width=w, anchor="w")
        # Per-grade row tag for color coding
        for grade, color in GRADE_COLORS.items():
            self._items_tree.tag_configure(f"grade_{grade}", foreground=color)
        self._items_tree.tag_configure("grade_empty", foreground="#444")
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._items_tree.yview)
        self._items_tree.configure(yscrollcommand=vsb.set)
        self._items_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self._items_tree.bind("<<TreeviewSelect>>", lambda _e: self._render_items_detail())
        self._items_tree.bind("<Double-Button-1>", lambda _e: self._cmd_pick_item_key())
        # Detail
        ttk.Separator(self._tab_items, orient="horizontal").pack(fill=tk.X, pady=6)
        self._items_detail_frame = ttk.Frame(self._tab_items)
        self._items_detail_frame.pack(side=tk.TOP, fill=tk.X)
        self._render_items_table()

    def _render_items_table(self) -> None:
        for iid in self._items_tree.get_children():
            self._items_tree.delete(iid)
        repo = self._get_repo()
        q = self._items_search_var.get().strip().lower()
        grade_filter = self._items_grade_var.get()
        for it in repo.psd.get("itemSaveDatas") or []:
            if not isinstance(it, dict):
                continue
            uid = int(it.get("UniqueId", 0))
            ikey = it.get("ItemKey")
            name = resolve_item_name(ikey) if ikey is not None else ""
            if q and q not in name.lower() and q not in str(uid):
                continue
            grade = _resolve_attr(ikey, "grade", "") if ikey is not None else ""
            typ = _resolve_attr(ikey, "type", "") if ikey is not None else ""
            if grade_filter and grade_filter != "(All)" and grade != grade_filter:
                continue
            ck = _catalog_get(ikey) if ikey is not None else None
            lv = int(ck.get("level") or 0) if ck else 0
            tag = f"grade_{grade}" if grade in GRADE_COLORS else "grade_empty"
            self._items_tree.insert("", "end", iid=str(uid),
                                    values=(uid, ikey, name, grade,
                                            lv if lv else "", typ),
                                    tags=(tag,))

    def _render_items_detail(self) -> None:
        for w in self._items_detail_frame.winfo_children():
            w.destroy()
        sel = self._items_tree.selection()
        if not sel:
            return
        uid = int(sel[0])
        repo = self._get_repo()
        it = repo.find_item_by_uid(uid)
        if it is None:
            return
        ttk.Label(self._items_detail_frame, text=f"Item UID {uid}",
                  font=("", 10, "bold")).pack(anchor="w")
        top = ttk.Frame(self._items_detail_frame)
        top.pack(fill=tk.X, pady=4)
        for label, field, ctype in [("ItemKey", "ItemKey", Field.INT),
                                     ("IsBlocked", "IsBlocked", Field.BOOL),
                                     ("IsChaotic", "IsChaotic", Field.BOOL),
                                     ("ServerPending", "IsServerPendingItem", Field.BOOL),
                                     ("EngravingApplied", "EngravingAppliedTotalCount", Field.INT),
                                     ("InscriptionApplied", "InscriptionAppliedTotalCount", Field.INT),
                                     ("DecorationApplied", "DecorationAppliedTotalCount", Field.INT)]:
            ttk.Label(top, text=f"{label}:").pack(side=tk.LEFT, padx=(8, 2))
            var = tk.StringVar(value=serialize_value(it.get(field, 0), ctype))
            ttk.Entry(top, textvariable=var, width=12).pack(side=tk.LEFT, padx=2)
            def _apply_item_field(v, u=uid, f=field, ct=ctype):
                self._begin_mutation(f"Set UID {u} {f}")
                self._get_repo().set_item_field(u, f, parse_value(v, ct))
                self._commit_mutation(f"Set UID {u} {f}")
            ttk.Button(top, text="Set",
                       command=lambda v=var, fn=_apply_item_field: fn(v.get())
                       ).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Pick ItemKey…",
                   command=lambda u=uid: self._cmd_pick_item_key_for(u)
                   ).pack(side=tk.LEFT, padx=(8, 2))
        # EnchantCount is an int[3] array (per IL2CPP ItemSaveData 0x28).
        # The 3 slots typically read as [count_in_slot_0, count_in_slot_1, count_in_slot_2],
        # with [1, 0, 0] being the only non-trivial value seen on live items.
        # Editing it as 3 separate ints keeps the shape invariant intact.
        ec_row = ttk.Frame(self._items_detail_frame)
        ec_row.pack(fill=tk.X, pady=2)
        ttk.Label(ec_row, text="EnchantCount:",
                  foreground="#555").pack(side=tk.LEFT, padx=(8, 2))
        ec_vars = []
        cur_ec = it.get("EnchantCount") or [0, 0, 0]
        if not isinstance(cur_ec, list):
            cur_ec = [0, 0, 0]
        while len(cur_ec) < 3:
            cur_ec.append(0)
        for i in range(3):
            v = tk.StringVar(value=str(int(cur_ec[i] or 0)))
            ttk.Label(ec_row, text=f"[{i}]").pack(side=tk.LEFT, padx=(6, 2))
            ttk.Entry(ec_row, textvariable=v, width=5).pack(side=tk.LEFT, padx=2)
            ec_vars.append(v)
        def _apply_ec(u=uid, vs=ec_vars):
            out = []
            for v in vs:
                try:
                    out.append(int(v.get().strip()))
                except (TypeError, ValueError):
                    out.append(0)
            self._begin_mutation(f"Set UID {u} EnchantCount")
            self._get_repo().set_item_field(u, "EnchantCount", out)
            self._commit_mutation(f"Set UID {u} EnchantCount")
        ttk.Button(ec_row, text="Set",
                   command=_apply_ec).pack(side=tk.LEFT, padx=(6, 2))
        self._render_enchant_table(self._items_detail_frame, uid)
        # Delete row: remove this itemSaveDatas entry. Refuses if the
        # UID is bound to a slot/hero/box — see _cmd_items_delete.
        ttk.Separator(self._items_detail_frame, orient="horizontal"
                      ).pack(fill=tk.X, pady=6)
        del_row = ttk.Frame(self._items_detail_frame)
        del_row.pack(fill=tk.X, pady=2)
        def _do_delete(u=uid):
            self._cmd_items_delete(u)
        ttk.Button(del_row, text="Delete this item",
                   command=_do_delete).pack(side=tk.LEFT, padx=4)

    def _cmd_items_delete(self, uid: int) -> None:
        """Delete the itemSaveDatas row for `uid`. If the UID is
        currently bound to any Inv / Stash / Trading slot, hero equip
        slot, or box, the binding is cleared as part of the same
        operation (the user is shown the list in the confirm dialog).
        """
        repo = self._get_repo()
        if repo.find_item_by_uid(uid) is None:
            messagebox.showerror(
                "Delete failed", f"Item UID {uid} is not in itemSaveDatas.")
            return
        owners = repo.find_uid_owners(uid)
        summary_lines = []
        for o in owners:
            if o[0] in ("inventorySaveDatas", "stashSaveDatas",
                        "tradingStashSaveDatas"):
                summary_lines.append(f"  - {o[0]}[{o[1]}]")
            elif o[0] == "heroSaveDatas":
                summary_lines.append(
                    f"  - heroSaveDatas[{o[1]}].equippedItemIds[{o[2]}]")
            elif o[0] == "boxData":
                summary_lines.append(f"  - boxData[{o[1]}]")
        if summary_lines:
            prompt = (
                f"Remove item UID {uid} from itemSaveDatas?\n\n"
                f"The following bindings will be cleared:\n"
                + "\n".join(summary_lines)
                + "\n\nProceed?"
            )
        else:
            prompt = (
                f"Remove item UID {uid} from itemSaveDatas?\n\n"
                f"It is not bound to any slot, hero, or box."
            )
        if not messagebox.askyesno("Delete item?", prompt):
            return
        self._begin_mutation(f"Delete item UID {uid}")
        ok = repo.delete_item(uid)
        self._commit_mutation(f"Delete item UID {uid}")
        if not ok:
            messagebox.showerror(
                "Delete failed",
                f"Repo refused to delete UID {uid} (state may have changed).")
            return
        # Re-render the items table; if the active sub-tab is Storage
        # or Heroes, those tables need a refresh too because slots
        # / equips may have changed.
        self._render_items_table()
        for iid in self._items_tree.selection():
            self._items_tree.selection_remove(iid)
        self._render_items_detail()
        if hasattr(self, "_render_storage_grid"):
            for tbl in ("inventorySaveDatas", "stashSaveDatas",
                        "tradingStashSaveDatas"):
                if getattr(self, "_storage_table_active", None) == tbl:
                    try:
                        self._render_storage_grid(tbl)
                        if self._selected_storage is not None:
                            self._render_storage_detail(*self._selected_storage)
                    except Exception:
                        pass
        if hasattr(self, "_render_hero_cards"):
            self._render_hero_cards()
            if self._selected_hero_key is not None:
                self._render_hero_detail(self._selected_hero_key)
        if hasattr(self, "_render_boxes_grid"):
            try:
                self._render_boxes_grid()
            except Exception:
                pass
        cleared = len(summary_lines)
        if cleared:
            self.var_status.set(
                f"Deleted item UID {uid} (cleared {cleared} binding(s))")
        else:
            self.var_status.set(f"Deleted item UID {uid}")

    def _cmd_items_new(self) -> None:
        """Open the full item_search.py-style batch picker. User can
        filter by any item type (GEAR / MATERIAL / STAGEBOX / etc.),
        multi-select rows, set a multiplier, and choose to either:
          - bind the new UIDs into the first empty Inv / Stash slots
            (truncating when both tables fill), or
          - only add the rows to itemSaveDatas without binding any slot.

        Reuses _BatchAddDialog (same dialog the Macros bar's
        "📦 Batch Add Items…" button uses) and the data work is
        delegated to _run_batch_add.
        """
        catalog = _load_catalog()
        if not catalog:
            messagebox.showerror(
                "New item",
                "Item catalog is empty. Make sure item_catalog.json "
                "is next to es3_search.py.")
            return
        def _on_confirm(ids, mult, bind):
            self._run_batch_add(ids, mult, bind)
        _BatchAddDialog(self, _on_confirm, catalog)

    def _cmd_pick_item_key(self) -> None:
        sel = self._items_tree.selection()
        if not sel:
            return
        self._cmd_pick_item_key_for(int(sel[0]))

    def _cmd_pick_item_key_for(self, uid: int) -> None:
        """Open the gear-filtered item picker and use the chosen item's
        ItemKey + UniqueId to overwrite the current item's fields. Useful
        for "replace this junk sword with a legendary sword of the same gear".
        """
        repo = self._get_repo()
        cur = repo.find_item_by_uid(uid)
        if cur is None:
            return
        # Pick a target item from the items table (no "(empty)" sentinel for
        # this flow — the user wants a real item, not a clear).
        new_item = self._pick_item_for_equip(hero_key=-1, slot_idx=-1,
                                              current_uid=uid)
        if new_item is None or new_item == 0:
            return
        if new_item == uid:
            return  # user picked the same item
        # Adopt the picked item's ItemKey (keep this item's UniqueId / EnchantData
        # / IsBlocked / IsChaotic; only swap the catalog pointer).
        target = repo.find_item_by_uid(new_item)
        if target is None:
            return
        new_key = target.get("ItemKey")
        self._begin_mutation("Pick ItemKey")
        if new_key is None:
            return
        repo.set_item_field(uid, "ItemKey", new_key)
        self._commit_mutation("Pick ItemKey")
        self._render_items_table()
        self._render_items_detail()
        self._render_hero_cards()
        self.var_status.set(
            f"UID {uid}: ItemKey → {new_key} ({resolve_item_name(new_key)})")

    # ==================================================================
    # 4. Cubes tab
    # ==================================================================
    def _build_cubes_tab(self) -> None:
        top = ttk.Frame(self._tab_cubes)
        top.pack(side=tk.TOP, fill=tk.X, pady=(0, 8))
        ttk.Label(top, text="Cube level/exp:", font=("", 10, "bold")
                  ).pack(side=tk.LEFT)
        for label, ctype, setter in [
                ("Level", Field.INT, lambda v: self._get_repo().set_account_level(parse_value(v, Field.INT))),
                ("Exp", Field.INT, lambda v: self._get_repo().set_account_exp(parse_value(v, Field.INT)))]:
            ttk.Label(top, text=f"{label}:").pack(side=tk.LEFT, padx=(12, 2))
            repo = self._get_repo()
            cur = repo.get_account_level() if label == "Level" else repo.get_account_exp()
            var = tk.StringVar(value=str(cur))
            ttk.Entry(top, textvariable=var, width=12).pack(side=tk.LEFT, padx=2)
            ttk.Button(top, text="Set", command=setter).pack(side=tk.LEFT, padx=2)
        # Recipe table
        tree_wrap = ttk.Frame(self._tab_cubes)
        tree_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        cols = ("type", "cube", "max")
        self._cubes_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings")
        for cid, label, w in [("type", "RecipeType", 120), ("cube", "CubeKey", 120),
                              ("max", "MaxUnlockRecipeKey", 180)]:
            self._cubes_tree.heading(cid, text=label)
            self._cubes_tree.column(cid, width=w, anchor="w")
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._cubes_tree.yview)
        self._cubes_tree.configure(yscrollcommand=vsb.set)
        self._cubes_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        repo = self._get_repo()
        for c in repo.psd.get("cubeRecipeSaveDatas") or []:
            if isinstance(c, dict) and "CubeRecipeTypeInt" in c:
                self._cubes_tree.insert("", "end", iid=str(int(c["CubeRecipeTypeInt"])),
                                        values=(c["CubeRecipeTypeInt"], c.get("CubeKey", 0),
                                                c.get("MaxUnlockRecipeKey", 0)))
        self._cubes_tree.bind("<<TreeviewSelect>>", lambda _e: self._render_cube_detail())
        # Detail
        self._cubes_detail_frame = ttk.Frame(self._tab_cubes)
        self._cubes_detail_frame.pack(side=tk.TOP, fill=tk.X, pady=6)

    def _render_cube_detail(self) -> None:
        for w in self._cubes_detail_frame.winfo_children():
            w.destroy()
        sel = self._cubes_tree.selection()
        if not sel:
            return
        rt = int(sel[0])
        repo = self._get_repo()
        c = repo.find_cube_recipe(rt)
        if c is None:
            return
        ttk.Label(self._cubes_detail_frame, text=f"Recipe {rt}",
                  font=("", 10, "bold")).pack(anchor="w")
        # Unlocked bool (first row)
        row_top = ttk.Frame(self._cubes_detail_frame)
        row_top.pack(fill=tk.X, pady=4)
        def _apply_cube_locked(v, r=rt):
            self._begin_mutation(f"Set cube {r} IsUnLock")
            self._get_repo().set_cube_recipe_locked(r, parse_value(v, Field.BOOL))
            self._commit_mutation(f"Set cube {r} IsUnLock")
        self._add_inline_bool_combo(
            row_top, "Unlocked",
            c.get("IsUnLock", False), _apply_cube_locked)
        # CubeKey / MaxUnlock (second row)
        top = ttk.Frame(self._cubes_detail_frame)
        top.pack(fill=tk.X, pady=4)
        for label, field in [("CubeKey", "CubeKey"), ("MaxUnlock", "MaxUnlockRecipeKey")]:
            ttk.Label(top, text=f"{label}:").pack(side=tk.LEFT, padx=(8, 2))
            var = tk.StringVar(value=str(c.get(field, 0)))
            ttk.Entry(top, textvariable=var, width=12).pack(side=tk.LEFT, padx=2)
            def _apply_cube_field(v, r=rt, f=field):
                self._begin_mutation(f"Set cube {r} {f}")
                self._get_repo().find_cube_recipe(r).__setitem__(f, parse_value(v, Field.INT))
                self._commit_mutation(f"Set cube {r} {f}")
            ttk.Button(top, text="Set",
                       command=lambda v=var, fn=_apply_cube_field: fn(v.get())
                       ).pack(side=tk.LEFT, padx=2)

    # ==================================================================
    # 5. Skills tab  (Skills / Rune / Pets via 3 sub-buttons)
    # ==================================================================
    def _build_skills_tab(self) -> None:
        bar = ttk.Frame(self._tab_skills)
        bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        self._skills_kind = tk.StringVar(value="Skills")
        for kind in ("Skills", "Rune", "Pets"):
            ttk.Button(bar, text=kind,
                       command=lambda k=kind: self._skills_kind.set(k) or self._render_skills_table()
                       ).pack(side=tk.LEFT, padx=2)
        # Tree
        tree_wrap = ttk.Frame(self._tab_skills)
        tree_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        cols = ("key", "level", "extra")
        self._skills_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings")
        for cid, label, w in [("key", "Key", 120), ("level", "Level", 100),
                              ("extra", "Other", 200)]:
            self._skills_tree.heading(cid, text=label)
            self._skills_tree.column(cid, width=w, anchor="w")
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._skills_tree.yview)
        self._skills_tree.configure(yscrollcommand=vsb.set)
        self._skills_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self._skills_tree.bind("<<TreeviewSelect>>", lambda _e: self._render_skills_detail())
        # Detail
        self._skills_detail_frame = ttk.Frame(self._tab_skills)
        self._skills_detail_frame.pack(side=tk.TOP, fill=tk.X, pady=6)
        self._render_skills_table()

    def _render_skills_table(self) -> None:
        for iid in self._skills_tree.get_children():
            self._skills_tree.delete(iid)
        repo = self._get_repo()
        kind = self._skills_kind.get()
        if kind == "Skills":
            keymap, keyfield = "attributeSaveDatas", "Key"
        elif kind == "Rune":
            keymap, keyfield = "RuneSaveData", "RuneKey"
        else:
            keymap, keyfield = "PetSaveData", "PetKey"
        for r in repo.psd.get(keymap) or []:
            if not isinstance(r, dict):
                continue
            k = int(r.get(keyfield, -1))
            if kind == "Pets":
                extra = f"🔓 {r.get('IsUnlock')}  👁 {r.get('IsViewed')}"
            else:
                extra = ""
            self._skills_tree.insert("", "end", iid=str(k),
                                    values=(k, r.get("Level", 0), extra))

    def _render_skills_detail(self) -> None:
        for w in self._skills_detail_frame.winfo_children():
            w.destroy()
        sel = self._skills_tree.selection()
        if not sel:
            return
        k = int(sel[0])
        repo = self._get_repo()
        kind = self._skills_kind.get()
        if kind == "Skills":
            row = repo.find_skill(k)
            ttk.Label(self._skills_detail_frame, text=f"Skill {k}",
                      font=("", 10, "bold")).pack(anchor="w")
            self._add_inline_entry(self._skills_detail_frame, "Level",
                                   row.get("Level", 0) if row else 0,
                                   lambda v, kk=k: self._get_repo().set_skill_level(kk, parse_value(v, Field.INT)),
                                   Field.INT)
        elif kind == "Rune":
            row = repo.find_rune(k)
            ttk.Label(self._skills_detail_frame, text=f"Rune {k}",
                      font=("", 10, "bold")).pack(anchor="w")
            self._add_inline_entry(self._skills_detail_frame, "Level",
                                   row.get("Level", 0) if row else 0,
                                   lambda v, kk=k: self._get_repo().set_rune_level(kk, parse_value(v, Field.INT)),
                                   Field.INT)
        else:
            row = repo.find_pet(k)
            ttk.Label(self._skills_detail_frame, text=f"Pet {k}",
                      font=("", 10, "bold")).pack(anchor="w")
            if row is not None:
                for label, field in [("IsUnlock", "IsUnlock"),
                                       ("IsViewed", "IsViewed")]:
                    setter = (lambda v, kk=k, f=field:
                              self._get_repo().set_pet_unlocked(kk, parse_value(v, Field.BOOL))
                              if f == "IsUnlock"
                              else self._get_repo().set_pet_viewed(kk, parse_value(v, Field.BOOL)))
                    self._add_inline_bool_combo(self._skills_detail_frame, label,
                                                row.get(field, False), setter)

    # ==================================================================
    # 6. Resources tab
    # ==================================================================
    def _build_resources_tab(self) -> None:
        ttk.Label(self._tab_resources, text="Resources (singleton fields)",
                  font=("", 11, "bold")).pack(anchor="w", pady=(0, 8))
        grid = ttk.Frame(self._tab_resources)
        grid.pack(fill=tk.X)
        repo = self._get_repo()
        fields = [
            ("Gold", Field.INT, repo.get_gold(), repo.set_gold),
            ("Max Stage", Field.INT, repo.get_max_stage(), repo.set_max_stage),
            ("Current Stage", Field.INT, repo.get_current_stage_key(),
             repo.set_current_stage_key),
            ("Current Wave", Field.INT, repo.get_current_stage_wave(),
             repo.set_current_stage_wave),
            ("Account Level", Field.INT, repo.get_account_level(), repo.set_account_level),
            ("Account Exp", Field.INT, repo.get_account_exp(), repo.set_account_exp),
            ("Play Time (s)", Field.FLOAT, repo.get_play_time(), repo.set_play_time),
        ]
        for i, (label, ctype, cur, setter) in enumerate(fields):
            r = ttk.Frame(grid)
            r.grid(row=i, column=0, sticky="ew", padx=4, pady=2)
            ttk.Label(r, text=label, width=18, anchor="w").pack(side=tk.LEFT)
            var = tk.StringVar(value=serialize_value(cur, ctype))
            ttk.Entry(r, textvariable=var, width=18).pack(side=tk.LEFT, padx=4)
            ttk.Button(r, text="Set",
                       command=lambda v=var, ct=ctype, s=setter:
                           s(parse_value(v, ct))
                       ).pack(side=tk.LEFT, padx=2)
        # Aggregate list
        ttk.Separator(self._tab_resources, orient="horizontal").pack(fill=tk.X, pady=12)
        ttk.Label(self._tab_resources, text="Aggregate rows (Type/SubKey/Value)",
                  font=("", 10, "bold")).pack(anchor="w")
        agg_wrap = ttk.Frame(self._tab_resources)
        agg_wrap.pack(fill=tk.BOTH, expand=True, pady=4)
        cols = ("type", "sub", "val")
        self._agg_tree = ttk.Treeview(agg_wrap, columns=cols, show="headings", height=10)
        for cid, label, w in [("type", "Type", 100), ("sub", "SubKey", 120),
                              ("val", "Value", 160)]:
            self._agg_tree.heading(cid, text=label)
            self._agg_tree.column(cid, width=w, anchor="w")
        vsb = ttk.Scrollbar(agg_wrap, orient="vertical", command=self._agg_tree.yview)
        self._agg_tree.configure(yscrollcommand=vsb.set)
        self._agg_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        for r in repo.psd.get("aggregateSaveDatas") or []:
            if isinstance(r, dict):
                self._agg_tree.insert("", "end", iid=f"{r.get('Type')}_{r.get('SubKey')}",
                                      values=(r.get("Type", 0), r.get("SubKey", 0),
                                              r.get("Value", 0)))

    # ==================================================================
    # Cross-table actions
    # ==================================================================
    def _cmd_unequip(self, hero_key: int, slot_idx: int) -> None:
        self._begin_mutation(
            f"Unequip hero {hero_key} slot {slot_idx} → stash")
        try:
            try:
                self._get_repo().detach_uid_from_hero(hero_key, slot_idx,
                                                      into="stashSaveDatas")
            except NoEmptySlotError as e:
                choice = self._pick_slot(
                    f"No empty stash slot for UID {e.uid}. Pick one:",
                    allowed_tables=("stashSaveDatas",))
                if choice is None:
                    # User cancelled the slot picker — undo the pre-snapshot
                    # we already captured so the chain stays clean.
                    self._undo_pre_dirty_snap = None
                    self._last_edit_pre = None
                    self._last_edit_diff = None
                    self._update_diff_button()
                    self._dirty = False
                    self.file.dirty = False
                    self._update_dirty_indicator()
                    return
                self._get_repo().detach_uid_from_hero(
                    hero_key, slot_idx, into="stashSaveDatas",
                    target_idx=choice[1])
        except Exception as e:
            messagebox.showerror("Unequip failed", str(e))
            return
        self._commit_mutation(
            f"Unequip hero {hero_key} slot {slot_idx} → stash")
        if self._selected_hero_key == hero_key:
            self._render_hero_detail(hero_key)
        self._render_hero_cards()
        self._refresh_topbar()
        self.var_status.set(f"Unequipped hero {hero_key} slot {slot_idx}")

    def _cmd_swap_from_hero(self, hero_key: int, slot_idx: int) -> None:
        # Pre-populate gear filter to match the currently equipped item's gear
        repo = self._get_repo()
        h = repo.find_hero(hero_key)
        equipped = (h.get("equippedItemIds") or []) if h else []
        current_uid = int(equipped[slot_idx]) if slot_idx < len(equipped) else 0
        default_gear = ""
        if current_uid:
            it = repo.find_item_by_uid(current_uid)
            if it is not None:
                default_gear = repo.gear_of(it)
        # Pick a target slot (across all 3 tables)
        choice = self._pick_slot(
            "Swap with… (this hero slot ↔ target slot)",
            default_gear=default_gear)
        if choice is None:
            return
        target_table, target_idx = choice
        self._begin_mutation(f"Swap hero {hero_key}[{slot_idx}]")
        self._get_repo().swap_hero_equip_with_slot(
            hero_key, slot_idx, target_table, target_idx)
        self._commit_mutation(f"Swap hero {hero_key}[{slot_idx}]")
        self._render_hero_detail(hero_key)
        self._render_hero_cards()
        self.var_status.set(f"Swapped hero {hero_key}[{slot_idx}] ↔ {target_table}[{target_idx}]")

    def _cmd_move_storage_to_hero(self) -> None:
        if not self._selected_storage:
            messagebox.showinfo("Move to Hero", "Pick a storage row first.")
            return
        src_table, src_idx = self._selected_storage
        choice = self._pick_hero_slot()
        if choice is None:
            return
        hero_key, equip_idx = choice
        repo = self._get_repo()
        slot = repo._find_slot_row(src_table, src_idx)
        if slot is None or not slot.get("ItemUniqueId"):
            messagebox.showinfo("Move to Hero", "Source slot is empty.")
            return
        self._begin_mutation(f"Move {src_table}[{src_idx}] → hero {hero_key}")
        repo.swap_hero_equip_with_slot(hero_key, equip_idx, src_table, src_idx)
        self._commit_mutation(f"Move {src_table}[{src_idx}] → hero {hero_key}")
        self._render_storage_grid(src_table)
        self._render_hero_cards()
        self.var_status.set(f"Moved {src_table}[{src_idx}] → hero {hero_key} equip[{equip_idx}]")

    def _cmd_drop_to_stash(self) -> None:
        if not self._selected_storage:
            return
        src_table, src_idx = self._selected_storage
        if src_table == "stashSaveDatas":
            return
        repo = self._get_repo()
        free = repo.find_empty_slot("stashSaveDatas")
        if free is None:
            choice = self._pick_slot("No empty stash — pick one to overwrite:",
                                     allowed_tables=("stashSaveDatas",))
            if choice is None:
                return
            free = choice[1]
        self._begin_mutation(f"Drop {src_table}[{src_idx}] → stash")
        repo.move_uid_between_slots((src_table, src_idx), ("stashSaveDatas", free))
        self._commit_mutation(f"Drop {src_table}[{src_idx}] → stash")
        self._render_storage_grid(src_table)
        self.var_status.set(f"Moved {src_table}[{src_idx}] → stash[{free}]")

    def _cmd_clear_slot(self) -> None:
        if not self._selected_storage:
            return
        src_table, src_idx = self._selected_storage
        self._begin_mutation("Clear slot")
        self._get_repo().clear_slot(src_table, src_idx)
        self._commit_mutation("Clear slot")
        self._render_storage_grid(src_table)
        self._render_hero_cards()
        self.var_status.set(f"Cleared {src_table}[{src_idx}]")

    # ==================================================================
    # Pickers (reused from v16)
    # ==================================================================
    def _pick_slot(self, prompt: str,
                   allowed_tables: tuple[str, ...] = (
                       "inventorySaveDatas", "stashSaveDatas",
                       "tradingStashSaveDatas"),
                   default_gear: str = "",
                   allow_empty: bool = True) -> tuple[str, int] | None:
        """Slot picker dialog with table tabs, gear filter, and empty-slot
        checkbox. Returns (table, idx), or None on cancel.

        ``allowed_tables`` restricts which tables the All/Inventory/Stash/Trading
        tabs may show. ``default_gear`` pre-selects a gear filter (case-insensitive).
        ``allow_empty`` controls whether the empty-slot checkbox defaults to checked.
        """
        repo = self._get_repo()
        psd = get_save_data()

        # Distinct gear values in the current save, ordered by count desc
        gear_counts: dict = {}
        for r in (psd.get("itemSaveDatas") or []):
            if not isinstance(r, dict):
                continue
            g = repo.gear_of(r)
            if g:
                gear_counts[g] = gear_counts.get(g, 0) + 1
        all_gears = ["(All)"] + sorted(
            gear_counts.keys(), key=lambda g: -gear_counts[g])

        top = tk.Toplevel(self)
        top.title(prompt); top.geometry("780x540")
        top.transient(self); top.grab_set()

        # Mutable state for the closures below
        state = {"choice": None, "table_filter": "(All)"}

        # ---- Header
        ttk.Label(top, text=prompt, font=("", 10, "bold"),
                  padding=(8, 6)).pack(anchor="w")

        # ---- Tabs (table filter) + Gear dropdown + Empty checkbox
        ctrl = ttk.Frame(top, padding=(8, 4))
        ctrl.pack(fill=tk.X)
        tab_bar = ttk.Frame(ctrl)
        tab_bar.pack(side=tk.LEFT, padx=(0, 12))
        gear_combo_holder = ttk.Frame(ctrl)
        gear_combo_holder.pack(side=tk.LEFT)

        gear_var = tk.StringVar(value=default_gear or "(All)")
        gear_combo = ttk.Combobox(
            gear_combo_holder, textvariable=gear_var, values=all_gears,
            state="readonly", width=14)
        gear_combo.pack(side=tk.LEFT)
        include_empty_var = tk.BooleanVar(value=bool(allow_empty))
        ttk.Checkbutton(ctrl, text="Include empty slots",
                        variable=include_empty_var).pack(side=tk.LEFT, padx=(12, 0))

        # ---- Listbox
        frm = ttk.Frame(top, padding=(8, 0))
        frm.pack(fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(frm, font=("TkFixedFont", 10), selectmode=tk.SINGLE)
        vsb = ttk.Scrollbar(frm, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=vsb.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        # All rows from allowed tables
        all_rows: list[tuple[str, int, dict]] = []
        for tbl in allowed_tables:
            for r in (psd.get(tbl) or []):
                if not isinstance(r, dict):
                    continue
                idx = r.get("Index")
                if idx is None:
                    continue
                all_rows.append((tbl, int(idx), r))

        display: list[tuple[str, int]] = []

        def rebuild_list():
            listbox.delete(0, tk.END)
            display.clear()
            table_filter = state["table_filter"]
            gear_filter = gear_var.get()
            want_empty = include_empty_var.get()
            label_map = {"Inventory": "inventorySaveDatas",
                         "Stash": "stashSaveDatas",
                         "Trading": "tradingStashSaveDatas"}
            for tbl, idx, r in all_rows:
                if table_filter != "(All)" and tbl != label_map.get(table_filter):
                    continue
                uid = int(r.get("ItemUniqueId", 0))
                if uid == 0:
                    if not want_empty:
                        continue
                else:
                    if gear_filter and gear_filter != "(All)":
                        it = repo.find_item_by_uid(uid)
                        if it is None or repo.gear_of(it) != gear_filter:
                            continue
                short = tbl.replace("SaveDatas", "")
                if uid:
                    it = repo.find_item_by_uid(uid)
                    ikey = it.get("ItemKey") if it else None
                    name = resolve_item_name(ikey) if ikey is not None else ""
                    g = repo.gear_of(it) if it else ""
                    ck = _catalog_get(ikey) if ikey is not None else None
                    grade = str(ck.get("grade", "")) if ck else ""
                    lv = int(ck.get("level") or 0) if ck else 0
                    label = (f"[{short:<10}] idx={idx:3d}  uid={uid:>20}  "
                             f"{grade[:6]:<6} Lv{lv:<3}  {g:<7}  {name[:30]}")
                else:
                    label = (f"[{short:<10}] idx={idx:3d}  uid=         0  (empty)")
                listbox.insert(tk.END, label)
                display.append((tbl, idx))
            if listbox.size() > 0:
                listbox.selection_set(0)
                listbox.activate(0)
                listbox.focus_set()
            # Update tab button label: prefix active tab with a marker so the
            # user can see which table filter is selected. (ttk themes vary
            # across macOS / Linux / Windows, so we avoid relying on background
            # color or relief — we use a text marker instead.)
            for lbl, b in tab_btns.items():
                marker = "● " if lbl == table_filter else "○ "
                b.configure(text=f"{marker}{lbl}")

        # Tab buttons (created after rebuild_list so they can reference it)
        tab_btns: dict = {}
        for tbl_label in ("(All)", "Inventory", "Stash", "Trading"):
            b = ttk.Button(
                tab_bar, text=f"○ {tbl_label}",
                command=lambda t=tbl_label: (state.update(table_filter=t), rebuild_list()))
            b.pack(side=tk.LEFT, padx=2)
            tab_btns[tbl_label] = b

        gear_combo.bind("<<ComboboxSelected>>", lambda _e: rebuild_list())
        include_empty_var.trace_add("write", lambda *_: rebuild_list())
        rebuild_list()

        def on_ok():
            s = listbox.curselection()
            if not s:
                return
            state["choice"] = display[int(s[0])]
            top.destroy()
        def on_cancel():
            top.destroy()
        listbox.bind("<Double-Button-1>", lambda _e: on_ok())
        listbox.bind("<Return>", lambda _e: on_ok())
        top.bind("<Escape>", lambda _e: on_cancel())

        btns = ttk.Frame(top, padding=8); btns.pack(fill=tk.X)
        ttk.Button(btns, text="Select", command=on_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=on_cancel).pack(side=tk.RIGHT)
        self.wait_window(top)
        return state["choice"]

    def _pick_hero_slot(self) -> tuple[int, int] | None:
        repo = self._get_repo()
        heroes = repo.psd.get("heroSaveDatas") or []
        if not heroes:
            return None
        top = tk.Toplevel(self); top.title("Pick hero & equip slot")
        top.geometry("520x420"); top.transient(self); top.grab_set()
        sel: dict = {"choice": None}
        ttk.Label(top, text="Hero:", font=("", 10, "bold"),
                  padding=(8, 8)).pack(anchor="w")
        hero_list = tk.Listbox(top, font=("TkFixedFont", 10), selectmode=tk.SINGLE, height=6)
        hero_list.pack(fill=tk.X, padx=8)
        hero_keys: list[int] = []
        for h in heroes:
            if isinstance(h, dict) and "heroKey" in h:
                hk = int(h["heroKey"])
                hero_keys.append(hk)
                equipped = h.get("equippedItemIds") or []
                cur = int(equipped[0]) if equipped else 0
                hero_list.insert(tk.END, f"Hero {hk}  (equip[0]={cur})")
        if hero_keys:
            hero_list.selection_set(0)
        ttk.Label(top, text="Equip slot (0..9):", font=("", 10, "bold"),
                  padding=(8, 8)).pack(anchor="w")
        slot_var = tk.IntVar(value=0)
        for i in range(10):
            ttk.Radiobutton(top, text=str(i), variable=slot_var, value=i
                            ).pack(side=tk.LEFT, padx=4, pady=4)
        def on_ok():
            s = hero_list.curselection()
            if not s: return
            sel["choice"] = (hero_keys[int(s[0])], int(slot_var.get()))
            top.destroy()
        def on_cancel():
            sel["choice"] = None
            top.destroy()
        btns = ttk.Frame(top, padding=8); btns.pack(fill=tk.X)
        ttk.Button(btns, text="Select", command=on_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=on_cancel).pack(side=tk.RIGHT)
        self.wait_window(top)
        return sel["choice"]

    def _pick_item_for_equip(self, hero_key: int, slot_idx: int,
                              current_uid: int) -> int | None:
        """Dialog to choose a new ItemUniqueId for a hero equip slot.

        Returns the chosen UID (int), 0 to clear, or None on cancel.
        Defaults: gear filter = currently-equipped item's gear (or "SWORD"
        if slot is empty), search box empty.
        """
        repo = self._get_repo()
        # Determine default gear from current_uid
        default_gear = ""
        if current_uid:
            it = repo.find_item_by_uid(current_uid)
            if it is not None:
                default_gear = repo.gear_of(it)
        if not default_gear:
            default_gear = "SWORD"  # sensible default for empty slot

        # All distinct gear values present in the save, ordered by count desc
        gear_counts: dict = {}
        for r in (repo.psd.get("itemSaveDatas") or []):
            if not isinstance(r, dict):
                continue
            g = repo.gear_of(r)
            if g:
                gear_counts[g] = gear_counts.get(g, 0) + 1
        all_gears = ["(All)"] + sorted(
            gear_counts.keys(), key=lambda g: -gear_counts[g])

        top = tk.Toplevel(self)
        top.title(f"Pick item for Hero {hero_key} equip[{slot_idx}]")
        top.geometry("820x560")
        top.transient(self); top.grab_set()
        sel: dict = {"choice": None}

        # ---- Top: gear dropdown + search box
        topbar = ttk.Frame(top, padding=(8, 8))
        topbar.pack(fill=tk.X)
        ttk.Label(topbar, text="Gear:").pack(side=tk.LEFT)
        gear_var = tk.StringVar(value=default_gear)
        gear_combo = ttk.Combobox(
            topbar, textvariable=gear_var, values=all_gears,
            state="readonly", width=14)
        gear_combo.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(topbar, text="Search:").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        search_entry = ttk.Entry(topbar, textvariable=search_var, width=24)
        search_entry.pack(side=tk.LEFT, padx=(4, 0))

        # ---- Listbox (uid=0 sentinel for clear)
        body = ttk.Frame(top, padding=(8, 0))
        body.pack(fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(body, font=("TkFixedFont", 10),
                             selectmode=tk.SINGLE, activestyle="dotbox")
        vsb = ttk.Scrollbar(body, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=vsb.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        display: list = []  # uid values; 0 = "(empty / clear slot)"

        def rebuild_list():
            listbox.delete(0, tk.END)
            display.clear()
            gear_filter = gear_var.get()
            q = search_var.get().strip().lower()
            display.append(0)
            listbox.insert(tk.END, "(empty / clear slot)")
            items = (repo.items_with_gear(gear_filter)
                     if gear_filter != "(All)"
                     else repo.items_with_gear(None))
            for r in items:
                if not isinstance(r, dict):
                    continue
                ikey = r.get("ItemKey")
                uid = int(r.get("UniqueId", 0) or 0)
                if not uid:
                    continue
                entry = _catalog_get(ikey)
                name = resolve_item_name(ikey)
                if q and q not in name.lower():
                    continue
                g = str(entry.get("gear", "") if entry else "")
                grade = str(entry.get("grade", "") if entry else "")
                lv = int(entry.get("level", 0) if entry else 0)
                listbox.insert(tk.END,
                    f"uid={uid:>20}  {grade[:6]:<6}  {g:<7}  Lv{lv:<3}  {name[:40]}")
                display.append(uid)
            if listbox.size() > 0:
                listbox.selection_set(0)
                listbox.activate(0)
                listbox.focus_set()

        def on_gear_change(_e=None):
            rebuild_list()
        def on_search_change(*_a):
            rebuild_list()
        gear_combo.bind("<<ComboboxSelected>>", on_gear_change)
        search_var.trace_add("write", on_search_change)
        rebuild_list()

        def on_ok():
            s = listbox.curselection()
            if not s:
                return
            sel["choice"] = int(display[int(s[0])])
            top.destroy()
        def on_cancel():
            sel["choice"] = None
            top.destroy()
        listbox.bind("<Double-Button-1>", lambda _e: on_ok())
        listbox.bind("<Return>", lambda _e: on_ok())
        top.bind("<Escape>", lambda _e: on_cancel())

        btns = ttk.Frame(top, padding=8); btns.pack(fill=tk.X)
        ttk.Button(btns, text="Select", command=on_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=on_cancel).pack(side=tk.RIGHT)
        self.wait_window(top)
        return sel["choice"]
    # ==================================================================
    # Inline form helpers
    # ==================================================================
    def _add_inline_entry(self, parent: ttk.Frame, label: str, value: Any,
                          on_set, ctype: str = "str", width: int = 14) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=label, width=18, anchor="w").pack(side=tk.LEFT)
        var = tk.StringVar(value=serialize_value(value, ctype))
        ttk.Entry(row, textvariable=var, width=width).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="Set",
                   command=lambda v=var, cb=on_set: self._apply_field(v, cb)
                   ).pack(side=tk.LEFT, padx=2)

    def _apply_field(self, var: tk.StringVar, callback) -> None:
        was_dirty = self._dirty
        try:
            self._begin_mutation("Edit field")
            callback(var.get())
        except Exception as e:
            # Roll back: the edit never happened, so we should not be dirty
            # and we should not have a pre-snapshot that points to a state
            # the user can no longer reach.
            self._dirty = was_dirty
            self.file.dirty = was_dirty
            self._update_dirty_indicator()
            messagebox.showerror("Apply failed", str(e))
            self.var_status.set(f"FAILED: {e}")
            return
        self._commit_mutation("Edit field")
        self._refresh_topbar()
        self.var_status.set(f"Applied: {var.get()}")

    def _add_inline_bool_combo(self, parent: ttk.Frame, label: str, value: Any,
                              on_set) -> None:
        """Like _add_inline_entry but uses a ttk.Combobox (True / False) for
        bool fields — safer than letting the user type 'tru' or '0' etc."""
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=label, width=18, anchor="w").pack(side=tk.LEFT)
        var = tk.StringVar(value=serialize_value(bool(value), Field.BOOL))
        cb = ttk.Combobox(row, textvariable=var, values=("True", "False"),
                          state="readonly", width=12)
        cb.pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="Set",
                   command=lambda v=var, cb=on_set: self._apply_field(v, cb)
                   ).pack(side=tk.LEFT, padx=2)

    def _set_hero(self, hero_key: int, field: str, raw: str, ctype: str) -> None:
        self._get_repo().set_hero_field(hero_key, field, parse_value(raw, ctype))

    def _set_hero_skill(self, hero_key: int, idx: int, raw: str, ctype: str) -> None:
        h = self._get_repo().find_hero(hero_key)
        if h is None:
            return
        skills = list(h.get("equippedSKillKey") or [])
        while len(skills) < 3:
            skills.append(-1)
        skills[idx] = parse_value(raw, ctype)
        h["equippedSKillKey"] = skills

    def _set_hero_equip(self, hero_key: int, slot_idx: int, raw: str,
                        name_var: tk.StringVar,
                        name_label: ttk.Label | None = None) -> None:
        try:
            new_uid = int(raw.strip())
        except ValueError:
            raise ValueError(f"UID must be integer, got {raw!r}")
        self._get_repo().attach_uid_to_hero(hero_key, slot_idx, new_uid)
        it = self._get_repo().find_item_by_uid(new_uid)
        name_var.set(resolve_item_name(it.get("ItemKey")) if it else "")
        if name_label is not None:
            name_label.configure(foreground=self._color_for_uid(new_uid))
        self._render_hero_cards()
    def _pick_hero_equip_uid(self, hero_key: int, slot_idx: int,
                             uid_var: tk.StringVar,
                             name_var: tk.StringVar,
                             name_label: ttk.Label | None = None) -> None:
        """Open the item picker, set the equip slot to the chosen UID, and
        update the row widgets. Cancelling leaves the row unchanged."""
        try:
            current_uid = int(uid_var.get() or 0)
        except ValueError:
            current_uid = 0
        new_uid = self._pick_item_for_equip(hero_key, slot_idx, current_uid)
        if new_uid is None:
            return  # user cancelled
        self._begin_mutation(f"Pick equip {hero_key}[{slot_idx}] → UID {new_uid}")
        self._get_repo().attach_uid_to_hero(hero_key, slot_idx, new_uid)
        self._commit_mutation(f"Pick equip {hero_key}[{slot_idx}] → UID {new_uid}")
        uid_var.set(str(new_uid))
        it = self._get_repo().find_item_by_uid(new_uid)
        name_var.set(resolve_item_name(it.get("ItemKey")) if it else "")
        if name_label is not None:
            name_label.configure(foreground=self._color_for_uid(new_uid))
        self._render_hero_cards()
        self.var_status.set(
            f"Hero {hero_key} equip[{slot_idx}] ← UID {new_uid}")

    def _color_for_uid(self, uid: int) -> str:
        """Return the Grade color for the given UID, or the default blue."""
        if not uid:
            return "#1565c0"
        it = self._get_repo().find_item_by_uid(uid)
        if it is None:
            return "#1565c0"
        ck = _catalog_get(it.get("ItemKey"))
        if not ck:
            return "#1565c0"
        return _grade_color(str(ck.get("grade", "")))


    def _apply_enchant_row(self, uid: int, row_idx: int,
                           row_vars: dict[str, tk.StringVar]) -> None:
        it = self._get_repo().find_item_by_uid(uid)
        if it is None:
            return
        ed = it.get("EnchantData")
        if not isinstance(ed, list) or row_idx >= len(ed):
            return
        target = ed[row_idx]
        if not isinstance(target, dict):
            return
        self._begin_mutation(f"Enchant row {row_idx} of UID {uid}")
        for k, var in row_vars.items():
            target[k] = parse_value(var.get(), Field.INT)
        self._commit_mutation(f"Enchant row {row_idx} of UID {uid}")
        self.var_status.set(f"Applied enchant row {row_idx} of UID {uid}")

    # ==================================================================
    # Top-level commands
    # ==================================================================
    def _cmd_save(self) -> None:
        try:
            self.file.save()
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return
        self._dirty = False
        self.file.dirty = False
        # Save is a new baseline — clear undo + redo history
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._undo_pre_dirty_snap = None
        self._last_edit_pre = None
        self._last_edit_diff = None
        # Fresh baseline + clear log: from this point on, "All changes"
        # in the Diff popup compares against what we just saved.
        import copy
        psd = get_save_data()
        self._baseline_psd = copy.deepcopy(psd) if psd is not None else None
        self._mutation_log = []
        self._update_diff_button()
        self._update_dirty_indicator()
        self._update_undo_button()
        self.var_status.set(f"Saved → {self.file.path}")

    def _cmd_save_as(self) -> None:
        """Save to a user-chosen path. Also re-points the editor at the new
        path so subsequent Saves write there. Always makes a backup of the
        target file (not the current one) so the user can roll back."""
        path = filedialog.asksaveasfilename(
            title="Save ES3 file as…",
            initialdir=os.path.dirname(self.file.path),
            initialfile=os.path.basename(self.file.path),
            defaultextension=".es3",
            filetypes=[("ES3 save", "*.es3"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.file.save(path)
        except Exception as e:
            messagebox.showerror("Save As failed", str(e))
            return
        if os.path.abspath(path) != os.path.abspath(self.file.path):
            self.file.path = path
            self._path_var.set(path)
            self.title(f"ES3 Save Editor — {os.path.basename(path)}")
        self._dirty = False
        self.file.dirty = False
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._undo_pre_dirty_snap = None
        self._last_edit_pre = None
        self._last_edit_diff = None
        import copy
        psd = get_save_data()
        self._baseline_psd = copy.deepcopy(psd) if psd is not None else None
        self._mutation_log = []
        self._update_diff_button()
        self._update_dirty_indicator()
        self._update_undo_button()
        self.var_status.set(f"Saved as → {path}")

    def _swap_file(self, new_path: str, *, status: str,
                   old_path: str | None = None,
                   old_psd: dict | None = None) -> None:
        """Load new_path into self.file, refresh all tabs. Assumes the
        caller has already confirmed the user wants to discard dirty state.
        Raises on failure; caller should messagebox.showerror.

        Optional kwargs:
          old_path  — path of the file being replaced (for the "open
                      diff" modal). Defaults to self.file.path.
          old_psd   — PlayerSaveData dict captured BEFORE the swap; if
                      provided AND new_path differs from old_path, a
                      diff modal will pop up comparing the two. Reload
                      (same path) does not trigger the modal.
        """
        if old_path is None:
            old_path = self.file.path if self.file else new_path
        # If old_psd wasn't provided (e.g. simple reload), don't pop the
        # diff modal. Only _cmd_open passes old_psd explicitly.
        show_diff = old_psd is not None and os.path.abspath(new_path) != os.path.abspath(old_path)
        self.file = ES3File.load(new_path)
        _APP_REF[0] = self.file
        _DATA_CACHE.clear()
        self._dirty = False
        self.file.dirty = False
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._undo_pre_dirty_snap = None
        self._last_edit_pre = None
        self._last_edit_diff = None
        import copy
        psd = get_save_data()
        self._baseline_psd = copy.deepcopy(psd) if psd is not None else None
        self._mutation_log = []
        self._update_diff_button()
        self._update_undo_button()
        self._path_var.set(self.file.path)
        self.title(f"ES3 Save Editor — {os.path.basename(self.file.path)}")
        # Rebuild all tabs
        self._render_hero_cards()
        if self._selected_hero_key is not None:
            self._render_hero_detail(self._selected_hero_key)
        if hasattr(self, "_storage_table_active"):
            self._render_storage_grid(self._storage_table_active)
        self._render_items_table()
        self._refresh_topbar()
        self._refresh_integrity()
        self._update_dirty_indicator()
        self.var_status.set(status)
        # Show diff modal if we just opened a *different* file
        if show_diff:
            try:
                new_psd = copy.deepcopy(get_save_data())
                self._show_open_diff(old_path, new_path, old_psd, new_psd)
            except Exception:
                pass

    def _show_open_diff(self, old_path: str, new_path: str,
                        old_psd: dict, new_psd: dict) -> None:
        """Show a modal listing the top scalar differences between two
        PlayerSaveData dicts. Helps the user confirm they opened the right
        file (or notice they swapped to a different character)."""
        diffs = self._compute_psd_diff(old_psd, new_psd)
        if not diffs:
            return
        top = tk.Toplevel(self)
        top.title("Open: changes from previous file")
        top.geometry("560x460")
        top.transient(self)
        ttk.Label(top,
                  text=f"Differences between:\n  {os.path.basename(old_path)}\n  →  {os.path.basename(new_path)}",
                  padding=(8, 8), font=("", 10, "bold")).pack(anchor="w")
        body = ttk.Frame(top, padding=(8, 0))
        body.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(body, columns=("field", "old", "new"),
                           show="headings")
        for cid, label, w in [("field", "Field", 200),
                               ("old", "Previous", 160),
                               ("new", "Now", 160)]:
            tree.heading(cid, text=label)
            tree.column(cid, width=w, anchor="w")
        for d in diffs:
            tree.insert("", "end", values=d)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        if len(diffs) >= 10:
            ttk.Label(top, text=f"(showing first {len(diffs)} of more changes)",
                      foreground="#888").pack(anchor="w", padx=8)
        ttk.Label(top, text="Tip: use Ctrl-Z (Undo) to revert edits you don't want.",
                  padding=(8, 4), foreground="#666").pack(anchor="w")
        btns = ttk.Frame(top, padding=8); btns.pack(fill=tk.X)
        ttk.Button(btns, text="Close", command=top.destroy).pack(side=tk.RIGHT)

    @staticmethod
    def _compute_psd_diff(old_psd: dict, new_psd: dict,
                          limit: int = 20) -> list[tuple[str, str, str]]:
        """Walk the two PSD dicts and return a list of (field_path, old, new)
        tuples for scalar values that differ. Lists / dicts are summarized
        by length. Returns at most ``limit`` rows, sorted by importance
        (defined by the field-order list below)."""
        if not isinstance(old_psd, dict) or not isinstance(new_psd, dict):
            return []
        # Importance-ordered scalar fields to surface first
        scalars = [
            ("commonSaveData.maxCompletedStage", lambda d: d.get("commonSaveData", {}).get("maxCompletedStage"), "int"),
            ("commonSaveData.currentStageKey", lambda d: d.get("commonSaveData", {}).get("currentStageKey"), "int"),
            ("commonSaveData.currentStageWave", lambda d: d.get("commonSaveData", {}).get("currentStageWave"), "int"),
            ("commonSaveData.playTime", lambda d: d.get("commonSaveData", {}).get("playTime"), "float"),
            ("currenySaveDatas[0].Quantity", lambda d: (d.get("currenySaveDatas") or [{}])[0].get("Quantity"), "int"),
            ("cubeSaveLevelData.Level", lambda d: d.get("cubeSaveLevelData", {}).get("Level"), "int"),
            ("cubeSaveLevelData.Exp", lambda d: d.get("cubeSaveLevelData", {}).get("Exp"), "int"),
            ("settingSaveData.language", lambda d: d.get("settingSaveData", {}).get("language"), "int"),
        ]
        diffs: list[tuple[str, str, str]] = []
        for path, getter, kind in scalars:
            try:
                old = getter(old_psd); new = getter(new_psd)
            except Exception:
                continue
            if old == new:
                continue
            old_s = f"{int(old):,}" if isinstance(old, (int, float)) and kind == "int" else (f"{old:,.2f}" if isinstance(old, float) and kind == "float" else str(old))
            new_s = f"{int(new):,}" if isinstance(new, (int, float)) and kind == "int" else (f"{new:,.2f}" if isinstance(new, float) and kind == "float" else str(new))
            diffs.append((path, old_s, new_s))
            if len(diffs) >= limit:
                return diffs
        # Per-hero equippedItemIds UID changes. Most user-driven mutations
        # (swap / move / drop / unequip / pick-equip) boil down to writing
        # a UID into one of these arrays; without this section a swap
        # shows up only as a top-level list-length delta, which is useless.
        # We walk heroKey-by-heroKey, slot-by-slot. Truncate the per-slot
        # walk at _UID_WALK_HARD_CAP rows to keep the diff readable on
        # huge saves; the user can always fall back to a smaller diff.
        old_heroes = old_psd.get("heroSaveDatas") or []
        new_heroes = new_psd.get("heroSaveDatas") or []
        # Build hero-key -> list-of-equips map for both sides
        def _hero_equip_map(heroes):
            out = {}
            for h in heroes:
                if not isinstance(h, dict):
                    continue
                key = h.get("heroKey")
                if key is None:
                    continue
                equipped = list(h.get("equippedItemIds") or [])
                out[int(key)] = equipped
            return out
        old_eq = _hero_equip_map(old_heroes)
        new_eq = _hero_equip_map(new_heroes)
        all_hero_keys = sorted(set(old_eq.keys()) | set(new_eq.keys()))
        for hk in all_hero_keys:
            o = old_eq.get(hk, [])
            n = new_eq.get(hk, [])
            limit_slots = max(len(o), len(n))
            for slot in range(limit_slots):
                o_v = int(o[slot]) if slot < len(o) else 0
                n_v = int(n[slot]) if slot < len(n) else 0
                if o_v == n_v:
                    continue
                diffs.append((
                    f"heroSaveDatas[heroKey={hk}].equippedItemIds[{slot}]",
                    f"{o_v:,}" if o_v else "(empty)",
                    f"{n_v:,}" if n_v else "(empty)",
                ))
                if len(diffs) >= limit:
                    return diffs
        # Per-slot ItemUniqueId changes in the 3 storage tables.
        # Indexed by the row's Index field; a row missing on one side is
        # treated as UID=0 so deletions/additions both surface.
        for table in ("inventorySaveDatas", "stashSaveDatas",
                      "tradingStashSaveDatas"):
            old_rows = {int(r.get("Index", i)): r
                        for i, r in enumerate(old_psd.get(table) or [])
                        if isinstance(r, dict)}
            new_rows = {int(r.get("Index", i)): r
                        for i, r in enumerate(new_psd.get(table) or [])
                        if isinstance(r, dict)}
            all_idx = sorted(set(old_rows.keys()) | set(new_rows.keys()))
            for idx in all_idx:
                o_uid = int((old_rows.get(idx) or {}).get("ItemUniqueId", 0))
                n_uid = int((new_rows.get(idx) or {}).get("ItemUniqueId", 0))
                if o_uid == n_uid:
                    continue
                diffs.append((
                    f"{table}[Index={idx}].ItemUniqueId",
                    f"{o_uid:,}" if o_uid else "(empty)",
                    f"{n_uid:,}" if n_uid else "(empty)",
                ))
                if len(diffs) >= limit:
                    return diffs
        # Hero count summary
        if len(old_heroes) != len(new_heroes):
            diffs.append(("heroSaveDatas count",
                         f"{len(old_heroes)} heroes",
                         f"{len(new_heroes)} heroes"))
        # Top-level list-length diffs (kept as a last-resort summary).
        # Only emit a count row when the per-row walk above didn't already
        # surface the change — otherwise the same edit shows up twice.
        for key in ("itemSaveDatas", "inventorySaveDatas", "stashSaveDatas",
                    "tradingStashSaveDatas", "attributeSaveDatas",
                    "RuneSaveData", "PetSaveData", "aggregateSaveDatas"):
            old_n = len(old_psd.get(key) or [])
            new_n = len(new_psd.get(key) or [])
            if old_n != new_n:
                diffs.append((f"{key} count", str(old_n), str(new_n)))
                if len(diffs) >= limit:
                    return diffs
        return diffs

    def _cmd_open(self) -> None:
        path = filedialog.askopenfilename(
            title="Open ES3 save file",
            initialdir=os.path.dirname(self.file.path),
            filetypes=[("ES3 save", "*.es3"), ("All files", "*.*")])
        if not path:
            return
        self._cmd_open_path(path)

    def _cmd_open_path(self, path: str | None = None) -> None:
        target = path or self._path_var.get().strip()
        if not target:
            return
        if not os.path.isfile(target):
            messagebox.showerror("Open failed", f"Not a file: {target}")
            return
        if self._dirty:
            if not messagebox.askyesno(
                    "Discard changes?",
                    f"Unsaved changes will be lost. Open {os.path.basename(target)}?"):
                return
        # Capture the current PSD so _swap_file can show a diff modal
        # when the user opens a *different* file (not a same-path reload).
        try:
            old_psd = copy.deepcopy(get_save_data())
        except Exception:
            old_psd = None
        old_path = self.file.path
        try:
            self._swap_file(
                target,
                status=f"Opened {os.path.basename(target)}",
                old_path=old_path,
                old_psd=old_psd,
            )
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    def _cmd_reload(self) -> None:
        if self._dirty:
            if not messagebox.askyesno("Discard changes?",
                                       "Unsaved changes will be lost. Reload?"):
                return
        try:
            self._swap_file(self.file.path, status="Reloaded.")
        except Exception as e:
            messagebox.showerror("Reload failed", str(e))

    def _cmd_backup(self) -> None:
        try:
            self.file.save(make_backup=True)
            self.var_status.set("Backup written.")
        except Exception as e:
            messagebox.showerror("Backup failed", str(e))

    def _begin_mutation(self, label: str = "Edit") -> None:
        """Call BEFORE any mutation. Captures the pre-state snapshot for
        undo and marks the file dirty. Equivalent to the old _mark_dirty,
        but with pre-mutation semantics. A new mutation also invalidates
        the redo trail (you can no longer redo through a divergent path)."""
        import copy
        if not self._dirty:
            psd = get_save_data()
            if psd is not None:
                self._undo_pre_dirty_snap = (label, copy.deepcopy(psd))
            # New edit chain — drop any pending redo states
            if self._redo_stack:
                self._redo_stack.clear()
        # Always capture a pre-snapshot for the per-edit Diff view (even when
        # the file is already dirty, so each macro/edit shows what changed).
        psd = get_save_data()
        if psd is not None:
            self._last_edit_pre = (label, copy.deepcopy(psd))
        self._dirty = True
        self.file.dirty = True
        self._update_dirty_indicator()
        self._update_undo_button()

    def _commit_mutation(self, label: str = "Edit") -> None:
        """Call AFTER a successful mutation. Compares the pre-snapshot
        captured by _begin_mutation against the current state and stores
        the diff for the user to inspect. Safe to call multiple times."""
        import copy
        import time
        pre = getattr(self, "_last_edit_pre", None)
        psd = get_save_data()
        if pre is None or psd is None:
            self._last_edit_pre = None
            return
        pre_label, pre_snap = pre
        try:
            diffs = self._compute_psd_diff(pre_snap, psd, limit=8)
        except Exception:
            diffs = []
        self._last_edit_diff = (label or pre_label, diffs)
        self._last_edit_pre = None
        # Append to the chronological log so the user can browse every
        # edit made in this session. Truncate to avoid unbounded growth.
        if not hasattr(self, "_mutation_log"):
            self._mutation_log = []
        self._mutation_log.append({
            "ts": time.time(),
            "label": label or pre_label,
            "diffs": diffs,
        })
        if len(self._mutation_log) > 200:
            del self._mutation_log[:len(self._mutation_log) - 200]
        if hasattr(self, "_diff_btn"):
            # Button shows cumulative count: log entries + current dirty chain
            n = len(self._mutation_log)
            self._diff_btn.configure(
                text=f"Diff ({n})" if n else "Diff",
                state=("normal" if n else "disabled"))

    def _mark_dirty(self) -> None:
        """Lightweight dirty marker (no snapshot). Use _begin_mutation for
        undoable edits."""
        self._dirty = True
        self.file.dirty = True
        self._update_dirty_indicator()

    def _record_undo(self, label: str) -> None:
        """Snapshot the current PlayerSaveData dict so the user can Ctrl-Z
        back to this state. Called before any mutation."""
        import copy
        psd = get_save_data()
        if psd is None:
            return
        snap = copy.deepcopy(psd)
        self._undo_stack.append((label, snap))
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)
        self._update_undo_button()

    def _cmd_undo(self) -> None:
        # Two undo sources, in priority order:
        #  1. The in-flight pre-dirty snapshot (captured on the first edit
        #     of a dirty chain). Undoing it rolls back the WHOLE chain in
        #     one step — there is no "earlier" step to undo further.
        #  2. The persistent _undo_stack (each entry is a pre-state from
        #     _record_undo, used by code paths that bypass the
        #     pre-dirty-snap mechanism).
        if self._undo_pre_dirty_snap is None and not self._undo_stack:
            self.var_status.set("Nothing to undo.")
            return
        if self._undo_pre_dirty_snap is not None:
            label, snap = self._undo_pre_dirty_snap
            self._undo_pre_dirty_snap = None
            # Capture the post-state for Redo only. DO NOT push it onto
            # _undo_stack — the post-state is "after" the change, not
            # "before" something else, so popping it as an undo target
            # would re-apply an unrelated earlier edit.
            import copy
            psd = get_save_data()
            if psd is not None and isinstance(psd, dict):
                post = copy.deepcopy(psd)
                self._redo_stack.append((label, post))
                if len(self._redo_stack) > self._MAX_UNDO:
                    self._redo_stack.pop(0)
        else:
            label, snap = self._undo_stack.pop()
            # Mirror to redo
            self._redo_stack.append((label, snap))
            if len(self._redo_stack) > self._MAX_UNDO:
                self._redo_stack.pop(0)
        # Apply the snapshot
        psd_entry = self.file.data.get("PlayerSaveData")
        if isinstance(psd_entry, dict) and "__type" in psd_entry:
            psd_entry["value"] = snap
        _DATA_CACHE.clear()
        self._dirty = False
        self.file.dirty = False
        # The "last edit" snapshot no longer reflects a clean pre-state now
        # that we rolled back; clear it so the Diff button is honest.
        self._last_edit_pre = None
        self._last_edit_diff = None
        # Log the undo as a synthetic entry so the chronological log shows
        # the action taken (and the cumulative diff still reflects the
        # post-undo state). Append BEFORE refreshing the button so the
        # count includes this entry.
        if hasattr(self, "_mutation_log"):
            import time
            self._mutation_log.append({
                "ts": time.time(),
                "label": f"↶ Undo: {label}",
                "diffs": [],
            })
            if len(self._mutation_log) > 200:
                del self._mutation_log[:len(self._mutation_log) - 200]
        self._update_diff_button()
        self._update_dirty_indicator()
        self._update_undo_button()
        # Re-render everything that might be stale
        self._render_hero_cards()
        if self._selected_hero_key is not None:
            self._render_hero_detail(self._selected_hero_key)
        if hasattr(self, "_storage_table_active"):
            self._render_storage_grid(self._storage_table_active)
            if self._selected_storage:
                self._render_storage_detail(*self._selected_storage)
        self._render_items_table()
        if self._items_tree.selection():
            self._render_items_detail()
        self._refresh_topbar()
        self._refresh_integrity()
        self.var_status.set(
            f"Undo: {label}  (chain cleared; {len(self._undo_stack)} in stack)")

    def _cmd_redo(self) -> None:
        if not self._redo_stack:
            self.var_status.set("Nothing to redo.")
            return
        label, snap = self._redo_stack.pop()
        # Apply the snapshot and push current state back to undo
        import copy
        psd = get_save_data()
        if psd is not None and isinstance(psd, dict):
            self._undo_stack.append((label, copy.deepcopy(psd)))
            if len(self._undo_stack) > self._MAX_UNDO:
                self._undo_stack.pop(0)
        psd_entry = self.file.data.get("PlayerSaveData")
        if isinstance(psd_entry, dict) and "__type" in psd_entry:
            psd_entry["value"] = snap
        _DATA_CACHE.clear()
        self._dirty = True
        self.file.dirty = True
        # The "last edit" pre-state is no longer the right baseline after a redo
        self._last_edit_pre = None
        self._last_edit_diff = None
        if hasattr(self, "_mutation_log"):
            import time
            self._mutation_log.append({
                "ts": time.time(),
                "label": f"↷ Redo: {label}",
                "diffs": [],
            })
            if len(self._mutation_log) > 200:
                del self._mutation_log[:len(self._mutation_log) - 200]
        self._update_diff_button()
        self._update_dirty_indicator()
        self._update_undo_button()
        self._render_hero_cards()
        if self._selected_hero_key is not None:
            self._render_hero_detail(self._selected_hero_key)
        if hasattr(self, "_storage_table_active"):
            self._render_storage_grid(self._storage_table_active)
            if self._selected_storage:
                self._render_storage_detail(*self._selected_storage)
        self._render_items_table()
        if self._items_tree.selection():
            self._render_items_detail()
        self._refresh_topbar()
        self._refresh_integrity()
        self.var_status.set(
            f"Redo: {label}  ({len(self._redo_stack)} left)")

    def _update_undo_button(self) -> None:
        if hasattr(self, "_undo_btn"):
            stack_n = len(self._undo_stack)
            chain_n = 1 if getattr(self, "_undo_pre_dirty_snap", None) is not None else 0
            total = stack_n + chain_n
            self._undo_btn.configure(
                text=f"Undo ({total})" if total else "Undo")
        if hasattr(self, "_redo_btn"):
            n = len(self._redo_stack)
            self._redo_btn.configure(text=f"Redo ({n})" if n else "Redo")

    def _update_diff_button(self) -> None:
        """Sync the topbar Diff button label/state to current log size.
        The button is enabled whenever the log is non-empty (you can always
        look at history) and shows the entry count."""
        if not hasattr(self, "_diff_btn"):
            return
        n = len(getattr(self, "_mutation_log", []))
        self._diff_btn.configure(
            text=f"Diff ({n})" if n else "Diff",
            state=("normal" if n else "disabled"))

    def _cmd_show_diff(self) -> None:
        """Open the diff/log viewer. Three views, selectable via a radio:
          • Last edit  — the per-edit field diff from _last_edit_diff.
          • All changes — cumulative field diff from _baseline_psd to the
            current state, covering every mutation since the last
            load/save.
          • Log        — chronological list of every _commit_mutation
            call in this session, with timestamps. Selecting an entry
            shows its per-edit diff in the right pane.
        """
        top = tk.Toplevel(self)
        top.title("Diff & Edit Log")
        top.geometry("920x560")
        top.transient(self)
        mode = tk.StringVar(value="all")
        # Left pane: log
        left = ttk.Frame(top, padding=(8, 8))
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="📋 Edit log (this session)",
                  font=("", 10, "bold")).pack(anchor="w")
        log_box = ttk.Frame(left); log_box.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        log_tree = ttk.Treeview(log_box, columns=("time", "label"),
                                show="headings", height=18)
        log_tree.heading("time", text="Time")
        log_tree.heading("label", text="Edit")
        log_tree.column("time", width=120, anchor="w")
        log_tree.column("label", width=240, anchor="w")
        log_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lvsb = ttk.Scrollbar(log_box, orient="vertical", command=log_tree.yview)
        log_tree.configure(yscrollcommand=lvsb.set)
        lvsb.pack(side=tk.LEFT, fill=tk.Y)
        # Populate log
        import datetime as _dt
        log_entries = getattr(self, "_mutation_log", [])
        for i, e in enumerate(log_entries):
            ts = _dt.datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
            log_tree.insert("", "end", iid=str(i), values=(ts, e["label"]))
        if log_entries:
            log_tree.selection_set("0")
        # Right pane: diff view
        right = ttk.Frame(top, padding=(8, 8))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        mode_bar = ttk.Frame(right); mode_bar.pack(fill=tk.X)
        ttk.Label(mode_bar, text="View:", font=("", 10, "bold")).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_bar, text="Last edit", variable=mode,
                        value="last").pack(side=tk.LEFT, padx=(8, 4))
        ttk.Radiobutton(mode_bar, text="All changes (cumulative)",
                        variable=mode, value="all").pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(mode_bar, text="Selected log entry",
                        variable=mode, value="selected").pack(side=tk.LEFT, padx=4)
        summary = ttk.Label(right, text="", foreground="#666")
        summary.pack(anchor="w", pady=(6, 0))
        body = ttk.Frame(right); body.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        diff_tree = ttk.Treeview(body, columns=("field", "old", "new"),
                                 show="headings")
        for cid, col_label, w in [("field", "Field", 280),
                                   ("old", "Before", 180),
                                   ("new", "After", 180)]:
            diff_tree.heading(cid, text=col_label)
            diff_tree.column(cid, width=w, anchor="w")
        diff_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dvsb = ttk.Scrollbar(body, orient="vertical", command=diff_tree.yview)
        diff_tree.configure(yscrollcommand=dvsb.set)
        dvsb.pack(side=tk.LEFT, fill=tk.Y)

        def render():
            for iid in diff_tree.get_children():
                diff_tree.delete(iid)
            sel_mode = mode.get()
            diffs: list = []
            title = ""
            if sel_mode == "last":
                le = getattr(self, "_last_edit_diff", None)
                if le is None:
                    summary.configure(text="No last edit to show.")
                    return
                title, diffs = le
                summary.configure(
                    text=f"Per-edit diff for: {title}  ({len(diffs)} change(s))")
            elif sel_mode == "all":
                base = getattr(self, "_baseline_psd", None)
                cur = get_save_data()
                if base is None or cur is None:
                    summary.configure(text="No baseline; load or save first.")
                    return
                try:
                    diffs = self._compute_psd_diff(base, cur, limit=200)
                except Exception as e:
                    summary.configure(text=f"Diff failed: {e}")
                    return
                summary.configure(
                    text=f"Cumulative diff: {len(diffs)} change(s) since "
                         f"load/save baseline.")
            else:  # selected
                sel = log_tree.selection()
                if not sel:
                    summary.configure(text="Pick a log entry on the left.")
                    return
                idx = int(sel[0])
                if idx < 0 or idx >= len(log_entries):
                    summary.configure(text="(no entry)")
                    return
                e = log_entries[idx]
                diffs = e.get("diffs") or []
                title = e.get("label", "")
                summary.configure(
                    text=f"Log entry #{idx + 1}: {title}  ({len(diffs)} change(s))")
            for d in diffs:
                diff_tree.insert("", "end", values=d)
            if not diffs:
                # show a friendly empty row
                diff_tree.insert("", "end", values=(
                    "—", "(no scalar fields changed)", ""))

        def on_mode_change(*_a):
            render()
        def on_log_select(_e=None):
            if mode.get() == "selected":
                render()
        mode.trace_add("write", on_mode_change)
        log_tree.bind("<<TreeviewSelect>>", on_log_select)
        render()
        # Bottom buttons
        btns = ttk.Frame(top, padding=8); btns.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(btns, text="Close", command=top.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Undo this edit",
                   command=lambda: (top.destroy(), self._cmd_undo())
                   ).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Open baseline file…",
                   command=lambda: self._cmd_show_diff_vs_disk(top)
                   ).pack(side=tk.LEFT)

    def _cmd_show_diff_vs_disk(self, parent: tk.Misc) -> None:
        """Spawn a child of the diff popup: show what would change if the
        user saved right now. Reads the on-disk ES3 file, decodes the
        PlayerSaveData payload, and diffs it against the in-memory state.
        """
        try:
            on_disk = ES3File.load(self.file.path)
        except Exception as e:
            messagebox.showerror("Diff vs disk", f"Reload failed: {e}",
                                 parent=parent)
            return
        disk_psd = on_disk.data.get("PlayerSaveData")
        if isinstance(disk_psd, dict) and "__type" in disk_psd:
            disk_psd = disk_psd.get("value")
        cur = get_save_data()
        if not isinstance(disk_psd, dict) or not isinstance(cur, dict):
            messagebox.showinfo("Diff vs disk", "PlayerSaveData missing.",
                                parent=parent)
            return
        diffs = self._compute_psd_diff(disk_psd, cur, limit=200)
        top = tk.Toplevel(parent)
        top.title("Diff vs on-disk file")
        top.geometry("780x480")
        top.transient(parent)
        ttk.Label(top, text=f"On-disk → in-memory  ({len(diffs)} change(s))",
                  padding=(8, 8), font=("", 10, "bold")).pack(anchor="w")
        body = ttk.Frame(top, padding=(8, 0))
        body.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(body, columns=("field", "old", "new"),
                            show="headings")
        for cid, col_label, w in [("field", "Field", 280),
                                   ("old", "On disk", 180),
                                   ("new", "In memory", 180)]:
            tree.heading(cid, text=col_label)
            tree.column(cid, width=w, anchor="w")
        for d in diffs:
            tree.insert("", "end", values=d)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Button(top, text="Close", command=top.destroy
                   ).pack(side=tk.RIGHT, padx=8, pady=8)
        if not diffs:
            ttk.Label(top, text="In-memory matches on-disk — nothing to save.",
                      foreground="#666", padding=(8, 4)).pack(anchor="w")

    def _update_dirty_indicator(self) -> None:
        self._dirty_lbl.configure(
            foreground="#c2185b" if self._dirty else "#aaaaaa")

    def _refresh_topbar(self) -> None:
        repo = self._get_repo()
        self._topbar_vars["gold"].set(f"{repo.get_gold():,}")
        self._topbar_vars["stage"].set(str(repo.get_max_stage()))
        self._topbar_vars["account_lv"].set(str(repo.get_account_level()))
        self._topbar_vars["play_time"].set(f"{repo.get_play_time():,.0f}s")

    def _refresh_integrity(self) -> None:
        repo = self._get_repo()
        orphans = len(repo.all_orphan_uids())
        unreferenced = len(repo.all_unreferenced_items())
        msg = f"Integrity: {orphans} orphan UIDs, {unreferenced} unreferenced items"
        self._integrity_lbl.configure(text=msg,
            foreground="#c2185b" if (orphans or unreferenced) else "#888")

    def _get_repo(self) -> PlayerSaveRepository:
        return PlayerSaveRepository(get_save_data())

    def _on_close(self) -> None:
        if self._dirty:
            if not messagebox.askyesno("Unsaved changes",
                                       "You have unsaved changes. Quit anyway?"):
                return
        self.destroy()

# ---------------------------------------------------------------------------
# Data access layer (Repository pattern)
# ---------------------------------------------------------------------------
from typing import Any

SlotTable = str  # "inventorySaveDatas" | "stashSaveDatas" | "tradingStashSaveDatas"


class NoEmptySlotError(Exception):
    """Raised by detach_uid_from_hero when no empty slot is available in the
    target table. UI layer should catch this and prompt the user to pick one."""
    def __init__(self, table: str, uid: int) -> None:
        super().__init__(f"No empty slot in {table} for UID {uid}")
        self.table = table
        self.uid = uid


class Field:
    """Field-type tags for value parsing/serialization in the UI layer."""
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STR = "str"
    LIST_INT = "list_int"
    ENCHANT_DATA = "enchant_data"
    LOCALE_NAME = "locale_name"


def parse_value(raw, ctype):
    if ctype == Field.INT:
        if isinstance(raw, int):
            return raw
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0
    if ctype == Field.FLOAT:
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return 0.0
    if ctype == Field.BOOL:
        if isinstance(raw, bool):
            return raw
        return _parse_bool(str(raw))
    if ctype == Field.LIST_INT:
        if isinstance(raw, list):
            out = []
            for x in raw:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    pass
            return out
        return []
    if ctype == Field.ENCHANT_DATA:
        if not isinstance(raw, list):
            return []
        out = []
        keys = ("StatModKey", "Tier", "Value", "RecipeType",
                "ModType", "MaterialKey", "StatType")
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            row = {}
            for k in keys:
                try:
                    row[k] = int(entry.get(k, 0))
                except (TypeError, ValueError):
                    row[k] = 0
            out.append(row)
        return out
    return str(raw) if raw is not None else ""


def serialize_value(val, ctype):
    if ctype == Field.INT:
        return str(int(val)) if val is not None else "0"
    if ctype == Field.FLOAT:
        return f"{float(val):.4f}".rstrip("0").rstrip(".") if val is not None else "0"
    if ctype == Field.BOOL:
        return _serialize_bool(bool(val)) if isinstance(val, bool) else "false"
    if ctype == Field.LIST_INT:
        if not isinstance(raw, list) if False else not isinstance(val, list):
            return ""
        return ", ".join(str(int(x)) for x in val if _try_int(x) is not None)
    if ctype == Field.ENCHANT_DATA:
        if not isinstance(val, list):
            return "(no enchant data)"
        return f"{len(val)} rows"
    if ctype == Field.LOCALE_NAME:
        if isinstance(val, dict):
            return val.get("en-US") or val.get("zh-Hans") or next(iter(val.values()), "")
        return str(val) if val is not None else ""
    return str(val) if val is not None else ""


def _try_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


class PlayerSaveRepository:
    def __init__(self, psd):
        self.psd = psd

    # Mode A scalars
    def get_gold(self):
        rows = self.psd.get("currenySaveDatas") or []
        if rows and isinstance(rows[0], dict):
            try:
                return int(rows[0].get("Quantity", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def set_gold(self, value):
        rows = self.psd.setdefault("currenySaveDatas", [])
        if not rows:
            rows.append({"Key": 100001, "Quantity": 0})
        rows[0]["Quantity"] = int(value)

    def get_account_level(self):
        cube = self.psd.get("cubeSaveLevelData") or {}
        try:
            return int(cube.get("Level", 0))
        except (TypeError, ValueError):
            return 0

    def set_account_level(self, value):
        cube = self.psd.setdefault("cubeSaveLevelData", {})
        cube["Level"] = int(value)

    def get_account_exp(self):
        cube = self.psd.get("cubeSaveLevelData") or {}
        try:
            return int(cube.get("Exp", 0))
        except (TypeError, ValueError):
            return 0

    def set_account_exp(self, value):
        cube = self.psd.setdefault("cubeSaveLevelData", {})
        cube["Exp"] = int(value)

    def get_play_time(self):
        common = self.psd.get("commonSaveData") or {}
        try:
            return float(common.get("playTime", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def set_play_time(self, value):
        common = self.psd.setdefault("commonSaveData", {})
        common["playTime"] = float(value)

    def get_max_stage(self):
        common = self.psd.get("commonSaveData") or {}
        try:
            return int(common.get("maxCompletedStage", 0))
        except (TypeError, ValueError):
            return 0

    def set_max_stage(self, value):
        common = self.psd.setdefault("commonSaveData", {})
        common["maxCompletedStage"] = int(value)

    def get_current_stage_key(self):
        common = self.psd.get("commonSaveData") or {}
        try:
            return int(common.get("currentStageKey", 0))
        except (TypeError, ValueError):
            return 0

    def set_current_stage_key(self, value):
        common = self.psd.setdefault("commonSaveData", {})
        common["currentStageKey"] = int(value)

    def get_current_stage_wave(self):
        common = self.psd.get("commonSaveData") or {}
        try:
            return int(common.get("currentStageWave", 0))
        except (TypeError, ValueError):
            return 0

    def set_current_stage_wave(self, value):
        common = self.psd.setdefault("commonSaveData", {})
        common["currentStageWave"] = int(value)

    # Mode B lookups
    def find_skill(self, key):
        return self._find_in_list("attributeSaveDatas",
                                  lambda r: int(r.get("Key", -1)) == int(key))

    def find_rune(self, key):
        return self._find_in_list("RuneSaveData",
                                  lambda r: int(r.get("RuneKey", -1)) == int(key))

    def find_pet(self, key):
        return self._find_in_list("PetSaveData",
                                  lambda r: int(r.get("PetKey", -1)) == int(key))

    def find_cube_recipe(self, type_int):
        return self._find_in_list("cubeRecipeSaveDatas",
                                  lambda r: int(r.get("CubeRecipeTypeInt", -1)) == int(type_int))

    def find_item_by_uid(self, uid):
        target = _try_int(uid)
        if target is None or target == 0:
            return None
        return self._find_in_list("itemSaveDatas",
                                  lambda r: int(r.get("UniqueId", 0)) == target)

    def zero_enchant(self, uid):
        it = self.find_item_by_uid(uid)
        if it is None:
            return False
        ed = it.get("EnchantData")
        if not isinstance(ed, list):
            return False
        for row in ed:
            if isinstance(row, dict):
                for k in list(row.keys()):
                    row[k] = 0
        return True

    def copy_enchant(self, src_uid, dst_uid):
        src = self.find_item_by_uid(src_uid)
        dst = self.find_item_by_uid(dst_uid)
        if src is None or dst is None or src is dst:
            return False
        src_ed = src.get("EnchantData")
        if not isinstance(src_ed, list):
            return False
        dst["EnchantData"] = [dict(r) if isinstance(r, dict) else {}
                                for r in src_ed]
        return True

    def find_item_by_key(self, key):
        target = _try_int(key)
        if target is None:
            return None
        return self._find_in_list("itemSaveDatas",
                                  lambda r: int(r.get("ItemKey", -1)) == target)

    # ---- Stage box (loot box) management ---------------------------------
    def get_box_data(self):
        """Return the BoxData dict (creating it if absent).

        The three parallel lists (BoxTypes, BoxUniqueId, BoxQuantity) are
        created together on first access.  They stay in sync on every
        subsequent add / delete operation."""
        bd = self.psd.get("BoxData")
        if not isinstance(bd, dict):
            bd = {"BoxTypes": [], "BoxUniqueId": [], "BoxQuantity": []}
            self.psd["BoxData"] = bd
        for k in ("BoxTypes", "BoxUniqueId", "BoxQuantity"):
            if not isinstance(bd.get(k), list):
                bd[k] = []
        return bd

    def list_boxes(self):
        """Return a list of box dicts: {index, box_type, uid, qty, item}.

        The 'item' is the resolved itemSaveDatas dict for the box's UID
        (None when the UID is 0 or points at a non-existent item)."""
        bd = self.get_box_data()
        types = bd.get("BoxTypes") or []
        uids = bd.get("BoxUniqueId") or []
        qtys = bd.get("BoxQuantity") or []
        n = min(len(types), len(uids), len(qtys))
        out = []
        items = self.psd.get("itemSaveDatas") or []
        for i in range(n):
            uid = int(uids[i])
            item = None
            if uid:
                for it in items:
                    if isinstance(it, dict) and int(it.get("UniqueId", -1)) == uid:
                        item = it
                        break
            out.append({
                "index": i,
                "box_type": int(types[i]),
                "uid": uid,
                "qty": int(qtys[i]),
                "item": item,
            })
        return out

    def add_box(self, box_type=None, uid=0, qty=None):
        """Append a new box.  Returns the new box index."""
        bd = self.get_box_data()
        bd["BoxTypes"].append(int(box_type if box_type is not None else BOX_DEFAULT_TYPE))
        bd["BoxUniqueId"].append(int(uid))
        bd["BoxQuantity"].append(int(qty if qty is not None else BOX_DEFAULT_QTY))
        return len(bd["BoxTypes"]) - 1

    def delete_box(self, index):
        """Remove the box at ``index`` from all three parallel lists."""
        bd = self.get_box_data()
        for k in ("BoxTypes", "BoxUniqueId", "BoxQuantity"):
            lst = bd.get(k)
            if 0 <= index < len(lst):
                lst.pop(index)
        return None

    def set_box_field(self, index, field, value):
        """Set one field of one box.  ``field`` is 'box_type' / 'uid' / 'qty'."""
        keymap = {"box_type": "BoxTypes", "uid": "BoxUniqueId", "qty": "BoxQuantity"}
        list_key = keymap.get(field)
        if list_key is None:
            raise ValueError(f"unknown box field: {field!r}")
        bd = self.get_box_data()
        lst = bd.get(list_key)
        if lst is None or not (0 <= index < len(lst)):
            raise IndexError(f"box index out of range: {index}")
        if field == "qty":
            lst[index] = int(value)
        elif field == "uid":
            lst[index] = int(value)
        else:  # box_type
            lst[index] = int(value)
        return None

    def clear_boxes(self):
        """Empty all three box lists.  Returns the number of boxes removed."""
        bd = self.get_box_data()
        n = len(bd.get("BoxTypes") or [])
        bd["BoxTypes"] = []
        bd["BoxUniqueId"] = []
        bd["BoxQuantity"] = []
        return n

    def fill_box(self, index, item_save_data):
        """Attach an existing itemSaveDatas row to a box (uid+item_key)."""
        uid = int(item_save_data.get("UniqueId", 0))
        return self.set_box_field(index, "uid", uid)


    def items_with_gear(self, gear=None):
        """Return all itemSaveDatas rows whose catalog entry has the given
        ``gear`` (e.g. 'SWORD', 'HELMET'). If gear is None or empty, return
        every item. Items missing from the catalog are kept only when gear
        is None/empty."""
        if not gear:
            return [r for r in (self.psd.get("itemSaveDatas") or [])
                    if isinstance(r, dict)]
        target = str(gear).upper()
        out = []
        for r in self.psd.get("itemSaveDatas") or []:
            if not isinstance(r, dict):
                continue
            entry = _catalog_get(r.get("ItemKey"))
            if entry and str(entry.get("gear", "")).upper() == target:
                out.append(r)
        return out

    # ---- Quick-macro primitives (used by the Quick Macros bar) ----

    def unlock_all_heroes(self) -> int:
        """Set IsUnLock=True for every hero. Returns the number changed.

        Note: this only flips the IsUnLock flag on rows that already exist
        in heroSaveDatas. Heroes that the player has not yet unlocked in the
        game do not have a row at all - use unlock_hero() or
        unlock_all_heroes_including_missing() to add them."""
        n = 0
        for h in (self.psd.get("heroSaveDatas") or []):
            if isinstance(h, dict) and not h.get("IsUnLock"):
                h["IsUnLock"] = True
                n += 1
        return n

    def unlock_all_heroes_including_missing(self, *, level: int = 1) -> tuple:
        """Unlock every known heroKey, adding new rows for any that the player
        has not yet encountered in the game.

        Returns a tuple (unlocked_existing, added_new) so the UI can report
        both counts. New heroes start at the given level (default 1)."""
        existing_unlocked = self.unlock_all_heroes()
        added = 0
        for hk in list_known_hero_keys():
            if self.find_hero(hk) is not None:
                continue
            self.psd.setdefault("heroSaveDatas", []).append(
                new_hero_row(hk, level=level, unlocked=True))
            added += 1
        return (existing_unlocked, added)

    def add_hero(self, hero_key, *, level: int = 1, unlocked: bool = True):
        """Insert a new heroSaveDatas row for hero_key.

        If a row with that heroKey already exists, it is returned as-is and
        no second row is created (heroKey is unique in the schema).
        Returns the (existing or newly created) dict, or None if hero_key is
        not a positive integer."""
        try:
            hk = int(hero_key)
        except (TypeError, ValueError):
            return None
        if hk <= 0:
            return None
        existing = self.find_hero(hk)
        if existing is not None:
            return existing
        row = new_hero_row(hk, level=level, unlocked=unlocked)
        self.psd.setdefault("heroSaveDatas", []).append(row)
        return row

    def delete_hero(self, hero_key) -> bool:
        """Remove a hero row from heroSaveDatas. Returns True if removed."""
        try:
            hk = int(hero_key)
        except (TypeError, ValueError):
            return False
        rows = self.psd.get("heroSaveDatas") or []
        for i, h in enumerate(list(rows)):
            if isinstance(h, dict) and int(h.get("heroKey", -1)) == hk:
                rows.pop(i)
                return True
        return False

    def unlock_hero(self, hero_key, *, level: int = 1) -> bool:
        """Ensure a row exists for hero_key and that IsUnLock is True.

        Creates the row at the given level if missing. Returns True if the
        save now contains an unlocked row for that heroKey."""
        try:
            hk = int(hero_key)
        except (TypeError, ValueError):
            return False
        if hk <= 0:
            return False
        row = self.find_hero(hk)
        if row is None:
            row = new_hero_row(hk, level=level, unlocked=True)
            self.psd.setdefault("heroSaveDatas", []).append(row)
            return True
        changed = False
        if not row.get("IsUnLock"):
            row["IsUnLock"] = True
            changed = True
        return changed

    def lock_hero(self, hero_key) -> bool:
        """Set IsUnLock=False on the row for hero_key without removing it.

        Returns True if the flag was actually changed."""
        row = self.find_hero(hero_key)
        if row is None:
            return False
        if row.get("IsUnLock"):
            row["IsUnLock"] = False
            return True
        return False

    def list_heroes(self):
        """Return a snapshot of all heroSaveDatas rows (the live dicts).

        Sorted by heroKey ascending so the UI can render them in the same
        order the in-game collection screen uses."""
        rows = [h for h in (self.psd.get("heroSaveDatas") or [])
                if isinstance(h, dict)]
        rows.sort(key=lambda h: int(h.get("heroKey", 0)))
        return rows

    def list_missing_hero_keys(self):
        """Return the list of known heroKeys that are not yet in the save.

        Useful for the UI to show 'you can add: Knight#102, Ranger#102, ...'."""
        present = {int(h.get("heroKey", -1))
                   for h in (self.psd.get("heroSaveDatas") or [])
                   if isinstance(h, dict)}
        return [hk for hk in list_known_hero_keys() if hk not in present]

    def set_all_hero_levels(self, level: int) -> int:
        n = 0
        for h in (self.psd.get("heroSaveDatas") or []):
            if isinstance(h, dict):
                h["HeroLevel"] = int(level)
                n += 1
        return n

    def unlock_all_slots(self) -> int:
        """Set IsUnLock=True for every slot in inventory / stash / trading.
        Returns total slots changed."""
        n = 0
        for tbl in ("inventorySaveDatas", "stashSaveDatas",
                    "tradingStashSaveDatas"):
            for r in (self.psd.get(tbl) or []):
                if isinstance(r, dict) and not r.get("IsUnLock"):
                    r["IsUnLock"] = True
                    n += 1
        return n

    def clear_empty_slots(self) -> int:
        """Remove slot rows whose ItemUniqueId is 0 / falsy. Returns count
        removed. Index values are preserved (we don't renumber; that's the
        game's job)."""
        removed = 0
        for tbl in ("inventorySaveDatas", "stashSaveDatas",
                    "tradingStashSaveDatas"):
            rows = self.psd.get(tbl) or []
            keep = []
            for r in rows:
                if not isinstance(r, dict):
                    keep.append(r); continue
                uid = int(r.get("ItemUniqueId", 0) or 0)
                if uid == 0:
                    removed += 1
                else:
                    keep.append(r)
            self.psd[tbl] = keep
        return removed

    def gear_of(self, item):
        """Return the gear string (e.g. 'SWORD') for an itemSaveDatas row,
        or '' if not in the catalog."""
        if not isinstance(item, dict):
            return ""
        entry = _catalog_get(item.get("ItemKey"))
        return str(entry.get("gear", "")) if entry else ""

    def find_hero(self, key):
        return self._find_in_list("heroSaveDatas",
                                  lambda r: int(r.get("heroKey", -1)) == int(key))

    def find_inventory_slot(self, idx):
        return self._find_in_list("inventorySaveDatas",
                                  lambda r: int(r.get("Index", -1)) == int(idx))

    def find_stash_slot(self, idx):
        return self._find_in_list("stashSaveDatas",
                                  lambda r: int(r.get("Index", -1)) == int(idx))

    def find_trading_slot(self, idx):
        return self._find_in_list("tradingStashSaveDatas",
                                  lambda r: int(r.get("Index", -1)) == int(idx))

    def find_aggregate(self, type_int, sub_key):
        return self._find_in_list("aggregateSaveDatas",
                                  lambda r: int(r.get("Type", -1)) == int(type_int)
                                            and int(r.get("SubKey", -1)) == int(sub_key))

    def _find_in_list(self, key, predicate):
        for row in self.psd.get(key) or []:
            if isinstance(row, dict) and predicate(row):
                return row
        return None

    # Mode B setters
    def set_skill_level(self, key, level):
        row = self.find_skill(key)
        if row is None: return False
        row["Level"] = int(level)
        return True

    def set_rune_level(self, key, level):
        row = self.find_rune(key)
        if row is None: return False
        row["Level"] = int(level)
        return True

    def set_pet_unlocked(self, key, unlocked):
        row = self.find_pet(key)
        if row is None: return False
        row["IsUnlock"] = bool(unlocked)
        return True

    def set_pet_viewed(self, key, viewed):
        row = self.find_pet(key)
        if row is None: return False
        row["IsViewed"] = bool(viewed)
        return True

    def set_cube_recipe_max_unlock(self, type_int, max_key):
        row = self.find_cube_recipe(type_int)
        if row is None: return False
        row["MaxUnlockRecipeKey"] = int(max_key)
        return True

    def set_inventory_slot_field(self, idx, field, value):
        row = self.find_inventory_slot(idx)
        if row is None: return False
        row[field] = value
        return True

    def set_stash_slot_field(self, idx, field, value):
        row = self.find_stash_slot(idx)
        if row is None: return False
        row[field] = value
        return True

    def set_trading_slot_field(self, idx, field, value):
        row = self.find_trading_slot(idx)
        if row is None: return False
        row[field] = value
        return True

    def set_item_field(self, uid, field, value):
        row = self.find_item_by_uid(uid)
        if row is None: return False
        row[field] = value
        return True

    def set_hero_field(self, key, field, value):
        row = self.find_hero(key)
        if row is None: return False
        row[field] = value
        return True

    def set_aggregate_value(self, type_int, sub_key, value):
        row = self.find_aggregate(type_int, sub_key)
        if row is None: return False
        row["Value"] = int(value)
        return True

    # Mode C primitives
    def create_item(self, uid, item_key):
        existing = self.find_item_by_uid(uid)
        if existing is not None:
            existing["ItemKey"] = int(item_key)
            return existing
        # Field order mirrors the IL2CPP ItemSaveData layout (TypeDefIndex 2922):
        # ItemKey / UniqueId / 3 bools / EnchantCount / EnchantData / 3 totals.
        # The game re-serialises this dict on save; keep the canonical order
        # so freshly-created items are byte-equivalent to game-issued ones
        # (only the values differ).
        entry = {
            "ItemKey": int(item_key),
            "UniqueId": int(uid),
            "IsChaotic": False,
            "IsBlocked": False,
            "IsServerPendingItem": False,
            "EnchantCount": [0, 0, 0],
            "EnchantData": [
                {"StatModKey": 0, "Tier": 0, "Value": 0,
                 "RecipeType": 0, "ModType": 0,
                 "MaterialKey": 0, "StatType": 0}
                for _ in range(6)
            ],
            "DecorationAppliedTotalCount": 0,
            "EngravingAppliedTotalCount": 0,
            "InscriptionAppliedTotalCount": 0,
        }
        self.psd.setdefault("itemSaveDatas", []).append(entry)
        return entry

    def find_uid_owners(self, uid: int) -> list[tuple]:
        """Return a list of (table, ...) tuples identifying every place
        this UID is currently bound. Used by delete_item to refuse
        unsafe deletes and to surface a useful error to the user.

        Looks at:
          - inventorySaveDatas / stashSaveDatas / tradingStashSaveDatas
            (each row's ItemUniqueId)
          - heroSaveDatas[i].equippedItemIds (10 slots per hero)
          - boxData[i].uid (the box's referenced item UID)

        Returns [] if the UID is unbound.
        """
        uid = int(uid)
        owners: list[tuple] = []
        if not uid:
            return owners
        for tbl in ("inventorySaveDatas", "stashSaveDatas",
                    "tradingStashSaveDatas"):
            for r in self.psd.get(tbl) or []:
                if isinstance(r, dict) and int(r.get("ItemUniqueId", 0)) == uid:
                    idx = r.get("Index", -1)
                    owners.append((tbl, int(idx)))
        for i, h in enumerate(self.psd.get("heroSaveDatas") or []):
            if not isinstance(h, dict):
                continue
            eq = h.get("equippedItemIds") or []
            for slot_i, slot_uid in enumerate(eq):
                try:
                    if int(slot_uid) == uid:
                        owners.append(("heroSaveDatas", i, slot_i))
                except (TypeError, ValueError):
                    continue
        for i, b in enumerate(self.psd.get("boxData") or []):
            if isinstance(b, dict) and int(b.get("uid", 0)) == uid:
                owners.append(("boxData", i))
        return owners

    def delete_item(self, uid: int) -> bool:
        """Remove the itemSaveDatas row for `uid`. Before removing the
        row, cascade-clear every binding that references this UID so
        the save is left in a consistent state:
          - inventorySaveDatas / stashSaveDatas / tradingStashSaveDatas
            rows have ItemUniqueId set to 0
          - heroSaveDatas[i].equippedItemIds[slot_i] is set to 0
          - boxData[i].uid is set to 0

        Returns True on success, False if the row does not exist.
        """
        uid = int(uid)
        if not uid:
            return False
        if self.find_item_by_uid(uid) is None:
            return False
        self._clear_uid_bindings(uid)
        items = self.psd.get("itemSaveDatas") or []
        for i, it in enumerate(items):
            if isinstance(it, dict) and int(it.get("UniqueId", 0)) == uid:
                items.pop(i)
                return True
        return False

    def _clear_uid_bindings(self, uid: int) -> int:
        """Internal: zero out every place this UID is referenced.
        Returns the number of bindings cleared. Used by delete_item
        to leave the save consistent. Does not touch itemSaveDatas.
        """
        uid = int(uid)
        if not uid:
            return 0
        cleared = 0
        for tbl in ("inventorySaveDatas", "stashSaveDatas",
                    "tradingStashSaveDatas"):
            for r in self.psd.get(tbl) or []:
                if isinstance(r, dict) and int(r.get("ItemUniqueId", 0)) == uid:
                    r["ItemUniqueId"] = 0
                    cleared += 1
        for h in self.psd.get("heroSaveDatas") or []:
            if not isinstance(h, dict):
                continue
            eq = h.get("equippedItemIds") or []
            for slot_i, slot_uid in enumerate(eq):
                try:
                    if int(slot_uid) == uid:
                        eq[slot_i] = 0
                        cleared += 1
                except (TypeError, ValueError):
                    continue
            h["equippedItemIds"] = eq
        for b in self.psd.get("boxData") or []:
            if isinstance(b, dict) and int(b.get("uid", 0)) == uid:
                b["uid"] = 0
                cleared += 1
        return cleared

    def clear_slot(self, table, idx):
        if table == "inventorySaveDatas":
            row = self.find_inventory_slot(idx)
            if row is None: return False
            row["ItemUniqueId"] = 0
            row["IsUnlock"] = True
            row["IsUnlockedByRune"] = False
            return True
        if table == "stashSaveDatas":
            row = self.find_stash_slot(idx)
            if row is None: return False
            row["ItemUniqueId"] = 0
            row["IsUnLock"] = True
            return True
        if table == "tradingStashSaveDatas":
            row = self.find_trading_slot(idx)
            if row is None: return False
            row["ItemUniqueId"] = 0
            row["IsUnLock"] = True
            return True
        raise ValueError(f"Unknown slot table: {table}")

    def set_slot_uid(self, table, idx, uid):
        if table == "inventorySaveDatas":
            return self.set_inventory_slot_field(idx, "ItemUniqueId", int(uid))
        if table == "stashSaveDatas":
            return self.set_stash_slot_field(idx, "ItemUniqueId", int(uid))
        if table == "tradingStashSaveDatas":
            return self.set_trading_slot_field(idx, "ItemUniqueId", int(uid))
        raise ValueError(f"Unknown slot table: {table}")

    def set_slot_locked(self, table, idx, locked):
        """Set IsUnLock on a slot row. locked=True means the slot is unlocked."""
        if table == "inventorySaveDatas":
            return self.set_inventory_slot_field(idx, "IsUnLock", bool(locked))
        if table == "stashSaveDatas":
            return self.set_stash_slot_field(idx, "IsUnLock", bool(locked))
        if table == "tradingStashSaveDatas":
            return self.set_trading_slot_field(idx, "IsUnLock", bool(locked))
        raise ValueError(f"Unknown slot table: {table}")

    def set_cube_recipe_locked(self, type_int, locked):
        row = self.find_cube_recipe(type_int)
        if row is None: return False
        row["IsUnLock"] = bool(locked)
        return True

    def attach_uid(self, table, idx, uid, item_key=None):
        if not self.set_slot_uid(table, idx, uid):
            return False
        if item_key is not None:
            self.create_item(uid, item_key)
        return True

    def swap_slots(self, a, b):
        """Swap *content* (UID + lock flags) between two slots. Index preserved."""
        ra = self._find_slot_row(a[0], a[1])
        rb = self._find_slot_row(b[0], b[1])
        if ra is None or rb is None: return False
        if a[0] == "inventorySaveDatas":
            content_keys = ("ItemUniqueId", "IsUnlock", "IsUnlockedByRune")
        else:
            content_keys = ("ItemUniqueId", "IsUnLock")
        if a[0] != b[0]:
            ra["ItemUniqueId"], rb["ItemUniqueId"] = (
                rb.get("ItemUniqueId", 0), ra.get("ItemUniqueId", 0))
            return True
        snap_a = {k: ra.get(k) for k in content_keys}
        snap_b = {k: rb.get(k) for k in content_keys}
        for k in content_keys:
            ra[k] = snap_b.get(k)
            rb[k] = snap_a.get(k)
        return True

    def move_uid_between_slots(self, src, dst):
        if src == dst: return True
        src_row = self._find_slot_row(src[0], src[1])
        dst_row = self._find_slot_row(dst[0], dst[1])
        if src_row is None or dst_row is None: return False
        uid = src_row.get("ItemUniqueId", 0)
        if not uid: return False
        src_keys = self._slot_field_names(src[0])
        dst_keys = self._slot_field_names(dst[0])
        snapshot = {k: src_row.get(k) for k in src_keys}
        for k in dst_keys:
            if k == "Index": continue
            if k in snapshot:
                dst_row[k] = snapshot[k]
        self.clear_slot(src[0], src[1])
        return True

    def attach_uid_to_hero(self, hero_key, slot_idx, uid, item_key=None):
        hero = self.find_hero(hero_key)
        if hero is None: return False
        equipped = list(hero.get("equippedItemIds") or [])
        while len(equipped) < 10: equipped.append(0)
        if not (0 <= slot_idx < len(equipped)): return False
        equipped[slot_idx] = int(uid)
        hero["equippedItemIds"] = equipped
        if item_key is not None:
            self.create_item(uid, item_key)
        return True

    def detach_uid_from_hero(self, hero_key, slot_idx, into="stashSaveDatas",
                             target_idx=None):
        hero = self.find_hero(hero_key)
        if hero is None:
            raise ValueError(f"hero not found: hero_key={hero_key}")
        equipped = list(hero.get("equippedItemIds") or [])
        while len(equipped) < 10: equipped.append(0)
        if not (0 <= slot_idx < len(equipped)):
            raise ValueError(f"slot_idx out of range: {slot_idx}")
        uid = equipped[slot_idx]
        if not uid:
            raise ValueError(f"hero slot is already empty: hero_key={hero_key} slot={slot_idx}")
        if target_idx is None:
            target_idx = self.find_empty_slot(into)
            if target_idx is None:
                raise NoEmptySlotError(into, uid)
        if not self.set_slot_uid(into, target_idx, uid):
            raise ValueError(f"target slot not found: table={into} idx={target_idx}")
        equipped[slot_idx] = 0
        hero["equippedItemIds"] = equipped
        return target_idx

    def swap_hero_equip_with_slot(self, hero_key, slot_idx, target_table, target_idx):
        hero = self.find_hero(hero_key)
        if hero is None:
            raise ValueError(f"hero not found: hero_key={hero_key}")
        equipped = list(hero.get("equippedItemIds") or [])
        while len(equipped) < 10: equipped.append(0)
        if not (0 <= slot_idx < len(equipped)):
            raise ValueError(f"slot_idx out of range: {slot_idx}")
        target_row = self._find_slot_row(target_table, target_idx)
        if target_row is None:
            raise ValueError(f"target slot not found: table={target_table} idx={target_idx}")
        hero_uid = int(equipped[slot_idx])
        target_uid = int(target_row.get("ItemUniqueId", 0))
        if hero_uid and hero_uid == target_uid:
            equipped[slot_idx] = 0
            hero["equippedItemIds"] = equipped
            return 0
        equipped[slot_idx] = target_uid
        target_row["ItemUniqueId"] = hero_uid
        hero["equippedItemIds"] = equipped
        return target_uid

    def find_empty_slot(self, table):
        rows = self.psd.get(table) or []
        for row in rows:
            if isinstance(row, dict) and not int(row.get("ItemUniqueId", 0)):
                try:
                    return int(row.get("Index", -1))
                except (TypeError, ValueError):
                    continue
        return None

    def count_filled_slots(self, table):
        rows = self.psd.get(table) or []
        return sum(1 for r in rows
                   if isinstance(r, dict) and int(r.get("ItemUniqueId", 0)) != 0)

    def all_orphan_uids(self):
        items = self.psd.get("itemSaveDatas") or []
        present = {int(it.get("UniqueId", 0)) for it in items
                   if isinstance(it, dict)}
        referenced = set()
        for table in ("inventorySaveDatas", "stashSaveDatas", "tradingStashSaveDatas"):
            for r in self.psd.get(table) or []:
                if isinstance(r, dict):
                    uid = int(r.get("ItemUniqueId", 0))
                    if uid: referenced.add(uid)
        for hero in self.psd.get("heroSaveDatas") or []:
            if isinstance(hero, dict):
                for uid in (hero.get("equippedItemIds") or []):
                    try:
                        u = int(uid)
                    except (TypeError, ValueError):
                        continue
                    if uid:
                        referenced.add(u)
        return sorted(referenced - present)

    def all_unreferenced_items(self):
        items = self.psd.get("itemSaveDatas") or []
        referenced = set()
        for table in ("inventorySaveDatas", "stashSaveDatas", "tradingStashSaveDatas"):
            for r in self.psd.get(table) or []:
                if isinstance(r, dict):
                    uid = int(r.get("ItemUniqueId", 0))
                    if uid: referenced.add(uid)
        for hero in self.psd.get("heroSaveDatas") or []:
            if isinstance(hero, dict):
                for uid in (hero.get("equippedItemIds") or []):
                    try:
                        u = int(uid)
                    except (TypeError, ValueError):
                        continue
                    if u: referenced.add(u)
        return [it for it in items
                if isinstance(it, dict)
                and int(it.get("UniqueId", 0)) not in referenced]

    def _slot_field_names(self, table):
        if table == "inventorySaveDatas":
            return ("Index", "ItemUniqueId", "IsUnlock", "IsUnlockedByRune")
        if table in ("stashSaveDatas", "tradingStashSaveDatas"):
            return ("Index", "ItemUniqueId", "IsUnLock")
        raise ValueError(f"Unknown slot table: {table}")

    def _find_slot_row(self, table, idx):
        if table == "inventorySaveDatas":
            return self.find_inventory_slot(idx)
        if table == "stashSaveDatas":
            return self.find_stash_slot(idx)
        if table == "tradingStashSaveDatas":
            return self.find_trading_slot(idx)
        raise ValueError(f"Unknown slot table: {table}")


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_cell(value: Any, ctype: str) -> str:
    if value is None:
        return ""
    if ctype == "bool":
        return "✓" if value else "✗"
    if ctype == "list":
        if not isinstance(value, list):
            return str(value)
        if len(value) == 0:
            return "[]"
        preview = ", ".join(str(x) for x in value[:3])
        if len(value) > 3:
            preview += f", … (+{len(value)-3})"
        return preview
    if isinstance(value, float) and ctype == "float" and abs(value) >= 1000:
        return f"{value:,.2f}"
    return str(value)


def _default_for_type(ctype: str) -> Any:
    if ctype == "int":
        return 0
    if ctype == "float":
        return 0.0
    if ctype == "bool":
        return False
    return ""


def main() -> None:
    file = ES3File.load(DEFAULT_PATH)
    app = App(file)
    app.mainloop()


if __name__ == "__main__":
    main()
