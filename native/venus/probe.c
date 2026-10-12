/* Probe for the RTX-through-Venus buffer path, run in a plain Linux container.
 *
 * Device A (one vtest connection) allocates an exportable image the way the
 * planned gralloc module would, clears it to a known colour and exports a
 * dma-buf. Device B (a second vtest connection, standing in for another
 * Android process) imports that dma-buf and reads the colour back.
 *
 *   probe [width height]
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <vulkan/vulkan.h>

#define CHECK(x)                                                             \
   do {                                                                      \
      VkResult r_ = (x);                                                     \
      if (r_ != VK_SUCCESS) {                                                \
         fprintf(stderr, "FAIL %s = %d (line %d)\n", #x, r_, __LINE__);      \
         exit(1);                                                            \
      }                                                                      \
   } while (0)

/* extension entry points are not exported by the loader */
#define EXT(dev, name) ((PFN_##name)vkGetDeviceProcAddr((dev), #name))

struct dev {
   VkInstance inst;
   VkPhysicalDevice pd;
   VkDevice dev;
   VkQueue queue;
   VkCommandPool pool;
   VkPhysicalDeviceMemoryProperties mp;
};

static void
dev_init(struct dev *d, const char *name)
{
   const VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
                                   .pApplicationName = name,
                                   .apiVersion = VK_API_VERSION_1_2 };
   const VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
                                      .pApplicationInfo = &app };
   CHECK(vkCreateInstance(&ici, NULL, &d->inst));
   uint32_t n = 1;
   VkResult r = vkEnumeratePhysicalDevices(d->inst, &n, &d->pd);
   if ((r != VK_SUCCESS && r != VK_INCOMPLETE) || n == 0) {
      fprintf(stderr, "FAIL no physical device\n");
      exit(1);
   }
   VkPhysicalDeviceProperties props;
   vkGetPhysicalDeviceProperties(d->pd, &props);
   printf("[%s] device: %s\n", name, props.deviceName);
   vkGetPhysicalDeviceMemoryProperties(d->pd, &d->mp);

   static const char *exts[] = {
      "VK_KHR_external_memory_fd",
      "VK_EXT_external_memory_dma_buf",
      "VK_EXT_image_drm_format_modifier",
      "VK_EXT_queue_family_foreign",
   };
   const float prio = 1.0f;
   const VkDeviceQueueCreateInfo qci = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
                                         .queueFamilyIndex = 0,
                                         .queueCount = 1,
                                         .pQueuePriorities = &prio };
   const VkDeviceCreateInfo dci = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
                                    .queueCreateInfoCount = 1,
                                    .pQueueCreateInfos = &qci,
                                    .enabledExtensionCount = 4,
                                    .ppEnabledExtensionNames = exts };
   CHECK(vkCreateDevice(d->pd, &dci, NULL, &d->dev));
   vkGetDeviceQueue(d->dev, 0, 0, &d->queue);
   const VkCommandPoolCreateInfo pci = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                         .queueFamilyIndex = 0 };
   CHECK(vkCreateCommandPool(d->dev, &pci, NULL, &d->pool));
}

static VkCommandBuffer
cmd_begin(struct dev *d)
{
   const VkCommandBufferAllocateInfo ai = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
                                            .commandPool = d->pool,
                                            .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
                                            .commandBufferCount = 1 };
   VkCommandBuffer cb;
   CHECK(vkAllocateCommandBuffers(d->dev, &ai, &cb));
   const VkCommandBufferBeginInfo bi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
   CHECK(vkBeginCommandBuffer(cb, &bi));
   return cb;
}

static void
cmd_end(struct dev *d, VkCommandBuffer cb)
{
   CHECK(vkEndCommandBuffer(cb));
   const VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
                             .commandBufferCount = 1,
                             .pCommandBuffers = &cb };
   CHECK(vkQueueSubmit(d->queue, 1, &si, VK_NULL_HANDLE));
   CHECK(vkQueueWaitIdle(d->queue));
}

static uint32_t
mem_type(struct dev *d, uint32_t bits, VkMemoryPropertyFlags want)
{
   for (uint32_t i = 0; i < d->mp.memoryTypeCount; i++)
      if ((bits & (1u << i)) && (d->mp.memoryTypes[i].propertyFlags & want) == want)
         return i;
   return UINT32_MAX;
}

static void
layout(VkCommandBuffer cb, VkImage img, VkImageLayout from, VkImageLayout to)
{
   const VkImageMemoryBarrier b = {
      .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
      .dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_TRANSFER_READ_BIT,
      .oldLayout = from,
      .newLayout = to,
      .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
      .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
      .image = img,
      .subresourceRange = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 },
   };
   vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                        NULL, 0, NULL, 1, &b);
}

