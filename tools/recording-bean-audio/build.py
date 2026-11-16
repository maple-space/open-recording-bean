#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a complete Recording Bean Audio source/binary release, without installing."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/recording-bean-audio"
TOOLS = ROOT / "tools/recording-bean-audio"
STEM = "RecordingBeanAudio2ch-1.0.1"
BUNDLE_ID = "io.github.maple-space.RecordingBeanAudio2ch"
FACTORY = "005aef35-afbf-5794-9404-d56f0d8bae3c"


def run(*args):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--application-identity", help="Your Developer ID Application identity")
    parser.add_argument("--installer-identity", help="Your Developer ID Installer identity")
    parser.add_argument("--notary-profile", help="Existing notarytool keychain profile; never pass passwords here")
    args = parser.parse_args()
    if bool(args.application_identity) != bool(args.installer_identity):
        parser.error("Supply both application and installer signing identities, or neither")
    if args.notary_profile and not args.application_identity:
        parser.error("Notarization requires both Developer ID identities")
    out = (args.output or ROOT / ".local/releases" /
           (STEM + "-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    # Refuse to overwrite an earlier deliverable or erase intermediates.
    out.mkdir(parents=True, exist_ok=False)
    release_zip = out.with_name(out.name + "-release.zip")
    if release_zip.exists():
        raise SystemExit("Refusing to overwrite an existing release archive")
    work = out / "build"
    stage = work / "root"
    driver = stage / "Library/Audio/Plug-Ins/HAL/RecordingBeanAudio2ch.driver"
    binary = driver / "Contents/MacOS/RecordingBeanAudio"
    resources = driver / "Contents/Resources"
    binary.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    source_text = (SOURCE / "RecordingBeanAudio.c").read_text()
    assert source_text.count("*((UInt32*)outData) = kAudioDeviceTransportTypeUnknown;") == 2
    assert "*((UInt32*)outData) = kAudioDeviceTransportTypeVirtual;" not in source_text

    sdk = run("xcrun", "--show-sdk-path").strip()
    compile_cmd = ["xcrun", "clang", "-O2", "-fblocks", "-bundle", "-arch", "x86_64",
                   "-arch", "arm64", "-mmacosx-version-min=11.0", "-isysroot", sdk,
                   '-DkPlugIn_BundleID="' + BUNDLE_ID + '"', "-DkNumber_Of_Channels=2",
                   SOURCE / "RecordingBeanAudio.c", "-framework", "CoreAudio",
                   "-framework", "CoreFoundation", "-framework", "Accelerate", "-o", binary]
    (work / "compile.log").write_text(run(*compile_cmd))
    info = plistlib.loads((SOURCE / "Info.plist").read_bytes())
    info.update(CFBundleExecutable="RecordingBeanAudio", CFBundleIdentifier=BUNDLE_ID,
                CFBundleName="RecordingBeanAudio2ch", CFBundleShortVersionString="1.0.1",
                CFBundleVersion="2", LSMinimumSystemVersion="11.0",
                RecordingBeanAudioBuild="1.0.1 / Unknown transport compatibility build")
    assert info["CFPlugInFactories"][FACTORY] == "RecordingBeanAudio_Create"
    (driver / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    for name in ["LICENSE", "NOTICE.md", "VERSION"]:
        shutil.copyfile(SOURCE / name, resources / name)
    # All payload files are readable by CoreAudio; executable stays executable.
    for item in stage.rglob("*"):
        item.chmod(0o755 if item.is_dir() or item == binary else 0o644)
    signing = ["codesign", "--force", "--sign", args.application_identity or "-"]
    if args.application_identity:
        signing += ["--options", "runtime", "--timestamp"]
    checker = driver / "Contents/Helpers/check-device"
    checker.parent.mkdir()
    run("xcrun", "clang", "-O2", "-arch", "x86_64", "-arch", "arm64",
        "-mmacosx-version-min=11.0", "-framework", "CoreAudio", "-framework", "CoreFoundation",
        TOOLS / "check-device.c", "-o", checker)
    run(*signing, checker)
    run("codesign", "--verify", "--strict", checker)
    assert set(run("lipo", "-archs", checker).split()) == {"x86_64", "arm64"}

    (work / "codesign.log").write_text(run(*signing, driver))
    verify_sign = run("codesign", "--verify", "--deep", "--strict", "--verbose=2", driver)
    architectures = run("lipo", "-archs", binary).strip()
    assert set(architectures.split()) == {"x86_64", "arm64"}
    probe = work / "probe"
    run("xcrun", "clang", "-O2", "-framework", "CoreAudio", "-framework", "CoreFoundation",
        TOOLS / "probe.c", "-o", probe)
    probe_result = run(probe, binary)
    print(probe_result)
    (work / "probe.log").write_text(probe_result)
    (work / "install-flow-tests.log").write_text(run(sys.executable, TOOLS / "test_install_flow.py"))

    component_info = work / "components.plist"
    component_info.write_bytes(plistlib.dumps([{
        "RootRelativeBundlePath": "Library/Audio/Plug-Ins/HAL/RecordingBeanAudio2ch.driver",
        "BundleIsRelocatable": False, "BundleHasStrictIdentifier": True,
        "BundleIsVersionChecked": True, "BundleOverwriteAction": "upgrade"}]))
    component = work / "driver.pkg"
    run("pkgbuild", "--root", stage, "--install-location", "/", "--ownership", "recommended",
        "--identifier", BUNDLE_ID, "--version", "1.0.1",
        "--component-plist", component_info, component)
    distribution = work / "distribution.xml"
    distribution.write_text('''<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>Recording Bean Audio 2ch 1.0.1</title>
  <welcome file="welcome.html"/>
  <license file="LICENSE"/>
  <conclusion file="conclusion.html"/>
  <domains enable_anywhere="false" enable_currentUserHome="false" enable_localSystem="true"/>
  <options customize="never" require-scripts="false" hostArchitectures="x86_64,arm64"/>
  <volume-check><allowed-os-versions><os-version min="11.0"/></allowed-os-versions></volume-check>
  <choices-outline><line choice="driver"/></choices-outline>
  <choice id="driver" visible="false"><pkg-ref id="io.github.maple-space.RecordingBeanAudio2ch"/></choice>
  <pkg-ref id="io.github.maple-space.RecordingBeanAudio2ch" version="1.0.1">driver.pkg</pkg-ref>
</installer-gui-script>
''')
    installer_resources = work / "installer-resources"
    installer_resources.mkdir()
    shutil.copyfile(SOURCE / "LICENSE", installer_resources / "LICENSE")
    signing_note = ("安装包由发布者的 Developer ID 签名。" if args.installer_identity
                    else "本地构建安装包，未作 Developer ID 签名或 Apple 公证。")
    (installer_resources / "welcome.html").write_text(
        '<html><meta charset="utf-8"><h2>Recording Bean Audio 2ch</h2>'
        '<p>本项目配套虚拟声卡，用于把录音豆的声音送到输入法。可与其他声卡共存。</p>'
        '<p>包含 Intel 与 Apple Silicon 架构。' + signing_note + '</p></html>')
    (installer_resources / "conclusion.html").write_text(
        '<html><meta charset="utf-8"><h2>安装完成后</h2><p>回到网页和输入法的设备列表，选择 Recording Bean Audio 2ch，无需先重启电脑。</p>'
        '<p>若设备没有出现，运行配套发布包中的“安装并启用.command”。它会先检测，必要时在你确认后重新加载系统音频服务，声音会短暂中断。</p></html>')
    package = out / (STEM + "-universal.pkg")
    product = ["productbuild", "--distribution", distribution, "--resources", installer_resources,
               "--package-path", work]
    if args.installer_identity:
        product += ["--sign", args.installer_identity, "--timestamp"]
    run(*product, package)
    if args.notary_profile:
        (work / "notarization.log").write_text(run("xcrun", "notarytool", "submit", package,
            "--keychain-profile", args.notary_profile, "--wait"))
        run("xcrun", "stapler", "staple", package)
        run("xcrun", "stapler", "validate", package)
    expanded = work / "package-expanded"
    run("pkgutil", "--expand-full", package, expanded)
    payload_drivers = list(expanded.rglob("RecordingBeanAudio2ch.driver/Contents/MacOS/RecordingBeanAudio"))
    assert len(payload_drivers) == 1 and digest(payload_drivers[0]) == digest(binary)
    assert not list(expanded.rglob("Scripts")), "No installer scripts are permitted"
    run("codesign", "--verify", "--deep", "--strict", payload_drivers[0].parents[2])

    # Package no upstream logos, artwork or branded installer resources.
    assert not list(driver.rglob("*.icns"))
    assert not list(SOURCE.rglob("*.icns")) and not list(SOURCE.rglob("*.png"))
    assert b"BlackHole" not in binary.read_bytes()
    assert b"audio.existential" not in binary.read_bytes()
    source_archive = out / (STEM + "-source.tar.gz")
    with tarfile.open(source_archive, "w:gz") as archive:
        archive.add(SOURCE, arcname="recording-bean-audio-source/third_party/recording-bean-audio")
        archive.add(TOOLS, arcname="recording-bean-audio-source/tools/recording-bean-audio",
                    filter=lambda item: None if "__pycache__" in Path(item.name).parts else item)
    shutil.copyfile(TOOLS / "README.md", out / "README.md")
    shutil.copyfile(SOURCE / "LICENSE", out / "LICENSE")
    command = out / "安装并启用.command"
    install_text = (TOOLS / "install.command.in").read_text()
    install_text = install_text.replace("@PACKAGE_HASH@", digest(package)).replace("@PACKAGE_NAME@", package.name)
    install_text = install_text.replace("@CHECK_HASH@", digest(checker))
    command.write_text(install_text)
    command.chmod(0o755)
    manifest = {"version": "1.0.1", "device_name": "Recording Bean Audio 2ch",
                "transport": "Unknown", "architectures": architectures.split(),
                "driver_signing": "Developer ID" if args.application_identity else "ad-hoc",
                "installer_signing": "Developer ID" if args.installer_identity else "unsigned",
                "notarized": bool(args.notary_profile),
                "install_path": "/Library/Audio/Plug-Ins/HAL/RecordingBeanAudio2ch.driver",
                "probe_host": os.uname().machine, "probe_result": probe_result.strip(),
                "codesign_verification": verify_sign.strip().replace(str(out), "OUTPUT_DIR").replace(str(ROOT), "SOURCE_ROOT"),
                "system_install_verified": False, "typeless_ui_verified": False,
                "activation_end_to_end_verified": False,
                "activation_policy": "Detect first; reload only coreaudiod after explicit y confirmation; no computer reboot",
                "check_device_sha256": digest(checker),
                "package_sha256": digest(package), "source_sha256": digest(source_archive),
                "source_c_sha256": digest(SOURCE / "RecordingBeanAudio.c"),
                "binary_sha256": digest(binary),
                "build_command": [str(arg).replace(str(out), "OUTPUT_DIR").replace(str(ROOT), "SOURCE_ROOT")
                                  for arg in compile_cmd]}
    (out / "verification.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{digest(p)}  {p.name}\n" for p in sorted(out.iterdir()) if p.is_file()))
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(out.iterdir()):
            if item.is_file():
                archive.write(item, arcname=STEM + "/" + item.name)
    with zipfile.ZipFile(release_zip) as archive:
        assert archive.testzip() is None
    print("\nRelease ready:", out)
    print("Release archive:", release_zip)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", flush=True)
        raise SystemExit(error.returncode)
