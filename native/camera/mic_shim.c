/*
 * Gives redroid's stub audio HAL (audio.primary.default.so) a real microphone.
 *
 * The stub's input stream only sleeps and returns silence. This library is
 * added to it as a dependency (patchelf --add-needed). When the HAL loads, the
 * constructor wraps open_input_stream so that each new input stream reads
 * 48 kHz stereo 16-bit PCM from the FIFO /dev/farm_mic. That FIFO lives on the
 * host and is bind-mounted into the container; the farm's microphone forwarder
 * (scripts/camera/mic_forwarder.py) fills it from the same source that drives
 * the camera. With no feed, or for a stream in another format, the stub's
 * silence is used unchanged.
 *
 * Built freestanding for x86 and x86_64 with the host gcc; see build_shim.sh.
 */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned long uptr;

struct hw_module_methods;

struct hw_module {
    u32 tag;
    u16 module_api_version, hal_api_version;
    const char *id, *name, *author;
    const struct hw_module_methods *methods;
    void *dso;
    uptr reserved[32 - 7];
};

struct hw_device {
    u32 tag, version;
    struct hw_module *module;
    uptr reserved[12];
    int (*close)(struct hw_device *);
};

struct hw_module_methods {
    int (*open)(const struct hw_module *, const char *, struct hw_device **);
};

/* audio_stream_in: 14 common methods, set_gain, then read. */
struct stream_in {
    u32 (*get_sample_rate)(const struct stream_in *);
    void *set_sample_rate, *get_buffer_size;
    u32 (*get_channels)(const struct stream_in *);
    int (*get_format)(const struct stream_in *);
    void *rest_of_common[9];
    void *set_gain;
    long (*read)(struct stream_in *, void *, unsigned long);
};

typedef int (*open_input_fn)(struct hw_device *, int, u32, void *, struct stream_in **,
                             int, const char *, int);

/* audio_hw_device: hw_device, then methods; open_input_stream is the 14th. */
struct audio_device {
    struct hw_device common;
    void *before[13];
    open_input_fn open_input_stream;
};

struct timespec { long tv_sec, tv_nsec; };
struct pollfd { int fd; short events, revents; };

int open(const char *, int, ...);
long read(int, void *, unsigned long);
int poll(struct pollfd *, unsigned long, int);
int usleep(u32);
int clock_gettime(int, struct timespec *);
void *dlopen(const char *, int);
void *dlsym(void *, const char *);
int __android_log_print(int, const char *, const char *, ...);
#define LOGI(...) __android_log_print(4, "farm_mic", __VA_ARGS__)

#define MIC_PATH "/dev/farm_mic"
#define RATE 48000u
#define STEREO 0xCu       /* AUDIO_CHANNEL_IN_STEREO */
#define PCM_16 1
#define FRAME_BYTES 4u
#define LATE_MS 40u        /* how late sound may be before a gap is filled with silence */
#define O_NONBLOCK 04000

static const struct hw_module_methods *real_methods;
static open_input_fn real_open_input;
static long (*real_read)(struct stream_in *, void *, unsigned long);
static int mic_fd = -1;
static u32 last_read_ms;
static u32 stat_reads, stat_fed, stat_silent;
static int stat_peak;

static u32 now_ms(void)
{
    struct timespec ts = { 0, 0 };
    clock_gettime(1 /* CLOCK_MONOTONIC */, &ts);
    return (u32)ts.tv_sec * 1000u + (u32)ts.tv_nsec / 1000000u;
}

static long mic_read(struct stream_in *stream, void *buffer, unsigned long bytes)
{
    if (stream->get_sample_rate(stream) != RATE || stream->get_channels(stream) != STEREO ||
        stream->get_format(stream) != PCM_16)
        return real_read(stream, buffer, bytes);
    if (mic_fd < 0)
        mic_fd = open(MIC_PATH, O_NONBLOCK /* | O_RDONLY */);
    if (mic_fd < 0)
        return real_read(stream, buffer, bytes);

    char *out = buffer;
    char scratch[1024];
    unsigned long got = 0;
    u32 start = now_ms();
    u32 budget = (u32)(bytes / FRAME_BYTES) * 1000u / RATE;

    /* After a pause the FIFO holds old sound; skip it so the mic stays live. */
    if (start - last_read_ms > 500u)
        while (read(mic_fd, scratch, sizeof(scratch)) > 0)
            ;

    while (got < bytes) {
        long n = read(mic_fd, out + got, bytes - got);
        if (n > 0) {
            got += (unsigned long)n;
            continue;
        }
        u32 waited = now_ms() - start;
        if (waited >= budget + LATE_MS)
            break;
        if (n == 0) {              /* nothing is feeding the FIFO: plain silence */
            usleep((budget - (waited < budget ? waited : budget)) * 1000u);
            break;
        }
        struct pollfd p = { mic_fd, 1 /* POLLIN */, 0 };
        poll(&p, 1, (int)(budget + LATE_MS - waited));
    }
    got -= got % FRAME_BYTES;
    for (unsigned long i = got; i < bytes; i++)
        out[i] = 0;

    /* One log line roughly every minute of recording. */
    const short *samples = buffer;
    for (unsigned long i = 0; i < got / 2; i += 16) {
        int v = samples[i] < 0 ? -samples[i] : samples[i];
        if (v > stat_peak)
            stat_peak = v;
    }
    stat_fed += (u32)got;
    stat_silent += (u32)(bytes - got);
    if (++stat_reads * budget >= 60000u) {
        LOGI("fed %u bytes, silent %u bytes, peak %d", stat_fed, stat_silent, stat_peak);
        stat_reads = stat_fed = stat_silent = 0;
        stat_peak = 0;
    }
    last_read_ms = now_ms();
    return (long)bytes;
}

static int open_input(struct hw_device *dev, int handle, u32 devices, void *config,
                      struct stream_in **stream, int flags, const char *address, int source)
{
    int result = real_open_input(dev, handle, devices, config, stream, flags, address, source);
    if (!result && stream && *stream && (*stream)->read && (*stream)->read != mic_read) {
        real_read = (*stream)->read;
        (*stream)->read = mic_read;
        LOGI("input stream opened: %u Hz, channels 0x%x", (*stream)->get_sample_rate(*stream),
             (*stream)->get_channels(*stream));
    }
    return result;
}

static int open_device(const struct hw_module *module, const char *name,
                       struct hw_device **device)
{
    int result = real_methods->open(module, name, device);
    if (!result && device && *device) {
        struct audio_device *audio = (struct audio_device *)*device;
        if (audio->open_input_stream && audio->open_input_stream != open_input) {
            real_open_input = audio->open_input_stream;
            audio->open_input_stream = open_input;
        }
    }
    return result;
}

static const struct hw_module_methods shim_methods = { open_device };

__attribute__((constructor)) static void install(void)
{
    void *lib = dlopen("audio.primary.default.so", 2 /* RTLD_NOW */);
    if (!lib)
        return;
    struct hw_module *module = dlsym(lib, "HMI");
    if (module && module->methods && module->methods != &shim_methods) {
        real_methods = module->methods;
        module->methods = &shim_methods;
    }
}
