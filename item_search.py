#!/usr/bin/env python3
"""tkinter desktop app to search and browse item_catalog.json.

Usage:
    python3 item_search.py
"""

from __future__ import annotations

import json
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Iterable


CATALOG_PATH = "item_catalog.json"

COLUMNS = (
    ("id", "ID", 80, "e"),
    ("name", "Name", 260, "w"),
    ("level", "Lv", 50, "e"),
    ("type", "Type", 90, "w"),
    ("gear", "Gear", 90, "w"),
    ("grade", "Grade", 90, "w"),
    ("stat", "Stat", 50, "center"),
)
COLUMN_KEYS = tuple(c[0] for c in COLUMNS)

KNOWN_TYPES = ("GEAR", "MATERIAL", "STAGEBOX")
KNOWN_GRADES = (
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
KNOWN_GEARS = (
    "SWORD", "DAGGER", "BOW", "STAFF", "SHIELD", "AXE", "HATCHET",
    "MACE", "SPEAR", "CROSSBOW", "ARMOR", "HELMET", "GLOVES", "BOOTS",
    "BRACER", "RING", "AMULET", "EARING", "SCEPTER", "ORB", "TOME",
    "ARROW", "BOLT",
)
FACET_ALL = "All"

LANG_EN = "EN"
LANG_ZH = "中文"
LANGUAGES = (LANG_EN, LANG_ZH)

ZH = {
    "All": "全部",
    "GEAR": "装备", "MATERIAL": "材料", "STAGEBOX": "关卡宝箱",
    "COMMON": "普通", "UNCOMMON": "高级", "RARE": "稀有",
    "LEGENDARY": "传说", "IMMORTAL": "不朽", "ARCANA": "奥秘",
    "BEYOND": "超越", "CELESTIAL": "天堂", "DIVINE": "神器", "COSMIC": "宇宙",
    "SWORD": "剑", "DAGGER": "匕首", "BOW": "弓", "STAFF": "法杖", "SHIELD": "盾牌",
    "AXE": "斧", "HATCHET": "短柄斧", "MACE": "锤", "SPEAR": "矛", "CROSSBOW": "弩",
    "ARMOR": "护甲", "HELMET": "头盔", "GLOVES": "手套", "BOOTS": "靴子", "BRACER": "护腕",
    "RING": "戒指", "AMULET": "护身符", "EARING": "耳环",
    "SCEPTER": "权杖", "ORB": "法球", "TOME": "魔典",
    "ARROW": "箭", "BOLT": "弩箭",
}
ZH_TO_EN = {v: k for k, v in ZH.items()}


def label_for(canonical: str, mode: str) -> str:
    if mode == LANG_ZH:
        return ZH.get(canonical, canonical)
    return canonical


def to_canonical(value: str, mode: str) -> str:
    if mode == LANG_ZH:
        return ZH_TO_EN.get(value, value)
    return value


def fatal_error(title: str, message: str) -> None:
    """Show an error dialog and exit. Used for unrecoverable startup errors."""
    try:
        messagebox.showerror(title, message)
    finally:
        sys.exit(1)


class ItemCatalog:
    """Loads item_catalog.json once and exposes a search() method."""

    def __init__(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            fatal_error("Item catalog not found", f"Cannot open: {path}")
            return
        except json.JSONDecodeError as exc:
            fatal_error("Invalid JSON", f"{path}\n\n{exc}")
            return

        if not isinstance(data, list):
            fatal_error(
                "Unexpected catalog shape",
                f"Expected a JSON array, got {type(data).__name__}",
            )
            return

        self.items: list[dict] = data
        n = len(data)
        self._haystacks: list[str] = [self._build_haystack(it) for it in self.items]
        self._locales: list[str] = self._collect_locales(self.items)
        self._build_indices()

    @staticmethod
    def _build_haystack(item: dict) -> str:
        # `name` is now a locale→string dict (en-US, zh-Hans, ja-JP, ...).
        # Fall back to display_name / name_key if no localized entries yet.
        name_field = item.get("name") or {}
        if isinstance(name_field, dict):
            names = " ".join(filter(None, name_field.values()))
        else:
            names = str(name_field or "")
        if not names:
            names = (
                item.get("display_name", "")
                or item.get("name_key", "")
                or item.get("icon", "")
            )
        # Gear field: prefer gear_type (new schema), fall back to legacy "gear".
        gear_val = item.get("gear_type") or item.get("gear") or ""
        parts = [
            names,
            str(item.get("id", "")),
            item.get("slug", "") or "",
            item.get("type", "") or "",
            gear_val,
        ]
        return " ".join(parts).lower()

    @staticmethod
    def _collect_locales(items: Iterable[dict]) -> list[str]:
        seen: set[str] = set()
        for item in items:
            name_field = item.get("name") or {}
            if isinstance(name_field, dict):
                for locale in name_field.keys():
                    seen.add(locale)
        return sorted(seen)

    def _build_indices(self) -> None:
        """Pre-compute per-facet index sets so search() is O(facets + k) not O(n)."""
        idx_type: dict[str, set[int]] = {}
        idx_grade: dict[str, set[int]] = {}
        idx_gear: dict[str, set[int]] = {}
        idx_level: dict[int, set[int]] = {}
        for i, item in enumerate(self.items):
            t = item.get("type") or ""
            if t not in idx_type:
                idx_type[t] = set()
            idx_type[t].add(i)
            g = item.get("grade") or ""
            if g not in idx_grade:
                idx_grade[g] = set()
            idx_grade[g].add(i)
            # New schema uses gear_type; old schema used "gear". Either works.
            ge = item.get("gear_type") or item.get("gear") or ""
            if ge:
                if ge not in idx_gear:
                    idx_gear[ge] = set()
                idx_gear[ge].add(i)
            lv = item.get("level")
            if isinstance(lv, int):
                if lv not in idx_level:
                    idx_level[lv] = set()
                idx_level[lv].add(i)
        self._idx_type = idx_type
        self._idx_grade = idx_grade
        self._idx_gear = idx_gear
        self._idx_level = idx_level

    def search(
        self,
        query: str = "",
        *,
        type_: str = FACET_ALL,
        grade: str = FACET_ALL,
        gear: str = FACET_ALL,
        lv_min: int | None = None,
        lv_max: int | None = None,
        include_deleted: bool = False,
    ) -> list[dict]:
        q = (query or "").strip().lower()
        # Narrow candidate indices by intersecting active facet sets.
        # None means "no constraint yet"; we start with the first active constraint
        # and intersect the rest onto it.
        candidates: set[int] | None = None
        if type_ != FACET_ALL:
            candidates = set(self._idx_type.get(type_, ()))
        if grade != FACET_ALL:
            gset = self._idx_grade.get(grade, ())
            candidates = set(gset) if candidates is None else (candidates & gset)
        if gear != FACET_ALL:
            gset = self._idx_gear.get(gear, ())
            candidates = set(gset) if candidates is None else (candidates & gset)
        if lv_min is not None or lv_max is not None:
            lvset: set[int] = set()
            for lv, idxs in self._idx_level.items():
                if (lv_min is None or lv >= lv_min) and (lv_max is None or lv <= lv_max):
                    lvset |= idxs
            candidates = lvset if candidates is None else (candidates & lvset)
        if candidates is None:
            candidates = set(range(len(self.items)))

        results: list[dict] = []
        for i in candidates:
            item = self.items[i]
            # New schema: deleted = legacy boolean OR is_deleted_server from CSV.
            is_deleted = item.get("deleted") is True or item.get("is_deleted_server") is True
            if not include_deleted and is_deleted:
                continue
            if q and q not in self._haystacks[i]:
                continue
            results.append(item)
        return results

    def count_deleted_in_level_range(
        self, lv_min: int | None, lv_max: int | None
    ) -> int:
        """Count items in a level range that are marked deleted. O(1) per level
        thanks to the pre-computed _idx_level. Used for the 'X deleted in range'
        hint when the active search returns 0.
        """
        total = 0
        for lv, idxs in self._idx_level.items():
            if (lv_min is None or lv >= lv_min) and (lv_max is None or lv <= lv_max):
                for i in idxs:
                    item = self.items[i]
                    if item.get("deleted") is True or item.get("is_deleted_server") is True:
                        total += 1
        return total


class App(tk.Tk):
    """Tk root window. Owns widgets and orchestrates search/refresh."""

    def __init__(self, catalog: ItemCatalog) -> None:
        super().__init__()
        self.catalog = catalog
        self.title("Item Catalog Search")
        self.geometry("1100x720")
        self.minsize(900, 560)

        self.var_query = tk.StringVar()
        self.var_lang = tk.StringVar(value=LANG_EN)
        self.var_type = tk.StringVar(value=FACET_ALL)
        self.var_grade = tk.StringVar(value=FACET_ALL)
        self.var_gear = tk.StringVar(value=FACET_ALL)
        self.var_lv_min = tk.StringVar()
        self.var_lv_max = tk.StringVar()
        self.var_show_deleted = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="")
        self.var_selection = tk.StringVar(value="Selected: 0")
        self._after_id: str | None = None
        self._current_mode = LANG_EN
        self._copy_columns: list[str] = [c[0] for c in COLUMNS]
        self._last_selection: tuple[str, ...] = ()

        self._build_filter_row()
        self._build_treeview()
        self._build_detail_row()
        self._refresh()

        self._search_entry.focus_set()
        self._search_entry.bind("<Escape>", lambda _e: (self._on_clear(), "break"))
        self._lv_min_entry.bind("<Escape>", lambda _e: self._clear_lv(self.var_lv_min))
        self._lv_max_entry.bind("<Escape>", lambda _e: self._clear_lv(self.var_lv_max))
        self.bind("<Control-q>", lambda _e: self.destroy())
        self.bind("<Control-Q>", lambda _e: self.destroy())

    def _build_filter_row(self) -> None:
        frame = ttk.Frame(self, padding=(8, 8, 8, 4))
        frame.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(frame, text="Search:").pack(side=tk.LEFT)
        self._search_entry = ttk.Entry(frame, textvariable=self.var_query, width=48)
        self._search_entry.pack(side=tk.LEFT, padx=(6, 4))
        self._search_entry.bind("<KeyRelease>", self._on_query_keyrelease)
        ttk.Button(frame, text="Clear", command=self._on_clear).pack(side=tk.LEFT)

        self._lang_combo = ttk.Combobox(
            frame, textvariable=self.var_lang,
            values=LANGUAGES, state="readonly", width=6,
        )
        self._lang_combo.pack(side=tk.LEFT, padx=(12, 0))
        self.var_lang.trace_add("write", lambda *_: self._on_lang_change())

        ttk.Button(frame, text="Copy", command=self._copy_selected,
                   width=8).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(frame, text="Copy all", command=self._copy_all_visible,
                   width=10).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(frame, text="Columns…", command=self._open_copy_columns_dialog,
                   width=10).pack(side=tk.LEFT, padx=(4, 0))

        type_frame = ttk.Frame(self, padding=(8, 0, 8, 4))
        type_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(type_frame, text="Type:").pack(side=tk.LEFT)
        self._type_combo = ttk.Combobox(
            type_frame, textvariable=self.var_type,
            values=[label_for(v, LANG_EN) for v in (FACET_ALL, *KNOWN_TYPES)],
            state="readonly", width=12,
        )
        self._type_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.var_type.trace_add("write", lambda *_: self._schedule_refresh())

        ttk.Label(type_frame, text="Grade:").pack(side=tk.LEFT)
        self._grade_combo = ttk.Combobox(
            type_frame, textvariable=self.var_grade,
            values=[label_for(v, LANG_EN) for v in (FACET_ALL, *KNOWN_GRADES)],
            state="readonly", width=12,
        )
        self._grade_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.var_grade.trace_add("write", lambda *_: self._schedule_refresh())

        ttk.Label(type_frame, text="Gear:").pack(side=tk.LEFT)
        self._gear_values = (FACET_ALL, *KNOWN_GEARS)
        self._gear_combo = ttk.Combobox(
            type_frame, textvariable=self.var_gear,
            values=[label_for(v, LANG_EN) for v in self._gear_values],
            state="readonly", width=12,
        )
        self._gear_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.var_gear.trace_add("write", lambda *_: self._schedule_refresh())

        ttk.Label(type_frame, text="Lv:").pack(side=tk.LEFT, padx=(12, 0))
        self._lv_min_entry = ttk.Entry(type_frame, textvariable=self.var_lv_min,
                                       width=5, justify="right")
        self._lv_min_entry.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(type_frame, text="–").pack(side=tk.LEFT, padx=(2, 2))
        self._lv_max_entry = ttk.Entry(type_frame, textvariable=self.var_lv_max,
                                       width=5, justify="right")
        self._lv_max_entry.pack(side=tk.LEFT)
        self._lv_min_entry.bind("<KeyRelease>", self._on_query_keyrelease)
        self._lv_max_entry.bind("<KeyRelease>", self._on_query_keyrelease)

        ttk.Checkbutton(
            type_frame, text="Show deleted",
            variable=self.var_show_deleted, command=self._schedule_refresh,
        ).pack(side=tk.LEFT, padx=(8, 0))

        self.var_selection = tk.StringVar(value="Selected: 0")
        sel_label = ttk.Label(self, textvariable=self.var_selection, anchor="w",
                              padding=(8, 0, 8, 0))
        sel_label.pack(side=tk.TOP, fill=tk.X)

        status = ttk.Label(self, textvariable=self.var_status, anchor="w",
                           padding=(8, 0, 8, 4))
        status.pack(side=tk.TOP, fill=tk.X)

    def _on_lang_change(self) -> None:
        old_mode = self._current_mode
        new_mode = self.var_lang.get()
        self._type_combo["values"] = [label_for(v, new_mode) for v in (FACET_ALL, *KNOWN_TYPES)]
        self._grade_combo["values"] = [label_for(v, new_mode) for v in (FACET_ALL, *KNOWN_GRADES)]
        self._gear_combo["values"] = [label_for(v, new_mode) for v in self._gear_values]
        # Translate each var: old displayed value → canonical → new displayed value
        self.var_type.set(label_for(to_canonical(self.var_type.get(), old_mode), new_mode))
        self.var_grade.set(label_for(to_canonical(self.var_grade.get(), old_mode), new_mode))
        self.var_gear.set(label_for(to_canonical(self.var_gear.get(), old_mode), new_mode))
        self._current_mode = new_mode
        self._refresh()

    def _on_query_keyrelease(self, _event: tk.Event) -> None:
        self._schedule_refresh()

    def _on_clear(self) -> None:
        self.var_query.set("")
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.after(150, self._refresh)

    def _refresh(self) -> None:
        self._after_id = None
        mode = self.var_lang.get()
        lv_min = self._parse_int_or_none(self.var_lv_min.get())
        lv_max = self._parse_int_or_none(self.var_lv_max.get())
        include_deleted = self.var_show_deleted.get()
        results = self.catalog.search(
            self.var_query.get(),
            type_=to_canonical(self.var_type.get(), mode),
            grade=to_canonical(self.var_grade.get(), mode),
            gear=to_canonical(self.var_gear.get(), mode),
            lv_min=lv_min,
            lv_max=lv_max,
            include_deleted=include_deleted,
        )
        self._render_rows(results)
        total = len(self.catalog.items)
        status = f"Matched: {len(results)} / {total}"
        # Hint when Lv range is active and the active set is empty but deleted items exist.
        lv_active = lv_min is not None or lv_max is not None
        if not results and not include_deleted and lv_active:
            deleted_in_range = self.catalog.count_deleted_in_level_range(lv_min, lv_max)
            if deleted_in_range:
                status += f"  ({deleted_in_range} deleted in range — enable Show deleted)"
        self.var_status.set(status)

    @staticmethod
    def _parse_int_or_none(text: str) -> int | None:
        s = (text or "").strip()
        if not s:
            return None
        try:
            n = int(s)
        except ValueError:
            return None
        return n if n >= 0 else None

    def _clear_lv(self, var: tk.StringVar) -> str:
        var.set("")
        self._schedule_refresh()
        return "break"

    def _build_treeview(self) -> None:
        frame = ttk.Frame(self, padding=(8, 0, 8, 4))
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            frame, columns=COLUMN_KEYS, show="headings", height=14,
        )
        for key, label, width, anchor in COLUMNS:
            self.tree.heading(key, text=label,
                              command=lambda k=key: self._on_header_click(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=True)

        self.tree.tag_configure("ok", foreground="#222222")
        self.tree.tag_configure("deleted", foreground="#999999")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Control-c>", lambda _e: self._copy_selected())
        self.tree.bind("<Control-C>", lambda _e: self._copy_selected())
        self.tree.bind("<Control-a>", self._on_select_all)
        self.tree.bind("<Command-a>", self._on_select_all)
        self.tree.bind("<Meta-a>", self._on_select_all)
        self.tree.bind("<Mod1-a>", self._on_select_all)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self._sort_col: str | None = None
        self._sort_desc: bool = False

    def _build_detail_row(self) -> None:
        frame = ttk.LabelFrame(self, text="Detail", padding=(8, 4, 8, 8))
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=False)
        self.detail = tk.Text(frame, height=10, wrap="word", state="disabled",
                              font=("TkFixedFont", 11))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=vsb.set)
        self.detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _render_rows(self, results: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        if not results:
            self.tree.insert("", tk.END, values=("[No results]",) + ("",) * 5,
                             tags=("ok",))
            return

        rows: list[tuple] = []
        mode = self.var_lang.get()
        for item in results:
            # Name: locale dict (new schema) or display_name fallback.
            names = item.get("name") or {}
            if not isinstance(names, dict):
                names = {}
            if mode == LANG_ZH:
                # 中文 mode: prefer zh-Hans / zh-Hant
                zh_hans = names.get("zh-Hans") or ""
                zh_hant = names.get("zh-Hant") or ""
                if zh_hans:
                    name = zh_hans
                elif zh_hant:
                    name = zh_hant
                else:
                    name = item.get("display_name") or names.get("en-US") or ""
            else:
                en = names.get("en-US") or ""
                zh = names.get("zh-Hans") or ""
                if en and zh:
                    name = f"{en} / {zh}"
                else:
                    name = en or item.get("display_name") or zh
            # Gear: gear_type (new) or gear (legacy).
            gear_val = item.get("gear_type") or item.get("gear") or ""
            stat = "✗" if item.get("deleted") is True or item.get("is_deleted_server") is True else "✓"
            rows.append((
                item.get("id", ""),
                name,
                item.get("level") if item.get("level") is not None else "",
                label_for(item.get("type", ""), mode),
                label_for(gear_val, mode),
                label_for(item.get("grade", ""), mode),
                stat,
            ))

        if self._sort_col:
            idx = COLUMN_KEYS.index(self._sort_col)
            key_fn = self._sort_key_fn(self._sort_col)
            rows.sort(key=lambda r: key_fn(r[idx]), reverse=self._sort_desc)

        for row in rows:
            tag = "deleted" if row[6] == "✗" else "ok"
            self.tree.insert("", tk.END, values=row, tags=(tag,))

    @staticmethod
    def _sort_key_fn(col: str):
        if col in ("id", "level"):
            return lambda v: int(v) if str(v).lstrip("-").isdigit() else -1
        if col == "name":
            def key(v: str) -> str:
                en, _, _ = str(v).partition(" / ")
                return en.lower()
            return key
        if col == "stat":
            return lambda v: 0 if v == "✓" else 1
        return lambda v: str(v).lower()

    def _on_header_click(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = False
        for key, label, _w, _a in COLUMNS:
            if key == col:
                arrow = " ▼" if self._sort_desc else " ▲"
                self.tree.heading(key, text=label + arrow)
            else:
                self.tree.heading(key, text=label)
        self._refresh()

    def _copy_selected(self) -> None:
        sel = self._last_selection
        n_sel = len(sel)
        print(f"[COPY DEBUG] _last_selection count={n_sel}, _copy_columns={self._copy_columns}")
        if not sel:
            self.var_status.set("Nothing to copy (no selection)")
            return
        self._do_copy(sel)

    def _copy_all_visible(self) -> None:
        children = self.tree.get_children()
        if not children:
            self.var_status.set("Nothing to copy (no visible rows)")
            return
        self._do_copy(tuple(children))

    def _do_copy(self, iids: tuple[str, ...]) -> None:
        col_idx = [COLUMN_KEYS.index(k) for k in self._copy_columns if k in COLUMN_KEYS]
        if not col_idx:
            self.var_status.set("Nothing to copy (no columns selected)")
            return
        lines: list[str] = []
        for iid in iids:
            values = self.tree.item(iid, "values")
            if not values or values[0] == "[No results]":
                continue
            row = [str(values[i]) for i in col_idx]
            lines.append("\t".join(row))
        if not lines:
            self.var_status.set("Nothing to copy")
            return
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.var_status.set(
            f"Copied {len(lines)} row(s) ({len(self._copy_columns)} col(s))"
        )
        print(f"[COPY DEBUG] wrote {len(lines)} lines, {len(col_idx)} cols to clipboard")

    def _open_copy_columns_dialog(self) -> tk.Toplevel:
        dlg = tk.Toplevel(self)
        dlg.title("Copy columns")
        dlg.transient(self)
        dlg.resizable(False, False)

        ttk.Label(
            dlg, text="Include these columns when copying:",
            padding=(12, 12, 12, 4),
        ).pack(anchor="w")

        # `selected` is the source of truth — it stays in sync with each BooleanVar
        # via trace_add callbacks. OK reads from this list directly.
        selected: list[str] = [k for k in self._copy_columns]
        vars_: dict[str, tk.BooleanVar] = {}

        def make_var(key: str, initial: bool) -> tk.BooleanVar:
            v = tk.BooleanVar(value=initial)
            def on_change(*_a: object) -> None:
                if v.get():
                    if key not in selected:
                        # insert in canonical COLUMNS order
                        order = [k for k, _, _, _ in COLUMNS]
                        idx = order.index(key)
                        # find insertion point in selected that preserves order
                        for i, s in enumerate(selected):
                            if order.index(s) > idx:
                                selected.insert(i, key)
                                return
                        selected.append(key)
                else:
                    if key in selected:
                        selected.remove(key)
            v.trace_add("write", on_change)
            return v

        for key, label, _w, _a in COLUMNS:
            v = make_var(key, key in self._copy_columns)
            vars_[key] = v
            ttk.Checkbutton(dlg, text=label, variable=v).pack(
                anchor="w", padx=20, pady=2
            )

        btn_frame = ttk.Frame(dlg, padding=(12, 8, 12, 12))
        btn_frame.pack(fill=tk.X)

        def on_ok() -> None:
            self._copy_columns = list(selected)
            dlg.destroy()

        def on_cancel() -> None:
            dlg.destroy()

        select_all_btn = ttk.Button(btn_frame, width=10)

        def refresh_toggle_label() -> None:
            all_on = all(v.get() for v in vars_.values())
            select_all_btn.configure(
                text="Deselect all" if all_on else "Select all"
            )

        def on_toggle_all() -> None:
            all_on = all(v.get() for v in vars_.values())
            target = not all_on
            for v in vars_.values():
                v.set(target)
            refresh_toggle_label()

        select_all_btn.configure(command=on_toggle_all, text="Select all")
        select_all_btn.pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="OK", command=on_ok, width=8).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel,
                   width=8).pack(side=tk.RIGHT, padx=(0, 4))

        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: on_cancel())
        dlg.grab_set()
        dlg.focus_set()
        refresh_toggle_label()
        return dlg

    def _on_select_all(self, _event: tk.Event) -> str:
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children)
            self._last_selection = tuple(children)
            self.var_selection.set(f"Selected: {len(children)}")
        return "break"

    def _on_tree_select(self, _event: tk.Event) -> None:
        sel = self.tree.selection()
        self._last_selection = tuple(sel)
        self.var_selection.set(f"Selected: {len(sel)}")
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if not values or values[0] == "[No results]":
            self._set_detail("")
            return
        try:
            item_id = int(values[0])
        except (TypeError, ValueError):
            self._set_detail("")
            return
        item = next((it for it in self.catalog.items if it.get("id") == item_id), None)
        if item is None:
            self._set_detail("")
            return
        self._set_detail(self._format_detail(item))

    @staticmethod
    def _format_detail(item: dict) -> str:
        lines: list[str] = []
        lines.append(f"ID:         {item.get('id', '')}")
        # Display name: en-US, zh-Hans, or fallback
        names = item.get("name") or {}
        if not isinstance(names, dict):
            names = {}
        display = (
            names.get("en-US")
            or item.get("display_name")
            or names.get("zh-Hans")
            or next(iter(names.values()), "")
        )
        lines.append(f"Name:       {display}")
        lines.append(f"NameKey:    {item.get('name_key', '')}")
        lines.append(f"Slug:       {item.get('slug', '')}")
        lines.append(f"Type:       {item.get('type', '')}")
        lines.append(f"GearType:   {item.get('gear_type') or '-'}")
        if item.get("parts"):
            lines.append(f"Parts:      {item.get('parts')}")
        if item.get("gear_group"):
            lines.append(f"GearGroup:  {item.get('gear_group')}")
        lines.append(f"Grade:      {item.get('grade', '')}")
        lines.append(f"Level:      {item.get('level') if item.get('level') is not None else '-'}")
        lines.append(f"Marketable: {'yes' if item.get('marketable') or item.get('is_can_exchange_marketable') else 'no'}")
        deleted = item.get('deleted') or item.get('is_deleted_server')
        lines.append(f"Deleted:    {'yes' if deleted else 'no'}")
        if item.get("affix"):
            lines.append(f"Affix:      {item.get('affix')}")
        lines.append(f"Icon:       {item.get('icon', '')}")
        if item.get("icon_full"):
            lines.append(f"IconPath:   {item.get('icon_full')}")
        if names:
            lines.append("")
            lines.append("Names:")
            for locale in sorted(names.keys()):
                lines.append(f"  {locale:<10} {names[locale]}")
        return "\n".join(lines)

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", tk.END)
        if text:
            self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")


def main() -> None:
    catalog = ItemCatalog(CATALOG_PATH)
    app = App(catalog)
    app.mainloop()


if __name__ == "__main__":
    main()