int
main(int argc, char **argv)
{
   const uint32_t W = argc > 2 ? atoi(argv[1]) : 480, H = argc > 2 ? atoi(argv[2]) : 854;
   const VkFormat fmt = VK_FORMAT_R8G8B8A8_UNORM;
   const VkImageUsageFlags usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                                   VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
   struct dev A, B;
   dev_init(&A, "A");

   /* which modifiers does the host offer for this format? */
   VkDrmFormatModifierPropertiesEXT mods[64];
   VkDrmFormatModifierPropertiesListEXT ml = { .sType = VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT,
                                               .drmFormatModifierCount = 64,
                                               .pDrmFormatModifierProperties = mods };
   VkFormatProperties2 fp = { .sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2, .pNext = &ml };
   vkGetPhysicalDeviceFormatProperties2(A.pd, fmt, &fp);
   printf("modifiers offered: %u\n", ml.drmFormatModifierCount);
   uint64_t usable[64];
   uint32_t n_usable = 0;
   int have_linear = 0;
   for (uint32_t i = 0; i < ml.drmFormatModifierCount; i++) {
      const VkPhysicalDeviceImageDrmFormatModifierInfoEXT mi = {
         .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT,
         .drmFormatModifier = mods[i].drmFormatModifier,
         .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
      };
      const VkPhysicalDeviceExternalImageFormatInfo ei = {
         .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
         .pNext = &mi,
         .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
      };
      const VkPhysicalDeviceImageFormatInfo2 ii = {
         .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2,
         .pNext = &ei,
         .format = fmt,
         .type = VK_IMAGE_TYPE_2D,
         .tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT,
         .usage = usage,
      };
      VkExternalImageFormatProperties ep = { .sType = VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES };
      VkImageFormatProperties2 ip = { .sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2, .pNext = &ep };
      VkResult r = vkGetPhysicalDeviceImageFormatProperties2(A.pd, &ii, &ip);
      const VkExternalMemoryFeatureFlags f = ep.externalMemoryProperties.externalMemoryFeatures;
      if (i < 6 || mods[i].drmFormatModifier == 0)
         printf("  mod 0x%016llx planes=%u features=0x%x -> %s export=%d import=%d\n",
                (unsigned long long)mods[i].drmFormatModifier, mods[i].drmFormatModifierPlaneCount,
                mods[i].drmFormatModifierTilingFeatures, r == VK_SUCCESS ? "ok" : "unsupported",
                !!(f & VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT), !!(f & VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT));
      if (r == VK_SUCCESS && (f & VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT) &&
          (f & VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT)) {
         usable[n_usable++] = mods[i].drmFormatModifier;
         if (mods[i].drmFormatModifier == 0)
            have_linear = 1;
      }
   }
   printf("usable for export+import: %u (linear %s)\n", n_usable, have_linear ? "yes" : "no");
   if (!n_usable)
      return 1;
   const char *want = getenv("PROBE_MOD");
   uint64_t one = want && !strcmp(want, "linear") ? 0 : usable[0];
   if (want && !strcmp(want, "linear") && !have_linear) {
      fprintf(stderr, "FAIL linear requested but not usable\n");
      return 1;
   }

   /* A: allocate, clear, export */
   uint64_t tiled[64];
   uint32_t n_tiled = 0;
   for (uint32_t i = 0; i < n_usable; i++)
      if (usable[i])
         tiled[n_tiled++] = usable[i];
   if (want && !strncmp(want, "0x", 2))
      one = strtoull(want, NULL, 16);
   const int all = want && !strcmp(want, "all");
   const VkImageDrmFormatModifierListCreateInfoEXT mlc = {
      .sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_LIST_CREATE_INFO_EXT,
      .drmFormatModifierCount = all ? n_tiled : 1,
      .pDrmFormatModifiers = all ? tiled : &one,
   };
   const VkExternalMemoryImageCreateInfo emi = { .sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
                                                 .pNext = &mlc,
                                                 .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
   VkImageCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
                             .pNext = &emi,
                             .imageType = VK_IMAGE_TYPE_2D,
                             .format = fmt,
                             .extent = { W, H, 1 },
                             .mipLevels = 1,
                             .arrayLayers = 1,
                             .samples = VK_SAMPLE_COUNT_1_BIT,
                             .tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT,
                             .usage = usage,
                             .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED };
   VkImage imgA;
   CHECK(vkCreateImage(A.dev, &ici, NULL, &imgA));
   VkImageDrmFormatModifierPropertiesEXT got = { .sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_PROPERTIES_EXT };
   CHECK(EXT(A.dev, vkGetImageDrmFormatModifierPropertiesEXT)(A.dev, imgA, &got));
   VkSubresourceLayout sl;
   vkGetImageSubresourceLayout(A.dev, imgA,
                               &(VkImageSubresource){ VK_IMAGE_ASPECT_MEMORY_PLANE_0_BIT_EXT, 0, 0 }, &sl);
   VkMemoryRequirements mr;
   vkGetImageMemoryRequirements(A.dev, imgA, &mr);
   {
      VkMemoryDedicatedRequirements dr = { .sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_REQUIREMENTS };
      VkMemoryRequirements2 mr2 = { .sType = VK_STRUCTURE_TYPE_MEMORY_REQUIREMENTS_2, .pNext = &dr };
      const VkImageMemoryRequirementsInfo2 ri = { .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_REQUIREMENTS_INFO_2,
                                                  .image = imgA };
      vkGetImageMemoryRequirements2(A.dev, &ri, &mr2);
      printf("[A] dedicated: prefers=%d requires=%d\n", dr.prefersDedicatedAllocation, dr.requiresDedicatedAllocation);
   }
   printf("[A] image %ux%u modifier 0x%016llx offset %llu pitch %llu size %llu typebits 0x%x\n", W, H,
          (unsigned long long)got.drmFormatModifier, (unsigned long long)sl.offset,
          (unsigned long long)sl.rowPitch, (unsigned long long)mr.size, mr.memoryTypeBits);
   uint32_t ti = mem_type(&A, mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
   const int host_visible = ti != UINT32_MAX;
   if (!host_visible)
      ti = mem_type(&A, mr.memoryTypeBits, 0);
   printf("[A] memory type %u (%s)\n", ti, host_visible ? "host visible" : "device only");
   const VkMemoryDedicatedAllocateInfo da = { .sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO, .image = imgA };
   const VkExportMemoryAllocateInfo ea = { .sType = VK_STRUCTURE_TYPE_EXPORT_MEMORY_ALLOCATE_INFO,
                                           .pNext = &da,
                                           .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
   const VkMemoryAllocateInfo mai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                      .pNext = &ea,
                                      .allocationSize = mr.size,
                                      .memoryTypeIndex = ti };
   VkDeviceMemory memA;
   CHECK(vkAllocateMemory(A.dev, &mai, NULL, &memA));
   CHECK(vkBindImageMemory(A.dev, imgA, memA, 0));
   VkCommandBuffer cb = cmd_begin(&A);
   layout(cb, imgA, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
   const VkClearColorValue color = { .float32 = { 0x11 / 255.0f, 0x66 / 255.0f, 0xcc / 255.0f, 1.0f } };
   vkCmdClearColorImage(cb, imgA, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &color, 1,
                        &(VkImageSubresourceRange){ VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 });
   cmd_end(&A, cb);
   int fd = -1;
   const VkMemoryGetFdInfoKHR gfi = { .sType = VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,
                                      .memory = memA,
                                      .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
   CHECK(EXT(A.dev, vkGetMemoryFdKHR)(A.dev, &gfi, &fd));
   const off_t fsz = lseek(fd, 0, SEEK_END);
   printf("[A] exported dma-buf fd %d, size by lseek %lld\n", fd, (long long)fsz);

   /* can the CPU see it? (what gralloc lock() would do) */
   void *p = mmap(NULL, mr.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
   if (p == MAP_FAILED) {
      printf("[A] mmap of dma-buf: FAILED (%s)\n", strerror(errno));
   } else {
      const uint8_t *px = (const uint8_t *)p + sl.offset;
      printf("[A] mmap of dma-buf: ok, first pixel %02x %02x %02x %02x (expect 11 66 cc ff if linear)\n", px[0],
             px[1], px[2], px[3]);
      munmap(p, mr.size);
   }

   if (getenv("PROBE_FREE")) {
      /* what the allocator does: drop its own objects, keep only the fd */
      vkDestroyImage(A.dev, imgA, NULL);
      vkFreeMemory(A.dev, memA, NULL);
      CHECK(vkQueueWaitIdle(A.queue));
      printf("[A] freed its image and memory, only the dma-buf fd is left\n");
      if (!strcmp(getenv("PROBE_FREE"), "2")) {
         vkDestroyDevice(A.dev, NULL);
         vkDestroyInstance(A.inst, NULL);
         printf("[A] closed its whole connection\n");
      }
   }

   /* B: a second connection imports the same buffer */
   dev_init(&B, "B");
   const VkSubresourceLayout plane = { .offset = sl.offset, .rowPitch = sl.rowPitch };
   const VkImageDrmFormatModifierExplicitCreateInfoEXT mec = {
      .sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT,
      .drmFormatModifier = got.drmFormatModifier,
      .drmFormatModifierPlaneCount = 1,
      .pPlaneLayouts = &plane,
   };
   const VkExternalMemoryImageCreateInfo emiB = { .sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
                                                  .pNext = &mec,
                                                  .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
   ici.pNext = &emiB;
   if (getenv("PROBE_USAGE_B"))
      ici.usage = (VkImageUsageFlags)strtoul(getenv("PROBE_USAGE_B"), NULL, 0);
   if (getenv("PROBE_USAGE_A_WAS"))
      printf("[B] importing with usage 0x%x\n", ici.usage);
   VkImage imgB;
   CHECK(vkCreateImage(B.dev, &ici, NULL, &imgB));
   VkMemoryRequirements mrB;
   vkGetImageMemoryRequirements(B.dev, imgB, &mrB);
   {
      VkMemoryDedicatedRequirements dr = { .sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_REQUIREMENTS };
      VkMemoryRequirements2 mr2 = { .sType = VK_STRUCTURE_TYPE_MEMORY_REQUIREMENTS_2, .pNext = &dr };
      const VkImageMemoryRequirementsInfo2 ri = { .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_REQUIREMENTS_INFO_2,
                                                  .image = imgB };
      vkGetImageMemoryRequirements2(B.dev, &ri, &mr2);
      printf("[B] dedicated: prefers=%d requires=%d\n", dr.prefersDedicatedAllocation, dr.requiresDedicatedAllocation);
   }
   VkMemoryFdPropertiesKHR fdp = { .sType = VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR };
   CHECK(EXT(B.dev, vkGetMemoryFdPropertiesKHR)(B.dev, VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, fd, &fdp));
   printf("[B] fd memory type bits 0x%x, image needs 0x%x size %llu\n", fdp.memoryTypeBits, mrB.memoryTypeBits,
          (unsigned long long)mrB.size);
   const uint32_t tiB = mem_type(&B, fdp.memoryTypeBits & mrB.memoryTypeBits, 0);
   if (tiB == UINT32_MAX) {
      fprintf(stderr, "FAIL no common memory type for import\n");
      return 1;
   }
   const VkMemoryDedicatedAllocateInfo daB = { .sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO, .image = imgB };
   const VkImportMemoryFdInfoKHR imp = { .sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
                                         .pNext = getenv("PROBE_NODED") ? NULL : &daB,
                                         .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
                                         .fd = dup(fd) };
   const VkMemoryAllocateInfo maiB = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                       .pNext = &imp,
                                       .allocationSize = mrB.size,
                                       .memoryTypeIndex = tiB };
   VkDeviceMemory memB;
   CHECK(vkAllocateMemory(B.dev, &maiB, NULL, &memB));
   CHECK(vkBindImageMemory(B.dev, imgB, memB, 0));
   printf("[B] imported and bound\n");

   const VkBufferCreateInfo bci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                                    .size = (VkDeviceSize)W * H * 4,
                                    .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT };
   VkBuffer buf;
   CHECK(vkCreateBuffer(B.dev, &bci, NULL, &buf));
   VkMemoryRequirements bmr;
   vkGetBufferMemoryRequirements(B.dev, buf, &bmr);
   const VkMemoryAllocateInfo bai = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
      .allocationSize = bmr.size,
      .memoryTypeIndex = mem_type(&B, bmr.memoryTypeBits,
                                  VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT),
   };
   VkDeviceMemory bmem;
   CHECK(vkAllocateMemory(B.dev, &bai, NULL, &bmem));
   CHECK(vkBindBufferMemory(B.dev, buf, bmem, 0));
   cb = cmd_begin(&B);
   layout(cb, imgB, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
   const VkBufferImageCopy reg = { .imageSubresource = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 },
                                   .imageExtent = { W, H, 1 } };
   vkCmdCopyImageToBuffer(cb, imgB, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, buf, 1, &reg);
   cmd_end(&B, cb);
   uint8_t *m;
   CHECK(vkMapMemory(B.dev, bmem, 0, VK_WHOLE_SIZE, 0, (void **)&m));
   const uint8_t *last = m + ((size_t)W * H - 1) * 4;
   printf("[B] read back first %02x %02x %02x %02x, last %02x %02x %02x %02x\n", m[0], m[1], m[2], m[3], last[0],
          last[1], last[2], last[3]);
   const int ok = m[0] == 0x11 && m[1] == 0x66 && m[2] == 0xcc && last[0] == 0x11 && last[2] == 0xcc;
   printf("%s\n", ok ? "RESULT: PASS (second connection sees what the first drew)"
                     : "RESULT: FAIL (contents differ; UNDEFINED->SRC transition may have discarded them)");
   return ok ? 0 : 2;
}
