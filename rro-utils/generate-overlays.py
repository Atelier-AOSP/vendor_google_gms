#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: The PixelOS Project
# SPDX-License-Identifier: Apache-2.0
#

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from argparse import ArgumentParser
from pathlib import Path

_LINEAGE_SCRIPTS_DEV = Path(__file__).resolve().parents[4] / 'lineage' / 'scripts' / 'dev'
GENERATE_RRO_PY = _LINEAGE_SCRIPTS_DEV / 'generate_rro.py'
BEAUTIFY_RRO_PY = _LINEAGE_SCRIPTS_DEV / 'beautify_rro.py'

_SCRIPT_DIR = Path(__file__).resolve().parent
EXCLUDE_TAGS_FILE = _SCRIPT_DIR / 'exclude-tag.txt'

_BEAUTIFY_SKIP_OVERLAYS = {
    'IdentityCheckSettingsOverlay',
    'PixelDocumentsUIGoogleOverlay',
    'PixelSetupWizardOverlayExpressive',
    'TelecomOverlayDeprecatedGoogle',
    'WildlifeSysuiVpnOverlay2022',
}

_EXCLUDED_FILENAMES = {
    'ic_launcher_phone.png',
    'ic_qs_branded_vpn.xml',
    'ic_safety_protection.xml',
    'stat_sys_branded_vpn.xml',
    'shortcut_base.png',
    'fingerprint_location_animation.mp4',
    'ic_5g_plus_mobiledata.xml',
    'ic_5g_plus_mobiledata_updated.xml',
}


_FAILURE_MARKERS = ('Unknown package name',)


def _fail(msg: str):
    print(f'\033[0;31merror: {msg}\033[0m', file=sys.stderr)
    sys.exit(1)


