/*
 * Makes redroid's software gralloc (gralloc.redroid.so) usable for camera frames.
 *
 * That module only implements plain lock(), so nothing can address the planes
 * of a YV12 buffer through the mapper HAL: the camera HAL cannot write preview
 * frames and the app's renderer cannot read them. It also refuses to allocate
 * YCbCr_420_888, the format apps request to process camera frames themselves.
 *
 * This library is added to gralloc.redroid.so as a dependency (patchelf
 * --add-needed). When gralloc loads, the constructor installs a lock_ycbcr
 * that locks the buffer normally and describes Android's standard YV12 layout
 * over it, and makes YCbCr_420_888 allocate as YV12, which that layout fits.
 *
 * Built freestanding for x86 and x86_64 with the host gcc; see build_shim.sh.
 */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned long uptr;

struct android_ycbcr {
    void *y, *cb, *cr;
    uptr ystride, cstride, chroma_step;
    u32 reserved[8];
};

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

struct alloc_device {
    struct hw_device common;
    int (*alloc)(struct alloc_device *, int, int, int, int, const void **, int *);
};

struct hw_module_methods {
    int (*open)(const struct hw_module *, const char *, struct hw_device **);
};

#define HAL_PIXEL_FORMAT_YCBCR_420_888 0x23
#define HAL_PIXEL_FORMAT_YV12 0x32315659

struct gralloc_module {
    struct hw_module common;
    void *register_buffer, *unregister_buffer;
    int (*lock)(struct gralloc_module *, const void *, int, int, int, int, int, void **);
    void *unlock, *perform;
    int (*lock_ycbcr)(struct gralloc_module *, const void *, int, int, int, int, int,
                      struct android_ycbcr *);
};

/* libhardware/modules/gralloc private_handle_t, as used by gralloc.redroid. */
struct private_handle {
    int version, num_fds, num_ints;
    int fd, magic, flags, size, offset;
    unsigned long long base __attribute__((aligned(8)));
    int pid;
};
#define PRIVATE_HANDLE_MAGIC 0x3141592

void *dlopen(const char *, int);
void *dlsym(void *, const char *);

/*
 * A gralloc0 handle carries no dimensions, and callers may lock with a crop
 * or an empty rectangle, so alloc() records the size in the buffer's unused
 * tail (YV12 gets 2 bytes per pixel here but needs 1.5).
 */
struct trailer {
    u32 magic, width, height, reserved;
};
#define TRAILER_MAGIC 0x594d4346

static struct trailer *trailer_of(const struct private_handle *priv, void *base)
{
    if (!base || (u32)priv->size < 4096)
        return 0;
    return (struct trailer *)((char *)base + (u32)priv->size - sizeof(struct trailer));
}

static int lock_ycbcr(struct gralloc_module *module, const void *handle, int usage,
                      int l, int t, int w, int h, struct android_ycbcr *out)
{
    const struct private_handle *priv = handle;
    void *base = 0;
    if (!priv || priv->magic != PRIVATE_HANDLE_MAGIC || !out)
        return -22;
    int result = module->lock(module, handle, usage, l, t, w, h, &base);
    if (result)
        return result;
    struct trailer *tail = trailer_of(priv, base);
    if (tail && tail->magic == TRAILER_MAGIC) {
        w = (int)tail->width;
        h = (int)tail->height;
    }
    if (!base || w <= 0 || h <= 0)
        return -22;
    u32 ystride = ((u32)w + 15) & ~15u;
    u32 cstride = (ystride / 2 + 15) & ~15u;
    /* Refuse buffers too small to be YV12 of this size (e.g. RGB565). */
    if ((u32)priv->size < ystride * (u32)h + 2 * cstride * ((u32)h / 2))
        return -22;
    out->y = base;
    out->cr = (char *)base + ystride * (u32)h;
    out->cb = (char *)out->cr + cstride * ((u32)h / 2);
    out->ystride = ystride;
    out->cstride = cstride;
    out->chroma_step = 1;
    return 0;
}

static int (*real_alloc)(struct alloc_device *, int, int, int, int, const void **, int *);
static const struct hw_module_methods *real_methods;

static int alloc(struct alloc_device *dev, int w, int h, int format, int usage,
                 const void **handle, int *stride)
{
    if (format == HAL_PIXEL_FORMAT_YCBCR_420_888)
        format = HAL_PIXEL_FORMAT_YV12;
    int result = real_alloc(dev, w, h, format, usage, handle, stride);
    if (!result && format == HAL_PIXEL_FORMAT_YV12 && handle && *handle) {
        const struct private_handle *priv = *handle;
        if (priv->magic == PRIVATE_HANDLE_MAGIC) {
            struct trailer *tail = trailer_of(priv, (void *)(uptr)priv->base);
            if (tail) {
                tail->magic = TRAILER_MAGIC;
                tail->width = (u32)w;
                tail->height = (u32)h;
                tail->reserved = 0;
            }
        }
    }
    return result;
}

static int is_gpu0(const char *name)
{
    const char *want = "gpu0";
    while (*want && *name == *want) { name++; want++; }
    return !*want && !*name;
}

static int open_device(const struct hw_module *module, const char *name,
                       struct hw_device **device)
{
    int result = real_methods->open(module, name, device);
    if (!result && name && is_gpu0(name) && device && *device) {
        struct alloc_device *gpu = (struct alloc_device *)*device;
        if (gpu->alloc && gpu->alloc != alloc) {
            real_alloc = gpu->alloc;
            gpu->alloc = alloc;
        }
    }
    return result;
}

static const struct hw_module_methods shim_methods = { open_device };

__attribute__((constructor)) static void install(void)
{
    void *lib = dlopen("gralloc.redroid.so", 2 /* RTLD_NOW */);
    if (!lib)
        return;
    struct gralloc_module *module = dlsym(lib, "HMI");
    if (!module || !module->lock || module->lock_ycbcr)
        return;
    module->lock_ycbcr = lock_ycbcr;
    if (module->common.methods && module->common.methods != &shim_methods) {
        real_methods = module->common.methods;
        module->common.methods = &shim_methods;
    }
}
