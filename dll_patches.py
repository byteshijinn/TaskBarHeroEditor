#!/usr/bin/env python3
"""dll_patches.py — generic byte-signature patcher for GameAssembly.dll.

Scans the DLL for each patch's signature (with `??` wildcards), verifies
uniqueness, writes the replacement. Survives game updates because sigs
match the compiler's prologue output, which only changes when the
function shape itself changes.

Usage:
  python dll_patches.py                  # dry-run, all patches
  python dll_patches.py --apply          # write all
  python dll_patches.py --only haz_always_true
  python dll_patches.py --rollback       # restore from .bak
  python dll_patches.py --list
"""
import argparse, hashlib, os, shutil, sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

DLL_PATH = r'C:\mntd\App\Steam\steamapps\common\TaskbarHero\GameAssembly.dll'

PATCHES = [
    {
        # DLCManager.<isOwned>(uint dlcAppId) — instance method.
        # 5-byte prologue alone is non-unique; 10 bytes disambiguates.
        'name': 'haz_always_true',
        'description': 'DLCManager.<isOwned>(uint) -> always true',
        'sig': '40 53 48 83 EC 20 80 79 38 00 8B DA',
        'patch': 'B8 01 00 00 00 C3',  # mov eax, 1; ret
        'pad': 0xC3,  # fill leftover sig bytes with ret
    },
    # Add more patches by appending dicts. See README for format.
]


@dataclass
class Sig:
    raw: bytes   # bytes to match
    mask: bytes  # 0xFF = exact, 0x00 = wildcard

    @classmethod
    def from_hex(cls, s: str) -> 'Sig':
        raw, mask = bytearray(), bytearray()
        for t in s.split():
            if t in ('??', '?'):
                raw.append(0); mask.append(0x00)
            else:
                raw.append(int(t, 16)); mask.append(0xFF)
        return cls(bytes(raw), bytes(mask))

    def __len__(self): return len(self.raw)


def parse_patches(raw):
    return [
        dict(name=p['name'], description=p.get('description', ''),
             sig=Sig.from_hex(p['sig']),
             patch=bytes.fromhex(p['patch'].replace(' ', '')),
             pad=int(p.get('pad', 0x90)))
        for p in raw
    ]


def find_sig(data: bytes, sig: Sig, start=0, end=None) -> int:
    """First offset where sig matches, or -1."""
    end = end if end is not None else len(data)
    n = len(sig)
    for i in range(start, end - n + 1):
        for j in range(n):
            if (data[i+j] & sig.mask[j]) != (sig.raw[j] & sig.mask[j]):
                break
        else:
            return i
    return -1


def find_sig_all(data: bytes, sig: Sig, limit=10) -> List[int]:
    out, i = [], 0
    while i < len(data) and len(out) < limit:
        off = find_sig(data, sig, i)
        if off < 0: break
        out.append(off)
        i = off + 1
    return out


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def find_backup(dll_path: str) -> str:
    folder, base = os.path.dirname(dll_path), os.path.basename(dll_path)
    cands = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.startswith(base + '.pre_') and f.endswith('.bak')]
    return max(cands, key=os.path.getmtime) if cands else ''


def rollback(dll_path: str) -> int:
    bak = find_backup(dll_path)
    if not bak:
        print('No .bak found.', file=sys.stderr); return 1
    shutil.copy2(bak, dll_path)
    print(f'Restored from {bak}\nNew SHA: {sha256_of(dll_path)}')
    return 0


def verify(data: bytes, p) -> Tuple[bool, str, int]:
    matches = find_sig_all(bytes(data), p['sig'])
    if not matches:
        return False, 'signature not found', -1
    if len(matches) > 1:
        return False, f'non-unique ({len(matches)} matches at {[hex(m) for m in matches[:3]]}...)', -1
    if len(p['patch']) > len(p['sig']):
        return False, f'patch {len(p["patch"])}b > sig {len(p["sig"])}b', -1
    off = matches[0]
    for j in range(len(p['sig'])):
        if p['sig'].mask[j] == 0xFF and data[off+j] != p['sig'].raw[j]:
            return False, f'mismatch @0x{off:X} byte {j}', -1
    return True, 'ok', off


def apply(data: bytearray, p, off: int) -> bytes:
    n = len(p['sig'])
    out = bytearray(n)
    out[:len(p['patch'])] = p['patch']
    out[len(p['patch']):] = bytes([p['pad']]) * (n - len(p['patch']))
    data[off:off+n] = out
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    ap.add_argument('--dll', default=DLL_PATH)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    ap.add_argument('--only', default='')
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args()

    patches = parse_patches(PATCHES)
    if args.list:
        for p in patches:
            print(f'  - {p["name"]}: {p["description"]}')
            print(f'    sig={p["sig"].raw.hex(" ").upper()}  '
                  f'patch={p["patch"].hex(" ").upper()}  pad=0x{p["pad"]:02X}')
        return 0
    if args.only:
        patches = [p for p in patches if p['name'] == args.only]
        if not patches:
            print(f'ERROR: no patch {args.only!r}', file=sys.stderr); return 1

    if not os.path.isfile(args.dll):
        print(f'ERROR: {args.dll} not found', file=sys.stderr); return 1
    if args.rollback:
        return rollback(args.dll)

    cur_sha = sha256_of(args.dll)
    print(f'== dll_patches ==\n  DLL: {args.dll}\n  SHA: {cur_sha}\n  patches: {[p["name"] for p in patches]}\n')

    data = bytearray(open(args.dll, 'rb').read())
    results, any_change = [], False

    for p in patches:
        ok, msg, off = verify(data, p)
        print(f'[{p["name"]}]')
        if not ok:
            print(f'  [skip] {msg}'); results.append((p['name'], 'skip', msg))
        else:
            actual = bytes(data[off:off+len(p['sig'])])
            print(f'  [match] 0x{off:X} sig={actual.hex().upper()}')
            if args.apply:
                written = apply(data, p, off)
                if bytes(data[off:off+len(written)]) != written:
                    print('  [ERROR] verify failed after write', file=sys.stderr)
                    results.append((p['name'], 'error', 'verify'))
                else:
                    print(f'  [APPLY] wrote {written.hex().upper()}')
                    results.append((p['name'], 'applied', f'@0x{off:X}'))
                    any_change = True
            else:
                print(f'  [DRY] would write {p["patch"].hex().upper()}')
                results.append((p['name'], 'dry', f'@0x{off:X}'))
        print()

    print('== summary ==')
    for n, s, d in results:
        print(f'  {n:25s} {s:8s} {d}')

    if not any_change:
        if not args.apply: print('\nRe-run with --apply to write.')
        else: print('\nNo patches written.')
        return 0

    if not args.apply: return 0

    bak = f'{args.dll}.pre_{cur_sha[:12]}.bak'
    if not os.path.isfile(bak):
        shutil.copy2(args.dll, bak); print(f'\nBackup: {bak}')
    else:
        print(f'\nBackup exists: {bak}')

    open(args.dll, 'wb').write(data)
    print(f'New SHA: {sha256_of(args.dll)}')
    print('\nNext: run unlock_dlc_heroes.py -w and unlock_inventory_slots.py -w')
    return 0


if __name__ == '__main__':
    sys.exit(main())