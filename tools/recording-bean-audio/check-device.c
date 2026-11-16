// SPDX-License-Identifier: GPL-3.0-or-later
// Read-only system enumeration: no microphone stream and no service restart.
#include <CoreAudio/CoreAudio.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc > 2) return 2;
    const char *wanted = argc == 2 ? argv[1] : "RecordingBeanAudio2ch_UID";
    CFStringRef uid = CFStringCreateWithCString(NULL, wanted, kCFStringEncodingUTF8);
    if (!uid) return 2;
    AudioObjectPropertyAddress address = {kAudioHardwarePropertyTranslateUIDToDevice,
        kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    AudioDeviceID device = kAudioObjectUnknown;
    UInt32 size = sizeof(device);
    OSStatus status = AudioObjectGetPropertyData(kAudioObjectSystemObject, &address,
        sizeof(uid), &uid, &size, &device);
    CFRelease(uid);
    if (status != noErr) {
        fprintf(stderr, "Cannot query audio devices (status %d).\n", (int)status);
        return 2;
    }
    if (device == kAudioObjectUnknown) return 1;
    // For our normal activation path, also verify a live, compatible device.
    if (argc == 1) {
        UInt32 value = 0;
        address.mSelector = kAudioDevicePropertyDeviceIsAlive;
        size = sizeof(value);
        status = AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &value);
        if (status != noErr) {
            fprintf(stderr, "Cannot query device availability (status %d).\n", (int)status);
            return 2;
        }
        if (!value) return 1;
        address.mSelector = kAudioDevicePropertyTransportType;
        size = sizeof(value);
        status = AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &value);
        if (status != noErr) {
            fprintf(stderr, "Cannot query device transport (status %d).\n", (int)status);
            return 2;
        }
        if (value != kAudioDeviceTransportTypeUnknown) {
            fprintf(stderr, "Installed device transport is incompatible.\n");
            return 2;
        }
    }
    printf("Ready: %s\n", wanted);
    return 0;
}
