/* gralloc.venus.so: a gralloc0 module whose GPU buffers live on the host GPU.
 *
 * redroid ships two allocators: gralloc.redroid (CPU memory, for software
 * rendering) and gralloc.gbm (a DRM render node, Intel/AMD). Neither can give
 * out buffers an NVIDIA host GPU can draw into. This one allocates each
 * buffer as a Vulkan image through the Venus driver (which relays to a
 * virgl_test_server on the host), exports it as a dma-buf and frees its own
 * Vulkan objects again: the dma-buf keeps the memory alive, and every process
 * that receives the handle imports it into its own Venus connection.
 *
 * Images use the LINEAR modifier in host-visible memory, so lock() is a plain
 * mmap of the dma-buf. Formats the GPU never draws (YUV, BLOB) are shared
 * memory.
 */
#define LOG_TAG "gralloc-venus"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <hardware/gralloc.h>
#include <hardware/hardware.h>
#include <log/log.h>
#include <system/graphics.h>
#include <vulkan/vulkan.h>

#include "farm_gralloc_handle.h"

#define ALIGN(v, a) (((v) + (a)-1) & ~((a)-1))
#define FOURCC(a, b, c, d) ((uint32_t)(a) | ((uint32_t)(b) << 8) | ((uint32_t)(c) << 16) | ((uint32_t)(d) << 24))
#define DRM_FORMAT_MOD_LINEAR 0ull

/* formats newer than the stub headers */
#define FMT_RGBA_FP16 0x16
#define FMT_RGBA_1010102 0x2b
#define FMT_YCBCR_420_888 0x23
#define FMT_IMPLEMENTATION_DEFINED 0x22
#define FMT_BLOB 0x21
#define FMT_YV12 0x32315659
#define FMT_NV21 0x11

struct gpu_format {
   int hal;
   VkFormat vk;
   uint32_t fourcc;
   int bpp;
};

static const struct gpu_format gpu_formats[] = {
   { HAL_PIXEL_FORMAT_RGBA_8888, VK_FORMAT_R8G8B8A8_UNORM, FOURCC('A', 'B', '2', '4'), 4 },
   { HAL_PIXEL_FORMAT_RGBX_8888, VK_FORMAT_R8G8B8A8_UNORM, FOURCC('X', 'B', '2', '4'), 4 },
   { HAL_PIXEL_FORMAT_BGRA_8888, VK_FORMAT_B8G8R8A8_UNORM, FOURCC('A', 'R', '2', '4'), 4 },
   { HAL_PIXEL_FORMAT_RGB_565, VK_FORMAT_R5G6B5_UNORM_PACK16, FOURCC('R', 'G', '1', '6'), 2 },
   { FMT_RGBA_FP16, VK_FORMAT_R16G16B16A16_SFLOAT, FOURCC('A', 'B', '4', 'H'), 8 },
   { FMT_RGBA_1010102, VK_FORMAT_A2B10G10R10_UNORM_PACK32, FOURCC('A', 'B', '3', '0'), 4 },
   /* camera preview and video frames that end up on screen: treat as RGBX */
   { FMT_IMPLEMENTATION_DEFINED, VK_FORMAT_R8G8B8A8_UNORM, FOURCC('X', 'B', '2', '4'), 4 },
};

/* ---- Vulkan, only ever used in the allocator process ---- */

static struct {
   pthread_mutex_t lock;
   int state; /* 0 untried, 1 ready, -1 failed */
   VkInstance inst;
   VkPhysicalDevice pd;
   VkDevice dev;
   VkPhysicalDeviceMemoryProperties mp;
   PFN_vkGetMemoryFdKHR get_fd;
   PFN_vkGetImageDrmFormatModifierPropertiesEXT get_modifier;
} vk = { .lock = PTHREAD_MUTEX_INITIALIZER };

