// SPDX-License-Identifier: GPL-3.0-or-later
// Loads the real HAL binary in this process. Does not install or restart audio.
#include <CoreAudio/AudioServerPlugIn.h>
#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); exit(1); } } while (0)
static OSStatus storage(AudioServerPlugInHostRef h, CFStringRef k, CFPropertyListRef *v) {
    (void)h; (void)k; *v = NULL; return noErr;
}
static void get(AudioServerPlugInDriverRef d, AudioObjectID id,
                AudioObjectPropertySelector selector, UInt32 size, void *out) {
    AudioObjectPropertyAddress a = {selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    UInt32 actual = 0;
    CHECK((*d)->GetPropertyData(d, id, 0, &a, 0, NULL, size, &actual, out) == noErr);
    CHECK(actual == size);
}
int main(int argc, char **argv) {
    CHECK(argc == 2);
    void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!lib) { fprintf(stderr, "%s\n", dlerror()); return 1; }
    void *(*create)(CFAllocatorRef, CFUUIDRef) = dlsym(lib, "RecordingBeanAudio_Create");
    CHECK(create);
    AudioServerPlugInDriverRef d = create(NULL, kAudioServerPlugInTypeUUID);
    CHECK(d && *d);
    AudioServerPlugInHostInterface host = {0}; host.CopyFromStorage = storage;
    CHECK((*d)->Initialize(d, &host) == noErr);
    UInt32 type = 0;
    get(d, 3, kAudioDevicePropertyTransportType, sizeof(type), &type);
    printf("device transport = 0x%08x\n", type);
    CHECK(type == kAudioDeviceTransportTypeUnknown);
    get(d, 2, kAudioBoxPropertyTransportType, sizeof(type), &type);
    CHECK(type == kAudioDeviceTransportTypeUnknown);
    CFStringRef name = NULL, uid = NULL;
    get(d, 3, kAudioObjectPropertyName, sizeof(name), &name);
    get(d, 3, kAudioDevicePropertyDeviceUID, sizeof(uid), &uid);
    CHECK(CFEqual(name, CFSTR("Recording Bean Audio 2ch")));
    CHECK(CFEqual(uid, CFSTR("RecordingBeanAudio2ch_UID")));
    CFRelease(name); CFRelease(uid);
    CFStringRef manufacturer = NULL, model = NULL;
    get(d, 3, kAudioObjectPropertyManufacturer, sizeof(manufacturer), &manufacturer);
    get(d, 3, kAudioDevicePropertyModelUID, sizeof(model), &model);
    CHECK(CFEqual(manufacturer, CFSTR("open-recording-bean contributors")));
    CHECK(CFEqual(model, CFSTR("RecordingBeanAudio2ch_ModelUID")));
    CFRelease(manufacturer); CFRelease(model);
    AudioObjectPropertyAddress icon = {kAudioDevicePropertyIcon, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    CHECK(!(*d)->HasProperty(d, 3, 0, &icon));
    UInt32 unusedSize = 0;
    CHECK((*d)->GetPropertyDataSize(d, 3, 0, &icon, 0, NULL, &unusedSize) == kAudioHardwareUnknownPropertyError);
    for (AudioObjectID stream = 4; stream <= 7; stream += 3) {
        AudioStreamBasicDescription fmt = {0};
        get(d, stream, kAudioStreamPropertyVirtualFormat, sizeof(fmt), &fmt);
        CHECK(fmt.mChannelsPerFrame == 2 && fmt.mSampleRate == 48000);
        CHECK(fmt.mBitsPerChannel == 32 && (fmt.mFormatFlags & kAudioFormatFlagIsFloat));
    }
    CHECK((*d)->StartIO(d, 3, 1) == noErr);
    float sent[8192], received[8192];
    const UInt32 sizes[] = {64, 128, 256, 480, 512, 1024, 4096};
    UInt64 frames = 0;
    for (int block = 0; block < 1400; ++block) {
        UInt32 n = sizes[block % 7];
        for (UInt32 i = 0; i < n * 2; ++i)
            sent[i] = (float)((int)((frames * 2 + i) % 997) - 498) / 1024.0f;
        memset(received, 0, sizeof(received));
        AudioServerPlugInIOCycleInfo cycle = {0};
        cycle.mInputTime.mSampleTime = cycle.mOutputTime.mSampleTime = (Float64)frames;
        cycle.mCurrentTime.mSampleTime = (Float64)frames;
        CHECK((*d)->DoIOOperation(d, 3, 7, 1, kAudioServerPlugInIOOperationWriteMix, n, &cycle, sent, NULL) == noErr);
        CHECK((*d)->DoIOOperation(d, 3, 4, 1, kAudioServerPlugInIOOperationReadInput, n, &cycle, received, NULL) == noErr);
        CHECK(memcmp(sent, received, n * 2 * sizeof(float)) == 0);
        frames += n;
    }
    CHECK((*d)->StopIO(d, 3, 1) == noErr);
    printf("PASS: Unknown box/device transport; independent name and UID; stereo 48 kHz; %llu frames in 1400 blocks, bit-exact including ring wraps.\n", frames);
    // Leave unload to process teardown, like CoreAudio's lifetime ownership.
    return 0;
}
