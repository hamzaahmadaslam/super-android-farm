/* Buffer handle shared by gralloc.venus.so and the Venus Vulkan driver.
 *
 * Every buffer is one dma-buf (or memfd for CPU-only formats). GPU buffers are
 * allocated on the host GPU through Venus and exported, so any process can
 * import them into its own Venus connection.
 */
#ifndef FARM_GRALLOC_HANDLE_H
#define FARM_GRALLOC_HANDLE_H

#include <cutils/native_handle.h>
#include <stdint.h>

#define FARM_GRALLOC_MAGIC 0x4641524d /* 'FARM' */

enum farm_buffer_kind {
   FARM_BUFFER_GPU = 0, /* host GPU memory, importable by Vulkan */
   FARM_BUFFER_CPU = 1, /* plain shared memory, CPU access only */
};

struct farm_handle {
   native_handle_t base;

   /* fds */
   int fd;

   /* ints, identical in every process */
   int magic;
   int kind;
   int width;
   int height;
   int hal_format;
   int usage;
   int pixel_stride; /* in pixels, what alloc() reports */
   int drm_fourcc;
   int num_planes;
   int offsets[3];
   int pitches[3]; /* bytes per row */
   uint32_t modifier_lo;
   uint32_t modifier_hi;
   int size; /* bytes to map */

   /* ints, private to the process holding this copy */
   int map_pid;
   int lock_count;
   uint32_t map_lo;
   uint32_t map_hi;
   uint32_t shadow_lo; /* untiled copy handed out by lock() for tiled buffers */
   uint32_t shadow_hi;
   int lock_usage;
};

#define FARM_HANDLE_NUM_FDS 1
#define FARM_HANDLE_NUM_INTS                                                 \
   ((int)((sizeof(struct farm_handle) - sizeof(native_handle_t)) / sizeof(int)) - FARM_HANDLE_NUM_FDS)

static inline const struct farm_handle *
farm_handle_from(const native_handle_t *h)
{
   const struct farm_handle *fh = (const struct farm_handle *)h;
   if (!h || h->numFds != FARM_HANDLE_NUM_FDS || h->numInts != FARM_HANDLE_NUM_INTS ||
       fh->magic != FARM_GRALLOC_MAGIC)
      return NULL;
   return fh;
}

#endif