static int
vk_init_locked(void)
{
   if (vk.state)
      return vk.state;
   vk.state = -1;

   const VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
                                   .pApplicationName = "gralloc-venus",
                                   .apiVersion = VK_API_VERSION_1_1 };
   const VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   VkResult r = vkCreateInstance(&ici, NULL, &vk.inst);
   if (r != VK_SUCCESS) {
      ALOGE("vkCreateInstance failed: %d", r);
      return -1;
   }
   uint32_t n = 1;
   r = vkEnumeratePhysicalDevices(vk.inst, &n, &vk.pd);
   if ((r != VK_SUCCESS && r != VK_INCOMPLETE) || !n) {
      ALOGE("no Vulkan device: %d", r);
      return -1;
   }
   VkPhysicalDeviceProperties props;
   vkGetPhysicalDeviceProperties(vk.pd, &props);
   vkGetPhysicalDeviceMemoryProperties(vk.pd, &vk.mp);

   static const char *const exts[] = {
      "VK_KHR_external_memory_fd",
      "VK_EXT_external_memory_dma_buf",
      "VK_EXT_image_drm_format_modifier",
      "VK_KHR_image_format_list",
   };
   const float prio = 1.0f;
   const VkDeviceQueueCreateInfo qci = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
                                         .queueCount = 1,
                                         .pQueuePriorities = &prio };
   const VkDeviceCreateInfo dci = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
                                    .queueCreateInfoCount = 1,
                                    .pQueueCreateInfos = &qci,
                                    .enabledExtensionCount = sizeof(exts) / sizeof(exts[0]),
                                    .ppEnabledExtensionNames = exts };
   r = vkCreateDevice(vk.pd, &dci, NULL, &vk.dev);
   if (r != VK_SUCCESS) {
      ALOGE("vkCreateDevice failed: %d", r);
      return -1;
   }
   vk.get_fd = (PFN_vkGetMemoryFdKHR)vkGetDeviceProcAddr(vk.dev, "vkGetMemoryFdKHR");
   vk.get_modifier = (PFN_vkGetImageDrmFormatModifierPropertiesEXT)vkGetDeviceProcAddr(
      vk.dev, "vkGetImageDrmFormatModifierPropertiesEXT");
   if (!vk.get_fd || !vk.get_modifier) {
      ALOGE("external memory entry points missing");
      return -1;
   }
   ALOGI("allocating on %s", props.deviceName);
   vk.state = 1;
   return 1;
}

static uint32_t
vk_mem_type(uint32_t bits, VkMemoryPropertyFlags want)
{
   for (uint32_t i = 0; i < vk.mp.memoryTypeCount; i++)
      if ((bits & (1u << i)) && (vk.mp.memoryTypes[i].propertyFlags & want) == want)
         return i;
   return UINT32_MAX;
}