def _run(script: Path, *args: str):
    if not script.is_file():
        _fail(f'{script} not found')
    env = {**os.environ, 'PYTHONPATH': str(_LINEAGE_SCRIPTS_DEV)}
    proc = subprocess.Popen(
        [sys.executable, str(script), *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # The lineage scripts exit 0 when they skip an overlay, so scan their output.
    failures = []
    for line in proc.stdout:
        print(line, end='', flush=True)
        if any(m in line for m in _FAILURE_MARKERS):
            failures.append(line.strip())
    if proc.wait() != 0:
        _fail(f'{script.name} exited with {proc.returncode}')
    if failures:
        _fail(
            f'{script.name} skipped {len(failures)} overlay(s):\n  '
            + '\n  '.join(failures)
        )


def delete_excluded_files(overlay_dir: Path):
    for f in overlay_dir.rglob('*'):
        if f.is_file() and f.name in _EXCLUDED_FILENAMES:
            f.unlink()


def rename_dollar_files(overlay_dir: Path):
    for f in sorted(overlay_dir.rglob('*')):
        if not f.is_file() or '$' not in f.name:
            continue
        new_path = f.parent / f.name.replace('$', '')
        old_stem = f.stem
        new_stem = new_path.stem
        f.rename(new_path)
        for ref_file in overlay_dir.rglob('*'):
            if not ref_file.is_file():
                continue
            try:
                text = ref_file.read_text(encoding='utf-8')
                if old_stem in text:
                    ref_file.write_text(text.replace(old_stem, new_stem), encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue


_TEXT_SUBSTITUTIONS = [
    ('@android:color', '@*android:color'),
    ('@style/Theme.DeviceDefault', '@android:style/Theme.DeviceDefault'),
]


def apply_text_substitutions(overlay_dir: Path):
    for xml_file in overlay_dir.rglob('*.xml'):
        parts = xml_file.relative_to(overlay_dir).parts
        if xml_file.name != 'AndroidManifest.xml':
            if 'res' not in parts:
                continue
            res_sub = parts[list(parts).index('res') + 1] if list(parts).index('res') + 1 < len(parts) else ''
            if res_sub.startswith(('raw', 'drawable', 'xml')):
                continue
        text = xml_file.read_text(encoding='utf-8')
        new_text = text
        for old, new in _TEXT_SUBSTITUTIONS:
            new_text = new_text.replace(old, new)
        if new_text != text:
            xml_file.write_text(new_text, encoding='utf-8')


def apply_exclude_tags(overlay_dir: Path):
    if not EXCLUDE_TAGS_FILE.is_file():
        return

    entries = []
    for line in EXCLUDE_TAGS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or ':' not in line:
            continue
        tag_type, name = line.split(':', 1)
        entries.append((tag_type, name))

    if not entries:
        return

    for xml_file in overlay_dir.rglob('*.xml'):
        parts = xml_file.relative_to(overlay_dir).parts
        if 'res' not in parts:
            continue
        res_sub = parts[list(parts).index('res') + 1] if list(parts).index('res') + 1 < len(parts) else ''
        if res_sub.startswith(('raw', 'drawable', 'xml')):
            continue

        try:
            tree = ET.parse(xml_file)
        except ET.ParseError:
            continue

        root = tree.getroot()
        removed = False
        for tag_type, name in entries:
            for elem in root.findall(f"{tag_type}[@name='{name}']"):
                root.remove(elem)
                removed = True

        if removed:
            ET.indent(tree, space='    ')
            tree.write(str(xml_file), encoding='unicode')


def merge_split_drawables(overlay_dir: Path):
    android_ns = 'http://schemas.android.com/apk/res/android'
    aapt_ns = 'http://schemas.android.com/aapt'
    ET.register_namespace('android', android_ns)
    ET.register_namespace('aapt', aapt_ns)
    prefixes = {android_ns: 'android', aapt_ns: 'aapt'}

    def prefixed_name(qname):
        if qname.startswith('{'):
            uri, local = qname[1:].split('}', 1)
            return f'{prefixes.get(uri, uri)}:{local}'
        return qname

    def to_aapt2_style(root):
        def move_xmlns(m):
            ns = re.findall(r'xmlns:\w+="[^"]*"', m[2])
            if not ns:
                return m[0]
            ns.sort(key=lambda s: 'xmlns:android' not in s)  # android before aapt
            attrs = re.sub(r'\s*xmlns:\w+="[^"]*"', '', m[2]).strip()
            return f'{m[1]} {attrs}\n  {" ".join(ns)}>'

        body = re.sub(
            r'(<[\w-]+)([^>]*)>', move_xmlns, ET.tostring(root, encoding='unicode'), count=1
        )
        return f'<?xml version="1.0" encoding="utf-8"?>\n{body}\n'

    def fold_references(tree, stem, inner):
        matches = [
            (el, attr)
            for el in tree.iter()
            for attr, val in el.attrib.items()
            if val.startswith('@') and val.partition('/')[2] == stem
        ]
        for el, attr in matches:
            idx = list(el.attrib).index(attr)  # where the attribute sat
            aapt_attr = ET.Element(f'{{{aapt_ns}}}attr')
            aapt_attr.set('name', prefixed_name(attr))
            aapt_attr.append(copy.deepcopy(inner))
            el.insert(idx, aapt_attr)
            del el.attrib[attr]
        return bool(matches)

    def fold_child(child):
        try:
            inner = ET.parse(child).getroot()
        except ET.ParseError:
            return False
        folded = False
        overlay = overlay_dir / child.relative_to(overlay_dir).parts[0]
        for xml_file in overlay.rglob('*.xml'):
            if xml_file == child:
                continue
            try:
                text = xml_file.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            if child.stem not in text:  # cheap filter + skips non-XML resources
                continue
            try:
                tree = ET.parse(xml_file)
            except ET.ParseError:
                continue
            if fold_references(tree, child.stem, inner):
                ET.indent(tree, space='    ')
                xml_file.write_text(to_aapt2_style(tree.getroot()), encoding='utf-8')
                folded = True
        return folded

    for child in list(overlay_dir.rglob('$*__*.xml')):
        if fold_child(child):
            child.unlink()


def rename_mapped_resources(overlay_dir: Path):
    # beautify_rro drops overlay values it can't find in the target, which
    # includes renamed values a resourcesMap points at. Give them the target
    # names and keep the map, so unmapped values stay inert as on stock.
    android_ns = 'http://schemas.android.com/apk/res/android'
    for manifest in overlay_dir.glob('*/AndroidManifest.xml'):
        overlay = ET.parse(manifest).getroot().find('overlay')
        if overlay is None:
            continue
        resources_map = overlay.get(f'{{{android_ns}}}resourcesMap', '')
        if not resources_map.startswith('@xml/'):
            continue
        res_dir = manifest.parent / 'res'
        map_file = res_dir / 'xml' / f'{resources_map[5:]}.xml'
        renames = {}
        for item in ET.parse(map_file).getroot().iter('item'):
            target_type, _, target = item.get('target', '').partition('/')
            value = item.get('value', '')
            if value.startswith('@') and value[1:].partition('/')[0] == target_type:
                old = value[1:].partition('/')[2]
                if old != target:
                    renames[(target_type, old)] = target
        if not renames:
            continue
        for xml_file in [map_file, *res_dir.glob('values*/*.xml')]:
            text = xml_file.read_text(encoding='utf-8')
            new_text = text
            for (res_type, old), new in renames.items():
                new_text = new_text.replace(
                    f'<{res_type} name="{old}"', f'<{res_type} name="{new}"'
                ).replace(f'"@{res_type}/{old}"', f'"@{res_type}/{new}"')
            if new_text != text:
                xml_file.write_text(new_text, encoding='utf-8')


def inject_bp_target_packages(overlay_dir: Path):
    android_ns = 'http://schemas.android.com/apk/res/android'
    for meta in overlay_dir.glob('*/.overlay-meta.json'):
        bp = meta.parent / 'Android.bp'
        manifest = meta.parent / 'AndroidManifest.xml'
        if not bp.is_file() or not manifest.is_file():
            continue
        target = json.loads(meta.read_text())['original_target_package']
        overlay = ET.parse(manifest).getroot().find('overlay')
        if overlay is None or overlay.get(f'{{{android_ns}}}targetPackage') == target:
            continue
        body = bp.read_text().rstrip()
        if not body.endswith('}'):
            continue
        bp.write_text(f'{body[:-1].rstrip()}\n    target_package_name: "{target}",\n}}\n')


def remove_meta_files(overlay_dir: Path):
    for f in overlay_dir.rglob('.overlay-meta.json'):
        f.unlink()


def write_overlays_mk(overlay_dir: Path):
    common_dir = overlay_dir.parents[2]

    vendor_mk = common_dir / 'common-vendor.mk'
    if vendor_mk.is_file():
        lines = vendor_mk.read_text().splitlines(keepends=True)
        lines = [l for l in lines if not ('product/overlay/' in l and '.apk' in l)]
        vendor_mk.write_text(''.join(lines))

    overlays_mk = common_dir / 'overlays.mk'
    packages = sorted(
        d.name for d in overlay_dir.iterdir()
        if d.is_dir() and (d / 'Android.bp').is_file()
    )
    if not packages:
        return
    lines = ['PRODUCT_PACKAGES += \\']
    for i, pkg in enumerate(packages):
        sep = ' \\' if i < len(packages) - 1 else ''
        lines.append(f'    {pkg}{sep}')
    overlays_mk.write_text('\n'.join(lines) + '\n')


def main():
    parser = ArgumentParser(
        prog='generate-overlays',
        description='Generate GMS RRO overlays from APKs in the overlay directory',
    )
    parser.add_argument('overlay_dir', type=Path, help='Directory containing overlay APKs')
    parser.add_argument(
        '--framework-res',
        type=Path,
        required=True,
        help='Path to framework-res.apk',
    )
    args = parser.parse_args()

    overlay_dir: Path = args.overlay_dir
    framework_path: Path = args.framework_res

    if not framework_path.is_file():
        print(f'error: framework-res.apk not found at {framework_path}', file=sys.stderr)
        sys.exit(1)

    if not any(overlay_dir.glob('*.apk')):
        return

    with tempfile.TemporaryDirectory() as tmp:
        apk_backup = Path(tmp)
        try:
            generate_rros(overlay_dir, framework_path, apk_backup)
        except BaseException:
            for apk in apk_backup.glob('*.apk'):
                shutil.move(apk, overlay_dir)
            print(f'Restored overlay APKs in {overlay_dir}', file=sys.stderr)
            raise


def generate_rros(overlay_dir: Path, framework_path: Path, apk_backup: Path):
    _run(
        GENERATE_RRO_PY,
        str(overlay_dir),
        '--overlays', str(overlay_dir),
        '--framework', str(framework_path),
    )
    for apk in overlay_dir.glob('*.apk'):
        shutil.move(apk, apk_backup)

    merge_split_drawables(overlay_dir)
    delete_excluded_files(overlay_dir)
    rename_dollar_files(overlay_dir)
    apply_exclude_tags(overlay_dir)
    rename_mapped_resources(overlay_dir)
    for overlay_subdir in sorted(overlay_dir.iterdir()):
        if not overlay_subdir.is_dir():
            continue
        if overlay_subdir.name in _BEAUTIFY_SKIP_OVERLAYS:
            continue
        if not (overlay_subdir / 'Android.bp').is_file():
            continue
        _run(BEAUTIFY_RRO_PY, str(overlay_subdir), '--write-meta')
    inject_bp_target_packages(overlay_dir)
    apply_text_substitutions(overlay_dir)
    remove_meta_files(overlay_dir)
    write_overlays_mk(overlay_dir)


if __name__ == '__main__':
    main()