/* Returns a dma-buf fd, or -1 when the GPU cannot hold this format. */
static int
vk_alloc(VkFormat format, int planes, int w, int h, int render, int *out_pitch, int *out_offset, int *out_size,
         uint64_t *out_modifier)
{
   int fd = -1;
   VkImage img = VK_NULL_HANDLE;
   VkDeviceMemory mem = VK_NULL_HANDLE;

   pthread_mutex_lock(&vk.lock);
   if (vk_init_locked() < 0)
      goto out;

   /* NVIDIA only draws into its block-linear layouts; LINEAR can be sampled
    * and copied but not rendered to. Buffers something draws into therefore
    * take whichever tiled layout the host prefers, the rest stay LINEAR so
    * the CPU can use them in place. */
   const VkImageUsageFlags usage =
      VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT |
      (render ? VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT : 0);
   uint64_t candidates[32];
   uint32_t n_candidates = 0;
   if (render) {
      VkDrmFormatModifierPropertiesEXT props[32];
      VkDrmFormatModifierPropertiesListEXT list = {
         .sType = VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT,
         .drmFormatModifierCount = 32,
         .pDrmFormatModifierProperties = props,
      };
      VkFormatProperties2 fp = { .sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2, .pNext = &list };
      vkGetPhysicalDeviceFormatProperties2(vk.pd, format, &fp);
      for (uint32_t i = 0; i < list.drmFormatModifierCount; i++)
         if (props[i].drmFormatModifier != DRM_FORMAT_MOD_LINEAR && props[i].drmFormatModifierPlaneCount == 1 &&
             (props[i].drmFormatModifierTilingFeatures & VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT))
            candidates[n_candidates++] = props[i].drmFormatModifier;
   }
   if (!n_candidates)
      candidates[n_candidates++] = DRM_FORMAT_MOD_LINEAR;
   const VkImageDrmFormatModifierListCreateInfoEXT mods = {
      .sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT,
      .drmFormatModifierCount = n_candidates,
      .pDrmFormatModifiers = candidates,
   };
   const VkExternalMemoryImageCreateInfo ext = { .sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
                                                 .pNext = &mods,
                                                 .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
   const VkImageCreateInfo ici = {
      .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
      .pNext = &ext,
      .imageType = VK_IMAGE_TYPE_2D,
      .format = format,
      .extent = { (uint32_t)w, (uint32_t)h, 1 },
      .mipLevels = 1,
      .arrayLayers = 1,
      .samples = VK_SAMPLE_COUNT_1_BIT,
      .tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT,
      .usage = usage,
      .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED,
   };
   VkResult r = vkCreateImage(vk.dev, &ici, NULL, &img);
   if (r != VK_SUCCESS) {
      ALOGW("vkCreateImage(vk format %d %dx%d) failed: %d", format, w, h, r);
      goto out;
   }

   VkImageDrmFormatModifierPropertiesEXT chosen = { .sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_PROPERTIES_EXT };
   r = vk.get_modifier(vk.dev, img, &chosen);
   if (r != VK_SUCCESS) {
      ALOGE("no modifier for the new image: %d", r);
      goto out;
   }
   *out_modifier = chosen.drmFormatModifier;

   static const VkImageAspectFlags plane_aspect[3] = { VK_IMAGE_ASPECT_MEMORY_PLANE_0_BIT_EXT,
                                                       VK_IMAGE_ASPECT_MEMORY_PLANE_1_BIT_EXT,
                                                       VK_IMAGE_ASPECT_MEMORY_PLANE_2_BIT_EXT };
   VkSubresourceLayout layout[3];
   for (int i = 0; i < planes; i++)
      vkGetImageSubresourceLayout(vk.dev, img, &(VkImageSubresource){ plane_aspect[i], 0, 0 }, &layout[i]);
   VkMemoryRequirements mr;
   vkGetImageMemoryRequirements(vk.dev, img, &mr);
   uint32_t type = vk_mem_type(mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
   if (type == UINT32_MAX)
      type = vk_mem_type(mr.memoryTypeBits, 0);
   if (type == UINT32_MAX)
      goto out;

   const VkMemoryDedicatedAllocateInfo ded = { .sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO, .image = img };
   const VkExportMemoryAllocateInfo exp = { .sType = VK_STRUCTURE_TYPE_EXPORT_MEMORY_ALLOCATE_INFO,
                                            .pNext = &ded,
                                            .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
   const VkMemoryAllocateInfo mai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                      .pNext = &exp,
                                      .allocationSize = mr.size,
                                      .memoryTypeIndex = type };
   r = vkAllocateMemory(vk.dev, &mai, NULL, &mem);
   if (r != VK_SUCCESS) {
      ALOGE("vkAllocateMemory(%llu) failed: %d", (unsigned long long)mr.size, r);
      goto out;
   }
   r = vkBindImageMemory(vk.dev, img, mem, 0);
   if (r != VK_SUCCESS)
      goto out;

   const VkMemoryGetFdInfoKHR gfi = { .sType = VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,
                                      .memory = mem,
                                      .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
   r = vk.get_fd(vk.dev, &gfi, &fd);
   if (r != VK_SUCCESS) {
      ALOGE("vkGetMemoryFdKHR failed: %d", r);
      fd = -1;
      goto out;
   }

   const off_t real = lseek(fd, 0, SEEK_END);
   for (int i = 0; i < planes; i++) {
      out_pitch[i] = (int)layout[i].rowPitch;
      out_offset[i] = (int)layout[i].offset;
   }
   *out_size = (int)(real > 0 ? (uint64_t)real : mr.size);

out:
   /* the dma-buf holds the memory from here on */
   if (img)
      vkDestroyImage(vk.dev, img, NULL);
   if (mem)
      vkFreeMemory(vk.dev, mem, NULL);
   pthread_mutex_unlock(&vk.lock);
   return fd;
}

/* ---- NVIDIA block-linear layout ----
 *
 * Modifier 0x03000000006060 1h: "GOBs" of 64 bytes x 8 rows (512 bytes), the
 * Turing-and-later 2D colour arrangement, stacked 2^h high into blocks that
 * run left to right, top to bottom. Inside a GOB each 32-byte half holds 16
 * runs of 16 bytes in the order below. Same layout Mesa's nouveau driver
 * implements in src/nouveau/nil/copy.rs (CopyGOBTuring2D).
 */
static int
tiled_layout(uint64_t modifier, int *out_h_log2)
{
   if ((modifier >> 56) != 0x03 || !(modifier & 0x10))
      return 0;
   if (((modifier >> 20) & 0x3) != 2) /* GOB kind: Turing+ */
      return 0;
   *out_h_log2 = (int)(modifier & 0xf);
   return 1;
}

static inline size_t
tiled_offset(uint32_t xb, uint32_t y, uint32_t pitch, int h_log2)
{
   const uint32_t block_rows = 8u << h_log2;
   const size_t block_size = (size_t)512 << h_log2;
   const size_t block = (size_t)(y / block_rows) * (pitch / 64) + xb / 64;
   const uint32_t gob = (y % block_rows) / 8;
   const uint32_t xi = xb % 64, yi = y % 8;
   const uint32_t run = (yi / 4) * 8 + ((xi % 32) / 16) * 4 + (yi % 4);
   return block * block_size + (size_t)gob * 512 + (xi / 32) * 0x100 + run * 0x10 + (xi % 16);
}

/* to_linear != 0: tiled -> linear, else linear -> tiled */
static void
tiled_copy(uint8_t *tiled, uint8_t *linear, uint32_t pitch, uint32_t row_bytes, uint32_t rows, int h_log2,
           int to_linear)
{
   for (uint32_t y = 0; y < rows; y++) {
      uint8_t *row = linear + (size_t)y * pitch;
      for (uint32_t xb = 0; xb < row_bytes; xb += 16) {
         const uint32_t n = row_bytes - xb < 16 ? row_bytes - xb : 16;
         uint8_t *t = tiled + tiled_offset(xb, y, pitch, h_log2);
         if (to_linear)
            memcpy(row + xb, t, n);
         else
            memcpy(t, row + xb, n);
      }
   }
}

/* ---- buffers ---- */

static int
shared_memory(const char *name, size_t size)
{
   int fd = (int)syscall(SYS_memfd_create, name, 1u /* MFD_CLOEXEC */);
   if (fd < 0)
      return -errno;
   if (ftruncate(fd, (off_t)size) < 0) {
      const int err = -errno;
      close(fd);
      return err;
   }
   return fd;
}

static struct farm_handle *
new_handle(void)
{
   struct farm_handle *fh = calloc(1, sizeof(*fh));
   if (!fh)
      return NULL;
   fh->base.version = sizeof(native_handle_t);
   fh->base.numFds = FARM_HANDLE_NUM_FDS;
   fh->base.numInts = FARM_HANDLE_NUM_INTS;
   fh->fd = -1;
   fh->magic = FARM_GRALLOC_MAGIC;
   return fh;
}

static int
alloc_cpu(struct farm_handle *fh, int w, int h, int format)
{
   size_t size;

   fh->kind = FARM_BUFFER_CPU;
   switch (format) {
   case FMT_YV12:
   case FMT_YCBCR_420_888: {
      /* Y, then V, then U (YV12 order); 420_888 users go through lock_ycbcr */
      const int ys = ALIGN(w, 16), cs = ALIGN(ys / 2, 16), ch = (h + 1) / 2;
      fh->pixel_stride = ys;
      fh->drm_fourcc = FOURCC('Y', 'V', '1', '2');
      fh->num_planes = 3;
      fh->pitches[0] = ys;
      fh->pitches[1] = fh->pitches[2] = cs;
      fh->offsets[0] = 0;
      fh->offsets[1] = ys * h;
      fh->offsets[2] = ys * h + cs * ch;
      size = (size_t)ys * h + 2 * (size_t)cs * ch;
      break;
   }
   case FMT_NV21: {
      const int ys = ALIGN(w, 16);
      fh->pixel_stride = ys;
      fh->drm_fourcc = FOURCC('N', 'V', '2', '1');
      fh->num_planes = 2;
      fh->pitches[0] = fh->pitches[1] = ys;
      fh->offsets[1] = ys * h;
      size = (size_t)ys * h + (size_t)ys * ((h + 1) / 2);
      break;
   }
   case FMT_BLOB:
      fh->pixel_stride = w;
      fh->num_planes = 1;
      fh->pitches[0] = w;
      size = (size_t)w * h;
      break;
   default: {
      int bpp = 4;
      for (size_t i = 0; i < sizeof(gpu_formats) / sizeof(gpu_formats[0]); i++)
         if (gpu_formats[i].hal == format) {
            bpp = gpu_formats[i].bpp;
            fh->drm_fourcc = (int)gpu_formats[i].fourcc;
         }
      if (format == HAL_PIXEL_FORMAT_RGB_888)
         bpp = 3;
      fh->pixel_stride = ALIGN(w, 16);
      fh->num_planes = 1;
      fh->pitches[0] = fh->pixel_stride * bpp;
      size = (size_t)fh->pitches[0] * h;
      break;
   }
   }

   size = ALIGN(size, 4096);
   fh->fd = shared_memory("gralloc-venus", size);
   if (fh->fd < 0)
      return fh->fd;
   fh->size = (int)size;
   return 0;
}

static int
farm_alloc(alloc_device_t *dev, int w, int h, int format, int usage, buffer_handle_t *out, int *out_stride)
{
   (void)dev;
   if (w <= 0 || h <= 0 || !out || !out_stride)
      return -EINVAL;

   struct farm_handle *fh = new_handle();
   if (!fh)
      return -ENOMEM;
   fh->width = w;
   fh->height = h;
   fh->hal_format = format;
   fh->usage = usage;

   const struct gpu_format *gf = NULL;
   for (size_t i = 0; i < sizeof(gpu_formats) / sizeof(gpu_formats[0]); i++)
      if (gpu_formats[i].hal == format)
         gf = &gpu_formats[i];

   int ret = -1;
   if (gf) {
      int pitch[3] = { 0 }, offset[3] = { 0 }, size = 0;
      uint64_t modifier = DRM_FORMAT_MOD_LINEAR;
      /* Importers add usage of their own (ANGLE asks for attachment use even
       * on buffers it only samples), and NVIDIA refuses that on LINEAR, so
       * every GPU buffer gets a tiled layout. lock() untiles for the CPU. */
      const int render = 1;
      /* nothing on the GPU ever touches a buffer without HW usage bits */
      if (usage & (GRALLOC_USAGE_HW_MASK))
         fh->fd = vk_alloc(gf->vk, 1, w, h, render, pitch, offset, &size, &modifier);
      if (fh->fd >= 0) {
         fh->kind = FARM_BUFFER_GPU;
         fh->drm_fourcc = (int)gf->fourcc;
         fh->num_planes = 1;
         fh->pitches[0] = pitch[0];
         fh->offsets[0] = offset[0];
         fh->pixel_stride = pitch[0] / gf->bpp;
         fh->modifier_lo = (uint32_t)modifier;
         fh->modifier_hi = (uint32_t)(modifier >> 32);
         fh->size = size;
         ret = 0;
      } else if (usage & GRALLOC_USAGE_HW_MASK) {
         ALOGW("format 0x%x %dx%d usage 0x%x: no GPU buffer, using shared memory", format, w, h, usage);
      }
   }
   /* Video and camera frames: the host offers 4:2:0 only in the LINEAR layout
    * and only for sampling, which is what they need. The CPU (decoder, camera
    * HAL) fills the planes in place through lock_ycbcr(), the GPU samples. */
   if (ret && (format == FMT_YV12 || format == FMT_YCBCR_420_888) && (usage & GRALLOC_USAGE_HW_MASK)) {
      int pitch[3] = { 0 }, offset[3] = { 0 }, size = 0;
      uint64_t modifier = DRM_FORMAT_MOD_LINEAR;
      fh->fd = vk_alloc(VK_FORMAT_G8_B8_R8_3PLANE_420_UNORM, 3, w, h, 0, pitch, offset, &size, &modifier);
      if (fh->fd >= 0 && modifier == DRM_FORMAT_MOD_LINEAR) {
         fh->kind = FARM_BUFFER_GPU;
         fh->drm_fourcc = (int)FOURCC('Y', 'V', '1', '2'); /* DRM YVU420: Y, Cr, Cb */
         fh->num_planes = 3;
         fh->pitches[0] = pitch[0];
         fh->offsets[0] = offset[0];
         /* Vulkan orders the planes Y, Cb, Cr; the handle follows YVU420 */
         fh->pitches[1] = pitch[2];
         fh->offsets[1] = offset[2];
         fh->pitches[2] = pitch[1];
         fh->offsets[2] = offset[1];
         fh->pixel_stride = pitch[0];
         fh->modifier_lo = fh->modifier_hi = 0;
         fh->size = size;
         ret = 0;
      } else {
         if (fh->fd >= 0)
            close(fh->fd);
         fh->fd = -1;
         ALOGW("YUV 0x%x %dx%d usage 0x%x: no GPU buffer, using shared memory", format, w, h, usage);
      }
   }
   if (ret)
      ret = alloc_cpu(fh, w, h, format);
   if (ret) {
      ALOGE("alloc %dx%d format 0x%x usage 0x%x failed: %d", w, h, format, usage, ret);
      free(fh);
      return ret;
   }

   *out = &fh->base;
   *out_stride = fh->pixel_stride;
   return 0;
}

static void
unmap(struct farm_handle *fh)
{
   if (fh->map_pid == getpid()) {
      void *p = (void *)(uintptr_t)(((uint64_t)fh->map_hi << 32) | fh->map_lo);
      if (p)
         munmap(p, (size_t)fh->size);
   }
   if (fh->map_pid == getpid())
      free((void *)(uintptr_t)(((uint64_t)fh->shadow_hi << 32) | fh->shadow_lo));
   fh->shadow_lo = fh->shadow_hi = 0;
   fh->map_pid = 0;
   fh->map_lo = fh->map_hi = 0;
   fh->lock_count = 0;
}

static int
farm_free(alloc_device_t *dev, buffer_handle_t handle)
{
   (void)dev;
   struct farm_handle *fh = (struct farm_handle *)farm_handle_from(handle);
   if (!fh)
      return -EINVAL;
   unmap(fh);
   if (fh->fd >= 0)
      close(fh->fd);
   free(fh);
   return 0;
}

static int
farm_register(gralloc_module_t const *mod, buffer_handle_t handle)
{
   (void)mod;
   struct farm_handle *fh = (struct farm_handle *)farm_handle_from(handle);
   if (!fh)
      return -EINVAL;
   /* a mapping recorded by another process means nothing here */
   if (fh->map_pid != getpid()) {
      fh->map_pid = 0;
      fh->map_lo = fh->map_hi = 0;
      fh->shadow_lo = fh->shadow_hi = 0;
      fh->lock_count = 0;
   }
   return 0;
}

static int
farm_unregister(gralloc_module_t const *mod, buffer_handle_t handle)
{
   (void)mod;
   struct farm_handle *fh = (struct farm_handle *)farm_handle_from(handle);
   if (!fh)
      return -EINVAL;
   unmap(fh);
   return 0;
}

static uint8_t *
map(struct farm_handle *fh)
{
   if (fh->map_pid == getpid() && (fh->map_lo || fh->map_hi))
      return (uint8_t *)(uintptr_t)(((uint64_t)fh->map_hi << 32) | fh->map_lo);

   void *p = mmap(NULL, (size_t)fh->size, PROT_READ | PROT_WRITE, MAP_SHARED, fh->fd, 0);
   if (p == MAP_FAILED) {
      ALOGE("mmap of %s buffer (%d bytes) failed: %s", fh->kind == FARM_BUFFER_GPU ? "GPU" : "CPU", fh->size,
            strerror(errno));
      return NULL;
   }
   fh->map_pid = getpid();
   fh->map_lo = (uint32_t)(uintptr_t)p;
   fh->map_hi = (uint32_t)((uint64_t)(uintptr_t)p >> 32);
   return p;
}

static int
farm_lock(gralloc_module_t const *mod, buffer_handle_t handle, int usage, int l, int t, int w, int h, void **vaddr)
{
   (void)mod;
   (void)l;
   (void)t;
   (void)w;
   (void)h;
   struct farm_handle *fh = (struct farm_handle *)farm_handle_from(handle);
   if (!fh)
      return -EINVAL;
   if (usage & (GRALLOC_USAGE_SW_READ_MASK | GRALLOC_USAGE_SW_WRITE_MASK)) {
      uint8_t *p = map(fh);
      if (!p)
         return -ENOMEM;
      int h_log2 = 0;
      const uint64_t modifier = ((uint64_t)fh->modifier_hi << 32) | fh->modifier_lo;
      if (fh->kind == FARM_BUFFER_GPU && tiled_layout(modifier, &h_log2)) {
         /* the CPU works on an untiled copy; unlock() writes it back */
         const size_t bytes = (size_t)fh->pitches[0] * (size_t)fh->height;
         uint8_t *shadow = (uint8_t *)(uintptr_t)(((uint64_t)fh->shadow_hi << 32) | fh->shadow_lo);
         if (!shadow) {
            shadow = malloc(bytes);
            if (!shadow)
               return -ENOMEM;
            fh->shadow_lo = (uint32_t)(uintptr_t)shadow;
            fh->shadow_hi = (uint32_t)((uint64_t)(uintptr_t)shadow >> 32);
         }
         /* fetch even for write-only locks: they may touch part of the buffer */
         tiled_copy(p + fh->offsets[0], shadow, (uint32_t)fh->pitches[0],
                    (uint32_t)fh->pitches[0],
                    (uint32_t)fh->height, h_log2, 1);
         p = shadow - fh->offsets[0];
      }
      fh->lock_usage = usage;
      if (vaddr)
         *vaddr = p + fh->offsets[0];
   }
   fh->lock_count++;
   return 0;
}

static int
farm_unlock(gralloc_module_t const *mod, buffer_handle_t handle)
{
   (void)mod;
   struct farm_handle *fh = (struct farm_handle *)farm_handle_from(handle);
   if (!fh)
      return -EINVAL;
   uint8_t *shadow = (uint8_t *)(uintptr_t)(((uint64_t)fh->shadow_hi << 32) | fh->shadow_lo);
   int h_log2 = 0;
   const uint64_t modifier = ((uint64_t)fh->modifier_hi << 32) | fh->modifier_lo;
   if (shadow && fh->map_pid == getpid() && (fh->lock_usage & GRALLOC_USAGE_SW_WRITE_MASK) &&
       tiled_layout(modifier, &h_log2)) {
      uint8_t *p = (uint8_t *)(uintptr_t)(((uint64_t)fh->map_hi << 32) | fh->map_lo);
      if (p)
         tiled_copy(p + fh->offsets[0], shadow, (uint32_t)fh->pitches[0], (uint32_t)fh->pitches[0],
                    (uint32_t)fh->height, h_log2, 0);
   }
   fh->lock_usage = 0;
   if (fh->lock_count > 0)
      fh->lock_count--;
   return 0;
}

static int
farm_lock_ycbcr(gralloc_module_t const *mod, buffer_handle_t handle, int usage, int l, int t, int w, int h,
                struct android_ycbcr *ycbcr)
{
   (void)mod;
   (void)usage;
   (void)l;
   (void)t;
   (void)w;
   (void)h;
   struct farm_handle *fh = (struct farm_handle *)farm_handle_from(handle);
   if (!fh || !ycbcr)
      return -EINVAL;
   if (fh->num_planes < 2)
      return -EINVAL; /* an RGB buffer: callers take this as "not YUV" */

   uint8_t *p = map(fh);
   if (!p)
      return -ENOMEM;
   memset(ycbcr, 0, sizeof(*ycbcr));
   ycbcr->y = p + fh->offsets[0];
   ycbcr->ystride = (size_t)fh->pitches[0];
   if (fh->num_planes == 3) { /* YV12: plane 1 is V (Cr), plane 2 is U (Cb) */
      ycbcr->cr = p + fh->offsets[1];
      ycbcr->cb = p + fh->offsets[2];
      ycbcr->cstride = (size_t)fh->pitches[1];
      ycbcr->chroma_step = 1;
   } else { /* NV21: interleaved V, U */
      ycbcr->cr = p + fh->offsets[1];
      ycbcr->cb = p + fh->offsets[1] + 1;
      ycbcr->cstride = (size_t)fh->pitches[1];
      ycbcr->chroma_step = 2;
   }
   fh->lock_count++;
   return 0;
}

/* ---- module ---- */

static int
farm_close(struct hw_device_t *dev)
{
   free(dev);
   return 0;
}

static int
farm_open(const struct hw_module_t *mod, const char *name, struct hw_device_t **out)
{
   if (strcmp(name, GRALLOC_HARDWARE_GPU0))
      return -EINVAL;
   alloc_device_t *dev = calloc(1, sizeof(*dev));
   if (!dev)
      return -ENOMEM;
   dev->common.tag = HARDWARE_DEVICE_TAG;
   dev->common.version = 0;
   dev->common.module = (struct hw_module_t *)mod;
   dev->common.close = farm_close;
   dev->alloc = farm_alloc;
   dev->free = farm_free;
   *out = &dev->common;
   return 0;
}

static struct hw_module_methods_t farm_methods = { .open = farm_open };

__attribute__((visibility("default"))) struct gralloc_module_t HAL_MODULE_INFO_SYM = {
   .common = {
      .tag = HARDWARE_MODULE_TAG,
      .module_api_version = GRALLOC_MODULE_API_VERSION_0_2,
      .hal_api_version = HARDWARE_HAL_API_VERSION,
      .id = GRALLOC_HARDWARE_MODULE_ID,
      .name = "Venus (host GPU) gralloc",
      .author = "Super Farm",
      .methods = &farm_methods,
   },
   .registerBuffer = farm_register,
   .unregisterBuffer = farm_unregister,
   .lock = farm_lock,
   .unlock = farm_unlock,
   .lock_ycbcr = farm_lock_ycbcr,
};
